#!/usr/bin/env python3
"""The demo video's cards and its results segment (Python Playwright, in the pinned image).

    cards.py cards       title, section and end cards, and one card per recorded scenario (from
                         its timeline's title), PNG at 1920x1080, into $DEMO_OUT/cards
    cards.py results     the health results segment: a page built from the latest e2e.sh run (its
                         results-*.json), scrolled and recorded, $DEMO_OUT/results.webm
    cards.py telemetry   the telemetry segment from $DEMO_OUT/telemetry.json (telemetry.py report),
                         $DEMO_OUT/telemetry.webm

Nothing here talks to Caliban: the segments show exactly what the files say.
"""
import glob
import html
import json
import os
import sys

from playwright.sync_api import sync_playwright

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("E2E_WORK_DIR") or os.path.join(os.path.dirname(HERE), ".work")
OUT = os.environ.get("DEMO_OUT") or os.path.join(WORK, "demo")
TENANT = os.environ.get("E2E_TENANT", "medmarket")

STYLE = """
* { box-sizing: border-box; }
html, body { margin: 0; background: #0b1020; color: #f1f5f9;
  font-family: ui-sans-serif, system-ui, "Segoe UI", Roboto, "Liberation Sans", "Noto Sans", sans-serif; }
.card { width: 1920px; height: 1080px; display: flex; flex-direction: column; align-items: center;
  justify-content: center; text-align: center; padding: 80px; }
.eyebrow { font: 600 22px ui-monospace, "Liberation Mono", monospace; letter-spacing: .14em; text-transform: uppercase;
  color: #7dd3fc; }
.big { margin-top: 18px; font-size: 76px; font-weight: 750; letter-spacing: -0.02em; }
.sub { margin-top: 22px; font-size: 30px; line-height: 1.45; color: #cbd5e1; max-width: 1450px; }
.stages { margin-top: 44px; display: flex; gap: 16px; }
.stages span { font: 600 22px ui-monospace, "Liberation Mono", monospace; padding: 10px 16px; border-radius: 10px;
  background: #13213b; color: #7dd3fc; border: 1px solid #1e3a5f; }
.foot { margin-top: 48px; font-size: 22px; color: #94a3b8; }
.page { padding: 56px 72px 200px; }
h1 { font-size: 46px; margin: 0 0 8px; }
h2 { font-size: 28px; margin: 44px 0 14px; color: #7dd3fc; }
.meta { color: #94a3b8; font-size: 20px; }
.tiles { display: flex; gap: 18px; margin-top: 28px; }
.tile { flex: 1; background: #111a2e; border: 1px solid #22304a; border-radius: 14px; padding: 18px 22px; }
.tile .n { font-size: 52px; font-weight: 750; }
.tile .l { font-size: 18px; color: #94a3b8; }
.ok { color: #86efac; } .bad { color: #fca5a5; } .gap { color: #fde68a; }
table { width: 100%; border-collapse: collapse; font-size: 18px; }
th { text-align: left; color: #94a3b8; font-weight: 600; padding: 8px 10px; border-bottom: 1px solid #22304a; }
td { padding: 8px 10px; border-bottom: 1px solid #172036; vertical-align: top; }
code { font-family: ui-monospace, "Liberation Mono", monospace; font-size: 16px; color: #e2e8f0; }
"""


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


def page_html(body):
    return f"<!doctype html><html><head><meta charset=utf-8><style>{STYLE}</style></head><body>{body}</body></html>"


CARDS = {
    "title": ('<div class="card"><div class="eyebrow">Product walkthrough</div><div class="big">Caliban</div>'
              '<div class="sub">A sovereign AI gateway on the customer\'s own hardware: one OpenAI-compatible '
              'endpoint that classifies each request, screens personal data, grounds answers on the customer\'s data, '
              'runs versioned nodes, checks the answer and meters every call. Each scenario that follows is set up in '
              'the web console and checked end to end.</div>'
              '<div class="stages"><span>Clear</span><span>Route</span><span>Screen</span><span>Cache</span>'
              '<span>Ground</span><span>Land</span><span>Log</span></div>'
              '<div class="foot">{foot}</div></div>'),
    "infra": ('<div class="card"><div class="eyebrow">Part 1</div><div class="big">The gateway host</div>'
              '<div class="sub">Caliban\'s compose stack, a MongoDB replica set with the synthetic catalogue, and '
              'the model.</div></div>'),
    "end": ('<div class="card"><div class="big">Caliban</div><div class="sub">Clear, Route, Screen, Cache, Ground, '
            'Land, Log: on hardware the customer controls.</div><div class="foot">All data in this demo is synthetic; '
            'the identifiers are invented.</div></div>'),
}


