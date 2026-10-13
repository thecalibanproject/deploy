#!/usr/bin/env python3
"""Cedar Bank's refund system as an MCP server (Streamable HTTP, stateless, JSON responses).

Standard library only (Python 3.9+): no dependency to pin or download, so it runs in the stack's
python image as it is. One tool, `issue_refund`, a write. Caliban is the client (core
docs/tools.md): it registers the server, discovers and pins the manifest, and calls it from the
support-desk node once a human approved the call.

- Auth: `Authorization: Bearer <key>`, compared with the key in $BANK_OPS_KEY_FILE (written by
  e2e/fintech/seed.py, read on every request, so the server can start before the seed). No key
  file: every MCP request is refused (fail closed). Registered in Caliban with the API key method.
- Lifecycle: the legacy `initialize` handshake. A `server/discover` probe (MCP 2026-07-28) gets
  "method not found", which tells rmcp clients to fall back to `initialize`. GET and DELETE are
  405 (no server-sent stream, no sessions).
- The bank's own checks: the destination must be the account's IBAN on file, the amount at most
  2,000 EUR; a transaction is refunded once (a repeat returns the first refund). Every call is
  appended to $BANK_OPS_LEDGER (JSONL) as received, so check.py can prove what reached the bank.

    BANK_OPS_PORT=8000 BANK_OPS_KEY_FILE=... BANK_OPS_LEDGER=... python3 bank_ops.py
"""
import hashlib
import hmac
import json
import os
import re
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

KEY_FILE = os.environ.get("BANK_OPS_KEY_FILE", "/work/fintech/bank-ops.key")
LEDGER = os.environ.get("BANK_OPS_LEDGER", "/work/fintech/bank-ops-ledger.jsonl")
LOCK = threading.Lock()
VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
MAX_REFUND_EUR = 2000.0

TOOLS = [{
    "name": "issue_refund",
    "title": "Issue a refund",
    "description": "Refunds a card transaction to the customer's own account at Cedar Bank. Moves money: "
                   "the bank pays the amount to the destination IBAN.",
    "inputSchema": {
        "type": "object",
        "required": ["account_id", "transaction_id", "amount_eur", "destination_iban", "account_iban_on_file", "reason"],
        "additionalProperties": False,
        "properties": {
            "account_id": {"type": "string", "description": "Account the transaction was booked on (ACC-nnnn)"},
            "transaction_id": {"type": "string", "description": "The transaction to refund (TX-nnnnn)"},
            "amount_eur": {"type": "number", "minimum": 0, "description": "Amount to refund, in EUR"},
            "destination_iban": {"type": "string", "description": "Account the money is paid to"},
            "account_iban_on_file": {"type": "string", "description": "The account's IBAN as the bank's records hold it"},
            "reason": {"type": "string", "description": "Why the refund is due, one line"},
        },
    },
}]


def compact(iban):
    return re.sub(r"\s", "", str(iban or "")).upper()


def ledger(entry):
    os.makedirs(os.path.dirname(LEDGER) or ".", exist_ok=True)
    with LOCK, open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def previous(transaction_id):
    if not os.path.exists(LEDGER):
        return None
    with LOCK, open(LEDGER, encoding="utf-8") as f:
        for line in f:
            try:
                e = json.loads(line)
            except ValueError:
                continue
            if e.get("outcome") == "refunded" and e.get("args", {}).get("transaction_id") == transaction_id:
                return e.get("result")
    return None


def issue_refund(args):
    """(result, is_error)."""
    tx = str(args.get("transaction_id") or "")
    dest, on_file = compact(args.get("destination_iban")), compact(args.get("account_iban_on_file"))
    try:
        amount = float(args.get("amount_eur"))
    except (TypeError, ValueError):
        amount = -1.0
    if not re.fullmatch(r"TX-\d{5}", tx) or not re.fullmatch(r"ACC-\d{4}", str(args.get("account_id") or "")):
        return {"error": "unknown account or transaction"}, True
    if not dest or dest != on_file:
        return {"error": "refused: the destination is not the account's IBAN on file"}, True
    if not 0 < amount <= MAX_REFUND_EUR:
        return {"error": f"refused: amount outside (0, {MAX_REFUND_EUR:.0f}] EUR"}, True
    first = previous(tx)
    if first:
        return dict(first, repeated=True), False
    rid = "RF-" + hashlib.sha256(f"{tx}|{dest}|{amount:.2f}".encode()).hexdigest()[:8].upper()
    expected = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%d")
    return {"refund_id": rid, "status": "queued", "amount_eur": round(amount, 2), "transaction_id": tx,
            "expected_by": expected}, False


def authorized(header):
    try:
        with open(KEY_FILE, encoding="utf-8") as f:
            key = f.read().strip()
    except OSError:
        return False
    sent = header[7:].strip() if header.lower().startswith("bearer ") else ""
    return bool(key) and hmac.compare_digest(sent.encode(), key.encode())


def handle(msg):
    """The JSON-RPC response to one message, or None for a notification."""
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None
    if method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in VERSIONS else VERSIONS[0],
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "cedar-bank-ops", "version": "1.0.0"}}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name != "issue_refund":
            return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32602, "message": f"unknown tool {name}"}}
        out, is_error = issue_refund(args)
        ledger({"ts": time.time(), "tool": name, "args": args, "outcome": "error" if is_error else
                ("repeated" if out.get("repeated") else "refunded"), "result": out})
        result = {"content": [{"type": "text", "text": json.dumps(out)}], "structuredContent": out,
                  "isError": is_error}
    else:
        return {"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": f"method not found: {method}"}}
    return {"jsonrpc": "2.0", "id": mid, "result": result}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *a):
        sys.stderr.write("bank-ops: " + (fmt % a) + "\n")

    def reply(self, status, obj=None):
        data = b"" if obj is None else json.dumps(obj).encode()
        self.send_response(status)
        if obj is not None:
            self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        if self.path in ("/health", "/healthz"):
            return self.reply(200, {"ok": True, "key": os.path.exists(KEY_FILE)})
        self.reply(405, {"error": "no server-sent stream"})

    def do_DELETE(self):
        self.reply(405, {"error": "no sessions"})

    def do_POST(self):
        raw = self.rfile.read(int(self.headers.get("content-length") or 0))
        if self.path.rstrip("/") != "/mcp":
            return self.reply(404, {"error": "not found"})
        if not authorized(self.headers.get("authorization") or ""):
            return self.reply(401, {"jsonrpc": "2.0", "id": None, "error": {"code": -32001, "message": "unauthorized"}})
        try:
            msg = json.loads(raw or b"{}")
        except ValueError:
            return self.reply(400, {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}})
        if isinstance(msg, list):
            out = [r for r in (handle(m) for m in msg) if r is not None]
            return self.reply(200, out) if out else self.reply(202)
        r = handle(msg)
        return self.reply(202) if r is None else self.reply(200, r)


def main():
    port = int(os.environ.get("BANK_OPS_PORT", "8000"))
    host = os.environ.get("BANK_OPS_HOST", "0.0.0.0")
    srv = ThreadingHTTPServer((host, port), Handler)
    print(f"cedar bank ops MCP server on {host}:{port}/mcp, key file {KEY_FILE}, ledger {LEDGER}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
