#!/usr/bin/env python3
"""Scripted OpenAI-compatible model for the developer scenario (local testing only, stdlib only).

The real recording runs against Qwen3.8-27B-FP8 on vLLM; this stand-in lets the whole developer
journey run on a laptop, deterministically, without a GPU. It answers:

- /v1/embeddings: hashed bag-of-words vectors (Unicode words plus character trigrams), the same
  recipe as the health triage e2e model, good enough for caliban/auto's kNN over English, French
  and Arabic exemplars when they share words with the prompts;
- the five node prompts by their markers: [dev:plan] (the orchestrator's plan), [dev:billing],
  [dev:bug], [dev:docs] (an agent turn: search first, then answer from the passages) and
  [dev:reply] (the orchestrator's merged answer);
- the eval judge ("You grade the output of an AI system");
- anything else as a plain support chat model.

Every chat request is appended to $MOCK_LOG (JSONL) with how the gateway shaped it
(response_format, chat_template_kwargs, reasoning_effort), so a local run can show what the model
received.
"""
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LOG = os.environ.get("MOCK_LOG", "")
DIM = 512
LOCK = threading.Lock()
WORD = re.compile(r"\w+", re.UNICODE)
ARABIC = re.compile(r"[؀-ۿ]")
FRENCH = re.compile(r"\b(je|j'ai|mon|ma|mes|nous|notre|est|une|des|avec|le|la|les|pour|comment|deux|fois)\b",
                    re.IGNORECASE)

# ─────────────────────────── embeddings ───────────────────────────


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


# ─────────────────────────── helpers ───────────────────────────


def language(text):
    if ARABIC.search(text):
        return "ar"
    if FRENCH.search(text):
        return "fr"
    return "en"


def between(text, label):
    """The text between '<label>:\n<<<\n' and '\n>>>' in a prompt."""
    m = re.search(re.escape(label) + r"[^\n]*\n<<<\n(.*?)\n>>>", text, re.DOTALL)
    return m.group(1).strip() if m else ""


def json_after(text, label):
    """The JSON value that follows '<label>:' on its own line."""
    for line in text.splitlines():
        if line.startswith(label + ":"):
            try:
                return json.loads(line[len(label) + 1:].strip())
            except ValueError:
                return None
    return None


def has(text, words):
    low = text.lower()
    return any(w in low for w in words)


BILLING = ["charge", "invoice", "refund", "payment", "billed", "card", "vat", "receipt", "subscription",
           "factur", "rembours", "paiement", "prélev", "débit", "carte", "abonnement",
           "فاتورة", "خصم", "دفع", "استرداد", "مرتين", "اشتراك", "بطاقة"]
BUG = ["crash", "error", "broken", "bug", "fails", "freez", "not working", "doesn't work", "does not work",
       "blank", "plante", "erreur", "ne marche", "ne fonctionne", "bloqu", "يتعطل", "خطأ", "لا يعمل", "عطل", "تعطل"]
DOCS = ["how do i", "how to", "how can i", "where can i", "where do i", "configure", "set up", "enable",
        "comment ", "configurer", "activer", "où ", "كيف", "طريقة", "أين"]
DOUBLE = ["twice", "double", "two times", "deux fois", "en double", "مرتين", "مرتان"]


def clauses(message):
    parts = re.split(r"[.;!?؟\n]+|,?\s+\band also\b|,?\s+\band\b|,?\s+\bet\b|،\s*|\s+و(?=[؀-ۿ]{3,})",
                     message)
    return [p.strip() for p in parts if p and len(p.strip()) > 2]


# ─────────────────────────── the scripted nodes ───────────────────────────


def plan(prompt):
    """[dev:plan]: which subagents the message needs, with the part of it each one gets."""
    message = between(prompt, "Customer message")
    lang = language(message)
    tasks = []
    for agent, words in (("billing", BILLING), ("bug", BUG), ("docs", DOCS)):
        part = [c for c in clauses(message) if has(c, words)]
        if part:
            tasks.append({"agent": agent, "request": " ".join(part)})
    if not tasks:
        tasks.append({"agent": "docs", "request": message})
    return {"language": lang, "tasks": tasks[:3]}


def billing(prompt):
    req = between(prompt, "Customer request")
    lang = language(req)
    if has(req, DOUBLE):
        issue = "double_charge"
    elif has(req, ["refund", "rembours", "استرداد"]):
        issue = "refund"
    elif has(req, ["invoice", "receipt", "vat", "factur", "فاتورة"]):
        issue = "invoice"
    elif has(req, ["card", "carte", "بطاقة"]):
        issue = "payment_method"
    else:
        issue = "other"
    steps = {
        "double_charge": ["Confirm the two charges on the invoice history page",
                          "The duplicate charge is refunded automatically within 5 to 7 business days once verified"],
        "refund": ["Refunds of annual plans are prorated within 30 days of renewal",
                   "Reply with the invoice number to start the refund"],
        "invoice": ["Invoices are under Settings > Billing > Invoice history",
                    "Add the VAT number in Settings > Billing > Company details, then regenerate the invoice"],
        "payment_method": ["Update the card in Settings > Billing > Payment method"],
        "other": ["A billing specialist will follow up within one business day"],
    }[issue]
    summary = {"double_charge": "Duplicate charge on the subscription", "refund": "Refund request",
               "invoice": "Invoice question", "payment_method": "Payment method change",
               "other": "Billing question"}[issue]
    return {"issue": issue, "summary": summary, "next_steps": steps,
            "refund_eligible": issue in ("double_charge", "refund"), "language": lang}


