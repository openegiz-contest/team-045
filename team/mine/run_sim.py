#!/usr/bin/env python3
"""
Запуск симулятора ВЖР и публикация features каждого двойника в MQTT командами
`modify` протокола Eclipse Ditto (топик telemetry/<twin>), как это делал бы
шлюз АСУ рудника. Ditto обновляет двойники; Telegraf и Grafana идут следом.

Сценарии «что если» и наряд мастера приходят в MQTT-топик `zhezkazgan/commands`
как JSON {"action": ..., ...} (см. Mine.command).
События уходят в `zhezkazgan/events`. Симулятор не обращается к Ditto и
InfluxDB напрямую.
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

log = logging.getLogger("zhezkazgan")
COMMAND_TOPIC = "zhezkazgan/commands"
EVENT_TOPIC = "zhezkazgan/events"


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
    interval = float(os.environ.get("SIM_INTERVAL", "2"))      # реальных секунд между публикациями
    speedup = float(os.environ.get("SIM_SPEEDUP", "20"))       # модельных секунд на реальную секунду
    seed = int(os.environ.get("SIM_SEED", "42"))
    warmup_h = float(os.environ.get("SIM_WARMUP_H", "2.0"))    # стенд начинается посреди смены
    full_every = float(os.environ.get("SIM_FULL_PUBLISH_S", "60"))

    mine = Mine(seed)
    mine.run(warmup_h * 3600, dt=10.0)
    commands = queue.Queue()

    def on_message(_client, _userdata, msg):
        try:
            commands.put(json.loads(msg.payload.decode()))
        except Exception as exc:  # noqa: BLE001
            log.warning("bad command %r: %s", msg.payload[:200], exc)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"zhezkazgan-sim-{os.getpid()}")
    client.on_message = on_message
    for _ in range(60):
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
    log.info("publishing to mqtt://%s:%s every %.1fs at %.0fx; commands on %s", host, port, interval, speedup, COMMAND_TOPIC)

    stop = False

    def on_signal(*_):
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    last_sent = {}
    last_full = 0.0
    sent_events = 0
    published = 0
    while not stop:
        t0 = time.time()
        while not commands.empty():
            cmd = commands.get()
            result = mine.command(cmd)
            log.info("command %s -> %s", cmd, result)
        mine.run(interval * speedup, dt=10.0)
        snap = mine.snapshot()
        full = (t0 - last_full) >= full_every
        n = 0
        for name, features in snap.items():
            if full or last_sent.get(name) != features:
                client.publish(f"telemetry/{name}", json.dumps(modify_features(name, features), ensure_ascii=False), qos=0)
                last_sent[name] = features
                n += 1
        if full:
            last_full = t0
        if mine.events_total > sent_events:
            fresh = list(mine.events)[-(mine.events_total - sent_events):]
            for t_h, text in fresh:
                client.publish(EVENT_TOPIC, json.dumps({"t_h": t_h, "text": text}, ensure_ascii=False), qos=1)
            sent_events = mine.events_total
        published += 1
        if published % 30 == 0:
            m = snap[C.MINE_ID]
            log.info("sim %6.1f h | shift %d | people %s | hoisted %s t | verdicts %s/%s/%s | alarm %s | scenarios: %s | %d twins sent",
                     m["sim_time_h"]["value"], m["shift_no"]["value"], m["people_underground"]["value"], m["ore_hoisted_t"]["value"],
                     m["chambers_allowed"]["value"], m["chambers_restricted"]["value"], m["chambers_forbidden"]["value"],
                     m["alarm"]["value"], m["active_scenarios"]["value"], n)
        time.sleep(max(0.0, interval - (time.time() - t0)))

    client.loop_stop()
    client.disconnect()
    return 0


if __name__ == "__main__":
    sys.exit(main())
