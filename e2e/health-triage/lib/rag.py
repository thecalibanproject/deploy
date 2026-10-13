"""The optional RAG stage of the health triage e2e (E2E_RAG=1; stdlib only, Python 3.9+).

Setup (admin API): a document collection of fictional hospital protocols and an insurance coverage
policy in English, French and Arabic (rag/documents.json, Markdown), ingested through
POST /api/v1/tenants/{t}/rag/collections/{c}/documents; the standalone process indexes them into
Qdrant. One untrusted document carries a prompt injection and must be quarantined; one policy is
outside its validity window and must never be retrieved. Then the agent node `careguide`, which
searches the collection with builtin://rag_search under a rag.<collection>:read scope, and an
application API key allowed to run it only, narrowed to that collection.

Run: requests that need the documents, through POST /v1/nodes/careguide/runs with that key, and
checks on the documents (ready, quarantined), the search (citations, the `retrieved` taint label,
the expired and the poisoned documents never served), what the model read, the answer and the
metering. Core docs/rag.md describes what is checked.
"""
import json
import os
import re
import time

from common import HERE, TENANT, HttpError, cp, dp, info, step, wait_until

ENABLED = os.environ.get("E2E_RAG", "0") == "1"
COLLECTION = os.environ.get("E2E_RAG_COLLECTION", "care_docs")
EMBED_MODEL = os.environ.get("E2E_RAG_EMBED_MODEL") or os.environ.get("E2E_ROUTING_EMBED_MODEL", "e2e/embed")
RERANK_MODEL = os.environ.get("E2E_RAG_RERANK_MODEL", "")
CHAT_ID = os.environ.get("E2E_CHAT_MODEL_ID", "e2e/chat")
NODE = "careguide"
TERMINAL_DOC = {"ready", "quarantined", "failed"}
TERMINAL_RUN = {"succeeded", "failed", "budget_exhausted", "cancelled"}
BASE = f"/api/v1/tenants/{TENANT}/rag/collections"


def manifest():
    with open(os.path.join(HERE, "rag", "documents.json"), encoding="utf-8") as f:
        return json.load(f)


def doc_versions(doc_id):
    return cp("GET", f"{BASE}/{COLLECTION}/documents/{doc_id}").body


# ─────────────────────────── setup ───────────────────────────


