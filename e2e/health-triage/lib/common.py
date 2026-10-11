"""Shared helpers for the health triage e2e scripts (stdlib only, Python 3.9+).

Settings come from the environment that e2e.sh exports: E2E_ROUTER_URL, E2E_CP_URL,
CALIBAN_ADMIN_TOKEN, E2E_WORK_DIR, E2E_COMPOSE_CMD, E2E_TENANT and the E2E_* model settings.
"""
import json
import os
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROUTER = os.environ.get("E2E_ROUTER_URL", "http://127.0.0.1:18080").rstrip("/")
CP = os.environ.get("E2E_CP_URL", "http://127.0.0.1:18081").rstrip("/")
ADMIN = os.environ.get("CALIBAN_ADMIN_TOKEN", "")
WORK = os.environ.get("E2E_WORK_DIR", os.path.join(os.path.dirname(__file__), "..", ".work"))
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TENANT = os.environ.get("E2E_TENANT", "medmarket")
STATE = os.path.join(WORK, "state.json")


class HttpError(Exception):
    def __init__(self, status, body, headers):
        super().__init__(f"HTTP {status}: {body if isinstance(body, str) else json.dumps(body)[:800]}")
        self.status, self.body, self.headers = status, body, headers


class Resp:
    def __init__(self, status, headers, body, elapsed):
        self.status, self.headers, self.body, self.elapsed = status, headers, body, elapsed

    def header(self, name, default=None):
        for k, v in self.headers.items():
            if k.lower() == name.lower():
                return v
        return default


def request(method, url, token=None, body=None, headers=None, timeout=900, ok=(200, 201, 202, 204)):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            status, hdrs = r.status, dict(r.headers.items())
    except urllib.error.HTTPError as e:
        raw, status, hdrs = e.read(), e.code, dict(e.headers.items())
    elapsed = time.monotonic() - t0
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    if ok is not None and status not in ok:
        raise HttpError(status, parsed, hdrs)
    return Resp(status, hdrs, parsed, elapsed)


def cp(method, path, body=None, **kw):
    return request(method, CP + path, ADMIN, body, **kw)


def dp(method, path, key, body=None, **kw):
    return request(method, ROUTER + path, key, body, **kw)


def compose(*args, input_text=None, check=True):
    """Runs `docker compose ...` with the e2e project and files (E2E_COMPOSE_CMD)."""
    cmd = shlex.split(os.environ["E2E_COMPOSE_CMD"]) + list(args)
    r = subprocess.run(cmd, input=input_text, capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"{' '.join(args[:3])} failed: {r.stderr.strip()[-1500:]}")
    return r.stdout


def mongo_eval(js, member="caliban-e2e-mongo-analytics"):
    """Evaluates JavaScript on one MongoDB member as root (marketplace database), returns stdout."""
    uri = (f"mongodb://{os.environ['E2E_MONGO_ROOT_USER']}:{os.environ['E2E_MONGO_ROOT_PASSWORD']}"
           "@localhost:27017/marketplace?authSource=admin&directConnection=true")
    return compose("exec", "-T", member, "mongosh", "--quiet", uri, "--eval", js)


def wait_until(what, fn, timeout=120, every=1.0):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            v = fn()
            if v:
                return v
        except Exception as e:  # noqa: BLE001 (keep polling, report the last error)
            last = e
        time.sleep(every)
    raise TimeoutError(f"timed out waiting for {what}" + (f" (last error: {last})" if last else ""))


def wait_healthy(timeout=180):
    wait_until("the control plane", lambda: cp("GET", "/api/v1/health", ok=None).status == 200, timeout)
    wait_until("the data plane", lambda: request("GET", ROUTER + "/healthz", ok=None).status == 200, timeout)


def load_state():
    if os.path.exists(STATE):
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(st):
    os.makedirs(WORK, exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def step(title):
    print(f"\n== {title}", flush=True)


def info(msg):
    print(f"   {msg}", flush=True)


def fail(msg):
    print(f"FAILED: {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


def q(s):
    return urllib.parse.quote(s, safe="")
