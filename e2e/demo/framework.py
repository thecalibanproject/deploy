#!/usr/bin/env python3
"""The recorded demo framework (e2e/demo/README.md is the contract).

    framework.py record SCENARIO [SCENARIO...]   records e2e/<scenario>/demo/scenario.py
    framework.py list                            the scenarios found under e2e/

Runs inside the pinned Playwright image (run-demo.sh), or locally with DEMO_LOCAL=1 and a Python
that has playwright==1.63.0. Each scenario gets its own browser context, so its own video:
$DEMO_OUT/<scenario>.webm, with <scenario>.timeline.json and shots/<scenario>/ next to it.
"""
import argparse
import html
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from contextlib import contextmanager

from playwright.sync_api import Error as PwError
from playwright.sync_api import expect, sync_playwright

import telemetry

# Run as a script, this module is __main__: scenarios that `import framework` must get this same
# module (one ApiError class, one set of constants), not a second copy.
if __name__ == "__main__":
    sys.modules.setdefault("framework", sys.modules[__name__])

DEMO_DIR = os.path.dirname(os.path.abspath(__file__))
E2E_ROOT = os.path.dirname(DEMO_DIR)
WORK = os.environ.get("E2E_WORK_DIR") or os.path.join(E2E_ROOT, "health-triage", ".work")
OUT = os.environ.get("DEMO_OUT") or os.path.join(WORK, "demo")
CP_URL = os.environ.get("E2E_CP_URL", "http://127.0.0.1:18081").rstrip("/")
ROUTER_URL = os.environ.get("E2E_ROUTER_URL", "http://127.0.0.1:18080").rstrip("/")
ADMIN = os.environ.get("CALIBAN_ADMIN_TOKEN", "")
PACE = float(os.environ.get("DEMO_PACE", "1.0"))
SLOWMO = int(os.environ.get("DEMO_SLOWMO_MS", "120"))
SCALE = float(os.environ.get("DEMO_SCALE", "1.25"))
WIDTH, HEIGHT = 1920, 1080

TERMINAL_RUN = {"succeeded", "failed", "budget_exhausted", "cancelled"}
CALIBAN_HEADERS = ("x-caliban-intent", "x-caliban-route", "x-caliban-route-fallback", "x-caliban-routed-model",
                   "caliban-run-id", "x-caliban-run-status", "x-caliban-pii-entities", "x-caliban-cost-usd",
                   "x-caliban-billed-usd", "x-caliban-cache", "x-caliban-request-id")
PLACEHOLDER = re.compile(r"\[[A-Z][A-Z_]*(?:_\d+)?\]")


def label(scope, text):
    """The control labelled exactly `text` (a required field's label ends with a "*")."""
    return scope.get_by_label(re.compile("^" + re.escape(text) + r"\s*\*?$"))


def esc(s):
    return html.escape(str(s if s is not None else ""), quote=True)


# ─────────────────────────── HTTP ───────────────────────────


class ApiError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path}: HTTP {status}: {json.dumps(body, ensure_ascii=False)[:800]}"
                         if not isinstance(body, str) else f"{method} {path}: HTTP {status}: {body[:800]}")
        self.status, self.body = status, body


def _http(method, url, token, body=None, headers=None, timeout=900):
    status, hdrs, parsed, _ = _timed_http(method, url, token, body, headers, timeout)
    return status, hdrs, parsed