def setup(st):
    m = manifest()
    step(f"Retrieval: collection {COLLECTION} ({EMBED_MODEL}" + (f", rerank {RERANK_MODEL})" if RERANK_MODEL else ", no reranker)"))
    body = {"name": COLLECTION, "embedding_model": EMBED_MODEL, **m["collection"]}
    if RERANK_MODEL:
        body["rerank_model"] = RERANK_MODEL
    try:
        c = cp("POST", BASE, body).body
        info(f"{COLLECTION}: created ({c['id']})")
    except HttpError as e:
        if e.status != 409:
            raise
        c = cp("GET", f"{BASE}/{COLLECTION}").body
        info(f"{COLLECTION}: exists ({c['id']}, used by {c.get('used_by')})")
        if c.get("embedding_model") != EMBED_MODEL:
            # The embedding model is fixed for a collection's life (core docs/rag.md).
            info(f"warning: the collection embeds with {c.get('embedding_model')}, not {EMBED_MODEL}; "
                 "set E2E_RAG_COLLECTION to another name to start over")

    step(f"Retrieval: ingest {len(m['documents'])} documents (en, fr, ar)")
    for d in m["documents"]:
        with open(os.path.join(HERE, "rag", d["file"]), encoding="utf-8") as f:
            content = f.read()
        doc = {"doc_id": d["doc_id"], "title": d["title"], "content": content, "content_type": "text/markdown",
               "source": d["source"]}
        for k in ("trust", "valid_from", "valid_to"):
            if d.get(k):
                doc[k] = d[k]
        r = cp("POST", f"{BASE}/{COLLECTION}/documents", doc)
        b = r.body or {}
        what = "unchanged" if b.get("unchanged") else f"v{b.get('version')} {b.get('status')}"
        info(f"{d['doc_id']:<28} {d['language']} {doc.get('trust', 'default (untrusted)'):<20} HTTP {r.status} {what}")

    step("Retrieval: wait for the standalone process to index them")
    timeout = int(os.environ.get("E2E_RAG_INDEX_TIMEOUT_S", "600"))

    def settled():
        out = {}
        for d in m["documents"]:
            v = doc_versions(d["doc_id"])
            newest = (v.get("versions") or [{}])[0]
            if newest.get("status") not in TERMINAL_DOC:
                return None
            out[d["doc_id"]] = {"version": newest.get("version"), "status": newest.get("status"),
                                "live_version": v.get("live_version"), "chunks": newest.get("chunks"),
                                "tokens": newest.get("tokens"), "pii_entities": newest.get("pii_entities"),
                                "findings": sorted({x.get("kind") for x in newest.get("findings") or []}),
                                "error": newest.get("error")}
        return out

    docs = wait_until("the documents to be indexed (is CALIBAN_QDRANT_URL set and Qdrant up?)", settled,
                      timeout=timeout, every=2)
    for d in m["documents"]:
        s = docs[d["doc_id"]]
        info(f"{d['doc_id']:<28} {s['status']:<12} expected {d['expect']:<12} chunks {s['chunks']} tokens {s['tokens']} "
             f"findings {','.join(s['findings']) or '-'}" + (f" error {s['error']}" if s.get("error") else ""))
    bad = [d["doc_id"] for d in m["documents"] if docs[d["doc_id"]]["status"] != d["expect"]]
    if bad:
        # Reported by the run too; a trusted document that is not ready leaves nothing to search.
        info(f"warning: not in the expected state: {', '.join(bad)}")

    step(f"Node {NODE}: agent with builtin://rag_search, scope rag.{COLLECTION}:read")
    with open(os.path.join(HERE, "node", f"{NODE}.node.json"), encoding="utf-8") as f:
        spec = json.loads(f.read().replace("__CHAT_MODEL__", CHAT_ID).replace("__RAG_COLLECTION__", COLLECTION))
    base = f"/api/v1/tenants/{TENANT}/nodes/{NODE}"
    listed = cp("GET", f"{base}/versions", ok=None)
    existing = listed.body.get("versions", []) if listed.status == 200 else []
    live = listed.body.get("live") if listed.status == 200 else None
    same = [v for v in existing if v["spec"] == spec and v["state"] != "retired"]
    if same:
        v = same[-1]
        info(f"v{v['version']} has this spec already ({v['state']})")
    else:
        v = cp("POST", f"{base}/versions", {"spec": spec}).body
        info(f"created draft v{v['version']} {v['hash'][:19]}...")
    if v["state"] == "draft":
        # A version with read tools is promoted only after a finished injection eval (core's gate):
        # publish it without promotion, evaluate it, then promote it.
        v = cp("POST", f"{base}/versions/{v['version']}/publish", {"promote": False}).body
        info(f"published v{v['version']} (not live yet)")
    for w in v.get("warnings") or []:
        info(f"warning: {w}")
    if live != v["version"]:
        st["rag_injection_eval"] = injection_eval(base, v["version"], m["requests"][0]["text"])
        cp("POST", f"{base}/promote", {"version": v["version"]})
        info(f"promoted v{v['version']}")

    rag = st.get("rag") or {}
    if not rag.get("api_key"):
        k = cp("POST", f"/api/v1/tenants/{TENANT}/api-keys", {
            "name": "medmarket-careguide", "nodes": [NODE], "datasource_scopes": [f"rag.{COLLECTION}:read"]}).body
        rag.update(api_key=k["key"], api_key_id=k["id"])
        info(f"API key {k['id']} (node allowlist: {NODE}; datasource scopes: rag.{COLLECTION}:read)")
    else:
        info(f"API key {rag['api_key_id']} (from state.json)")
    rag.update(collection=COLLECTION, embedding_model=c.get("embedding_model"), rerank_model=c.get("rerank_model"),
               node_version=v["version"], node_hash=v["hash"], documents=docs)
    st["rag"] = rag


