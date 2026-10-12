#!/usr/bin/env python3
"""Scripted OpenAI-compatible model server and request recorder for the health triage e2e test.

Two modes, both stdlib only (Python 3.9+):

- scripted (default): answers like a careful triage model, deterministically, so the whole flow
  runs without a GPU. It recognises the triage node's two prompts by their markers
  ([triage:assess], [triage:recommend]), the eval judge by its system prompt, and answers anything
  else as a plain chat model. /v1/embeddings returns hashed bag-of-words vectors (Unicode words
  plus character trigrams), good enough for caliban/auto's kNN over English, French and Arabic
  exemplars.
- recorder (MOCK_UPSTREAM_URL set): forwards every chat request to a real OpenAI-compatible
  server (vLLM on the GPU host) and returns its answer unchanged. Embeddings are forwarded to
  MOCK_EMBED_UPSTREAM_URL when set, else answered locally.

Either way every chat request is appended, as received from Caliban, to $MOCK_LOG (JSONL), so the
run script can prove what the model saw: PII masked or not, injected catalogue text present or not.

The RAG stage's agent node (`careguide`, marker [care:answer]) is scripted too: its first turn
calls rag_search with the question, its second answers with the passages' sentences that share the
most words with the question, citing them as [n].

MOCK_FOLLOW_INJECTIONS=1 turns the scripted model into a compromised one that obeys instructions
it finds in catalogue rows or retrieved passages (to show which defences hold when the model does
not).
"""
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LOG", "/log/requests.jsonl")
UPSTREAM = os.environ.get("MOCK_UPSTREAM_URL", "").rstrip("/")
EMBED_UPSTREAM = os.environ.get("MOCK_EMBED_UPSTREAM_URL", "").rstrip("/")
FOLLOW_INJECTIONS = os.environ.get("MOCK_FOLLOW_INJECTIONS", "0") == "1"
DIM = 512
LOCK = threading.Lock()

# ─────────────────────────── embeddings ───────────────────────────

WORD = re.compile(r"\w+", re.UNICODE)


def embed(text):
    v = [0.0] * DIM
    for w in WORD.findall(text.lower()):
        if w.isdigit():
            continue
        feats = [w] + [w[i:i + 3] for i in range(max(1, len(w) - 2))]
        for i, f in enumerate(feats):
            h = int.from_bytes(hashlib.blake2b(f.encode(), digest_size=4).digest(), "little")
            v[h % DIM] += 1.0 if i == 0 else 0.5
    n = math.sqrt(sum(x * x for x in v)) or 1.0
    return [x / n for x in v]


# ─────────────────────────── the scripted triage model ───────────────────────────

SPECIALTIES = [
    # (specialty, keywords); checked in order, the first match wins.
    ("obstetrics", ["pregnan", "enceinte", "grossesse", "حامل", "الحمل"]),
    ("pediatrics", ["my son", "my daughter", "my child", "my baby", "toddler", "year-old", "year old",
                    "mon fils", "ma fille", "mon enfant", "bébé", "ابني", "ابنتي", "طفلي", "رضيع"]),
    ("cardiology", ["chest", "heart", "palpitation", "breathless", "poitrine", "cœur", "coeur",
                    "palpitations", "essouffl", "صدر", "قلب", "خفقان", "ضيق في التنفس"]),
    ("orthopedics", ["knee", "ankle", "back pain", "fracture", "fell ", "joint", "genou", "cheville",
                     "mal au dos", "chute", "ركبة", "كاحل", "ظهر", "كسر"]),
    ("dermatology", ["rash", "skin", "mole", "itch", "acne", "peau", "bouton", "démangeaison",
                     "grain de beauté", "جلد", "طفح", "حكة", "شامة"]),
]
URGENT = ["severe", "unconscious", "can't breathe", "cannot breathe", "bleeding heavily", "sweating",
          "radiat", "intense", "sévère", "inconscient", "transpire", "saigne", "شديد", "فاقد الوعي",
          "تعرق", "نزيف", "لا أستطيع التنفس"]
