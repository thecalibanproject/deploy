#!/usr/bin/env python3
"""Checks the outcome of the law firm recording (stdlib only, Python 3.9+). Exit status 1 when a
check fails; GAP lines are known limits, INFO lines facts that depend on the model.

Reads what the recording left in $E2E_WORK_DIR/law/state.json (the API key, the node versions, the
evals, the requests and their run ids), then asks Caliban itself through the admin API and the run
API: the tenant's privacy settings, the approved ontology and its policy, the documents and the
quarantine, the nodes and their gates, every request's run (route, subnode steps, answer, citations,
answer guard, metering), the model's request log (no client identifier in clear), the queries
MongoDB ran (profiler, when the compose command is available) and the audit chain.
"""
import json
import os
import re
import shlex
import subprocess
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
import dataset  # noqa: E402
import law  # noqa: E402
from law import TENANT, cp, dp  # noqa: E402

RESULTS = []


def check(kind, name, ok, detail=""):
    status = ok if isinstance(ok, str) else ("PASS" if ok else "FAIL")
    RESULTS.append((kind, name, status, str(detail)[:400]))
    print(f"{status:<5} {kind:<9} {name}" + (f"  ({str(detail)[:300]})" if detail else ""), flush=True)


def safe(fn, default=None):
    try:
        return fn()
    except Exception as e:  # noqa: BLE001
        print(f"      ({type(e).__name__}: {str(e)[:300]})")
        return default


def setup_checks(st):
    t = cp("GET", f"/api/v1/tenants/{TENANT}")
    check("privacy", "tenant PII mode is reversible, on sovereign models too",
          t.get("pii_default") == "reversible" and t.get("pii_on_sovereign") is True,
          f"pii_default {t.get('pii_default')}, pii_on_sovereign {t.get('pii_on_sovereign')}")
    pii = law.load("config/pii.json")
    check("privacy", "the national ID pattern is set",
          any(p.get("pattern") == pii["pii_patterns"][0]["pattern"] for p in t.get("pii_patterns") or []),
          json.dumps(t.get("pii_patterns")))
    n = t.get("pii_dictionary_count", len(t.get("pii_dictionary") or []))
    check("privacy", "the PII dictionary holds every person name term", n == len(pii["pii_dictionary"]),
          f"{n} of {len(pii['pii_dictionary'])} terms")

    onto = cp("GET", f"/api/v1/tenants/{TENANT}/ontology")
    approved = {e["id"]: e for e in onto["elements"] if e["status"] == "approved"}
    csm = law.load("ontology/law.csm.json")
    missing = [e["id"] for e in csm["elements"] if e["id"] not in approved]
    check("ontology", "every curated element is approved", not missing, ", ".join(missing) or f"version {onto['version']}")
    pol = approved.get("policy.client_identifiers", {}).get("spec") or {}
    check("ontology", "the policy denies the national identifiers",
          set(pol.get("denied_attributes") or []) >= {"Client.national_id", "Client.nir"}, json.dumps(pol))
    rb = st.get("review_batch")
    if rb:
        check("ontology", "the review batch made one version", rb["to"] == rb["from"] + 1, json.dumps(rb))
    intro = st.get("introspection") or {}
    check("ontology", "introspection of the practice database succeeded",
          True if intro.get("status") == "succeeded" else "GAP", json.dumps(intro))

    base = f"/api/v1/tenants/{TENANT}/rag/collections/{law.COLLECTION}"
    m = law.load("rag/documents.json")
    for d in m["documents"]:
        v = safe(lambda d=d: cp("GET", f"{base}/documents/{d['doc_id']}"), {})
        newest = (v.get("versions") or [{}])[0]
        kinds = sorted({x.get("kind") for x in newest.get("findings") or []})
        if d.get("poisoned"):
            check("rag", f"{d['doc_id']} is quarantined and not served",
                  newest.get("status") == "quarantined" and v.get("live_version") is None,
                  f"status {newest.get('status')}, live {v.get('live_version')}")
            check("rag", f"{d['doc_id']} quarantine names the injection", bool({"instruction", "exfiltration"} & set(kinds)),
                  ",".join(kinds) or "-")
        else:
            check("rag", f"{d['doc_id']} is ready and served",
                  newest.get("status") == "ready" and v.get("live_version") == newest.get("version"),
                  f"status {newest.get('status')}, live {v.get('live_version')}, {newest.get('chunks')} chunks")

    for name in law.NODES:
        info = safe(lambda name=name: cp("GET", f"/api/v1/tenants/{TENANT}/nodes/{name}/versions"), {})
        want = ((st.get("nodes") or {}).get(name) or {}).get("version")
        check("nodes", f"{name} v{want} is live", want is not None and info.get("live") == want,
              f"live {info.get('live')}")
        g = (st.get("gates") or {}).get(name) or {}
        check("nodes", f"{name} passed its promotion gate", g.get("allowed") is True,
              json.dumps(g.get("problems") or g.get("evidence") or {})[:300])
    for key, e in (st.get("evals") or {}).items():
        s = e.get("summary") or {}
        if key.endswith("injection"):
            check("evals", f"{key}: every attack blocked", e.get("status") == "succeeded" and s.get("pass_rate") == 1,
                  f"{e.get('status')}, pass_rate {s.get('pass_rate')}, trials {s.get('trials')}, attacked {s.get('attacked')}")
        else:
            check("evals", f"{key}: pass^k", e.get("status") == "succeeded",
                  f"pass_hat_k {s.get('pass_hat_k')}, cost/success {s.get('cost_per_success_usd')}, "
                  f"p95 {(s.get('latency_ms') or {}).get('p95')}")
    li = (st.get("intents") or {}).get(law.INTENT) or {}
    check("route", "the legal intent comes from the admin API and hands to legal-assistant",
          li.get("source") == "api" and li.get("node") == "node/legal-assistant@published", json.dumps(li))