def _timed_http(method, url, token, body=None, headers=None, timeout=900):
    """Like _http, plus the client-side times in ms: to the response headers, to the first body
    byte, total. A streamed answer (`"stream": true`) is read whole; its text is the SSE body."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.monotonic()
    t_first = None
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            t_head = time.monotonic()
            first = r.read(1)
            t_first = time.monotonic()
            raw, status, hdrs = first + r.read(), r.status, dict(r.headers.items())
    except urllib.error.HTTPError as e:
        t_head = time.monotonic()
        raw, status, hdrs = e.read(), e.code, dict(e.headers.items())
    t_end = time.monotonic()
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    timing = {"headers_ms": round((t_head - t0) * 1000, 1),
              "ttfb_ms": round(((t_first or t_end) - t0) * 1000, 1), "total_ms": round((t_end - t0) * 1000, 1)}
    return status, {k.lower(): v for k, v in hdrs.items()}, parsed, timing


class Api:
    """The admin API as the operator admin (CALIBAN_ADMIN_TOKEN)."""

    def __init__(self, base=CP_URL, token=ADMIN):
        self.base, self.token = base, token

    def call(self, method, path, json=None, tenant_key=None):
        status, _, body = _http(method, self.base + path, tenant_key or self.token, json)
        if not 200 <= status < 300:
            raise ApiError(method, path, status, body)
        return body

    def get(self, path, json=None, tenant_key=None):
        return self.call("GET", path, json, tenant_key)

    def post(self, path, json=None, tenant_key=None):
        return self.call("POST", path, json, tenant_key)

    def put(self, path, json=None, tenant_key=None):
        return self.call("PUT", path, json, tenant_key)

    def patch(self, path, json=None, tenant_key=None):
        return self.call("PATCH", path, json, tenant_key)

    def delete(self, path, json=None, tenant_key=None):
        return self.call("DELETE", path, json, tenant_key)


def wait_healthy(timeout=180):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if _http("GET", CP_URL + "/api/v1/health", ADMIN, timeout=5)[0] == 200:
                return
        except OSError:
            pass
        time.sleep(1)
    raise TimeoutError(f"the control plane at {CP_URL} is not healthy")


# ─────────────────────────── the client panel ───────────────────────────


def mark_placeholders(text):
    """Escapes text and highlights mask placeholders such as [EMAIL] or [PHONE_1]."""
    return PLACEHOLDER.sub(lambda m: f"<mark>{m.group(0)}</mark>", esc(text))


def answer_text(body):
    if isinstance(body, dict):
        choices = body.get("choices")
        if choices:
            return ((choices[0] or {}).get("message") or {}).get("content") or ""
        return json.dumps(body, indent=2, ensure_ascii=False)
    return "" if body is None else str(body)


SEG_COLOURS = {"auth": "#a5b4fc", "route": "#7dd3fc", "embed": "#67e8f9", "pii": "#fcd34d", "cache": "#86efac",
               "upstream": "#f9a8d4", "upstream_total": "#f472b6", "node": "#c4b5fd", "other": "#64748b"}


def steps_text(steps):
    return " · ".join(f"{s.get('vertex')} {s.get('duration_ms') if s.get('duration_ms') is not None else '-'}"
                      for s in steps) or "-"


def timing_html(timing, steps=None):
    """A compact bar: the gateway's Server-Timing stages as segments (ms), else the client's times;
    then a node run's steps with their durations."""
    if not timing:
        return ""
    cl = timing.get("client") or {}
    st = timing.get("server") or {}
    total = (st.get("total") or {}).get("dur") or cl.get("total_ms") or 0
    parts = []
    if st:
        segs = [(k, (st[k] or {}).get("dur")) for k in ("auth", "route", "embed", "pii", "cache", "node")
                if (st.get(k) or {}).get("dur") is not None]
        up = (st.get("upstream_total") or st.get("upstream") or {}).get("dur")
        if up is not None:
            segs.append(("upstream_total" if st.get("upstream_total") else "upstream", up))
        used = sum(d for _, d in segs)
        if total and total > used:
            segs.append(("other", total - used))
        bar = "".join(
            f'<span style="flex:{max(d, 0.001)};background:{SEG_COLOURS.get(k, "#94a3b8")}" '
            f'title="{esc(k)}">{esc(k)} {d:.0f}</span>' for k, d in segs)
        descs = ", ".join(f"{k} {esc(v.get('desc'))}" for k, v in st.items() if v.get("desc") and k != "node")
        parts.append(f'<h4>Timing (Server-Timing)</h4><div class="tbar">{bar}</div>'
                     f'<div class="tlegend">gateway total {total:.0f} ms · client total {cl.get("total_ms", 0):.0f} ms'
                     f'{" · " + descs if descs else ""}</div>')
    else:
        parts.append(f'<h4>Timing (client side)</h4><div class="tlegend">headers {cl.get("headers_ms", 0):.0f} ms · '
                     f'first byte {cl.get("ttfb_ms", 0):.0f} ms · total {cl.get("total_ms", 0):.0f} ms '
                     '(no Server-Timing header from this build)</div>')
    steps = steps if steps is not None else timing.get("steps")
    if steps:
        parts.append(f'<div class="tlegend">node steps (ms): {esc(steps_text(steps))}</div>')
    return "".join(parts)