def bug(prompt):
    req = between(prompt, "Bug report")
    lang = language(req)
    if has(req, ["export", "csv", "exporter", "تصدير"]):
        component, known, workaround = "export", "EXP-311", "Export a shorter date range (under 10,000 rows) until the fix ships"
    elif has(req, ["login", "sign in", "connexion", "تسجيل الدخول"]):
        component, known, workaround = "auth", "", "Clear the browser cache and sign in again"
    elif has(req, ["sync", "synchron", "مزامنة"]):
        component, known, workaround = "sync", "SYN-208", "Trigger a manual sync from Settings > Integrations"
    elif has(req, ["editor", "éditeur", "محرر"]):
        component, known, workaround = "editor", "", "Reload the page; drafts are saved every 10 seconds"
    else:
        component, known, workaround = "other", "", "None known yet"
    severity = "critical" if has(req, ["data loss", "lost data", "perte de données"]) else (
        "high" if has(req, ["crash", "plante", "يتعطل", "تعطل"]) else "medium")
    return {"severity": severity, "component": component,
            "title": f"{component.capitalize()}: {req[:80]}", "known_issue": known,
            "workaround": workaround, "language": lang}


PASSAGE = re.compile(r"\n\n\[(\d+)\] ([^\n]*)\n(.*?)(?=\n\n\[\d+\] |\Z)", re.DOTALL)
STOP = {"the", "and", "how", "can", "what", "where", "does", "for", "with", "this", "that", "our", "you",
        "les", "des", "une", "est", "que", "qui", "pour", "dans", "comment", "كيف"}
NOT_FOUND = {"en": "The product documentation does not answer this question.",
             "fr": "La documentation produit ne répond pas à cette question.",
             "ar": "لا تجيب وثائق المنتج عن هذا السؤال."}


def words(text):
    return {w for w in WORD.findall(text.lower()) if len(w) >= 3 and w not in STOP}


def docs(msgs, tools):
    """[dev:docs]: an agent turn of docs-helper: search first, then answer from the passages."""
    user = next((m.get("content") for m in msgs if m.get("role") == "user"), "") or ""
    try:
        question = json.loads(user).get("request", user)
    except (ValueError, AttributeError):
        question = user
    lang = language(question)
    results = [m for m in msgs if m.get("role") == "tool"]
    if not results:
        if "rag_search" not in tools:
            return json.dumps({"answer": NOT_FOUND[lang], "citations": [], "language": lang})
        args = json.dumps({"query": question, "k": 4}, ensure_ascii=False)
        return {"role": "assistant", "content": None, "tool_calls": [
            {"id": "call_docs_0", "type": "function", "function": {"name": "rag_search", "arguments": args}}]}
    raw = results[-1].get("content") or ""
    try:
        out = json.loads(raw)
    except ValueError:
        out = {"context": raw, "results": []}
    if not isinstance(out, dict):
        out = {"context": str(out), "results": []}
    doc_of = {r.get("n"): r.get("doc_id") for r in out.get("results") or []}
    q = words(question)
    scored = []
    for m in PASSAGE.finditer(out.get("context") or ""):
        n = int(m.group(1))
        for seg in re.split(r"(?<=[.!?؟])\s+|\n+", m.group(3)):
            seg = seg.strip(" |-*#\t")
            if len(seg) >= 12:
                scored.append((len(q & words(seg)), -n, seg, n))
    best = [x for x in sorted(scored, reverse=True)[:2] if x[0] > 0]
    if not best:
        return json.dumps({"answer": NOT_FOUND[lang], "citations": [], "language": lang}, ensure_ascii=False)
    answer = " ".join(f"{seg} [{n}]" for _, _, seg, n in best)
    cited = sorted({n for _, _, _, n in best})
    return json.dumps({"answer": answer, "citations": [{"n": n, "doc_id": doc_of.get(n, "")} for n in cited],
                       "language": lang}, ensure_ascii=False)


