#!/usr/bin/env python3
"""Asserts the developer scenario's outcome through the admin API (stdlib only). Exit 1 on a failure.

What the recording must have left: the tenant's intents set through the API and the support intents
handed to the orchestrator; the classification after the runtime fix at least as good as before and
at the bar; the five nodes live at the versions the app published, the orchestrator's gate met with
its subagents' evidence; the orchestrator run caliban/auto started, with two subagents nested in
it and its budget; usage per node and the node spend against the caps.
"""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from dev import api, load_state, settings  # noqa: E402

NODES = ["billing-agent", "bug-triage", "docs-helper", "support-dispatch", "support-orchestrator"]
SUPPORT = ["billing", "bug_report", "how_to", "multi_issue"]
FAILED = []


def check(ok, what, detail=""):
    print(f"{'PASS' if ok else 'FAIL'}  {what}" + (f"  ({detail})" if detail else ""), flush=True)
    if not ok:
        FAILED.append(what)


def read(s, name):
    try:
        with open(os.path.join(s["work"], name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def main():
    s = settings()
    t = s["tenant"]
    st = load_state(s)
    min_ok = int(os.environ.get("DEV_MIN_ROUTED_OK", "14"))

    # Intents and routing.
    listed = api(s, "GET", f"/api/v1/tenants/{t}/intents")
    mine = {i["intent"]: i for i in listed.get("intents") or [] if i.get("source") == "api"}
    check({"billing", "bug_report", "how_to", "account_security", "sales", "multi_issue"} <= set(mine),
          "intents set through the API", ", ".join(sorted(mine)))
    check(len(mine.get("sales", {}).get("exemplars") or []) >= 15, "sales exemplars added at runtime",
          f"{len(mine.get('sales', {}).get('exemplars') or [])} exemplars")
    for i in SUPPORT:
        node = (mine.get(i) or {}).get("node") or ""
        check(node.startswith("node/support-orchestrator"), f"intent {i} hands to the orchestrator", node or "a model")

    first, taught = read(s, "classify-first.json"), read(s, "classify-taught.json")
    check(bool(taught), "classification ran after the fix")
    if taught:
        check(taught["ok"] >= first.get("ok", 0), "the fix did not make classification worse",
              f"{first.get('ok')} -> {taught['ok']} of {taught['total']}")
        check(taught["ok"] >= min_ok, f"at least {min_ok} of {taught['total']} routed as expected", f"{taught['ok']}")
        sales = [r for r in taught["results"] if r["expected"] == "sales"]
        check(all(r["ok"] for r in sales), "every sales prompt classified as sales after the fix",
              "; ".join(f"#{r['n']} {r['intent']} ({r['stage']})" for r in sales))
        check(all(r.get("intent") for r in taught["results"]), "x-caliban-intent on every answer")

    # Nodes, evals, the gate.
    versions = st.get("versions") or {}
    for n in NODES:
        v = api(s, "GET", f"/api/v1/tenants/{t}/nodes/{n}/versions")
        check(v.get("live") == versions.get(n) and versions.get(n) is not None, f"{n} is live at the version the app published",
              f"live {v.get('live')}, published {versions.get(n)}")
    o = versions.get("support-orchestrator")
    if o:
        g = api(s, "GET", f"/api/v1/tenants/{t}/nodes/support-orchestrator/versions/{o}/gate")
        subs = (g.get("evidence") or {}).get("subnodes") or {}
        check(g.get("allowed") is True, "the orchestrator meets its promotion bar")
        check(all(any(k.startswith(c + "@") for k in subs) for c in NODES[:4]),
              "the gate's evidence covers every subnode it pins", ", ".join(sorted(subs)))
        dh = next((e for k, e in subs.items() if k.startswith("docs-helper@")), None) or {}
        check(bool(dh.get("injection")), "docs-helper (reads documents) passed the injection suite", json.dumps(dh.get("injection")))
    for n, suites in (st.get("evals") or {}).items():
        for suite, e in suites.items():
            check(e.get("status") == "succeeded", f"{n} {suite} eval succeeded", e.get("id", ""))

    # The orchestrator's run.
    sr = st.get("support_run") or {}
    run_id = sr.get("run_id")
    check(bool(run_id), "caliban/auto handed the support message to a node", sr.get("route") or "no run")
    if run_id:
        r = api(s, "GET", f"/api/v1/tenants/{t}/runs/{run_id}")
        steps = r.get("steps") or []
        subs = sorted({x["id"].split(">")[1].split("#")[0] for x in steps if ">" in x["id"]})
        b = r.get("budget") or {}
        check(r.get("node") == "support-orchestrator" and r.get("status") == "succeeded",
              "the orchestrator run succeeded", f"{r.get('node')} {r.get('status')}")
        check(r.get("origin") == "auto", "the run's origin is caliban/auto", r.get("origin"))
        check(len(subs) >= 2, "two or more subagents ran inside the run", ", ".join(subs))
        check((b.get("depth_used") or 0) == 2 and (b.get("fanout_used") or 0) >= 2,
              "nesting and fan-out recorded in the budget", f"depth {b.get('depth_used')}/{b.get('depth_limit')}, "
              f"fan-out {b.get('fanout_used')}/{b.get('fanout_limit')}")
        check(all(x.get("status") == "completed" for x in steps), "every step completed", f"{len(steps)} steps")
        check(all(x.get("duration_ms") is not None for x in steps), "every step has its duration")
        check((r.get("cost_usd") or 0) > 0, "the run's cost is metered", f"billed {r.get('cost_usd') or 0:.5f}, "
              f"model {r.get('model_cost_usd') or 0:.5f}")

    # Usage and budget.
    u = api(s, "GET", f"/api/v1/usage?tenant_id={t}&limit=1")
    by_node = {n.get("node") for n in u.get("by_node") or []}
    check({"support-orchestrator", "docs-helper"} <= by_node, "usage per node", ", ".join(sorted(x or "-" for x in by_node)))
    intents = {r.get("intent") for r in u.get("by_route") or []}
    check(bool(set(SUPPORT) & intents), "routing decisions counted per intent", ", ".join(sorted(x or "-" for x in intents)))
    ns = api(s, "GET", f"/api/v1/tenants/{t}/node-spend")
    check((ns.get("today_usd") or 0) > 0 and (ns.get("caps") or {}).get("daily_usd"), "node spend against the caps",
          f"today {ns.get('today_usd') or 0:.4f} of {(ns.get('caps') or {}).get('daily_usd')}")

    print(f"\n{'FAILED: ' + str(len(FAILED)) if FAILED else 'all checks passed'}")
    sys.exit(1 if FAILED else 0)


if __name__ == "__main__":
    main()