def render_cards(pw):
    os.makedirs(os.path.join(OUT, "cards"), exist_ok=True)
    foot = (f"Tenant {TENANT} · model {os.environ.get('E2E_UPSTREAM_MODEL', 'e2e-scripted')} · "
            f"target {os.environ.get('E2E_TARGET', 'local')}")
    browser = pw.chromium.launch()
    page = browser.new_page(viewport={"width": 1920, "height": 1080})
    cards = dict(CARDS)
    for i, tl in enumerate(sorted(glob.glob(os.path.join(OUT, "*.timeline.json")))):
        name = os.path.basename(tl)[:-len(".timeline.json")]
        with open(tl, encoding="utf-8") as f:
            t = json.load(f)
        verdict = ""
        if t.get("failed"):
            verdict = f'<div class="foot">The recording stopped early: {esc(t["failed"][:200])}</div>'
        cards[f"scenario-{name}"] = (
            f'<div class="card"><div class="eyebrow">Scenario {i + 1}</div><div class="big">{esc(t.get("title") or name)}'
            '</div><div class="sub">An operator sets it up in the web console, a client sends its requests, the '
            f'console shows what happened, then a script checks the outcome.</div>{verdict}</div>')
    for name, body in cards.items():
        page.set_content(page_html(body.replace("{foot}", esc(foot))))
        page.screenshot(path=os.path.join(OUT, "cards", f"{name}.png"))
    browser.close()
    print(f"cards.py: {len(cards)} cards in {os.path.join(OUT, 'cards')}")


def latest(pattern):
    files = sorted(glob.glob(os.path.join(WORK, pattern)))
    return files[-1] if files else None


def results_body():
    path = latest("results-*.json")
    if not path:
        sys.exit(f"cards.py: no results-*.json in {WORK} (run e2e.sh run first)")
    with open(path, encoding="utf-8") as f:
        res = json.load(f)
    checks = res.get("checks") or []
    count = {s: sum(1 for c in checks if c[2] == s) for s in ("PASS", "FAIL", "GAP", "INFO")}
    by_kind = {}
    for kind, _, status, _ in checks:
        by_kind.setdefault(kind, {"PASS": 0, "FAIL": 0, "GAP": 0, "INFO": 0})[status] = \
            by_kind.setdefault(kind, {}).get(status, 0) + 1
    state = {}
    if os.path.exists(os.path.join(WORK, "state.json")):
        with open(os.path.join(WORK, "state.json"), encoding="utf-8") as f:
            state = json.load(f)
    rows = []
    for r in res.get("results") or []:
        h = r.get("headers") or {}
        run = r.get("run") or {}
        pii = r.get("pii") or []
        masked = sum(1 for p in pii if p.get("result") == "masked")
        steps = " > ".join(s.get("vertex") or "" for s in r.get("steps") or []) or "model answer"
        billed = run.get("cost_usd") if run else r.get("cost_usd")
        rows.append(
            f"<tr><td><b>{esc(r['id'])}</b></td><td><code>{esc((h.get('x-caliban-intent') or '-').split(';')[0])}</code></td>"
            f"<td><code>{esc(h.get('x-caliban-routed-model') or h.get('x-caliban-route') or '-')}</code></td>"
            f"<td>{esc(steps)}</td><td>{len(r.get('queries') or [])}</td>"
            f"<td>{esc(f'{masked} of {len(pii)}') if pii else '-'}</td>"
            f"<td>{esc(billed if billed not in (None, '') else '-')} / {esc(run.get('model_cost_usd', '-') if run else '-')}</td>"
            f"<td>{esc(r.get('latency_s'))} s</td></tr>")
    failed = [c for c in checks if c[2] == "FAIL"]
    gaps = [c for c in checks if c[2] == "GAP"]
    ev = state.get("evals") or {}
    gsum = (ev.get("golden") or {}).get("summary") or {}
    isum = (ev.get("injection") or {}).get("summary") or {}
    kinds = "".join(f"<tr><td>{esc(k)}</td><td class=ok>{v.get('PASS', 0)}</td><td class=bad>{v.get('FAIL', 0)}</td>"
                    f"<td class=gap>{v.get('GAP', 0)}</td></tr>" for k, v in sorted(by_kind.items()))
    listing = "".join(f"<tr><td>{esc(k)}</td><td>{esc(n)}</td><td class={'bad' if s == 'FAIL' else 'gap'}>{esc(s)}</td>"
                      f"<td><code>{esc(str(dt)[:160])}</code></td></tr>" for k, n, s, dt in failed + gaps)
    return (
        '<div class="page"><h1>The checks after the console setup</h1>'
        f'<div class="meta">{esc(os.path.basename(path))} · tenant {esc(TENANT)} · node triage v{esc(state.get("node_version"))}'
        f' · ontology {esc(state.get("ontology_version"))}</div>'
        '<div class="tiles">'
        f'<div class="tile"><div class="n ok">{count["PASS"]}</div><div class="l">checks passed</div></div>'
        f'<div class="tile"><div class="n {"bad" if count["FAIL"] else "ok"}">{count["FAIL"]}</div><div class="l">failed</div></div>'
        f'<div class="tile"><div class="n gap">{count["GAP"]}</div><div class="l">known gaps (names need the NER tier)</div></div>'
        f'<div class="tile"><div class="n">{esc(gsum.get("pass_hat_k"))}</div><div class="l">golden pass^k (k = 3)</div></div>'
        f'<div class="tile"><div class="n">{esc(isum.get("blocked"))}/{esc(isum.get("trials"))}</div><div class="l">attacks blocked</div></div>'
        '</div><h2>Requests</h2><table><tr><th>Request</th><th>Intent</th><th>Routed to</th><th>Steps</th>'
        '<th>Queries</th><th>PII masked</th><th>Billed / model cost (USD)</th><th>Latency</th></tr>'
        + "".join(rows) + '</table><h2>Checks by kind</h2><table><tr><th>Kind</th><th>Pass</th><th>Fail</th>'
        '<th>Gap</th></tr>' + kinds + '</table>'
        + ('<h2>Failures and known gaps</h2><table><tr><th>Kind</th><th>Check</th><th>Result</th><th>Detail</th></tr>'
           + listing + '</table>' if listing else '')
        + '<h2>Where to read more</h2><div class="meta">The full report: .work/report.md; every request\'s raw '
          'results: .work/results-*.json; the console\'s shots and timeline: .work/demo/.</div></div>')