def client_html(title, request_json, status=None, headers=None, body=None, *, method="POST",
                path="/v1/chat/completions", answer=None, extra="", pending=None, rtl=False, model_saw=None,
                timing=None):
    """The customer app panel: the request (the key masked), the status, Caliban's headers, the answer.
    `answer` overrides the text taken from `body`; `extra` is trusted HTML appended on the right;
    `model_saw` is shown under the request with mask placeholders highlighted."""
    req = (f"{method} {path}\nAuthorization: Bearer cal_••••••••\ncontent-type: application/json\n\n"
           + json.dumps(request_json, indent=2, ensure_ascii=False))
    right = []
    if status is None:
        right.append(f'<p class="muted"><span class="spin"></span> {esc(pending or "waiting for Caliban…")}</p>')
    else:
        tone = "ok" if 200 <= int(status) < 300 else "warn"
        right.append(f'<p><span class="badge {tone}">HTTP {esc(status)}</span></p>')
        right.append(timing_html(timing))
    shown = {k: v for k, v in (headers or {}).items() if v and (k.startswith("x-caliban") or k.startswith("caliban-")
                                                              or k in ("run", "status"))}
    if shown:
        rows = "".join(f'<tr><td class="k">{esc(k)}</td><td>{esc(v)}</td></tr>' for k, v in shown.items())
        right.append(f"<h4>Caliban headers</h4><table>{rows}</table>")
    text = answer if answer is not None else (answer_text(body) if status is not None else "")
    if text:
        right.append(f'<h4>Answer</h4><div class="answer"{" dir=rtl" if rtl else ""}>{esc(text[:1500])}</div>')
    right.append(extra or "")
    left = f"<h4>Request</h4><pre>{esc(req)}</pre>"
    if model_saw:
        left += f"<h4>What the model received</h4><pre>{mark_placeholders(model_saw)}</pre>"
    return ('<div class="card"><div class="head"><span class="t">Customer app</span>'
            f'<span class="s">{esc(title)}</span></div><div class="cols"><div class="col">{left}</div>'
            f'<div class="col">{"".join(right)}</div></div></div>')


# ─────────────────────────── Ctx ───────────────────────────


