"""Step 4: what the support app costs, by node and by intent, against the tenant's node budget.

    python usage.py

GET /api/v1/usage (by_node: totals per node version; by_route: requests per day, intent, stage
and fallback) and GET /api/v1/tenants/{t}/node-spend (today and this month against the caps).
"""
from common import TENANT, admin, raw, table


def money(v):
    return "-" if v is None else f"${v:.4f}"


def main():
    adm = admin()
    u = raw(adm, "GET", "usage", params={"tenant_id": TENANT, "limit": 1})
    tot = u.get("totals") or {}
    print(f"tenant {TENANT}: {tot.get('requests')} requests, {tot.get('auto_requests')} through caliban/auto, "
          f"charged {money(tot.get('charged_usd'))}, routed models' cost {money(tot.get('routed_model_cost_usd'))}, "
          f"margin {money(tot.get('margin_usd'))}")
    print("\nby node")
    rows = [(n.get("node") or "(no node)", n.get("node_version") or "-", n["totals"].get("requests"),
             n["totals"].get("prompt_tokens", 0) + n["totals"].get("completion_tokens", 0),
             money(n["totals"].get("cost_usd")), money(n["totals"].get("charged_usd")))
            for n in u.get("by_node") or []]
    print(table(rows, ["node", "version", "model calls", "tokens", "model cost", "charged"]))
    print("\nby intent (caliban/auto routing decisions, daily counts)")
    rows = [(r.get("day"), r.get("intent"), r.get("route_stage"), r.get("route_fallback") or "-", r.get("requests"))
            for r in u.get("by_route") or [] if r.get("tenant_id") in (None, TENANT)]
    print(table(sorted(rows, key=lambda r: (str(r[0]), str(r[1]))), ["day", "intent", "stage", "fallback", "requests"]))
    s = raw(adm, "GET", f"tenants/{TENANT}/node-spend")
    caps, rem = s.get("caps") or {}, s.get("remaining") or {}
    print(f"\nnode budget: today {money(s.get('today_usd'))} of {money(caps.get('daily_usd'))} "
          f"(left {money(rem.get('daily_usd'))}), month {money(s.get('month_usd'))} of {money(caps.get('monthly_usd'))}"
          f"{'  CAPPED' if s.get('capped') else ''}")


if __name__ == "__main__":
    main()
