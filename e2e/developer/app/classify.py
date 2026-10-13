"""Step 2: send the support app's prompts to caliban/auto and see where each one goes.

    python classify.py [label]

Prints, per prompt: the intent Caliban classified (x-caliban-intent: name, confidence, stage), where
it went (a node, or the routed model), the client's wall time and the gateway's own per-stage times
(Server-Timing: route, embed, upstream, total), then the misroutes and p50/p95 of the times.
Writes the results to $DEV_OUT/classify-<label>.json.
"""
import collections
import os
import sys

from common import Timed, data, dur, ms, pct, short, table, write_out

OURS = {"billing", "bug_report", "how_to", "account_security", "sales", "multi_issue"}


def main(label):
    c = Timed()
    results = []
    for i, p in enumerate(data("prompts.json")["prompts"], 1):
        resp, t = c.chat(model="caliban/auto", messages=[{"role": "user", "content": p["text"]}],
                         max_tokens=160, caliban={"cache": "off", "reasoning": "off"})
        m = resp.caliban
        intent = m.intent.intent if m.intent else None
        stage = m.intent.stage if m.intent else None
        got = intent if intent in OURS and stage == "knn" else "other"
        timing = t["timing"]
        results.append({
            "n": i, "lang": p["lang"], "text": p["text"], "expected": p["expected"], "intent": intent,
            "confidence": m.intent.confidence if m.intent else None, "stage": stage,
            "knn_reason": m.intent.knn_reason if m.intent else None, "predicted": got,
            "ok": got == p["expected"], "target": m.route or m.routed_model, "fallback": m.route_fallback,
            "run_id": m.run_id, "client_ms": t["client_ms"],
            "route_ms": dur(timing, "route"), "route_desc": timing.get("route", {}).get("desc"),
            "embed_ms": dur(timing, "embed"), "upstream_ms": dur(timing, "upstream_total") or dur(timing, "node"),
            "server_ms": dur(timing, "total"), "server_timing": timing,
        })
    rows = []
    for r in results:
        conf = f"{r['confidence']:.2f}" if r["confidence"] is not None else "-"
        stage = {"keyword": "keyword", "knn": "knn", "rules": "rules"}.get(r["stage"], r["stage"] or "-")
        rows.append((r["n"], r["lang"], short(r["text"], 22), r["expected"], r["intent"] or "-", conf, stage,
                     "ok" if r["ok"] else "MISS", ms(r["client_ms"]), ms(r["route_ms"])))
    print(table(rows, ["#", "", "prompt", "expected", "classified", "conf", "stage", "", "client", "route"]))
    targets = collections.Counter(r["target"] or "-" for r in results)
    print("answered by: " + ", ".join(f"{t} ({n})" for t, n in targets.most_common())
          + "    times in ms: client wall time, Server-Timing route (classification)")
    ok = sum(r["ok"] for r in results)
    common_label, common_n = collections.Counter(r["expected"] for r in results).most_common(1)[0]
    print(f"\n{ok}/{len(results)} routed as expected"
          f"  (one-intent baseline, always '{common_label}': {common_n}/{len(results)})")
    for r in results:
        if not r["ok"]:
            why = f", kNN {r['knn_reason']}" if r["knn_reason"] else ""
            print(f"  misroute #{r['n']} ({r['lang']}): expected {r['expected']}, got {r['intent']} "
                  f"via {r['stage']}{why}")
    timed = [r for r in results if r["route_ms"] is not None]
    print(f"\nclient wall time   p50 {ms(pct([r['client_ms'] for r in results], 50))} ms"
          f"   p95 {ms(pct([r['client_ms'] for r in results], 95))} ms  (classification + answer)")
    if timed:
        print(f"classification     p50 {ms(pct([r['route_ms'] for r in timed], 50))} ms"
              f"   p95 {ms(pct([r['route_ms'] for r in timed], 95))} ms  (Server-Timing route, embed included)")
        print(f"query embedding    p50 {ms(pct([r['embed_ms'] for r in timed], 50))} ms"
              f"   p95 {ms(pct([r['embed_ms'] for r in timed], 95))} ms")
    else:
        print("classification     (this gateway sends no Server-Timing header: client time only)")
    path = write_out(f"classify-{label}.json", {"label": label, "results": results, "ok": ok, "total": len(results)})
    print(f"\nresults: {os.path.basename(path)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "run")
