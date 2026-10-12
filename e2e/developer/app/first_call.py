"""Step 1: the first call through Caliban, with a plain catalogue model.

    python first_call.py
"""
from common import CHAT_MODEL, Timed, ms, short

c = Timed()
resp, t = c.chat(
    model=CHAT_MODEL,
    messages=[{"role": "user", "content": "In two sentences: what can an AI assistant do for a SaaS support team?"}],
    max_tokens=200,
    caliban={"reasoning": "off"},  # a hybrid model (Qwen3) answers without thinking first
)
meta = resp.caliban
print("answer        ", short(resp.text, 300))
print("routed model  ", meta.routed_model)
print("intent        ", meta.intent.intent if meta.intent else "-",
      f"(stage {meta.intent.stage})" if meta.intent else "")
print("cost          ", f"${meta.cost_usd:.6f}" if meta.cost_usd is not None else "-")
print("request id    ", meta.request_id)
print("tokens        ", f"{resp.usage.prompt_tokens} in, {resp.usage.completion_tokens} out" if resp.usage else "-")
print("client time   ", ms(t["client_ms"]), "ms")
if t["timing"]:
    print("server timing ", ", ".join(f"{k} {ms(v.get('dur'))}" + (f" ({v['desc']})" if v.get("desc") else "")
                                      for k, v in t["timing"].items()))
else:
    print("server timing ", "(no Server-Timing header from this gateway)")
