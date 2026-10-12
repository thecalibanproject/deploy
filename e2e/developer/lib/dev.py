"""Settings and helpers shared by the developer scenario's seed, check and recording (stdlib only).

The developer's own code (app/) uses the Caliban Python SDK; this module only prepares what it
needs: the settings for the target (local or testbed), the SDK on a path, and the environment the
app scripts run with.
"""
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))   # e2e/developer
APP = os.path.join(HERE, "app")
DEPLOY = os.path.dirname(os.path.dirname(HERE))
SDK_GIT = "https://github.com/thecalibanproject/sdk-python/archive/refs/heads/main.tar.gz"  # pip needs no git


def settings(env=None):
    env = dict(os.environ if env is None else env)
    target = env.get("E2E_TARGET", "local")
    testbed = target == "testbed"
    work = os.path.join(env.get("E2E_WORK_DIR") or os.path.join(HERE, ".work"), "developer")
    local_sdk = os.path.join(os.path.dirname(DEPLOY), "sdk-python")
    s = {
        "target": target,
        "tenant": env.get("DEV_TENANT", "acme-dev"),
        "cp": (env.get("E2E_CP_URL") or "http://127.0.0.1:18081").rstrip("/"),
        "router": (env.get("E2E_ROUTER_URL") or "http://127.0.0.1:18080").rstrip("/"),
        "admin_token": env.get("CALIBAN_ADMIN_TOKEN", ""),
        "work": work,
        "state": os.path.join(work, "state.json"),
        # The chat model the developer's nodes and first call use: the GPU host's Qwen3.8 through the
        # stack's recorder on the testbed, the scripted model of mock/model.py locally.
        "chat_model": env.get("DEV_CHAT_MODEL", "dev/qwen3.8-27b" if testbed else "dev/chat"),
        "chat_base_url": env.get("DEV_CHAT_BASE_URL",
                                 "http://caliban-e2e-model:8000/v1" if testbed else "http://caliban-e2e-dev-model:8000/v1"),
        "upstream_model": env.get("DEV_UPSTREAM_MODEL", "Qwen/Qwen3.8-27B-FP8" if testbed else "dev-scripted"),
        "family": env.get("DEV_MODEL_FAMILY", "qwen3.8" if testbed else ""),
        "embed_model": env.get("DEV_EMBED_MODEL", env.get("E2E_ROUTING_EMBED_MODEL",
                                                          "local/qwen3-embedding-0.6b" if testbed else "e2e/embed")),
        "rerank_model": env.get("DEV_RERANK_MODEL", "local/qwen3-reranker-0.6b" if testbed else ""),
        "sdk_src": env.get("DEV_SDK_SRC", local_sdk if os.path.isdir(local_sdk) else SDK_GIT),
        "eval_k": env.get("DEV_EVAL_K", "3"),
        "min_pass_k": env.get("DEV_MIN_PASS_K", "0.66"),
        "max_p95_ms": env.get("DEV_MAX_P95_MS", "300000" if testbed else "60000"),
    }
    return s


# ─────────────────────────── admin API (stdlib) ───────────────────────────


class ApiError(Exception):
    def __init__(self, status, body):
        super().__init__(f"HTTP {status}: {json.dumps(body)[:600] if not isinstance(body, str) else body[:600]}")
        self.status, self.body = status, body


def http(method, url, token=None, body=None, timeout=120):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw, status = r.read(), r.status
    except urllib.error.HTTPError as e:
        raw, status = e.read(), e.code
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    if status >= 300:
        raise ApiError(status, parsed)
    return parsed


def api(s, method, path, body=None):
    return http(method, s["cp"] + path, s["admin_token"], body)


def wait_healthy(s, timeout=120):
    deadline = time.monotonic() + timeout
    while True:
        try:
            api(s, "GET", "/api/v1/health")
            http("GET", s["router"] + "/healthz")
            return
        except Exception:  # noqa: BLE001 (keep polling until the deadline)
            if time.monotonic() > deadline:
                raise
            time.sleep(1)


# ─────────────────────────── state ───────────────────────────


def load_state(s):
    try:
        with open(s["state"], encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_api_key(s, key):
    os.makedirs(s["work"], mode=0o700, exist_ok=True)
    path = os.path.join(s["work"], "api_key")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key)
    return path


def read_api_key(s):
    try:
        with open(os.path.join(s["work"], "api_key"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


# ─────────────────────────── the developer's app ───────────────────────────


def sdk_path(s, python=None):
    """A directory with the Caliban Python SDK and its dependencies (pip --target), installed once
    from DEV_SDK_SRC. Returns None when `python` already imports it."""
    python = python or sys.executable
    if subprocess.run([python, "-c", "import caliban.admin"], capture_output=True).returncode == 0:
        return None
    target = os.path.join(s["work"], "pylib")
    marker = os.path.join(target, ".installed")
    if not os.path.exists(marker):
        os.makedirs(target, exist_ok=True)
        r = subprocess.run([python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check",
                            "--target", target, s["sdk_src"]], capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"pip install of the Caliban SDK from {s['sdk_src']} failed: {r.stderr[-1500:]}")
        open(marker, "w").close()
    return target


def app_env(s, api_key=None, python=None):
    env = {k: v for k, v in os.environ.items() if not k.startswith(("CALIBAN_", "DEV_"))}
    lib = sdk_path(s, python)
    env.update({
        "PYTHONPATH": os.pathsep.join(p for p in (lib, APP) if p),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUNBUFFERED": "1",
        "CALIBAN_BASE_URL": s["router"] + "/v1",
        "CALIBAN_ADMIN_URL": s["cp"],
        "CALIBAN_ADMIN_TOKEN": s["admin_token"],
        "DEV_TENANT": s["tenant"],
        "DEV_CHAT_MODEL": s["chat_model"],
        "DEV_EMBED_MODEL": s["embed_model"],
        "DEV_RERANK_MODEL": s["rerank_model"],
        "DEV_STATE": s["state"],
        "DEV_OUT": s["work"],
        "DEV_EVAL_K": s["eval_k"],
        "DEV_MIN_PASS_K": s["min_pass_k"],
        "DEV_MAX_P95_MS": s["max_p95_ms"],
    })
    key = api_key or read_api_key(s)
    if key:
        env["CALIBAN_API_KEY"] = key
    return env


def run_app(s, script, *args, api_key=None, python=None, timeout=3600):
    """Runs app/<script> like the developer does; returns (exit code, combined output)."""
    python = python or sys.executable
    r = subprocess.run([python, os.path.join(APP, script), *args], cwd=APP, env=app_env(s, api_key, python),
                       capture_output=True, text=True, timeout=timeout)
    return r.returncode, (r.stdout + (("\n" + r.stderr) if r.stderr.strip() else "")).rstrip()


def source(script, start=None, end=None):
    """Lines of an app script (for the terminal card), without the module docstring."""
    with open(os.path.join(APP, script), encoding="utf-8") as f:
        text = f.read()
    if text.startswith('"""'):
        text = text[text.index('"""', 3) + 3:].lstrip("\n")
    lines = text.splitlines()
    return lines[start:end]