class Ctx:
    """What a scenario's chapters get (README.md, "Ctx")."""

    def __init__(self, page, scenario):
        self.page = page
        self.scenario = scenario
        self.console_url = CP_URL
        self.gateway_url = ROUTER_URL
        self.api = Api()
        self.work_dir = WORK
        self.out_dir = OUT
        names = set(filter(None, os.environ.get("DEMO_PASS_ENV", "").split()))
        self.env = {k: v for k, v in os.environ.items()
                    if k.startswith("E2E_") or k.startswith("DEMO_") or k in names}
        self.t0 = time.monotonic()
        self.timeline = {"scenario": scenario, "chapters": [], "idle": [], "marks": [], "api_steps": [],
                         "failed": None}
        self.shots = os.path.join(OUT, "shots", scenario)
        os.makedirs(self.shots, exist_ok=True)
        self._chapter = None
        self._n = 0
        self.last_timing = None

    # Time and the timeline.
    def now(self):
        return round(time.monotonic() - self.t0, 2)

    def pause(self, seconds=1.0):
        """Pacing for the viewer only (never for correctness)."""
        if seconds > 0:
            self.page.wait_for_timeout(int(seconds * 1000 * PACE))

    def mark(self, event):
        self.timeline["marks"].append({"t": self.now(), "event": event, "chapter": self._chapter})

    @contextmanager
    def idle(self, label):
        start = self.now()
        try:
            yield
        finally:
            self.timeline["idle"].append({"label": label, "start": start, "end": self.now()})

    @contextmanager
    def chapter(self, key, title=None):
        self._n += 1
        self._chapter = key
        start = self.now()
        print(f"== {self.scenario}: chapter {key} (t={start}s)", flush=True)
        try:
            yield
        finally:
            self.timeline["chapters"].append({"key": key, "title": title or key, "start": start, "end": self.now()})
            self.screenshot(f"{self._n:02d}-{key}")
            self.write_timeline()

    def write_timeline(self):
        with open(os.path.join(OUT, f"{self.scenario}.timeline.json"), "w", encoding="utf-8") as f:
            json.dump(self.timeline, f, indent=2, ensure_ascii=False)

    def screenshot(self, name):
        try:
            self.page.screenshot(path=os.path.join(self.shots, f"{name}.png"))
        except PwError as e:
            print(f"   screenshot {name}: {e}", flush=True)

    # The overlay.
    def caption(self, title, line, stage=None, note=None, clock=False, hold=0):
        self.page.evaluate("([s, c, l, n, k]) => window.__demo.caption(s, c, l, n, k)",
                           [stage, title, line, note, clock])
        self.pause(hold)

    def api_caption(self, title, line, what, stage=None, hold=4):
        self.timeline["api_steps"].append({"chapter": self._chapter, "what": what})
        self.caption(title, line, stage=stage, note=f"Admin API, no console page for this yet: {what}", hold=hold)

    def panel(self, inner_html):
        self.page.evaluate("(h) => window.__demo.panel(h)", inner_html)

    def hide_panel(self):
        self.page.evaluate("() => window.__demo.hidePanel()")

    def client_panel(self, title, request_json, status, headers, body, **kw):
        """The customer app panel; with a status, it shows the timing of the last ctx.gateway call
        (or `timing=`)."""
        if status is not None and "timing" not in kw:
            kw["timing"] = self.last_timing
        self.panel(client_html(title, request_json, status, headers, body, **kw))

    def terminal(self, title, lines, cps=None):
        self.page.evaluate("([t, l, c]) => window.__demo.terminal(t, l, c)",
                           [title, list(lines), cps or float(os.environ.get("DEMO_TYPE_CPS", "28")) / PACE])

    def card(self, inner_html):
        self.page.evaluate("(h) => window.__demo.card(h)", inner_html)

    def hide_card(self):
        self.page.evaluate("() => window.__demo.hideCard()")

    # The console.
    def goto(self, route):
        self.page.goto(f"{CP_URL}/#{route if route.startswith('/') else '/' + route}")
        expect(self.page.get_by_role("navigation", name="Primary")).to_be_visible()

    def point(self, loc, hold=0.7):
        loc.scroll_into_view_if_needed()
        box = loc.bounding_box()
        if box:
            self.page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=8)
            self.page.evaluate("([x, y, w, h]) => window.__demo.ring(x, y, w, h)",
                               [box["x"], box["y"], box["width"], box["height"]])
        self.pause(hold)
        self.page.evaluate("() => window.__demo.unring()")

    def click(self, loc, hold=0.5):
        expect(loc).to_be_enabled()
        self.point(loc, hold)
        loc.click()

    def type(self, loc, text, hold=0.3):
        self.point(loc, hold)
        loc.click()
        loc.fill("")
        if len(text) <= 48:
            loc.press_sequentially(text, delay=35)
        else:
            loc.fill(text)

    def nav(self, label):
        self.click(self.page.get_by_role("navigation", name="Primary").get_by_role("link", name=label, exact=True))

    def dialog(self, name):
        d = self.page.get_by_role("dialog", name=name)
        expect(d).to_be_visible()
        return d

    # Data plane, with telemetry.
    def gateway(self, path, json=None, api_key=None, headers=None, timeout=900, label=None):
        """A data-plane call: (status, headers, body). Always timed (client side) and recorded in
        <scenario>-telemetry.jsonl with the gateway's Server-Timing stages when present; for a node
        run that ended, its steps' durations are fetched and recorded too."""
        status, hdrs, body, client = _timed_http("GET" if json is None else "POST", ROUTER_URL + path, api_key, json,
                                                 headers, timeout)
        server = telemetry.parse_server_timing(hdrs.get("server-timing"))
        run_id = hdrs.get("caliban-run-id")
        if not run_id and isinstance(body, dict) and path.startswith("/v1/nodes/") and body.get("id"):
            run_id = body["id"]
        rec = {"kind": "request", "scenario": self.scenario, "chapter": self._chapter, "t": self.now(),
               "label": label or self._chapter, "path": path, "method": "GET" if json is None else "POST",
               "status": status, "intent": hdrs.get("x-caliban-intent"), "route": hdrs.get("x-caliban-route"),
               "routed_model": hdrs.get("x-caliban-routed-model"), "cache": hdrs.get("x-caliban-cache"),
               "run_id": run_id, "client": client, "server": server}
        self.last_timing = {"client": client, "server": server}
        if json is not None or not path.startswith("/v1/runs/"):
            telemetry.append(self.scenario, rec)
            if run_id and api_key:
                run = self._run(run_id, api_key)
                if run and run.get("status") in TERMINAL_RUN:
                    self._record_run(run, api_key, label or self._chapter, client.get("total_ms"))
        return status, hdrs, body

    def _run(self, run_id, api_key):
        status, _, run, _ = _timed_http("GET", f"{ROUTER_URL}/v1/runs/{run_id}", api_key)
        return run if status == 200 and isinstance(run, dict) else None

    def _record_run(self, run, api_key, label, end_to_end_ms=None):
        steps = [{k: s.get(k) for k in ("id", "vertex", "kind", "status", "duration_ms")} for s in run.get("steps") or []]
        duration = None
        if run.get("started_at") and run.get("finished_at"):
            try:
                from datetime import datetime
                a = datetime.fromisoformat(run["started_at"].replace("Z", "+00:00"))
                b = datetime.fromisoformat(run["finished_at"].replace("Z", "+00:00"))
                duration = round((b - a).total_seconds() * 1000, 1)
            except ValueError:
                pass
        telemetry.append(self.scenario, {"kind": "run", "scenario": self.scenario, "label": label, "t": self.now(),
                                         "run_id": run.get("id"), "status": run.get("status"), "steps": steps,
                                         "duration_ms": duration, "end_to_end_ms": end_to_end_ms})
        if self.last_timing is not None:
            self.last_timing["steps"] = steps
        return steps

    def run_timing(self, run_id, api_key, label=None, end_to_end_ms=None):
        """Records the step durations of a node run that ended after the call that started it (a
        human step, a sync wait that ran out); returns the run."""
        run = self._run(run_id, api_key)
        if run:
            self._record_run(run, api_key, label or self._chapter, end_to_end_ms)
        return run

    def record_eval(self, label, ev):
        """Records an eval's latency percentiles (its summary's latency_ms) for the telemetry report."""
        s = (ev or {}).get("summary") or {}
        lat = s.get("latency_ms") or {}
        telemetry.append(self.scenario, {"kind": "eval", "scenario": self.scenario, "label": label,
                                         "suite": ev.get("suite"), "eval_id": ev.get("id"), "trials": s.get("trials"),
                                         "p50": lat.get("p50"), "p95": lat.get("p95"), "t": self.now()})

    def telemetry_chapter(self):
        """The closing chapter of every scenario: its requests' timings and p50/p95 per stage."""
        rs = telemetry.rows(telemetry.load(self.scenario))
        evs = telemetry.evals(telemetry.load(self.scenario))
        if not rs and not evs:
            return
        with self.chapter("telemetry", "Telemetry"):
            ms = telemetry.ms
            body = "".join(
                f'<tr><td class="k">{esc(r["label"])}</td><td>{esc(r["intent"] or "-")}</td>'
                f'<td>{esc(r["route_stage"] or "-")}</td><td>{esc(telemetry.classify_text(r))}</td><td>{ms(r["pii_ms"])}</td>'
                f'<td>{esc(r["cache"] or "-")}</td><td>{ms(r["model_ttfb_ms"])} / {ms(r["model_total_ms"])}</td>'
                f'<td>{esc(steps_text(r["steps"]))}</td>'
                f'<td>{ms(r["end_to_end_ms"])}</td></tr>' for r in rs[:14])
            pcts = "".join(f'<tr><td class="k">{esc(p["metric"])}</td><td>{p["n"]}</td><td>{ms(p["p50"])}</td>'
                           f'<td>{ms(p["p95"])}</td></tr>' for p in telemetry.percentiles(rs)[:14])
            evt = "".join(f'<tr><td class="k">{esc(e.get("label"))}</td><td>{esc(e.get("trials"))}</td>'
                          f'<td>{ms(e.get("p50"))}</td><td>{ms(e.get("p95"))}</td></tr>' for e in evs)
            server = any(r["server_timing"] for r in rs)
            self.panel(
                '<div class="card"><div class="head"><span class="t">Telemetry</span><span class="s">'
                + ("stages from the gateway's Server-Timing header, times in ms" if server else
                   "client-side times in ms: this build sends no Server-Timing header")
                + '</span></div><div class="cols" style="grid-template-columns:1.9fr 1fr"><div class="col">'
                '<h4>Requests</h4><table style="font-size:13px"><tr><td class="k">request'
                '</td><td>intent</td><td>route</td><td>classify</td><td>PII</td><td>cache</td><td>model TTFB / total'
                f'</td><td>node steps</td><td>end to end</td></tr>{body}</table></div><div class="col">'
                f'<h4>p50 and p95 per stage</h4><table><tr><td class="k">stage</td><td>n</td><td>p50</td><td>p95</td>'
                f'</tr>{pcts}</table>'
                + (f'<h4>Evals</h4><table><tr><td class="k">eval</td><td>trials</td><td>p50</td><td>p95</td></tr>{evt}'
                   '</table>' if evt else "") + '</div></div></div>')
            self.caption("Telemetry", "Where the time goes, per request and per stage: classification, PII, cache, "
                         "the model and each node step.", stage="Log", hold=9)
            self.screenshot(f"{self._n:02d}-telemetry-panel")
            self.hide_panel()

    # Sign-in (recorded, done by the runner before the first chapter).
    def login(self):
        with self.chapter("login", "Sign in"):
            self.page.goto(f"{CP_URL}/#/login")
            self.caption("Sign in", "The operator opens the console the control plane serves and signs in with the "
                         "admin token. Every change made from here on is written to a hash-chained audit log.",
                         stage="Clear")
            tok = label(self.page, "Admin token")
            expect(tok).to_be_visible()
            self.point(tok)
            tok.fill(ADMIN)
            self.click(self.page.get_by_role("button", name=re.compile(r"^Sign in( with the token)?$")))
            expect(self.page.get_by_role("navigation", name="Primary")).to_be_visible()
            self.pause(1.5)


