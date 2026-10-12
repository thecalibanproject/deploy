"""Step 3: a customer message with two problems in it, through caliban/auto, and the run tree behind
the answer.

    python support.py ["message"]

The app sends a plain chat completion (model caliban/auto, the tenant's key, nothing Caliban
specific). Then, with the admin token, it reads the run the orchestrator made
(GET /api/v1/tenants/{t}/runs/{id}) and prints its steps as a tree: the plan, one branch per task,
each specialist's own steps nested under it, with durations, tokens and cost, and the run's budget.
"""
import json
import sys
from datetime import datetime

import support_agents as sa
from common import TENANT, Timed, admin, dur, load_state, ms, raw, save_state, short, write_out

MESSAGE = ("I was double charged on my card this month, and the export button crashes the app every time "
           "I click it. My login is jo.martin@example.com")


def _t(s):
    try:
        return datetime.fromisoformat(s["started_at"].replace("Z", "+00:00")).timestamp() * 1000.0
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


def tree(steps):
    """The run's steps as a tree. Step ids carry the nesting: `fanout#0/1` is branch 1 of the map
    vertex `fanout`, a `>` enters what a subnode vertex ran (`fanout#0/1>bug#0>triage#0`: the
    `triage` model call of bug-triage, run by dispatch's `bug` vertex, in branch 1)."""
    steps = sorted(steps, key=lambda s: (s.get("started_at") or "", s["id"]))
    lines, seen = [], set()
    for s in steps:
        parts = s["id"].split(">")
        for depth in range(1, len(parts)):
            prefix = ">".join(parts[:depth])
            if prefix in seen:
                continue
            seen.add(prefix)
            seg = parts[depth - 1]
            name, _, rest = seg.partition("#")
            sub = [x for x in steps if x["id"].startswith(prefix + ">")]
            t0 = [_t(x) for x in sub if _t(x) is not None]
            t1 = [_t(x) + (x.get("duration_ms") or 0) for x in sub if _t(x) is not None]
            span = f"{ms(max(t1) - min(t0)):>6} ms" if t0 else ""
            tok = sum(x.get("tokens") or 0 for x in sub)
            usd = sum(x.get("usd") or 0.0 for x in sub)
            if "/" in rest:
                label = f"{name} branch {rest.split('/', 1)[1]}"
            else:
                label = f"{name} -> {sa.SUBAGENT.get(name, name)}"
            pad = "   " * (depth - 1)
            lines.append(f"{pad}{label:<{40 - len(pad)}} {span:>9}  {tok:>5} tok  ${usd:.5f}")
        depth = len(parts) - 1
        name = parts[-1].split("#")[0]
        if name == "agent":  # an agent node's loop: model turns and the tool calls they make
            name = "agent turn" if s["kind"] == "llm" else "agent tool call"
        pad = "   " * depth
        lines.append(f"{pad}{name + ' (' + s['kind'] + ', ' + s['status'] + ')':<{40 - len(pad)}} "
                     f"{ms(s.get('duration_ms')):>6} ms  {s.get('tokens') or 0:>5} tok  ${s.get('usd') or 0.0:.5f}")
    return "\n".join(lines)


def main(message):
    c = Timed()
    resp, t = c.chat(model="caliban/auto", messages=[{"role": "user", "content": message}])
    m = resp.caliban
    try:
        out = json.loads(resp.text)
        reply = out.get("reply", resp.text)
    except ValueError:
        out, reply = None, resp.text
    print("customer      ", short(message, 200))
    print("classified    ", f"{m.intent.intent} {m.intent.confidence:.2f} via {m.intent.stage}" if m.intent else "-")
    print("route         ", m.route or m.routed_model, f"(fallback: {m.route_fallback})" if m.route_fallback else "")
    print("run           ", m.run_id, m.run_status or "")
    print("billed / cost ", f"${m.billed_usd}" if m.billed_usd is not None else "-", "/",
          f"${m.cost_usd}" if m.cost_usd is not None else "-", "(caliban/auto flat price / routed models' real cost)")
    print("PII protected ", m.pii_entities)
    print("time          ", f"client {ms(t['client_ms'])} ms", f"node {ms(dur(t['timing'], 'node'))} ms" if t["timing"] else "")
    print()
    print("reply:", reply)
    if out and out.get("handled_by"):
        print("handled by:", ", ".join(out["handled_by"]))
    st = load_state()
    st["support_run"] = {"run_id": m.run_id, "route": m.route, "intent": m.intent.intent if m.intent else None,
                         "client_ms": t["client_ms"], "server_timing": t["timing"]}
    save_state(st)
    if not m.run_id:
        print("\n(no node run: caliban/auto answered with a model)")
        return
    run = raw(admin(), "GET", f"tenants/{TENANT}/runs/{m.run_id}")
    b = run.get("budget") or {}
    print(f"\nrun {run['id']}  {run['node']} v{run['version']}  {run['status']}  origin {run.get('origin')}")
    print(f"budget: steps {b.get('steps_used')}/{b.get('steps_limit')}  tokens {b.get('tokens_used')}/{b.get('tokens_limit')}  "
          f"depth {b.get('depth_used')}/{b.get('depth_limit')}  fan-out {b.get('fanout_used')}/{b.get('fanout_limit')}  "
          f"usd {b.get('usd') or 0:.4f}/{b.get('usd_limit')}  wall {ms(b.get('wall_clock_ms_used'))} ms")
    print(tree(run.get("steps") or []))
    write_out("support-run.json", {k: v for k, v in run.items() if k not in ("input", "output")})


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else MESSAGE)