def model_log_text(since):
    out = []
    for p in law.model_logs():
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                if (e.get("ts") or 0) < since:
                    continue
                # Only this scenario's calls (the shared recorder logs every scenario's).
                blob = json.dumps(e.get("messages"), ensure_ascii=False)
                if "[law:" not in blob and "HAD-20" not in blob:
                    continue
                out.append(e)
    return out


def request_checks(st):
    key = st.get("api_key")
    reqs = {r["id"]: r for r in law.load("config/requests.json")["requests"]}
    recs = st.get("requests") or []
    check("flow", "the recording sent every request", len(recs) == len(reqs), f"{len(recs)} of {len(reqs)}")
    for rec in recs:
        spec = reqs.get(rec["id"]) or {}
        exp = spec.get("expect") or {}
        rid = rec.get("run_id")
        h = rec.get("headers") or {}
        check("flow", f"{rec['id']} answered by legal-assistant ({spec.get('model', 'caliban/auto')})",
              bool(rid) and (h.get("x-caliban-routed-model") or "").startswith("node/legal-assistant"),
              f"intent {h.get('x-caliban-intent')}, routed {h.get('x-caliban-routed-model')}, fallback {h.get('x-caliban-route-fallback')}")
        if not rid:
            continue
        run = safe(lambda: cp("GET", f"/api/v1/tenants/{TENANT}/runs/{rid}"), {}) or {}
        if key:
            pub = safe(lambda: dp("GET", f"/v1/runs/{rid}", key)[2], {}) or {}
            run.setdefault("input_labels", pub.get("input_labels"))
        check("flow", f"{rec['id']} run succeeded", run.get("status") == "succeeded",
              f"status {run.get('status')}, error {run.get('error')}")
        steps = run.get("steps") or []
        intake = next((s for s in steps if s.get("vertex") == "intake"), {})
        out = run.get("output")
        out = out if isinstance(out, dict) else (law.parse_json(out) or {})
        sub = {"matter_desk": "desk", "conflict_check": "conflicts", "research": "research"}.get(exp.get("route"))
        ran_sub = any((s.get("id") or "").startswith(f"{sub}") and ">" in (s.get("id") or "") for s in steps) if sub else False
        check("quality", f"{rec['id']} intake routed to {exp.get('route')}", ran_sub,
              " > ".join(s.get("id") or "" for s in steps)[:300])
        check("flow", f"{rec['id']} subnode steps are in the parent run", any(">" in (s.get("id") or "") for s in steps),
              f"{sum(1 for s in steps if '>' in (s.get('id') or ''))} subnode steps")
        check("quality", f"{rec['id']} answer kind {exp.get('kind')}", out.get("kind") in (exp.get("kind") or []),
              out.get("kind"))
        if exp.get("matters"):
            got = set(out.get("matters") or []) | set(re.findall(r"HAD-\d{4}-\d{3}", out.get("answer") or ""))
            check("quality", f"{rec['id']} answer names {', '.join(exp['matters'])}", set(exp["matters"]) <= got,
                  ", ".join(sorted(got)) or "-")
        if exp.get("conflict"):
            check("quality", f"{rec['id']} conflict verdict", out.get("conflict") in exp["conflict"], out.get("conflict"))
        if exp.get("cite"):
            cited = [c.get("doc_id") for c in out.get("citations") or []]
            results = []
            for s in steps:
                if s.get("vertex") == "search":
                    o = s.get("output")
                    o = o if isinstance(o, dict) else (law.parse_json(o) or {})
                    results += o.get("results") or []
            served = {(r.get("n"), r.get("doc_id")) for r in results}
            check("rag", f"{rec['id']} the search step carries the retrieved label",
                  any("retrieved" in (s.get("labels") or []) for s in steps if s.get("vertex") == "search"),
                  ";".join(",".join(s.get("labels") or []) for s in steps if s.get("vertex") == "search"))
            check("rag", f"{rec['id']} no passage of the quarantined upload is served",
                  not any((r.get("doc_id") or "").startswith("upload-") for r in results),
                  ", ".join(sorted({r.get('doc_id') for r in results})))
            check("rag", f"{rec['id']} every citation is a passage the search returned",
                  all((c.get("n"), c.get("doc_id")) in served for c in out.get("citations") or []),
                  json.dumps(out.get("citations")))
            check("quality", f"{rec['id']} cites one of {', '.join(exp['cite'])}", bool(set(cited) & set(exp["cite"])),
                  ", ".join(cited) or "-")
        ac = run.get("answer_check") or {}
        check("flow", f"{rec['id']} the answer guard checked the answer", bool(ac.get("labels")),
              f"labels {ac.get('labels')}, action {ac.get('action')}, {len(ac.get('findings') or [])} findings")
        if spec.get("untrusted"):
            check("flow", f"{rec['id']} the forwarded e-mail is labelled caller", "caller" in (run.get("input_labels") or []),
                  json.dumps(run.get("input_labels")))
            text = json.dumps(out, ensure_ascii=False)
            leaked = [m for m in spec.get("attack") or [] if m in text]
            check("flow", f"{rec['id']} nothing of the injected instruction reaches the lawyer", not leaked,
                  ", ".join(leaked) or f"action {ac.get('action')}")
            check("quality", f"{rec['id']} answer guard action", "INFO",
                  f"{ac.get('action')}: {json.dumps(ac.get('findings') or [])[:300]}")
        u = run.get("usage") or {}
        check("privacy", f"{rec['id']} personal data protected in the run's model calls", "INFO" if not u.get("pii_entities") else True,
              f"{u.get('pii_entities')} entities")
        check("flow", f"{rec['id']} billed and metered", run.get("cost_usd") is not None,
              f"billed {run.get('cost_usd')}, model cost {run.get('model_cost_usd')}")


