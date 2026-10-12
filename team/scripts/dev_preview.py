#!/usr/bin/env python3
"""
Developer preview of the mine map WITHOUT the platform: runs the simulator
in-process and serves the same /api/state the control service serves from
Ditto. Only for working on the page; the Submission itself always goes
through Ditto (see mine/control.py).

    python3 -m team.scripts.dev_preview   (from the repository root)
"""

import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
os.environ.setdefault("DITTO_PASSWORD", "x")

from mine import control  # noqa: E402
from mine.model import build_twins  # noqa: E402
from mine.sim import Mine  # noqa: E402

SPEEDUP = float(os.environ.get("SIM_SPEEDUP", "10"))


def main():
    mine = Mine(42)
    mine.run(1800)
    twins = build_twins()
    with control.cache.lock:
        control.cache.things = {k: {"attributes": v["attributes"], "features": v["features"]} for k, v in twins.items()}
        control.cache.loaded_at = time.time()

    class FakeMqtt:
        def publish(self, topic, payload, qos=0):
            if topic == control.COMMAND_TOPIC:
                print("command", payload, "->", mine.command(json.loads(payload)))

    control.mqtt_client = FakeMqtt()

    def loop():
        first = True
        sent = 0
        while True:
            mine.run(2 * SPEEDUP)
            snap = mine.snapshot(all_blocks=first)
            first = False
            with control.cache.lock:
                for name, feats in snap.items():
                    th = control.cache.things[f"{control.C.NAMESPACE}:{name}"]
                    th["features"] = {k: {"properties": v} for k, v in feats.items()}
                control.cache.updates += len(snap)
                if mine.events_total > sent:
                    for t_h, text in list(mine.events)[-(mine.events_total - sent):]:
                        control.cache.events.append({"t_h": t_h, "text": text})
                    sent = mine.events_total
            time.sleep(2)

    threading.Thread(target=loop, daemon=True).start()
    port = int(os.environ.get("PORT", "8095"))
    print(f"dev preview on http://localhost:{port}/")
    ThreadingHTTPServer(("127.0.0.1", port), control.Handler).serve_forever()


if __name__ == "__main__":
    main()