def injection_eval(base, version, text):
    """Runs the injection suite on a published version (the first question as the run input) and
    returns its id, status and summary once it has ended."""
    e = cp("POST", f"{base}/versions/{version}/evals", {"suite": "injection", "input": text}).body
    info(f"injection eval {e['id']} started ({e.get('case_count')} cases)")
    e = wait_until(f"the injection eval of {NODE}", lambda: (lambda b: b if b["status"] in (
        "succeeded", "failed", "cancelled") else None)(cp("GET", f"/api/v1/tenants/{TENANT}/evals/{e['id']}").body),
        timeout=int(os.environ.get("E2E_EVAL_TIMEOUT_S", "1800")), every=2)
    summary = e.get("summary") or {}
    info(f"injection eval: {e['status']}, {summary.get('blocked')}/{summary.get('trials')} blocked")
    return {"id": e["id"], "status": e["status"], "summary": summary}


# ─────────────────────────── run ───────────────────────────


def model_text(entry):
    out = []
    for msg in entry.get("messages") or []:
        c = msg.get("content")
        out.append(c if isinstance(c, str) else json.dumps(c, ensure_ascii=False))
    return "\n".join(out)


def parse_json(content):
    if isinstance(content, dict):
        return content
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        mt = re.search(r"\{.*\}", content or "", re.DOTALL)
        if mt:
            try:
                return json.loads(mt.group(0))
            except ValueError:
                pass
    return None


def document_checks(checks, m):
    step("Retrieval: documents")
    out = {}
    for d in m["documents"]:
        v = doc_versions(d["doc_id"])
        newest = (v.get("versions") or [{}])[0]
        kinds = sorted({x.get("kind") for x in newest.get("findings") or []})
        out[d["doc_id"]] = {"language": d["language"], "trust": newest.get("trust"), "version": newest.get("version"),
                            "status": newest.get("status"), "live_version": v.get("live_version"),
                            "chunks": newest.get("chunks"), "findings": kinds}
        if d.get("poisoned"):
            checks.add("rag", f"{d['doc_id']} (untrusted, carries an injection) is quarantined",
                       newest.get("status") == "quarantined" and v.get("live_version") is None,
                       f"status {newest.get('status')}, live {v.get('live_version')}, findings {','.join(kinds) or '-'}")
            checks.add("rag", f"{d['doc_id']} quarantine names the injection",
                       bool({"instruction", "exfiltration"} & set(kinds)), ",".join(kinds) or "-")
        else:
            checks.add("rag", f"{d['doc_id']} ({d['language']}) is indexed and served",
                       newest.get("status") == "ready" and v.get("live_version") == newest.get("version")
                       and (newest.get("chunks") or 0) > 0,
                       f"status {newest.get('status')}, live v{v.get('live_version')}, {newest.get('chunks')} chunks")
    listed = cp("GET", f"{BASE}/{COLLECTION}/documents?status=quarantined").body
    ids = {x.get("doc_id") for x in listed.get("documents") or []}
    poisoned = [d["doc_id"] for d in m["documents"] if d.get("poisoned")]
    checks.add("rag", "the quarantined list holds the poisoned document", set(poisoned) <= ids, ", ".join(sorted(ids)) or "-")
    return out