def reply(prompt):
    """[dev:reply]: the orchestrator's one answer from its subagents' results."""
    p = json_after(prompt, "PLAN_JSON") or {}
    results = json_after(prompt, "RESULTS_JSON") or []
    lang = p.get("language", "en")
    agents = [t.get("agent") for t in p.get("tasks") or []]
    parts = []
    for agent, r in zip(agents, results):
        if not isinstance(r, dict):
            continue
        if agent == "billing":
            parts.append({"en": f"Billing: {r.get('summary', '')}. " + " ".join(s + "." for s in r.get("next_steps") or []),
                          "fr": f"Facturation : {r.get('summary', '')}. " + " ".join(s + "." for s in r.get("next_steps") or []),
                          "ar": f"الفوترة: {r.get('summary', '')}. " + " ".join(s + "." for s in r.get("next_steps") or [])}[lang])
        elif agent == "bug":
            known = f" (known issue {r['known_issue']})" if r.get("known_issue") else ""
            parts.append({"en": f"Bug: logged as {r.get('severity')} severity in {r.get('component')}{known}. Workaround: {r.get('workaround')}.",
                          "fr": f"Bug : enregistré en sévérité {r.get('severity')} sur {r.get('component')}{known}. Contournement : {r.get('workaround')}.",
                          "ar": f"خلل: سُجّل بخطورة {r.get('severity')} في {r.get('component')}{known}. الحل المؤقت: {r.get('workaround')}."}[lang])
        elif agent == "docs":
            parts.append(r.get("answer", ""))
    text = " ".join(x for x in parts if x) or NOT_FOUND[lang]
    return {"reply": text, "handled_by": [a for a in agents if a], "language": lang}


def judge(prompt):
    out = prompt.split("Output to grade:", 1)[-1]
    ok = len(out.strip()) > 20 and "does not answer" not in out
    return {"score": 0.9 if ok else 0.3, "reason": "answers the request" if ok else "incomplete"}


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
    if "[dev:docs]" in system:
        return docs(msgs, [t.get("function", {}).get("name") for t in body.get("tools") or []])
    for marker, fn in (("[dev:plan]", plan), ("[dev:billing]", billing), ("[dev:bug]", bug), ("[dev:reply]", reply)):
        if marker in everything:
            return json.dumps(fn(everything), ensure_ascii=False)
    last = next((chat_text([m]) for m in reversed(msgs) if m.get("role") == "user"), "")
    return f"(scripted model) Thanks for reaching out. Here is a short answer to: {last[:160]}"


# ─────────────────────────── HTTP ───────────────────────────


def record(path, body):
    if not LOG:
        return
    rf = body.get("response_format") if isinstance(body.get("response_format"), dict) else {}
    schema = rf.get("json_schema") if isinstance(rf.get("json_schema"), dict) else {}
    entry = {"ts": time.time(), "path": path, "model": body.get("model"), "messages": body.get("messages"),
             "tools": [t.get("function", {}).get("name") for t in body.get("tools") or []],
             "response_format": rf.get("type"), "response_schema": schema.get("name"),
             "chat_template_kwargs": body.get("chat_template_kwargs"),
             "reasoning_effort": body.get("reasoning_effort")}
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

    def do_GET(self):
        if self.path.endswith("/models"):
            return self.send(200, {"object": "list", "data": [{"id": "dev-scripted", "object": "model"},
                                                              {"id": "dev-embed", "object": "model"}]})
        if self.path in ("/health", "/healthz"):
            return self.send(200, {"ok": True})
        self.send(404, {"error": "not found"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length", "0")))
        body = json.loads(raw or b"{}")
        if self.path.endswith("/embeddings"):
            inputs = body["input"] if isinstance(body["input"], list) else [body["input"]]
            tokens = sum(len(t) // 4 + 1 for t in inputs)
            return self.send(200, {"object": "list", "model": body.get("model"),
                                   "data": [{"object": "embedding", "index": i, "embedding": embed(t)}
                                            for i, t in enumerate(inputs)],
                                   "usage": {"prompt_tokens": tokens, "total_tokens": tokens}})
        if not self.path.endswith("/chat/completions"):
            return self.send(404, {"error": {"message": f"no route {self.path}"}})
        record(self.path, body)
        out = scripted(body)
        message = out if isinstance(out, dict) else {"role": "assistant", "content": out}
        finish = "tool_calls" if message.get("tool_calls") else "stop"
        text = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
        usage = {"prompt_tokens": len(chat_text(body.get("messages", [])).encode()) // 4 + 8,
                 "completion_tokens": len(text.encode()) // 4 + 1}
        usage["total_tokens"] = usage["prompt_tokens"] + usage["completion_tokens"]
        rid = "chatcmpl-dev-" + hashlib.sha1(raw).hexdigest()[:12]
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
                        "choices": [{"index": 0, "message": message, "finish_reason": finish}], "usage": usage})


def main():
    port = int(os.environ.get("MOCK_PORT", "8000"))
    if LOG:
        os.makedirs(os.path.dirname(LOG) or ".", exist_ok=True)
    srv = ThreadingHTTPServer((os.environ.get("MOCK_HOST", "0.0.0.0"), port), Handler)
    print(f"developer scripted model on :{port}" + (f", log {LOG}" if LOG else ""), flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    sys.exit(main())
