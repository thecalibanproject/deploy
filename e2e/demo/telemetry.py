#!/usr/bin/env python3
"""Request timing telemetry of the recorded demos (stdlib only, Python 3.9+).

framework.Ctx.gateway appends one record per data-plane call to
$DEMO_OUT/<scenario>-telemetry.jsonl: the client-side times (to the response headers, to the first
body byte, total) and the gateway's W3C `Server-Timing` header when it sends one (core's stage
metrics: auth, route with desc knn|rules|embedding|pinned, embed, pii, cache with desc hit|miss,
upstream (time to first byte), upstream_total, node, total). Node runs add a `run` record with
each step's duration; evals add an `eval` record with their latency percentiles.

    telemetry.py report [SCENARIO...]   writes $DEMO_OUT/telemetry.md, telemetry.csv, telemetry.json

Absent headers are not an error: the rows then carry the client-side times only.
"""
import csv
import glob
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("E2E_WORK_DIR") or os.path.join(os.path.dirname(HERE), "health-triage", ".work")
OUT = os.environ.get("DEMO_OUT") or os.path.join(WORK, "demo")

# Server-Timing metric names, in request-path order, with the website's stage they belong to.
STAGES = [("auth", "Clear"), ("route", "Route"), ("embed", "Route"), ("pii", "Screen"), ("cache", "Cache"),
          ("upstream", "Land"), ("upstream_total", "Land"), ("node", "Nodes"), ("total", "")]


def parse_server_timing(value):
    """`name;dur=1.2;desc="x", other;dur=3` -> {"name": {"dur": 1.2, "desc": "x"}, ...}."""
    out = {}
    if not value:
        return out
    for part in value.split(","):
        bits = [b.strip() for b in part.split(";") if b.strip()]
        if not bits:
            continue
        name, m = bits[0], {}
        for b in bits[1:]:
            k, _, v = b.partition("=")
            v = v.strip().strip('"')
            if k.strip().lower() == "dur":
                try:
                    m["dur"] = float(v)
                except ValueError:
                    pass
            elif k.strip().lower() == "desc":
                m["desc"] = v
        out[name] = m
    return out


def append(scenario, record):
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, f"{scenario}-telemetry.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def rotate(scenario):
    """Starts a scenario's telemetry fresh: an earlier recording's records move to
    $DEMO_OUT/history/<scenario>-telemetry-<UTC time>.jsonl, out of the report."""
    path = os.path.join(OUT, f"{scenario}-telemetry.jsonl")
    if not os.path.exists(path):
        return None
    os.makedirs(os.path.join(OUT, "history"), exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(os.path.getmtime(path)))
    dest = os.path.join(OUT, "history", f"{scenario}-telemetry-{stamp}.jsonl")
    n = 1
    while os.path.exists(dest):
        n += 1
        dest = os.path.join(OUT, "history", f"{scenario}-telemetry-{stamp}-{n}.jsonl")
    os.replace(path, dest)
    return dest


def load(scenario):
    path = os.path.join(OUT, f"{scenario}-telemetry.jsonl")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def _dur(st, name):
    return (st.get(name) or {}).get("dur")


def rows(records):
    """One row per data-plane request, its node run's steps merged in."""
    runs = {}
    for r in records:
        if r.get("kind") == "run" and r.get("run_id"):
            runs[r["run_id"]] = r
    out = []
    for r in records:
        if r.get("kind") != "request":
            continue
        st = r.get("server") or {}
        cl = r.get("client") or {}
        run = runs.get(r.get("run_id")) or {}
        steps = run.get("steps") or r.get("steps") or []
        route = st.get("route") or {}
        # `route` is the whole classification; `embed` (the routing embedder call) is inside it.
        classify = _dur(st, "route")
        out.append({
            "scenario": r.get("scenario"), "label": r.get("label"), "path": r.get("path"), "status": r.get("status"),
            "intent": (r.get("intent") or "").split(";")[0] or None, "route": r.get("route"),
            "route_stage": route.get("desc"), "classify_ms": classify, "embed_ms": _dur(st, "embed"),
            "pii_ms": _dur(st, "pii"),
            "cache": (st.get("cache") or {}).get("desc") or r.get("cache"), "cache_ms": _dur(st, "cache"),
            "model_ttfb_ms": _dur(st, "upstream"), "model_total_ms": _dur(st, "upstream_total"),
            "node_ms": _dur(st, "node") if _dur(st, "node") is not None else run.get("duration_ms"),
            "server_total_ms": _dur(st, "total"), "client_headers_ms": cl.get("headers_ms"),
            "client_ttfb_ms": cl.get("ttfb_ms"), "end_to_end_ms": run.get("end_to_end_ms") or cl.get("total_ms"),
            "steps": [{"vertex": s.get("vertex") or s.get("id"), "kind": s.get("kind"),
                       "duration_ms": s.get("duration_ms")} for s in steps],
            "server_timing": bool(st),
        })
    return out


def classify_text(r):
    """Classification ms, with the embedding share when the router embedded the request."""
    if r.get("classify_ms") is None:
        return "-"
    return ms(r["classify_ms"]) + (f" (embed {ms(r['embed_ms'])})" if r.get("embed_ms") is not None else "")