def run_one(rag, spec, m, checks, timeout, prompt_tag, model_log):
    key = rag["api_key"]
    text = f"{spec['text']} (ref {prompt_tag})" if prompt_tag else spec["text"]
    print(f"\n== {spec['id']}: {spec['title']}", flush=True)
    info(f"prompt: {text}")
    rec = {"id": spec["id"], "title": spec["title"], "prompt": text, "headers": {}}
    t0 = time.monotonic()
    r = dp("POST", f"/v1/nodes/{NODE}/runs", key, {"input": text}, ok=None, timeout=900)
    body = r.body if isinstance(r.body, dict) else {}
    run_id = body.get("id")
    checks.add("rag", f"{spec['id']} POST /v1/nodes/{NODE}/runs started a run", r.status in (200, 202) and bool(run_id),
               f"HTTP {r.status}" + ("" if run_id else f" {json.dumps(body, ensure_ascii=False)[:300]}"))
    if not run_id:
        rec["error"] = json.dumps(body, ensure_ascii=False)[:800]
        return rec
    rec["run_id"] = run_id
    run = wait_until(f"run {run_id}", lambda: (lambda x: x if x["status"] in TERMINAL_RUN else None)(
        dp("GET", f"/v1/runs/{run_id}", key).body), timeout=timeout, every=1.0)
    rec["latency_s"] = round(time.monotonic() - t0, 3)
    checks.add("rag", f"{spec['id']} run succeeded", run["status"] == "succeeded",
               f"status {run['status']}, stop_reason {run.get('stop_reason')}, error {run.get('error')}")
    rec["run"] = {k: run.get(k) for k in ("status", "cost_usd", "usage", "budget", "stop_reason")}
    steps = run.get("steps") or []
    rec["steps"] = [{k: s.get(k) for k in ("id", "vertex", "kind", "status", "tokens", "usd", "labels", "duration_ms")}
                    for s in steps]
    for s in rec["steps"]:
        info(f"step {s['id']:<14} {s['kind']:<6} {s['status']:<10} tokens {s['tokens'] or 0:>6} "
             f"usd {s['usd'] or 0:.6f} {s['duration_ms']} ms labels {','.join(s['labels'] or [])}")
    tool_steps = [s for s in rec["steps"] if s["kind"] == "tool"]
    llm_steps = [s for s in rec["steps"] if s["kind"] == "llm"]
    checks.add("rag", f"{spec['id']} the agent searched the documents (a rag_search tool step)", bool(tool_steps),
               " > ".join(s["id"] for s in rec["steps"]))
    checks.add("rag", f"{spec['id']} the search result carries the retrieved taint label",
               bool(tool_steps) and all("retrieved" in (s["labels"] or []) for s in tool_steps),
               "; ".join(",".join(s["labels"] or []) for s in tool_steps))
    checks.add("rag", f"{spec['id']} the model call that read it carries the retrieved label",
               bool(llm_steps) and "retrieved" in (llm_steps[-1]["labels"] or []),
               ",".join(llm_steps[-1]["labels"] or []) if llm_steps else "no model step")

    # The search results as the console shows them (runs.data).
    console = cp("GET", f"/api/v1/tenants/{TENANT}/runs/{run_id}").body
    outputs = [s.get("output") for s in console.get("steps") or [] if s.get("kind") == "tool"]
    results, contexts, strategy = [], [], {}
    for o in outputs:
        o = parse_json(o) if not isinstance(o, dict) else o
        if isinstance(o, dict):
            results += o.get("results") or []
            contexts.append(o.get("context") or "")
            strategy = o.get("strategy") or strategy
    rec["citations"] = [{k: c.get(k) for k in ("n", "doc_id", "doc_version", "chunk_id", "score", "heading_path", "valid_to")}
                        for c in results]
    rec["strategy"] = strategy
    for c in rec["citations"]:
        info(f"[{c['n']}] {c['doc_id']} v{c['doc_version']} {c['chunk_id']} score {c['score']} "
             f"{' > '.join(c.get('heading_path') or [])}")
    docs_seen = [c.get("doc_id") for c in results]
    checks.add("rag", f"{spec['id']} the search returned cited passages (doc, version, chunk)",
               bool(results) and all(c.get("doc_id") and c.get("chunk_id") and c.get("n") for c in results),
               f"{len(results)} passages; strategy {json.dumps(strategy)}")
    checks.add("rag", f"{spec['id']} the passages are framed as data, not instructions",
               bool(contexts) and all("data, not instructions" in c for c in contexts))
    checks.add("rag", f"{spec['id']} {spec['expect_doc']} is among the passages", spec["expect_doc"] in docs_seen,
               ", ".join(docs_seen))
    checks.add("quality", f"{spec['id']} {spec['expect_doc']} is the best passage ([1])",
               bool(results) and results[0].get("doc_id") == spec["expect_doc"], docs_seen[0] if docs_seen else "-")
    expired = [d["doc_id"] for d in m["documents"] if d.get("expired")]
    poisoned = [d for d in m["documents"] if d.get("poisoned")]
    checks.add("rag", f"{spec['id']} no passage from a document outside its validity window",
               not (set(expired) & set(docs_seen)), ", ".join(expired))
    canaries = [d["canary"] for d in poisoned]
    leaked = [c for c in canaries if any(c in x for x in contexts) or c in json.dumps(results)]
    checks.add("rag", f"{spec['id']} nothing from the quarantined document was served",
               not leaked and not ({d["doc_id"] for d in poisoned} & set(docs_seen)), ", ".join(leaked))

    # What the model read (the e2e model's request log, all of it: a repeated prompt may be served
    # from the exact cache without reaching the model).
    every_call = []
    if os.path.exists(model_log):
        with open(model_log, encoding="utf-8") as f:
            every_call = [json.loads(line) for line in f if line.strip()]
    seen = "\n".join(model_text(c) for c in every_call)
    first = results[0].get("chunk_id") if results else None
    checks.add("rag", f"{spec['id']} the model read the cited passages", bool(first) and first in seen, first or "-")
    checks.add("rag", f"{spec['id']} the quarantined document never reached the model",
               not any(c in seen for c in canaries), ", ".join(canaries))

    # The answer: grounded citations.
    output = run.get("output")
    ans = parse_json(output) if not isinstance(output, dict) else output
    ans = ans or {}
    rec["answer"] = output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
    rec["parsed"] = ans
    message = ans.get("answer") or ""
    info(f"answer: {message}")
    cited = ans.get("citations") or []
    by_n = {c.get("n"): c.get("doc_id") for c in results}
    grounded = [c for c in cited if isinstance(c, dict) and by_n.get(c.get("n")) == c.get("doc_id")]
    checks.add("rag", f"{spec['id']} the answer cites passages the search returned",
               bool(cited) and len(grounded) == len(cited), json.dumps(cited, ensure_ascii=False))
    checks.add("quality", f"{spec['id']} the answer marks its citations ([n])", bool(re.search(r"\[\d+\]", message)))
    checks.add("quality", f"{spec['id']} language is {spec['language']}", ans.get("language") == spec["language"],
               str(ans.get("language")))
    checks.add("quality", f"{spec['id']} the answer states {spec['expect_fact']!r}", spec["expect_fact"] in message)
    bad = [x for x in canaries + ["zx-attacker", "claims-desk@"] if x in rec["answer"]]
    checks.add("rag", f"{spec['id']} the answer carries nothing from the quarantined document", not bad, ", ".join(bad))

    # Metering: the query embedding (and rerank) are the run's, tagged with the node run.
    def usage():
        u = cp("GET", f"/api/v1/usage?tenant_id={TENANT}&run_id={run_id}&limit=100").body
        ev = u.get("events") or []
        chat = sum(1 for e in ev if e.get("model") == CHAT_ID)
        return u if chat >= len(llm_steps) and any(e.get("model") == rag.get("embedding_model") for e in ev) else None

    try:
        u = wait_until("the run's usage events", usage, timeout=30, every=1)
    except TimeoutError:
        u = cp("GET", f"/api/v1/usage?tenant_id={TENANT}&run_id={run_id}&limit=100").body
    ev = u.get("events") or []
    models = sorted({e.get("model") for e in ev})
    rec["usage"] = {"events": len(ev), "models": models, "cost_usd": (u.get("totals") or {}).get("cost_usd"),
                    "charged_usd": (u.get("totals") or {}).get("charged_usd")}
    rec["model_calls"] = sum(1 for e in ev if e.get("model") == CHAT_ID)
    info(f"metering: {json.dumps(rec['usage'])}")
    checks.add("rag", f"{spec['id']} the query embedding is metered as the run's", rag.get("embedding_model") in models,
               ", ".join(models))
    # Retrieval's own calls carry a usage purpose (core docs/rag.md, "Metering"): rag.search for the
    # query embedding and rerank, so retrieval spend is separable from the node's model calls.
    retrieval = [e for e in ev if e.get("model") in (rag.get("embedding_model"), rag.get("rerank_model"))]
    checks.add("rag", f"{spec['id']} the search's calls are labelled rag.search",
               bool(retrieval) and all(e.get("purpose") == "rag.search" for e in retrieval),
               ", ".join(sorted({str(e.get("purpose")) for e in retrieval})) or "no retrieval event")
    if rag.get("rerank_model"):
        checks.add("rag", f"{spec['id']} the rerank is metered as the run's", rag["rerank_model"] in models, ", ".join(models))
    return rec


