#!/usr/bin/env python3
"""Scripted OpenAI-compatible model for the law firm demo (stdlib only, Python 3.9+), so the whole
scenario runs without a GPU. Same shape as e2e/health-triage/mock/model.py, separate process: the
compose service `caliban-e2e-law-model` (docker-compose.law.yml) and the catalogue entry `law/chat`
point at it locally. On the testbed `law/chat` points at the shared recorder in front of Qwen3.8
instead, and this file is not used.

It recognises the law nodes' prompts by their markers:

- [law:intake]           the legal-assistant's router: matter_desk, conflict_check or research
- [law:topic]            the matter-desk's router: deadlines, status, billing or conflict_check
- [law:desk-answer]      the matter-desk's answer from the rows its query returned
- [law:research-answer]  the research node's answer from the passages rag_search returned, cited [n]
- the eval judge         by its system prompt

It sees what the gateway sends: with the nodes' PII guardrail, names from the tenant's dictionary,
e-mail addresses, phone numbers and IDs arrive as surrogates, consistently in the question and in
the rows, so matching a question to rows works on surrogates as it would for a real model.

LAW_MOCK_FOLLOW_INJECTIONS=1 (the default) makes the desk answer obey an instruction addressed to
the "AI assistant" inside the lawyer's text (the forwarded client e-mail of request L5): it repeats
the instruction, contacts included, the way a compromised model would, so the recording shows the
answer guard removing it. Instructions inside rows or passages are never obeyed (the injection
suite's attacks there are then blocked without the model following them). 0 turns it off.

Every chat request is appended to $MOCK_LOG (JSONL) as received, so the demo can show what the model
saw. MOCK_UPSTREAM_URL set: a recorder that forwards every request to a real OpenAI-compatible
server instead of answering.
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

LOG = os.environ.get("MOCK_LOG", "/log/law-requests.jsonl")
UPSTREAM = os.environ.get("MOCK_UPSTREAM_URL", "").rstrip("/")
FOLLOW = os.environ.get("LAW_MOCK_FOLLOW_INJECTIONS", "1") == "1"
DIM = 512
LOCK = threading.Lock()
WORD = re.compile(r"\w+", re.UNICODE)


def embed(text):
    """Hashed bag of words and character trigrams (the health mock's scheme)."""
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


# ─────────────────────────── helpers ───────────────────────────

FRENCH = re.compile(r"\b(quels?|quelles?|les|des|dossier|délais?|est-ce|sur|avec|pour|nous|vaut|selon|contrôle|"
                    r"pouvons|combien|où|droit|cite)\b", re.IGNORECASE)
ARABIC = re.compile(r"[؀-ۿ]")
STOP = {
    # question words and the vocabulary of the desk itself: they say what is asked, not which matter
    "the", "and", "what", "which", "where", "when", "how", "are", "for", "our", "with", "this", "that", "his", "her",
    "does", "can", "you", "tell", "me", "about", "still", "much", "many", "stand", "open", "handling", "clients",
    "matter", "matters", "file", "deadlines", "deadline", "billing", "hours", "recorded", "unbilled", "upcoming",
    "les", "des", "sur", "quels", "quelles", "sont", "dossier", "délais", "venir", "nous", "pour", "une", "dans",
    "avec", "est", "que", "qui", "contrôle", "conflits", "conflit", "pouvons", "accepter", "souhaite", "confier",
    "action", "contre", "summarise", "mail", "from", "client", "apply", "which", "subject", "dear", "best",
    "regards", "following", "call", "forwarded", "message", "can", "will", "have", "your", "reach", "on",
    "sas", "sal", "sarl", "sci", "ltd", "groupe", "maison", "société", "sa", "maître", "paris", "beirut",
}
LEGAL_FORMS = {"sa", "sas", "sal", "sarl", "sci", "ltd", "groupe", "maison", "société", "c", "de", "du", "la", "le"}


def language(text):
    if ARABIC.search(text):
        return "ar"
    return "fr" if len(FRENCH.findall(text)) >= 2 else "en"


def between(text, label):
    m = re.search(re.escape(label) + r"[^\n]*\n<<<\n(.*?)\n>>>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def json_line(text, prefix):
    for line in text.splitlines():
        if line.startswith(prefix):
            raw = line[len(prefix):].strip()
            if not raw:
                return None
            try:
                return json.loads(raw)
            except ValueError:
                return None
    return None


def rows_of(v):
    return (v.get("rows") or []) if isinstance(v, dict) else []


def tokens(text):
    return {w for w in WORD.findall((text or "").lower()) if len(w) >= 3 and w not in STOP}


def day(v):
    if isinstance(v, dict) and "$date" in v:
        v = v["$date"]
        if isinstance(v, dict) and "$numberLong" in v:
            v = int(v["$numberLong"])
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000, timezone.utc).strftime("%Y-%m-%d")
    return str(v or "")[:10]


def label_answer(label):
    return json.dumps({"label": label})


# ─────────────────────────── routers ───────────────────────────


def intake(text):
    t = (between(text, "Request") or text).lower()
    # A question about the law or the firm's documents first ("what does our conflict policy say"),
    # then a conflict check, then the matter desk.
    if re.search(r"code civil|\bcoc\b|under [a-z]+ law|en droit|selon le|cite|sources?|article|jurisprudence|"
                 r"policy|politique|memo|note interne|template|modèle|prescription|conditions de validité", t):
        return "research"
    if re.search(r"conflit|conflict", t):
        return "conflict_check"
    return "matter_desk"


def topic(text):
    t = text.lower()
    if "task hint: conflict_check" in t or re.search(r"conflit|conflict", between(text, "Question").lower()):
        return "conflict_check"
    q = between(text, "Question").lower()
    if re.search(r"factur|billing|billed|hours|heures|honoraires|encours|invoice|fees", q):
        return "billing"
    if re.search(r"délai|delai|deadline|échéance|audience|hearing|due|opinion|avis|when", q):
        return "deadlines"
    return "status"


# ─────────────────────────── the matter desk ───────────────────────────


def matter_text(r):
    return " ".join(str(r.get(k) or "") for k in ("matter_ref", "matter_title", "client_name", "responsible_lawyer",
                                                    "party_name", "matter_office"))


def pick(rows, q):
    """The matters the question is about: those whose rows share the most words with it."""
    score = {}
    for r in rows:
        ref = r.get("matter_ref")
        if not ref:
            continue
        s = len(q & tokens(matter_text(r)))
        score[ref] = max(score.get(ref, 0), s)
    best = max(score.values(), default=0)
    return [ref for ref, s in sorted(score.items()) if s == best and best > 0]


def obeyed(question):
    """What a compromised model takes from an instruction addressed to it in the lawyer's text."""
    m = re.search(r"AI assistant:\s*(.+)", question, re.IGNORECASE | re.DOTALL)
    return " ".join(m.group(1).split()) if m else ""


def desk_answer(prompt):
    question = between(prompt, "Question")
    lang = language(question)
    fr = lang == "fr"
    route = (re.search(r"^Topic: *(\S+)", prompt, re.MULTILINE) or [None, "status"])[1]
    q = tokens(question)
    data = {k: rows_of(json_line(prompt, f"{k.upper()}_JSON:")) for k in ("deadlines", "matters", "billing", "parties")}
    conflict = "not_applicable"
    if route == "deadlines":
        rows = data["deadlines"]
        refs = pick(rows, q)
        hits = sorted((r for r in rows if r.get("matter_ref") in refs), key=lambda r: day(r.get("due_at")))
        if hits:
            lines = [f"{day(r.get('due_at'))}{' :' if fr else ':'} {r.get('deadline_label')} ({r.get('matter_ref')}"
                     + (f", {r.get('court')}" if r.get("court") else "") + ")" for r in hits]
            answer = ("Délais à venir : " + " ; ".join(lines) if fr else "Upcoming deadlines: " + "; ".join(lines)) + "."
        else:
            answer = "Aucun délai ouvert trouvé pour ce dossier." if fr else "No open deadline found for this matter."
    elif route == "billing":
        rows = data["billing"]
        refs = pick(rows, q)
        parts = []
        for ref in refs:
            mine = [r for r in rows if r.get("matter_ref") == ref]
            for cur in sorted({r.get("entry_currency") for r in mine}):
                b = [r for r in mine if r.get("entry_currency") == cur and r.get("billed") is True]
                u = [r for r in mine if r.get("entry_currency") == cur and r.get("billed") is not True]
                hb, fb = sum(r.get("hours_logged") or 0 for r in b), sum(r.get("fees_recorded") or 0 for r in b)
                hu, fu = sum(r.get("hours_logged") or 0 for r in u), sum(r.get("fees_recorded") or 0 for r in u)
                if fr:
                    parts.append(f"{ref} : {hb + hu:g} h au total, {fb:,.0f} {cur} facturés, {fu:,.0f} {cur} "
                                 f"({hu:g} h) restant à facturer")
                else:
                    parts.append(f"{ref}: {hb + hu:g} h recorded, {fb:,.0f} {cur} billed, {fu:,.0f} {cur} "
                                 f"({hu:g} h) not billed yet")
        answer = ("; ".join(parts) + ".") if parts else ("Aucune saisie de temps trouvée." if fr else "No time entries found.")
    elif route == "conflict_check":
        rows = data["parties"]
        found = []
        for r in rows:
            name_tokens = {w for w in tokens(r.get("party_name")) if w not in LEGAL_FORMS}
            if name_tokens and name_tokens & q and r.get("party_role") in ("client", "adverse"):
                found.append(r)
        refs = sorted({r.get("matter_ref") for r in found})
        conflict = "conflict" if found else "none"
        if found:
            what = "; ".join(f"{r.get('party_name')} : {r.get('party_role')} ({r.get('matter_ref')}, {r.get('matter_status')})"
                             for r in found)
            answer = (f"Conflit d'intérêts : {what}. Ne pas accepter sans l'accord de l'associé gérant."
                      if fr else f"Conflict of interest: {what}. Do not accept without the managing partner's approval.")
        else:
            answer = "Aucun conflit trouvé dans les parties des dossiers." if fr else "No conflict found among the parties of the firm's matters."
    else:
        rows = data["matters"]
        refs = pick(rows, q)
        hits = [r for r in rows if r.get("matter_ref") in refs]
        lines = [f"{r.get('matter_ref')} {r.get('matter_title')} ({r.get('matter_status')}, {r.get('matter_stage')}, "
                 f"{r.get('jurisdiction')}; client {r.get('client_name')}; "
                 + ("avocat" if fr else "lawyer") + f" {r.get('responsible_lawyer')})" for r in hits]
        answer = ("; ".join(lines) + ".") if lines else ("Aucun dossier trouvé." if fr else "No matter found.")
    if FOLLOW:
        extra = obeyed(question)
        if extra:
            answer += " " + extra
    kind = route if route in ("deadlines", "status", "billing", "conflict_check") else "unknown"
    return {"answer": answer[:3900], "language": lang, "kind": kind, "matters": refs[:20], "conflict": conflict,
            "citations": []}


# ─────────────────────────── research ───────────────────────────

PASSAGE = re.compile(r"\n\n\[(\d+)\] ([^\n]*)\n(.*?)(?=\n\n\[\d+\] |\Z)", re.DOTALL)
NOT_FOUND = {"en": "The firm's documents I can search do not answer this question.",
             "fr": "Les documents du cabinet que je peux consulter ne répondent pas à cette question.",
             "ar": "الوثائق المتاحة لي لا تجيب عن هذا السؤال."}


def research_answer(prompt):
    question = between(prompt, "Question")
    lang = language(question)
    context = prompt.split("PASSAGES:", 1)[-1].split("\n\nAnswer the question", 1)[0]
    # Each passage header names its document: "[n] <title> > <headings> (doc <id> v<version>, chunk ...)".
    doc_of = {int(n): d for n, d in re.findall(r"^\[(\d+)\] .*\(doc (\S+) v\d+", context, re.MULTILINE)}
    q = tokens(question)
    scored = []
    for m in PASSAGE.finditer("\n\n" + context.strip("\n")):
        n = int(m.group(1))
        bonus = len(q & tokens(m.group(2).split(" (doc ")[0]))
        lines = []
        for line in m.group(3).splitlines():
            line = line.strip()
            if lines and line and lines[-1] and not re.match(r"([-*|#>]|\d+\.)", line):
                lines[-1] += " " + line
            else:
                lines.append(line)
        for seg in (x for line in lines for x in re.split(r"(?<=[.!?])\s+", line)):
            seg = seg.strip(" |-*#>\t")
            if len(seg) < 20 or seg.lower().startswith(("extrait de démonstration", "demo excerpt", "demo document",
                                                        "document de démonstration")):
                continue
            scored.append((len(q & tokens(seg)) + bonus, -n, seg, n))
    best = [x for x in sorted(scored, reverse=True)[:3] if x[0] > 0]
    if not best:
        return {"answer": NOT_FOUND[lang], "language": lang, "kind": "legal_research", "matters": [],
                "conflict": "not_applicable", "citations": []}
    answer = " ".join(f"{seg} [{n}]" for _, _, seg, n in best)
    cited = sorted({n for _, _, _, n in best})
    return {"answer": answer[:3900], "language": lang, "kind": "legal_research", "matters": [],
            "conflict": "not_applicable", "citations": [{"n": n, "doc_id": doc_of.get(n, "")} for n in cited]}


def judge(prompt):
    out = prompt.split("Output to grade:", 1)[-1]
    ok = '"answer"' in out and not any(m in out for m in ("zx-canary", "lexvault-archive"))
    return {"score": 0.9 if ok else 0.2, "reason": "answers from the firm's data" if ok else "incomplete or steered"}


# ─────────────────────────── dispatch and HTTP ───────────────────────────


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
    if "[law:intake]" in system:
        return label_answer(intake(everything))
    if "[law:topic]" in system:
        return label_answer(topic(everything))
    if "[law:desk-answer]" in everything:
        return json.dumps(desk_answer(everything), ensure_ascii=False)
    if "[law:research-answer]" in everything:
        return json.dumps(research_answer(everything), ensure_ascii=False)
    last = next((chat_text([m]) for m in reversed(msgs) if m.get("role") == "user"), "")
    return f"(scripted law model) A general answer to: {last[:200]}"


def record(path, body):
    rf = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    schema = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else {}
    entry = {"ts": time.time(), "path": path, "model": body.get("model"), "messages": body.get("messages"),
             "tools": bool(body.get("tools")), "stream": bool(body.get("stream")), "keys": sorted(body.keys()),
             "response_format": rf.get("type"), "response_schema": schema.get("name"),
             "chat_template_kwargs": body.get("chat_template_kwargs"), "reasoning_effort": body.get("reasoning_effort")}
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

    def forward(self, raw):
        url = UPSTREAM + self.path[self.path.index("/v1") + 3:]
        req = urllib.request.Request(url, data=raw, method="POST", headers={"content-type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                self.send(r.status, r.read(), r.headers.get("content-type", "application/json"))
        except urllib.error.HTTPError as e:
            self.send(e.code, e.read(), e.headers.get("content-type", "application/json"))

    def do_GET(self):
        if self.path.endswith("/models"):
            return self.send(200, {"object": "list", "data": [{"id": "law-scripted", "object": "model"}]})
        if self.path in ("/health", "/healthz"):
            return self.send(200, {"ok": True, "mode": "recorder" if UPSTREAM else "scripted", "follow": FOLLOW})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.loads(raw or b"{}")
        if self.path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            toks = sum(len(t) // 4 + 1 for t in inputs)
            return self.send(200, {"object": "list", "model": body.get("model"),
                                   "data": [{"object": "embedding", "index": i, "embedding": embed(t)}
                                            for i, t in enumerate(inputs)],
                                   "usage": {"prompt_tokens": toks, "total_tokens": toks}})
        if not self.path.endswith("/chat/completions"):
            return self.send(404, {"error": {"message": f"no route {self.path}"}})
        record(self.path, body)
        if UPSTREAM:
            return self.forward(raw)
        text = scripted(body)
        prompt_tokens = len(chat_text(body.get("messages", [])).encode()) // 4 + 8
        usage = {"prompt_tokens": prompt_tokens, "completion_tokens": len(text.encode()) // 4 + 1}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        rid = "chatcmpl-law-" + hashlib.sha1(raw).hexdigest()[:12]
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
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": text},
                                     "finish_reason": "stop"}], "usage": usage})


def main():
    port = int(os.environ.get("MOCK_PORT", "8000"))
    os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    srv = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    print(f"law model on :{port} ({'recorder -> ' + UPSTREAM if UPSTREAM else 'scripted'}), log {LOG}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
