"""Shared settings and helpers of the law firm scenario (stdlib only, Python 3.9+).

Used by demo/scenario.py (inside the recorder), seed.py and check.py (on the gateway host, with the
shared e2e stack's environment: CALIBAN_ADMIN_TOKEN, E2E_CP_URL, E2E_ROUTER_URL, E2E_WORK_DIR,
E2E_TARGET and the E2E_* model settings).
"""
import copy
import json
import os
import re
import time
import urllib.error
import urllib.request

LAW = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
E2E = os.path.dirname(LAW)

TENANT = os.environ.get("E2E_LAW_TENANT", "cabinet-hadad")
TESTBED = os.environ.get("E2E_TARGET") == "testbed"

# The chat model. Locally: the law scenario's own scripted model (mock/model.py, the compose service
# caliban-e2e-law-model, see docker-compose.law.yml). On the testbed: the shared recorder in front of
# Qwen3.8 on the GPU host (E2E_CHAT_BASE_URL), so the real model answers every call and the
# recorder logs what it received.
PROVIDER = os.environ.get("E2E_LAW_PROVIDER", "law-llm")
CHAT_ID = os.environ.get("E2E_LAW_CHAT_MODEL_ID", "law/chat")
CHAT_BASE_URL = os.environ.get("E2E_LAW_CHAT_BASE_URL") or (
    os.environ.get("E2E_CHAT_BASE_URL", "http://caliban-e2e-model:8000/v1") if TESTBED
    else "http://caliban-e2e-law-model:8000/v1")
UPSTREAM = os.environ.get("E2E_LAW_UPSTREAM_MODEL") or (
    os.environ.get("E2E_UPSTREAM_MODEL", "Qwen/Qwen3.8-27B-FP8") if TESTBED else "law-scripted")
FAMILY = os.environ.get("E2E_LAW_MODEL_FAMILY") or (os.environ.get("E2E_MODEL_FAMILY", "qwen3.8") if TESTBED else "")
TRUST_TIER = os.environ.get("E2E_CHAT_TRUST_TIER", "t0_sovereign")
PRICE_IN = float(os.environ.get("E2E_PRICE_IN_PER_MTOK", "0.2"))
PRICE_OUT = float(os.environ.get("E2E_PRICE_OUT_PER_MTOK", "0.8"))

# Retrieval: the shared stack's embedder (locally the scripted model's hashed vectors, on the
# testbed Qwen3-Embedding-0.6B) and, on the testbed, Qwen3-Reranker-0.6B.
COLLECTION = os.environ.get("E2E_LAW_RAG_COLLECTION", "legal_texts")
EMBED_MODEL = os.environ.get("E2E_RAG_EMBED_MODEL") or os.environ.get("E2E_ROUTING_EMBED_MODEL") or "e2e/embed"
RERANK_MODEL = os.environ.get("E2E_RAG_RERANK_MODEL", "")

# The practice database: MongoDB database `lawfirm` in the shared replica set, read by its own
# read-only principal.
DATASOURCE = "practice"
DATABASE = "lawfirm"
READER = "caliban_law_reader"
NODES = ("matter-desk", "research", "legal-assistant")
INTENT = "legal"
APP_KEY_NAME = "hadad-intranet"
ATTACK_MARKERS = ("zx-canary-law1", "lexvault-archive.example", "+33 7 56 12 98 40", "send the full case file")


def reader_password():
    return os.environ.get("E2E_LAW_READER_PASSWORD") or os.environ.get("E2E_MONGO_READER_PASSWORD", "")


def mongo_uri(password=None):
    pw = reader_password() if password is None else password
    return os.environ.get("E2E_LAW_MONGO_URI") or (
        f"mongodb://{READER}:{pw}@caliban-e2e-mongo:27017,caliban-e2e-mongo-analytics:27017/"
        f"?replicaSet=rs0&authSource={DATABASE}")


def path(*parts):
    return os.path.join(LAW, *parts)


def load(rel):
    with open(path(rel), encoding="utf-8") as f:
        return json.load(f)


def read_text(rel):
    with open(path(rel), encoding="utf-8") as f:
        return f.read()


def work_dir():
    return os.environ.get("E2E_WORK_DIR") or os.path.join(E2E, "health-triage", ".work")