# ─────────────────────────── the runner ───────────────────────────


def scenario_path(name):
    return os.path.join(E2E_ROOT, name, "demo", "scenario.py")


def load_scenario(name):
    path = scenario_path(name)
    if not os.path.exists(path):
        raise SystemExit(f"no scenario at {path}")
    for p in (DEMO_DIR, os.path.dirname(path)):
        if p not in sys.path:
            sys.path.insert(0, p)
    spec = importlib.util.spec_from_file_location(f"demo_scenario_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("NAME", "TITLE", "chapters"):
        if not hasattr(mod, attr):
            raise SystemExit(f"{path} has no {attr}")
    return mod


def list_scenarios():
    return sorted(d for d in os.listdir(E2E_ROOT) if os.path.exists(scenario_path(d)))


def record(pw, name, headed=False, only=None):
    mod = load_scenario(name)
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(DEMO_DIR, "overlay.js"), encoding="utf-8") as f:
        overlay = f.read()
    browser = pw.chromium.launch(headless=not headed, slow_mo=SLOWMO)
    ctx_pw = browser.new_context(
        viewport={"width": round(WIDTH / SCALE), "height": round(HEIGHT / SCALE)}, device_scale_factor=SCALE,
        # The screencast is in CSS pixels: the raw video is the viewport's size (1536x864 at the
        # default DEMO_SCALE 1.25, larger text) and the stitch scales it to 1920x1080.
        record_video_dir=os.path.join(OUT, "video"),
        record_video_size={"width": round(WIDTH / SCALE), "height": round(HEIGHT / SCALE)},
        locale="en-GB", color_scheme="light")
    ctx_pw.add_init_script(overlay)
    expect.set_options(timeout=int(os.environ.get("DEMO_EXPECT_TIMEOUT_MS", "30000")))
    page = ctx_pw.new_page()
    ctx = Ctx(page, mod.NAME)
    ctx.timeline["title"] = mod.TITLE
    failed = None
    try:
        ctx.login()
        for key, fn in mod.chapters(ctx):
            if only and key not in only:
                continue
            with ctx.chapter(key, getattr(fn, "__doc__", None) and fn.__doc__.strip().splitlines()[0]):
                fn(ctx)
        ctx.telemetry_chapter()
    except Exception as e:  # noqa: BLE001 (a failed scenario keeps its video and lets the others run)
        failed = f"{type(e).__name__}: {e}"
        print(f"FAILED {name}: {failed}", file=sys.stderr, flush=True)
        ctx.screenshot("zz-failure")
    finally:
        ctx.timeline["failed"] = failed
        ctx.timeline["duration"] = ctx.now()
        ctx.write_timeline()
        video = page.video
        try:
            path = video.path() if video else None
        except PwError:
            path = None
        ctx_pw.close()
        if path and os.path.exists(path):
            os.replace(path, os.path.join(OUT, f"{mod.NAME}.webm"))
        browser.close()
    print(f"{name}: {os.path.join(OUT, mod.NAME + '.webm')}"
          + (f"; console steps done through the admin API: "
             + "; ".join(f"{s['chapter']}: {s['what']}" for s in ctx.timeline['api_steps'])
             if ctx.timeline["api_steps"] else ""), flush=True)
    return failed is None


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record")
    r.add_argument("scenarios", nargs="+")
    r.add_argument("--only", help="comma-separated chapter keys (debugging)")
    r.add_argument("--headed", action="store_true")
    sub.add_parser("list")
    a = ap.parse_args()
    if a.cmd == "list":
        print("\n".join(list_scenarios()))
        return
    if not ADMIN:
        sys.exit("CALIBAN_ADMIN_TOKEN is not set (run through e2e/demo/run-demo.sh)")
    wait_healthy()
    names = list_scenarios() if a.scenarios == ["all"] else a.scenarios
    ok = True
    with sync_playwright() as pw:
        for n in names:
            ok = record(pw, n, a.headed, set(a.only.split(",")) if a.only else None) and ok
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
