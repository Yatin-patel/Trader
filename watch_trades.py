"""Tail-watcher for executed trades. Emits one stdout line per real submission.

Designed for the Monitor tool — each line becomes a notification.
Filters to project Test 2, Executor.EXECUTE events with status != DRY_RUN.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

PROJECT_ID = "Test 2"
URL = f"http://127.0.0.1:8000/api/projects/{urllib.parse.quote(PROJECT_ID)}/events?limit=50"
POLL_SECONDS = 30


def main() -> None:
    last_seen = 0
    # Skip events already in the log before we started watching.
    try:
        with urllib.request.urlopen(URL, timeout=10) as r:
            events = json.loads(r.read())
        if events:
            last_seen = max(e["event_id"] for e in events)
    except Exception:
        pass

    print(f"WATCHING: project={PROJECT_ID} from event_id>{last_seen}", flush=True)

    while True:
        try:
            with urllib.request.urlopen(URL, timeout=10) as r:
                events = json.loads(r.read())
            new_events = sorted(
                (e for e in events if e["event_id"] > last_seen),
                key=lambda x: x["event_id"],
            )
            for e in new_events:
                last_seen = max(last_seen, e["event_id"])
                if e.get("node_name") != "Executor" or e.get("event_type") != "EXECUTE":
                    continue
                payload = e.get("payload") or {}
                results = payload.get("results") or []
                for res in results:
                    status = res.get("status", "?")
                    if status == "DRY_RUN":
                        continue
                    t = res.get("trade") or {}
                    o = res.get("order") or {}
                    ticker = t.get("ticker", "?")
                    typ = t.get("type", "?")
                    strike = t.get("strike", "?")
                    qty = res.get("qty", 1)
                    prem = t.get("premium", "?")
                    oid = o.get("id", "n/a") if isinstance(o, dict) else "n/a"
                    print(
                        f"TRADE {status}: {ticker} {typ} strike={strike} qty={qty} "
                        f"premium={prem} order={oid} at {e['created_at']}",
                        flush=True,
                    )
        except Exception as ex:
            # transient HTTP / DB errors — silently retry
            pass
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
