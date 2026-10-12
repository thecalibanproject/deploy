#!/usr/bin/env python3
"""Seeds what the operator gives a developer before the recording (admin API, stdlib only):

- the chat model the developer's code names (a shared provider and a catalogue entry with prices):
  Qwen3.8-27B through the stack's model recorder on the testbed, mock/model.py locally;
- tenant acme-dev (PII reversible), its default route to that model, node spend caps.

Idempotent: what exists is kept. The developer's API key, intents, nodes and documents are made
during the recording, from the console and from the app's code.
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
from dev import ApiError, api, settings, wait_healthy  # noqa: E402


def created(s, path, body, what):
    try:
        api(s, "POST", path, body)
        print(f"{what}: created")
        return True
    except ApiError as e:
        if e.status != 409:
            raise
        print(f"{what}: exists")
        return False


def main():
    s = settings()
    if not s["admin_token"]:
        sys.exit("CALIBAN_ADMIN_TOKEN is not set")
    wait_healthy(s)
    created(s, "/api/v1/providers", {"id": "dev-llm", "kind": "openai_compatible", "base_url": s["chat_base_url"],
                                     "trust_tier": "t0_sovereign", "cache_salt": s["target"] == "testbed"},
            f"provider dev-llm ({s['chat_base_url']})")
    caps = {"tools": True}
    if s["family"].startswith("qwen3"):
        # Thinking on or off per call: the gateway maps caliban.reasoning to enable_thinking.
        caps.update({"reasoning": "hybrid", "reasoning_control": "enable_thinking"})
    created(s, "/api/v1/models", {
        "id": s["chat_model"], "provider": "dev-llm", "upstream_model": s["upstream_model"], "kind": "chat",
        "family": s["family"] or None, "trust_tier": "t0_sovereign", "capabilities": caps, "context_window": 32768,
        "licence": "apache-2.0", "price_in_per_mtok": 0.20, "price_out_per_mtok": 0.80},
        f"model {s['chat_model']} -> {s['upstream_model']}")
    t = s["tenant"]
    settings_ = {"pii_default": "reversible", "node_spend_caps": {"daily_usd": 5, "monthly_usd": 50}}
    if not created(s, "/api/v1/tenants", dict(settings_, name=t), f"tenant {t}"):
        api(s, "PATCH", f"/api/v1/tenants/{t}", settings_)
    api(s, "PUT", f"/api/v1/tenants/{t}/routes", {"routes": [{"intent": "default", "models": [s["chat_model"]]}]})
    print(f"tenant {t}: default route -> {s['chat_model']}, node spend caps $5/day $50/month, PII reversible")


if __name__ == "__main__":
    main()