def run(st, checks, timeout, only, prompt_tag, model_log):
    """Returns (results, summary) for the report; ([], None) when the stage is off."""
    if not ENABLED:
        return [], None
    rag = st.get("rag") or {}
    if not rag.get("api_key"):
        checks.add("rag", "RAG stage set up", False, "run e2e.sh setup with E2E_RAG=1 first")
        return [], {"documents": {}}
    m = manifest()
    wanted = [r for r in m["requests"] if not only or r["id"] in only or r["id"].split("-")[0] in only]
    if only and not wanted:
        return [], None
    docs = document_checks(checks, m)
    results = []
    for spec in wanted:
        try:
            results.append(run_one(rag, spec, m, checks, timeout, prompt_tag, model_log))
        except (HttpError, TimeoutError, RuntimeError) as e:
            checks.add("rag", f"{spec['id']} completed", False, str(e)[:300])
            results.append({"id": spec["id"], "title": spec["title"], "prompt": spec["text"], "error": str(e)})
    u = cp("GET", f"/api/v1/usage?tenant_id={TENANT}&limit=1").body
    purposes = {g.get("purpose"): (g.get("totals") or {}).get("requests") for g in u.get("by_purpose") or []}
    checks.add("rag", "usage totals retrieval by purpose (rag.ingest, rag.search)",
               bool(purposes.get("rag.ingest")) and (bool(purposes.get("rag.search")) or not results),
               json.dumps(purposes))
    a = cp("GET", "/api/v1/audit?limit=500").body
    actions = sorted({e.get("action") for e in a.get("entries") or [] if str(e.get("action", "")).startswith("rag.")})
    checks.add("rag", "retrieval writes are in the audit log",
               "rag.document.ingest" in actions if actions else "INFO",
               ", ".join(actions) or "none among the newest 500 entries")
    return results, {"collection": rag.get("collection"), "embedding_model": rag.get("embedding_model"),
                     "rerank_model": rag.get("rerank_model"), "node_version": rag.get("node_version"),
                     "documents": docs, "by_purpose": purposes}


