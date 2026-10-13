"""Step 2: the support app's caliban/auto intents, through the tenant intents API.

    python intents.py define     # the first draft (data/intents.json)
    python intents.py teach      # add exemplars at runtime (data/intents.teach.json)
    python intents.py show

Routers rebuild their kNN index from the next snapshot: no config file, no restart.
"""
import sys

from common import TENANT, admin, data, refresh_routing, table


def show(adm):
    listed = adm.intents.list(TENANT)
    rows = [(i.intent, len(i.exemplars or []), i.source, i.node or "a model answers")
            for i in listed.intents if i.source == "api"]
    print(table(rows, ["intent", "exemplars", "source", "hands to"]))


def main(cmd):
    adm = admin()
    if cmd == "define":
        for intent, exemplars in data("intents.json")["intents"].items():
            r = adm.intents.set(TENANT, intent, exemplars)
            print(f"PUT intents/{intent:<17} {len(r.exemplars)} exemplars")
    elif cmd == "teach":
        current = {i.intent: list(i.exemplars or []) for i in adm.intents.list(TENANT).intents if i.source == "api"}
        for intent, more in data("intents.teach.json")["intents"].items():
            merged = current.get(intent, []) + [e for e in more if e not in current.get(intent, [])]
            r = adm.intents.set(TENANT, intent, merged)
            print(f"PUT intents/{intent:<17} {len(current.get(intent, []))} -> {len(r.exemplars)} exemplars")
    # Routers take the change with their next snapshot (standalone: at once; split mode: within
    # CALIBAN_SNAPSHOT_POLL_SECS) and re-embed the exemplars in the background.
    if cmd in ("define", "teach"):
        refresh_routing()
    print()
    show(adm)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "show")