def telemetry_body():
    path = os.path.join(OUT, "telemetry.json")
    if not os.path.exists(path):
        sys.exit(f"cards.py: no {path} (run telemetry.py report first)")
    with open(path, encoding="utf-8") as f:
        agg = json.load(f)

    def ms(v):
        return "-" if v is None else (f"{v:,.0f}" if v >= 100 else f"{v:.1f}")

    parts = ['<div class="page"><h1>Where the time goes</h1><div class="meta">Per request and per stage, in ms. '
             "Stages come from the gateway's Server-Timing header when it sends one; end to end is measured by the "
             "client.</div>"]
    for sc, d in agg.items():
        rows = d.get("rows") or []
        parts.append(f"<h2>{esc(sc)}</h2>")
        if rows:
            parts.append("<table><tr><th>Request</th><th>Intent</th><th>Route stage</th><th>Classify</th><th>PII</th>"
                         "<th>Cache</th><th>Model TTFB / total</th><th>Node steps</th><th>End to end</th></tr>")
            for r in rows:
                steps = ", ".join(f"{s.get('vertex')} {ms(s.get('duration_ms'))}" for s in r.get("steps") or []) or "-"
                parts.append(f"<tr><td><b>{esc(r.get('label'))}</b></td><td><code>{esc(r.get('intent') or '-')}</code></td>"
                             f"<td>{esc(r.get('route_stage') or '-')}</td><td>{ms(r.get('classify_ms'))}</td>"
                             f"<td>{ms(r.get('pii_ms'))}</td><td>{esc(r.get('cache') or '-')}</td>"
                             f"<td>{ms(r.get('model_ttfb_ms'))} / {ms(r.get('model_total_ms'))}</td>"
                             f"<td>{esc(steps)}</td><td>{ms(r.get('end_to_end_ms'))}</td></tr>")
            parts.append("</table>")
        pc = d.get("percentiles") or []
        if pc:
            parts.append("<table style='margin-top:18px;width:auto'><tr><th>Stage</th><th>n</th><th>p50</th><th>p95</th></tr>"
                         + "".join(f"<tr><td>{esc(p['metric'])}</td><td>{p['n']}</td><td>{ms(p['p50'])}</td>"
                                   f"<td>{ms(p['p95'])}</td></tr>" for p in pc) + "</table>")
        ev = d.get("evals") or []
        if ev:
            parts.append("<table style='margin-top:18px;width:auto'><tr><th>Eval</th><th>Trials</th><th>p50</th>"
                         "<th>p95</th></tr>" + "".join(
                             f"<tr><td>{esc(e.get('label'))}</td><td>{esc(e.get('trials'))}</td><td>{ms(e.get('p50'))}</td>"
                             f"<td>{ms(e.get('p95'))}</td></tr>" for e in ev) + "</table>")
    parts.append("</div>")
    return "".join(parts)


def record_scroll(pw, body, name):
    os.makedirs(OUT, exist_ok=True)
    browser = pw.chromium.launch()
    ctx = browser.new_context(viewport={"width": 1920, "height": 1080},
                              record_video_dir=os.path.join(OUT, "video"),
                              record_video_size={"width": 1920, "height": 1080})
    page = ctx.new_page()
    page.set_content(page_html(body))
    page.wait_for_timeout(5000)
    height = page.evaluate("document.body.scrollHeight")
    y = 0
    while y + 1080 < height:
        y = min(y + 6, height - 1080)
        page.evaluate(f"window.scrollTo(0, {y})")
        page.wait_for_timeout(40)
    page.wait_for_timeout(4000)
    video = page.video
    ctx.close()
    video.save_as(os.path.join(OUT, f"{name}.webm"))
    video.delete()
    browser.close()
    print(f"cards.py: {os.path.join(OUT, name + '.webm')}")


def main():
    what = sys.argv[1] if len(sys.argv) > 1 else "cards"
    with sync_playwright() as pw:
        if what == "cards":
            render_cards(pw)
        elif what == "results":
            record_scroll(pw, results_body(), "results")
        elif what == "telemetry":
            record_scroll(pw, telemetry_body(), "telemetry")
        else:
            sys.exit("usage: cards.py cards | results | telemetry")


if __name__ == "__main__":
    main()