def state_path():
    return os.path.join(work_dir(), "law", "state.json")


def load_state():
    try:
        with open(state_path(), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(st):
    os.makedirs(os.path.dirname(state_path()), exist_ok=True)
    fd = os.open(state_path(), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2, ensure_ascii=False)


def model_logs():
    """Request logs of the model: the law scripted model's, and the shared recorder's (testbed)."""
    d = os.path.join(work_dir(), "model-log")
    return [p for p in (os.path.join(d, "law-requests.jsonl"), os.path.join(d, "requests.jsonl")) if os.path.exists(p)]


def reasoning(name):
    v = os.environ.get(f"E2E_LAW_{name}_REASONING", "off")
    if v not in ("off", "low", "medium", "high"):
        raise SystemExit(f"E2E_LAW_{name}_REASONING must be off, low, medium or high (got {v!r})")
    return v


def render_spec(name, ontology_version=None, versions=None):
    """The node spec as published: the chat model, the ontology version the reviewer approved,
    the collection, the pinned subnode versions and each vertex's reasoning effort (off by
    default: classifying and answering from returned rows need no thinking, research note 02)."""
    text = read_text(f"node/{name}.node.json")
    repl = {"__CHAT_MODEL__": CHAT_ID, "__RAG_COLLECTION__": COLLECTION,
            "__TOPIC_REASONING__": reasoning("TOPIC"), "__ANSWER_REASONING__": reasoning("ANSWER"),
            "__INTAKE_REASONING__": reasoning("INTAKE")}
    if ontology_version:
        repl["__ONTOLOGY_VERSION__"] = ontology_version
    for k, v in (versions or {}).items():
        repl[f"__{k.upper().replace('-', '_')}_VERSION__"] = str(v)
    for k, v in repl.items():
        text = text.replace(k, v)
    left = sorted(set(re.findall(r"__[A-Z][A-Z_]*__", text)))
    if left:
        raise ValueError(f"{name}: placeholders not filled: {left}")
    spec = json.loads(text)
    # The answer vertex answers with the node's output schema (constrained decoding), copied so the
    # two never drift apart.
    for v in (spec.get("graph") or {}).get("vertices") or []:
        if v["id"] == "answer":
            v["config"]["output_schema"] = copy.deepcopy(spec["prompt"]["output_schema"])
    return spec


def covers(current, wanted):
    """Whether a stored value says what the file says (keys the file leaves out may be filled in)."""
    if isinstance(wanted, dict):
        return isinstance(current, dict) and all(covers(current.get(k), v) for k, v in wanted.items())
    return current == wanted


def parse_json(content):
    if isinstance(content, dict):
        return content
    try:
        v = json.loads(content)
        return v if isinstance(v, dict) else None
    except (TypeError, ValueError):
        return None


# ─────────────────────────── stdlib HTTP (seed.py and check.py) ───────────────────────────


class HttpError(Exception):
    def __init__(self, method, url, status, body):
        super().__init__(f"{method} {url}: HTTP {status}: {json.dumps(body, ensure_ascii=False)[:600]}")
        self.status, self.body = status, body


def http(method, url, token=None, body=None, ok=(200, 201, 202, 204), timeout=120):
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("content-type", "application/json")
    if token:
        req.add_header("authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            status, raw, hdrs = r.status, r.read(), {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        status, raw, hdrs = e.code, e.read(), {k.lower(): v for k, v in e.headers.items()}
    text = raw.decode("utf-8", "replace")
    try:
        parsed = json.loads(text) if text else None
    except ValueError:
        parsed = text
    if ok is not None and status not in ok:
        raise HttpError(method, url, status, parsed)
    return status, hdrs, parsed


def cp(method, p, body=None, ok=(200, 201, 202, 204)):
    base = os.environ.get("E2E_CP_URL", "http://127.0.0.1:18081").rstrip("/")
    return http(method, base + p, os.environ.get("CALIBAN_ADMIN_TOKEN", ""), body, ok)[2]


def dp(method, p, key, body=None, ok=(200, 201, 202, 204)):
    base = os.environ.get("E2E_ROUTER_URL", "http://127.0.0.1:18080").rstrip("/")
    return http(method, base + p, key, body, ok)


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
