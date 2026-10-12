"""Shared bits of the support app's scripts: settings, the SDK clients, timings, the state file.

Everything comes from the environment, like a developer's shell:

  CALIBAN_BASE_URL     the gateway, with /v1 (http://127.0.0.1:18080/v1)
  CALIBAN_API_KEY      the tenant API key minted in the console
  CALIBAN_ADMIN_URL    the control plane (http://127.0.0.1:18081)
  CALIBAN_ADMIN_TOKEN  the admin token (intents, nodes, evals)
  DEV_TENANT           the tenant (acme-dev)
  DEV_CHAT_MODEL       the catalogue model the nodes and the first call use (dev/chat)
  DEV_STATE            where the scripts keep what they created (node versions, run ids)
"""
import json
import os
import statistics
import time

import httpx
from caliban import Caliban
from caliban.admin import CalibanAdmin

HERE = os.path.dirname(os.path.abspath(__file__))
TENANT = os.environ.get("DEV_TENANT", "acme-dev")
CHAT_MODEL = os.environ.get("DEV_CHAT_MODEL", "dev/chat")
STATE = os.environ.get("DEV_STATE", os.path.join(HERE, ".state.json"))
OUT = os.environ.get("DEV_OUT", os.path.dirname(STATE))


def data(name):
    with open(os.path.join(HERE, "data", name), encoding="utf-8") as f:
        return json.load(f)


# ─────────────────────────── clients ───────────────────────────


class Timed:
    """A Caliban client whose last response's headers and wall time are kept: the SDK parses the
    x-caliban-* headers into `.caliban`, and this adds the W3C Server-Timing header (per-stage
    durations) and the client's own clock."""

    def __init__(self, **kw):
        self.last = {}
        self._t0 = 0.0
        http = httpx.Client(timeout=kw.pop("timeout", 300.0),
                            event_hooks={"request": [self._start], "response": [self._end]})
        self.client = Caliban(http_client=http, **kw)

    def _start(self, request):
        self._t0 = time.perf_counter()

    def _end(self, response):
        self.last = {"headers": {k.lower(): v for k, v in response.headers.items()},
                     "client_ms": (time.perf_counter() - self._t0) * 1000.0}

    def chat(self, **kw):
        r = self.client.chat.completions.create(**kw)
        h = self.last.get("headers", {})
        return r, {"client_ms": self.last.get("client_ms"), "timing": server_timing(h.get("server-timing")),
                   "headers": h}


def refresh_routing(settle_s=None):
    """Routers rebuild the kNN index in the background when a request arrives after the snapshot
    changed (core: "config changes are picked up in the background on the next request"); that
    first request still routes with the old index. One small caliban/auto request starts the
    rebuild, then a short wait lets it finish."""
    if os.environ.get("CALIBAN_API_KEY"):
        try:
            Caliban(timeout=60.0).chat.completions.create(
                model="caliban/auto", messages=[{"role": "user", "content": "ping"}], max_tokens=1,
                caliban={"cache": "off", "reasoning": "off"})
        except Exception:  # noqa: BLE001 (only a trigger: its answer does not matter)
            pass
    time.sleep(float(settle_s if settle_s is not None else os.environ.get("DEV_INTENT_SETTLE_S", "5")))


def admin():
    return CalibanAdmin(timeout=120.0)


def raw(adm, method, path, body=None, params=None):
    """An admin call the Python SDK does not wrap yet (publish, promote, routes, node_routes, runs,
    node-spend, usage by node). Same auth and retries as the SDK; returns the parsed JSON."""
    r = adm._http.request(method, path.lstrip("/"), json=body, params=params)
    return r.json() if r.content else None


# ─────────────────────────── Server-Timing ───────────────────────────


def server_timing(value):
    """`name;dur=1.2;desc="knn", other;dur=3` -> {"name": {"dur": 1.2, "desc": "knn"}, ...}.
    Never raises: an absent or malformed header gives {}."""
    out = {}
    for part in (value or "").split(","):
        bits = [b.strip() for b in part.split(";") if b.strip()]
        if not bits or "=" in bits[0]:
            continue
        m = {}
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
        out[bits[0].lower()] = m
    return out


def dur(timing, name):
    v = (timing or {}).get(name, {}).get("dur")
    return v


def pct(values, p):
    """Nearest-rank percentile of the values that are not None (None when there are none)."""
    xs = sorted(v for v in values if v is not None)
    if not xs:
        return None
    k = max(1, min(len(xs), int(round(p / 100.0 * len(xs) + 0.4999))))
    return xs[k - 1]


def ms(v):
    return "-" if v is None else (f"{v:.0f}" if v >= 10 else f"{v:.1f}")


def median(values):
    xs = [v for v in values if v is not None]
    return statistics.median(xs) if xs else None


# ─────────────────────────── state ───────────────────────────


def load_state():
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(STATE) or ".", exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def write_out(name, obj):
    os.makedirs(OUT or ".", exist_ok=True)
    path = os.path.join(OUT or ".", name)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    return path


def table(rows, headers):
    """A plain text table (the terminal card shows it as is)."""
    cells = [[str(c) for c in r] for r in rows]
    widths = [max(len(h), *(len(r[i]) for r in cells)) if cells else len(h) for i, h in enumerate(headers)]
    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    out = [line, "  ".join("-" * w for w in widths)]
    out += ["  ".join(c.ljust(w) for c, w in zip(r, widths)) for r in cells]
    return "\n".join(out)


def short(text, n):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 3] + "..."