def privacy_log_checks(st):
    since = float(st.get("started_at") or 0)
    entries = model_log_text(since)
    if not entries:
        check("privacy", "the model's request log is readable", "GAP",
              "no model log in $E2E_WORK_DIR/model-log (the recorder is off)")
        return
    text = "\n".join(json.dumps(e.get("messages"), ensure_ascii=False) for e in entries)
    secrets = []
    for c in dataset.CLIENTS:
        for k in ("email", "phone", "national_id", "nir"):
            if c.get(k):
                secrets.append(c[k])
    secrets += ["Khoury", "Nassar", "Martin-Aziz", "Marchetti", "Elias", "Karim"]
    clear = [s for s in secrets if s in text]
    check("privacy", f"no client identifier or dictionary name in clear in {len(entries)} model requests", not clear,
          ", ".join(clear))
    forms = {e.get("response_format") for e in entries if any("[law:" in (m.get("content") or "")
                                                             for m in e.get("messages") or [] if isinstance(m.get("content"), str))}
    check("flow", "the law nodes' model calls ask for constrained decoding (json_schema)", forms == {"json_schema"},
          ", ".join(sorted(str(x) for x in forms)))
    if law.FAMILY.startswith("qwen3"):
        think = {json.dumps(e.get("chat_template_kwargs")) for e in entries}
        check("flow", "thinking is off on Qwen3 (enable_thinking false)", all('"enable_thinking": false' in t for t in think),
              ", ".join(sorted(think))[:200])