def evals(records):
    return [r for r in records if r.get("kind") == "eval"]


def pct(values, p):
    v = sorted(x for x in values if isinstance(x, (int, float)))
    if not v:
        return None
    k = max(0, min(len(v) - 1, round(p / 100 * (len(v) - 1))))
    return round(v[k], 1)


METRICS = [("classify_ms", "classification"), ("embed_ms", "of which embedding"), ("pii_ms", "PII"), ("cache_ms", "cache"),
           ("model_ttfb_ms", "model TTFB"), ("model_total_ms", "model total"), ("node_ms", "node run"),
           ("server_total_ms", "gateway total"), ("end_to_end_ms", "end to end (client)")]


def percentiles(rs):
    out = []
    for key, label in METRICS:
        vals = [r.get(key) for r in rs if r.get(key) is not None]
        if vals:
            out.append({"metric": label, "n": len(vals), "p50": pct(vals, 50), "p95": pct(vals, 95)})
    steps = {}
    for r in rs:
        for s in r["steps"]:
            if s.get("duration_ms") is not None:
                steps.setdefault(f"step {s['vertex']}", []).append(s["duration_ms"])
    for name, vals in sorted(steps.items()):
        out.append({"metric": name, "n": len(vals), "p50": pct(vals, 50), "p95": pct(vals, 95)})
    return out


def ms(v):
    return "-" if v is None else (f"{v:,.0f}" if v >= 100 else f"{v:.1f}")


def markdown(scenarios):
    lines = ["# Caliban demo: request timings", ""]
    allrows = []
    for sc in scenarios:
        recs = load(sc)
        rs = rows(recs)
        allrows += rs
        if not rs and not evals(recs):
            continue
        lines += [f"## {sc}", "", "| Request | Intent | Route stage | Classify ms | PII ms | Cache | Model TTFB ms | "
                  "Model total ms | Node steps (ms) | End to end ms |", "|---|---|---|---|---|---|---|---|---|---|"]
        for r in rs:
            steps = ", ".join(f"{s['vertex']} {ms(s['duration_ms'])}" for s in r["steps"]) or "-"
            lines.append(f"| {r['label']} | {r['intent'] or '-'} | {r['route_stage'] or '-'} | {classify_text(r)} | "
                         f"{ms(r['pii_ms'])} | {r['cache'] or '-'} | {ms(r['model_ttfb_ms'])} | "
                         f"{ms(r['model_total_ms'])} | {steps} | {ms(r['end_to_end_ms'])} |")
        lines += ["", "| Stage | n | p50 ms | p95 ms |", "|---|---|---|---|"]
        lines += [f"| {p['metric']} | {p['n']} | {ms(p['p50'])} | {ms(p['p95'])} |" for p in percentiles(rs)]
        ev = evals(recs)
        if ev:
            lines += ["", "| Eval | Suite | Trials | p50 ms | p95 ms |", "|---|---|---|---|---|"]
            lines += [f"| {e.get('label')} | {e.get('suite')} | {e.get('trials')} | {ms(e.get('p50'))} | "
                      f"{ms(e.get('p95'))} |" for e in ev]
        lines.append("")
    if not any(r["server_timing"] for r in allrows):
        lines += ["No response carried a `Server-Timing` header: the stage columns are empty and the times are the "
                  "client's.", ""]
    return "\n".join(lines), allrows


def report(scenarios):
    text, allrows = markdown(scenarios)
    with open(os.path.join(OUT, "telemetry.md"), "w", encoding="utf-8") as f:
        f.write(text)
    cols = ["scenario", "label", "path", "status", "intent", "route", "route_stage", "classify_ms", "embed_ms", "pii_ms",
            "cache",
            "cache_ms", "model_ttfb_ms", "model_total_ms", "node_ms", "server_total_ms", "client_headers_ms",
            "client_ttfb_ms", "end_to_end_ms", "steps"]
    with open(os.path.join(OUT, "telemetry.csv"), "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for r in allrows:
            w.writerow([json.dumps(r[c]) if c == "steps" else r[c] for c in cols])
    agg = {sc: {"rows": rows(load(sc)), "percentiles": percentiles(rows(load(sc))), "evals": evals(load(sc))}
           for sc in scenarios}
    with open(os.path.join(OUT, "telemetry.json"), "w", encoding="utf-8") as f:
        json.dump(agg, f, indent=2, ensure_ascii=False)
    print(f"telemetry.py: {os.path.join(OUT, 'telemetry.md')}, .csv, .json ({len(allrows)} requests)")


def scenarios_found():
    return sorted(os.path.basename(p)[:-len("-telemetry.jsonl")] for p in glob.glob(os.path.join(OUT, "*-telemetry.jsonl")))


def main():
    if len(sys.argv) < 2 or sys.argv[1] != "report":
        sys.exit("usage: telemetry.py report [SCENARIO...]")
    report(sys.argv[2:] or scenarios_found())


if __name__ == "__main__":
    main()