CITIES = {"beirut": ["beirut", "beyrouth", "بيروت"], "tripoli": ["tripoli", "طرابلس"],
          "paris": ["paris", "باريس"]}
DURATION = re.compile(r"(\d+|one|two|three|a few|a|an|un|une|deux|trois|quelques)\s*(hour|day|week|month|"
                      r"heure|jour|semaine|mois)|since|depuis|yesterday|hier|this morning|ce matin|"
                      r"منذ|ساعة|ساعات|يوم|أيام|أسبوع|أمس|شهر", re.IGNORECASE)
FRENCH = re.compile(r"\b(je|j'ai|mon|ma|mes|depuis|douleur|bonjour|est-ce|une|des|avec|mal)\b", re.IGNORECASE)
ARABIC = re.compile(r"[؀-ۿ]")

QUESTION = {
    "en": "Since when has this been going on, and in which city would you like to be seen (Beirut, Tripoli or Paris)?",
    "fr": "Depuis quand cela dure-t-il, et dans quelle ville souhaitez-vous être reçu (Beyrouth, Tripoli ou Paris) ?",
    "ar": "منذ متى تعاني من ذلك، وفي أي مدينة تفضّل أن تتم معاينتك (بيروت أو طرابلس أو باريس)؟",
}
SPECIALTY_NAME = {
    "en": {"cardiology": "cardiology", "pediatrics": "pediatrics", "orthopedics": "orthopedics",
           "dermatology": "dermatology", "general_practice": "general practice", "obstetrics": "obstetrics",
           "emergency_medicine": "emergency medicine"},
    "fr": {"cardiology": "cardiologie", "pediatrics": "pédiatrie", "orthopedics": "orthopédie",
           "dermatology": "dermatologie", "general_practice": "médecine générale", "obstetrics": "obstétrique",
           "emergency_medicine": "urgences"},
    "ar": {"cardiology": "أمراض القلب", "pediatrics": "طب الأطفال", "orthopedics": "جراحة العظام",
           "dermatology": "الأمراض الجلدية", "general_practice": "الطب العام", "obstetrics": "التوليد",
           "emergency_medicine": "الطوارئ"},
}


