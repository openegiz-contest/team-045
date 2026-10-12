#!/usr/bin/env python3
"""
Drive the Sarybulak simulator and publish every twin's features to MQTT as
Eclipse Ditto protocol `modify` commands (topic telemetry/<twin>), exactly as
a field gateway would. Ditto updates the twins; Telegraf and Grafana follow.

What-if scenarios arrive on MQTT topic `sarybulak/commands` as JSON
{"action": "...", ...} (see Mine.command). Operational events go out on
`sarybulak/events`. The simulator never talks to Ditto or InfluxDB directly.
"""

import json
import logging
import os
import queue
import signal
import sys
import time

import paho.mqtt.client as mqtt

from . import config as C
from .sim import Mine

log = logging.getLogger("sarybulak")
COMMAND_TOPIC = "sarybulak/commands"
EVENT_TOPIC = "sarybulak/events"


def modify_features(name, features):
    return {
        "topic": f"{C.NAMESPACE}/{name}/things/twin/commands/modify",
        "path": "/features",
        "value": {k: {"properties": v} for k, v in features.items()},
    }


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    host = os.environ.get("MQTT_HOST", "localhost")
    port = int(os.environ.get("MQTT_PORT", "1883"))
    interval = float(os.environ.get("SIM_INTERVAL", "2"))      # real seconds between publishes
    speedup = float(os.environ.get("SIM_SPEEDUP", "10"))       # simulated seconds per real second
    seed = int(os.environ.get("SIM_SEED", "42"))
    warmup_h = float(os.environ.get("SIM_WARMUP_H", "0.5"))    # start mid-shift so the stand is busy at once

    mine = Mine(seed)
    mine.run(warmup_h * 3600)
    commands = queue.Queue()

    def on_message(_client, _userdata, msg):
        try:
            cmd = json.loads(msg.payload.decode())
            commands.put(cmd)
        except Exception as exc:  # noqa: BLE001
            log.warning("bad command %r: %s", msg.payload[:200], exc)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sarybulak-sim-{os.getpid()}")
    client.on_message = on_message
    for attempt in range(60):
        try:
            client.connect(host, port, keepalive=30)
            break
        except OSError as exc:
            log.warning("MQTT %s:%s not reachable (%s), retrying", host, port, exc)
            time.sleep(2)
    else:
        log.error("giving up on MQTT %s:%s", host, port)
        return 1
    client.subscribe(COMMAND_TOPIC, qos=1)
    client.loop_start()
    log.info("publishing %d twins to mqtt://%s:%s every %.1fs at %.0fx", len(mine.blocks) + 40, host, port, interval, speedup)

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    first = True
    sent_events = 0
    published = 0
    while not stop:
        t0 = time.time()
        while not commands.empty():
            cmd = commands.get()
            result = mine.command(cmd)
            log.info("command %s -> %s", cmd, result)
            # events of the scenario go out immediately, before the next snapshot
        mine.run(interval * speedup, dt=2.0)
        snap = mine.snapshot(all_blocks=first)
        first = False
        for name, features in snap.items():
            client.publish(f"telemetry/{name}", json.dumps(modify_features(name, features)), qos=0)
        if mine.events_total > sent_events:
            fresh = list(mine.events)[-(mine.events_total - sent_events):]
            for t_h, text in fresh:
                client.publish(EVENT_TOPIC, json.dumps({"t_h": t_h, "text": text}, ensure_ascii=False), qos=1)
            sent_events = mine.events_total
        published += 1
        if published % 30 == 0:
            m = snap[C.MINE_ID]
            log.info("sim %6.1f h | ore %8.0f t | waste %8.0f t | conc %7.1f t | crusher %4.0f t/h | scenarios: %s",
                     m["sim_time_h"]["value"], m["ore_mined_t"]["value"], m["waste_mined_t"]["value"],
                     m["concentrate_t"]["value"], snap["crusher"]["throughput"]["value"], m["active_scenarios"]["value"])
        time.sleep(max(0.0, interval - (time.time() - t0)))

    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