def report_lines(summary, results):
    if summary is None:
        return []
    lines = ["## Retrieval (E2E_RAG=1)", "",
             f"Collection `{summary.get('collection')}` (embedding `{summary.get('embedding_model')}`, rerank "
             f"`{summary.get('rerank_model') or 'none'}`), node `{NODE}` v{summary.get('node_version')} "
             f"(agent, `builtin://rag_search`).", "",
             "| Document | Language | Trust | Status | Live | Chunks | Findings |", "|---|---|---|---|---|---|---|"]
    for doc_id, d in (summary.get("documents") or {}).items():
        lines.append(f"| `{doc_id}` | {d['language']} | {d['trust']} | {d['status']} | {d['live_version']} | "
                     f"{d['chunks']} | {', '.join(d['findings']) or '-'} |")
    lines.append("")
    for r in results:
        lines += [f"### {r['id']}: {r['title']}", "", f"Prompt: {r['prompt']}", ""]
        if r.get("citations"):
            lines += ["| n | Document | Chunk | Score | Heading path |", "|---|---|---|---|---|"]
            lines += [f"| {c['n']} | `{c['doc_id']}` v{c['doc_version']} | `{c['chunk_id']}` | {c['score']} | "
                      f"{' > '.join(c.get('heading_path') or []).replace('|', '/')} |" for c in r["citations"]]
            lines += ["", f"Strategy: `{json.dumps(r.get('strategy'))}`", ""]
        if r.get("usage"):
            lines += [f"Metering: {json.dumps(r['usage'])}", ""]
        lines += ["Answer:", "", "```json", json.dumps(r.get("parsed") or r.get("answer") or r.get("error"),
                                                      ensure_ascii=False, indent=2)[:3000], "```", ""]
    return lines