def profiler_checks(st):
    cmd = os.environ.get("E2E_COMPOSE_CMD")
    user, pw = os.environ.get("E2E_MONGO_ROOT_USER"), os.environ.get("E2E_MONGO_ROOT_PASSWORD")
    if not (cmd and user and pw):
        check("ground", "the queries MongoDB ran (profiler)", "GAP", "no E2E_COMPOSE_CMD or root credentials here")
        return
    js = ("print(JSON.stringify(db.system.profile.find({'command.comment': /^caliban:node:/}, "
          "{'command.comment': 1, ns: 1, millis: 1}).toArray()))")
    counts = {}
    for member in ("caliban-e2e-mongo-analytics", "caliban-e2e-mongo"):
        uri = f"mongodb://{user}:{pw}@localhost:27017/{law.DATABASE}?authSource=admin&directConnection=true"
        r = subprocess.run(shlex.split(cmd) + ["exec", "-T", member, "mongosh", "--quiet", uri, "--eval", js],
                           capture_output=True, text=True)
        try:
            counts[member] = len(json.loads(r.stdout.strip().splitlines()[-1]))
        except (ValueError, IndexError):
            counts[member] = None
    check("ground", "node queries ran on the analytics member, none on the primary",
          (counts.get("caliban-e2e-mongo-analytics") or 0) > 0 and counts.get("caliban-e2e-mongo") == 0, json.dumps(counts))


def audit_checks():
    a = cp("GET", "/api/v1/audit?limit=500")
    entries = [e for e in a.get("entries") or [] if e.get("tenant_id") == TENANT or (e.get("target") or "").startswith(TENANT)]
    actions = {e.get("action") for e in entries}
    check("log", "the audit chain verifies", a.get("chain_verified") is True, f"{len(entries)} entries for {TENANT}")
    for want in ("rag.document.ingest", "node.publish", "node.promote"):
        check("log", f"audit has {want}", want in actions, ", ".join(sorted(actions))[:300])
    usage = safe(lambda: cp("GET", f"/api/v1/usage?tenant_id={TENANT}&node=legal-assistant&limit=200"), {}) or {}
    tot = usage.get("totals") or {}
    check("log", "usage events of legal-assistant are metered", bool(usage.get("events")),
          f"{len(usage.get('events') or [])} events, charged {tot.get('charged_usd')}, by_purpose {json.dumps(usage.get('by_purpose'))[:200]}")


def main():
    if not os.environ.get("CALIBAN_ADMIN_TOKEN"):
        sys.exit("check.py: CALIBAN_ADMIN_TOKEN is not set (run through the e2e environment)")
    st = law.load_state()
    if not st:
        sys.exit(f"check.py: no state at {law.state_path()} (record the law scenario first)")
    print(f"== law firm checks: tenant {TENANT}, state {law.state_path()}", flush=True)
    for fn in (lambda: setup_checks(st), lambda: request_checks(st), lambda: privacy_log_checks(st),
               lambda: profiler_checks(st), audit_checks):
        try:
            fn()
        except Exception as e:  # noqa: BLE001 (a broken section is a failure, the others still run)
            check("flow", f"check section raised {type(e).__name__}", False, str(e)[:300])
    count = {s: sum(1 for r in RESULTS if r[2] == s) for s in ("PASS", "FAIL", "GAP", "INFO")}
    print(f"\n{count['PASS']} passed, {count['FAIL']} failed, {count['GAP']} gaps, {count['INFO']} info", flush=True)
    os.makedirs(os.path.dirname(law.state_path()), exist_ok=True)
    with open(os.path.join(os.path.dirname(law.state_path()), "check.json"), "w", encoding="utf-8") as f:
        json.dump({"results": RESULTS, "count": count}, f, indent=2, ensure_ascii=False)
    strict = os.environ.get("E2E_STRICT_QUALITY") == "1"
    failed = [r for r in RESULTS if r[2] == "FAIL" and (strict or r[0] != "quality")]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