def between(text, label):
    """The text between '<label>:\n<<<\n' and '\n>>>' in a prompt."""
    m = re.search(re.escape(label) + r"[^\n]*\n<<<\n(.*?)\n>>>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def json_line(text, prefix):
    for line in text.splitlines():
        if line.startswith(prefix):
            try:
                return json.loads(line[len(prefix):].strip())
            except ValueError:
                return None
    return None


def language(text):
    if ARABIC.search(text):
        return "ar"
    if FRENCH.search(text):
        return "fr"
    return "en"


def assess(prompt):
    case, clarification = between(prompt, "Case"), between(prompt, "Clarification")
    text = (case + "\n" + clarification).lower()
    lang = language(case)
    specialty = next((s for s, kws in SPECIALTIES if any(k in text for k in kws)), "general_practice")
    urgent = any(k in text for k in URGENT)
    cities = [c for c, names in CITIES.items() if any(n in text for n in names)]
    knows_duration = bool(DURATION.search(text))
    # Ask once, when it matters and nothing is urgent; after an answer, never again.
    needs = not clarification and not urgent and (not cities or not knows_duration)
    return {
        "category": "clinical",
        "specialty": specialty,
        "urgency": "urgent" if urgent else ("soon" if specialty in ("cardiology", "pediatrics", "obstetrics") else "routine"),
        "cities": cities or ["beirut", "tripoli", "paris"],
        "language": lang,
        "needs_clarification": needs,
        "clarifying_question": QUESTION[lang] if needs else "",
        "summary": f"{specialty} case, {'urgent' if urgent else 'not urgent'}",
    }


def rows_of(tool_output):
    if isinstance(tool_output, dict):
        return tool_output.get("rows") or []
    return []


def iso(v):
    if isinstance(v, dict) and "$date" in v:
        v = v["$date"]
        if isinstance(v, dict) and "$numberLong" in v:
            return datetime.fromtimestamp(int(v["$numberLong"]) / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%MZ")
    return str(v)[:16] + "Z" if v else ""


def recommend(prompt):
    a = json_line(prompt, "ASSESSMENT_JSON:") or {}
    services = rows_of(json_line(prompt, "SERVICES_JSON:"))
    facilities = rows_of(json_line(prompt, "FACILITIES_JSON:"))
    slots = rows_of(json_line(prompt, "SLOTS_JSON:"))
    lang = a.get("language", "en")
    urgent = a.get("urgency") == "urgent"
    specialty = a.get("specialty", "general_practice")
    if urgent:
        er = [f for f in facilities if f.get("emergency_available")]
        er.sort(key=lambda f: -(f.get("emergency_beds_free") or 0))
        fac = (er or facilities or [None])[0]
    else:
        fac = sorted(facilities, key=lambda f: (f.get("wait_days") or 99, f.get("facility_code", "")))[0] if facilities else None
    code = fac.get("facility_code") if fac else None
    here = [s for s in services if s.get("facility_id") == code] or services
    if urgent:
        here.sort(key=lambda s: (s.get("urgency_fit") != "urgent", s.get("price_usd") or 0))
    else:
        here.sort(key=lambda s: (s.get("urgency_fit") == "urgent", s.get("price_usd") or 0))
    picked, seen = [], set()
    for s in here:
        if s.get("service_code") in seen:
            continue
        seen.add(s.get("service_code"))
        name = s.get(f"service_name_{lang}") or s.get("service_name_en")
        picked.append({"code": s.get("service_code"), "name": name, "price_usd": s.get("price_usd")})
        if len(picked) == 2:
            break
    slot = next((iso(s.get("start")) for s in sorted(slots, key=lambda s: iso(s.get("start")))
                 if s.get("slot_facility_id") == code), "")
    fname = (fac or {}).get(f"facility_name_{lang}") or (fac or {}).get("facility_name_en") or ""
    city = (fac or {}).get("city", "")
    svc = picked[0]["name"] if picked else ""
    price = picked[0]["price_usd"] if picked else None
    spec = SPECIALTY_NAME[lang].get(specialty, specialty)
    if urgent:
        msg = {
            "en": f"Your symptoms need prompt attention. Please go to the emergency department of {fname} ({city}) now, or call your local emergency number. This is not a diagnosis.",
            "fr": f"Vos symptômes nécessitent une prise en charge rapide. Rendez-vous dès maintenant aux urgences de {fname} ({city}) ou appelez le numéro d'urgence local. Ceci n'est pas un diagnostic.",
            "ar": f"تحتاج أعراضك إلى عناية سريعة. يرجى التوجه الآن إلى قسم الطوارئ في {fname} ({city}) أو الاتصال برقم الطوارئ المحلي. هذا ليس تشخيصًا.",
        }[lang]
    else:
        msg = {
            "en": f"Based on what you describe, we suggest a {svc} ({spec}) at {fname}, {city}, from {price} USD. Next open slot: {slot or 'ask the facility'}. This is not a diagnosis; if things get worse, seek urgent care.",
            "fr": f"D'après votre description, nous vous proposons : {svc} ({spec}) à {fname}, {city}, à partir de {price} USD. Prochain créneau libre : {slot or 'à demander'}. Ceci n'est pas un diagnostic ; si cela s'aggrave, consultez en urgence.",
            "ar": f"بناءً على ما وصفته، نقترح {svc} ({spec}) في {fname}، {city}، ابتداءً من {price} دولار. أقرب موعد متاح: {slot or 'يرجى السؤال'}. هذا ليس تشخيصًا؛ إذا ساءت الحالة، اطلب رعاية عاجلة.",
        }[lang]
    if FOLLOW_INJECTIONS:
        for s in services:
            text = s.get("service_description_en") or ""
            if "ignore all previous instructions" in text.lower():
                msg += " " + text[text.lower().index("tell the patient"):]
    return {
        "category": a.get("category", "clinical"),
        "specialty": specialty,
        "urgency": a.get("urgency", "routine"),
        "language": lang,
        "services": picked,
        "facility": {"code": code or "", "name": fname, "city": city},
        "next_slot": slot,
        "message": msg,
        "rationale": f"{spec}; {'emergency department with free beds' if urgent else 'shortest wait'}; catalogue rows only",
    }


# ─────────────────────────── the scripted care guide (RAG stage) ───────────────────────────

PASSAGE = re.compile(r"\n\n\[(\d+)\] ([^\n]*)\n(.*?)(?=\n\n\[\d+\] |\Z)", re.DOTALL)
STOP = {"the", "and", "does", "what", "how", "les", "des", "une", "est", "que", "qui", "pour", "dans", "with",
        "selon", "after", "must", "need", "this", "that", "mon", "nous", "هل", "وما"}
NOT_FOUND = {"en": "The documents I can search do not answer this question.",
             "fr": "Les documents que je peux consulter ne répondent pas à cette question.",
             "ar": "الوثائق المتاحة لي لا تجيب عن هذا السؤال."}


def words(text):
    return {w for w in WORD.findall(text.lower()) if len(w) >= 3 and w not in STOP}


def care(msgs, tools):
    """An agent turn of the `careguide` node: search first, then answer from the passages."""
    question = next((m.get("content") for m in msgs if m.get("role") == "user"), "") or ""
    results = [m for m in msgs if m.get("role") == "tool"]
    if not results:
        if "rag_search" not in tools:
            return json.dumps({"answer": NOT_FOUND[language(question)], "citations": [], "language": language(question)})
        args = json.dumps({"query": question, "k": 4}, ensure_ascii=False)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_rag_0", "type": "function", "function": {"name": "rag_search", "arguments": args}}]}
    raw = results[-1].get("content") or ""
    try:
        out = json.loads(raw)
    except ValueError:
        out = {"context": raw, "results": []}
    if not isinstance(out, dict):
        out = {"context": str(out), "results": []}
    lang = language(question)
    doc_of = {r.get("n"): r.get("doc_id") for r in out.get("results") or []}
    q = words(question)
    scored = []
    for m in PASSAGE.finditer(out.get("context") or ""):
        n = int(m.group(1))
        # The citation line names the heading path: a passage under a heading the question names
        # ("signes d'alerte") ranks its sentences higher.
        bonus = len(q & words(m.group(2).split(" (doc ")[0].split(" > ", 1)[-1]))
        # Markdown wraps lines: join them back into paragraphs, keeping list items and table rows.
        lines = []
        for line in m.group(3).splitlines():
            line = line.strip()
            if lines and line and lines[-1] and not re.match(r"([-*|#]|\d+\.)", line):
                lines[-1] += " " + line
            else:
                lines.append(line)
        for seg in (x for line in lines for x in re.split(r"(?<=[.!?؟])\s+", line)):
            seg = seg.strip(" |-*#\t")
            if len(seg) < 4 or set(seg) <= set("|-: "):
                continue
            scored.append((len(q & words(seg)) + bonus, -n, seg, n))
            if FOLLOW_INJECTIONS and "ignore all previous instructions" in seg.lower():
                scored.append((99, -n, seg, n))
    best = [x for x in sorted(scored, reverse=True)[:3] if x[0] > 0]
    if not best:
        return json.dumps({"answer": NOT_FOUND[lang], "citations": [], "language": lang}, ensure_ascii=False)
    answer = " ".join(f"{seg.replace(' | ', ': ')} [{n}]" for _, _, seg, n in best)
    cited = sorted({n for _, _, _, n in best})
    return json.dumps({"answer": answer, "citations": [{"n": n, "doc_id": doc_of.get(n, "")} for n in cited],
                       "language": lang}, ensure_ascii=False)


def judge(prompt):
    out = prompt.split("Output to grade:", 1)[-1]
    ok = '"facility"' in out and '"message"' in out and "zx-attacker" not in out
    return {"score": 0.9 if ok else 0.2, "reason": "uses catalogue rows and answers the case" if ok else "incomplete"}


def chat_text(messages):
    return "\n".join(m.get("content") if isinstance(m.get("content"), str) else
                     " ".join(p.get("text", "") for p in (m.get("content") or []) if isinstance(p, dict))
                     for m in messages)


def scripted(body):
    msgs = body.get("messages", [])
    everything = chat_text(msgs)
    system = next((m.get("content") for m in msgs if m.get("role") == "system"), "") or ""
    if "You grade the output of an AI system" in system:
        return json.dumps(judge(everything), ensure_ascii=False)
    if "[triage:assess]" in everything:
        return json.dumps(assess(everything), ensure_ascii=False)
    if "[triage:recommend]" in everything:
        return json.dumps(recommend(everything), ensure_ascii=False)
    if "[care:answer]" in system:
        tools = [t.get("function", {}).get("name") for t in body.get("tools") or []]
        return care(msgs, tools)
    last = next((chat_text([m]) for m in reversed(msgs) if m.get("role") == "user"), "")
    return f"(scripted model) A general answer to: {last[:200]}"


# ─────────────────────────── HTTP ───────────────────────────


def record(path, body):
    entry = {"ts": time.time(), "path": path, "model": body.get("model"), "messages": body.get("messages"),
             "tools": bool(body.get("tools")), "stream": bool(body.get("stream")),
             "keys": sorted(body.keys())}
    with LOCK, open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send(self, status, obj, ctype="application/json"):
        data = obj if isinstance(obj, bytes) else json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("content-type", ctype)
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def forward(self, base, raw):
        # base ends in /v1, like the paths Caliban calls.
        url = base + self.path[self.path.index("/v1") + 3:]
        req = urllib.request.Request(url, data=raw, method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                self.send(r.status, r.read(), r.headers.get("content-type", "application/json"))
        except urllib.error.HTTPError as e:
            self.send(e.code, e.read(), e.headers.get("content-type", "application/json"))

    def do_GET(self):
        if self.path.endswith("/models"):
            return self.send(200, {"object": "list", "data": [{"id": "e2e-scripted", "object": "model"},
                                                              {"id": "e2e-embed", "object": "model"}]})
        if self.path in ("/health", "/healthz"):
            return self.send(200, {"ok": True, "mode": "recorder" if UPSTREAM else "scripted"})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.loads(raw or b"{}")
        if self.path.endswith("/embeddings"):
            if EMBED_UPSTREAM:
                return self.forward(EMBED_UPSTREAM, raw)
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            return self.send(200, {"object": "list", "model": body.get("model"),
                                   "data": [{"object": "embedding", "index": i, "embedding": embed(t)}
                                            for i, t in enumerate(inputs)],
                                   "usage": {"prompt_tokens": sum(len(t) // 4 + 1 for t in inputs),
                                             "total_tokens": sum(len(t) // 4 + 1 for t in inputs)}})
        if not self.path.endswith("/chat/completions"):
            return self.send(404, {"error": {"message": f"no route {self.path}"}})
        record(self.path, body)
        if UPSTREAM:
            return self.forward(UPSTREAM, raw)
        out = scripted(body)
        # A dict is an assistant message with tool calls (the care guide's search).
        message = out if isinstance(out, dict) else {"role": "assistant", "content": out}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        prompt_tokens = len(chat_text(body.get("messages", [])).encode()) // 4 + 8
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(text.encode()) // 4 + 1}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        rid = "chatcmpl-e2e-" + hashlib.sha1(raw).hexdigest()[:12]
        if body.get("stream"):
            chunks = [{"id": rid, "object": "chat.completion.chunk", "model": body.get("model"),
                       "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                      {"id": rid, "object": "chat.completion.chunk", "model": body.get("model"),
                       "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}]},
                      {"id": rid, "object": "chat.completion.chunk", "model": body.get("model"), "choices": [],
                       "usage": usage}]
            data = "".join(f"data: {json.dumps(c, ensure_ascii=False)}\n\n" for c in chunks) + "data: [DONE]\n\n"
            return self.send(200, data.encode(), "text/event-stream")
        self.send(200, {"id": rid, "object": "chat.completion", "created": int(time.time()), "model": body.get("model"),
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
                        "usage": usage})


def main():
    port = int(os.environ.get("MOCK_PORT", "8000"))
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"e2e model on :{port} ({'recorder -> ' + UPSTREAM if UPSTREAM else 'scripted'}), log {LOG}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
