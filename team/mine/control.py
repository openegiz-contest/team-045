#!/usr/bin/env python3
"""
Mine map and what-if scenario panel.

Serves a live 2D map of the Sarybulak pit. All data on the map comes from the
platform: on start the twins are loaded from Ditto's search API (attributes
hold the geometry, features the live state); afterwards Ditto's own change
events, which the platform republishes on MQTT `opentwins/twin/events/#`,
keep the cache current. The page polls /api/state once a second.

Scenario buttons POST /api/command; the command is published on MQTT
`sarybulak/commands`, where the simulator picks it up.
"""

import base64
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import paho.mqtt.client as mqtt

from . import config as C

log = logging.getLogger("control")
STATIC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "static")
DITTO_URL = os.environ.get("DITTO_URL", "http://localhost:8080").rstrip("/")
DITTO_AUTH = "Basic " + base64.b64encode(f"ditto:{os.environ.get('DITTO_PASSWORD', '')}".encode()).decode()
MQTT_HOST = os.environ.get("MQTT_HOST", "localhost")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
PORT = int(os.environ.get("PORT", "8095"))
COMMAND_TOPIC = "sarybulak/commands"
EVENT_TOPIC = "sarybulak/events"
DITTO_EVENTS = f"opentwins/twin/events/{C.NAMESPACE}/#"


class TwinCache:
    def __init__(self):
        self.lock = threading.Lock()
        self.things = {}           # thingId -> {"attributes": {...}, "features": {...}}
        self.events = deque(maxlen=60)
        self.loaded_at = 0.0
        self.updates = 0

    # -- full load from Ditto search --------------------------------------
    def load_from_ditto(self):
        q = urllib.parse.urlencode({"namespaces": C.NAMESPACE, "option": "size(200)",
                                    "fields": "thingId,attributes,features"})
        req = urllib.request.Request(f"{DITTO_URL}/api/2/search/things?{q}", headers={"Authorization": DITTO_AUTH})
        with urllib.request.urlopen(req, timeout=30) as r:
            items = json.load(r)["items"]
        with self.lock:
            for it in items:
                self.things[it["thingId"]] = {"attributes": it.get("attributes", {}), "features": it.get("features", {})}
            self.loaded_at = time.time()
        return len(items)

    # -- incremental update from a Ditto event ----------------------------
    def apply_event(self, msg):
        thing_id = msg.get("extra", {}).get("thingId")
        if not thing_id:
            ns, name = msg["topic"].split("/")[:2]
            thing_id = f"{ns}:{name}"
        path = msg.get("path", "/")
        value = msg.get("value")
        with self.lock:
            thing = self.things.setdefault(thing_id, {"attributes": {}, "features": {}})
            parts = [p for p in path.split("/") if p]
            if not parts:
                thing["attributes"] = value.get("attributes", thing["attributes"])
                thing["features"] = value.get("features", thing["features"])
            else:
                node = thing
                for p in parts[:-1]:
                    node = node.setdefault(p, {})
                node[parts[-1]] = value
            self.updates += 1

    def state(self):
        with self.lock:
            out = {}
            for tid, th in self.things.items():
                feats = {}
                units = {}
                for name, f in th.get("features", {}).items():
                    props = f.get("properties", {}) if isinstance(f, dict) else {}
                    feats[name] = props.get("value")
                    if "unit" in props:
                        units[name] = props["unit"]
                out[tid] = {"a": th.get("attributes", {}), "f": feats, "u": units}
            return {"now": time.time(), "loaded_at": self.loaded_at, "updates": self.updates,
                    "namespace": C.NAMESPACE, "twins": out, "events": list(self.events)}


cache = TwinCache()
mqtt_client = None


def mqtt_setup():
    global mqtt_client

    def on_connect(client, _u, _f, reason, _p=None):
        log.info("MQTT connected: %s", reason)
        client.subscribe([(DITTO_EVENTS, 0), (EVENT_TOPIC, 1)])

    def on_message(_c, _u, msg):
        try:
            payload = json.loads(msg.payload.decode())
        except Exception:  # noqa: BLE001
            return
        if msg.topic == EVENT_TOPIC:
            cache.events.append(payload)
        else:
            cache.apply_event(payload)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"sarybulak-control-{os.getpid()}")
    client.on_connect = on_connect
    client.on_message = on_message
    while True:
        try:
            client.connect(MQTT_HOST, MQTT_PORT, keepalive=30)
            break
        except OSError as exc:
            log.warning("MQTT not reachable (%s), retrying", exc)
            time.sleep(2)
    client.loop_start()
    mqtt_client = client


def ditto_loader():
    """Full reload from Ditto on start and every 60 s (search index catch-up, missed events)."""
    while True:
        try:
            n = cache.load_from_ditto()
            log.info("loaded %d twins from Ditto", n)
            if n >= 60:
                time.sleep(60)
            else:
                time.sleep(5)
        except Exception as exc:  # noqa: BLE001
            log.warning("Ditto not ready (%s), retrying", exc)
            time.sleep(5)


SCENARIOS = [
    {"id": "excavator_down:excavator-01", "title": "Отказ экскаватора ЭКГ-1 (уступ 545)",
     "cmd": {"action": "excavator_down", "target": "excavator-01", "on": True},
     "undo": {"action": "excavator_down", "target": "excavator-01", "on": False},
     "effect": "Диспетчер переназначает самосвалы на ЭКГ-2, очередь у забоя 530 растёт, добыча падает"},
    {"id": "excavator_down:excavator-02", "title": "Отказ экскаватора ЭКГ-2 (уступ 530)",
     "cmd": {"action": "excavator_down", "target": "excavator-02", "on": True},
     "undo": {"action": "excavator_down", "target": "excavator-02", "on": False},
     "effect": "Весь парк едет к ЭКГ-1; если он на породе, питание фабрики идёт только со склада"},
    {"id": "crusher_down", "title": "Остановка дробилки",
     "cmd": {"action": "crusher_down", "on": True}, "undo": {"action": "crusher_down", "on": False},
     "effect": "Рудные самосвалы разгружаются на рудный склад, фабрика останавливается, нагрузка подстанции падает"},
    {"id": "road_closed:road-ramp-west", "title": "Закрыть западный съезд (забой 545 → выезд)",
     "cmd": {"action": "road_closed", "target": "road-ramp-west", "on": True},
     "undo": {"action": "road_closed", "target": "road-ramp-west", "on": False},
     "effect": "Самосвалы с уступа 545 идут объездом через уступ 530 и восточный съезд: цикл длиннее, расход топлива выше"},
    {"id": "road_closed:road-main", "title": "Закрыть главную дорогу (выезд → развязка)",
     "cmd": {"action": "road_closed", "target": "road-main", "on": True},
     "undo": {"action": "road_closed", "target": "road-main", "on": False},
     "effect": "Объезда нет: гружёные самосвалы останавливаются, дробилка переходит на склад, затем голодает"},
    {"id": "fleet", "title": "Парк: 4 самосвала вместо 6",
     "cmd": {"action": "fleet", "count": 4}, "undo": {"action": "fleet", "count": C.TRUCK_COUNT},
     "effect": "Экскаваторы ждут самосвалы, производительность падает, очереди исчезают"},
    {"id": "fleet8", "title": "Парк: 3 самосвала",
     "cmd": {"action": "fleet", "count": 3}, "undo": {"action": "fleet", "count": C.TRUCK_COUNT},
     "effect": "Половина парка: дробилка голодает, склад разгружается на питание"},
    {"id": "cutoff", "title": "Бортовое содержание 0,45 % вместо 0,25 %",
     "cmd": {"action": "cutoff", "value": 0.45}, "undo": {"action": "cutoff", "value": C.CUTOFF_GRADE},
     "effect": "Бедные блоки становятся породой: коэффициент вскрыши растёт, содержание питания растёт, тонн руды меньше"},
    {"id": "cutoff_low", "title": "Бортовое содержание 0,15 %",
     "cmd": {"action": "cutoff", "value": 0.15}, "undo": {"action": "cutoff", "value": C.CUTOFF_GRADE},
     "effect": "Почти всё идёт в руду: питание фабрики беднеет, извлечение падает, концентрата не прибавляется"},
]


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):  # quieter access log
        if "/api/state" not in (args[0] if args else ""):
            log.info(fmt, *args)

    def _json(self, obj, status=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        path = os.path.normpath(os.path.join(STATIC, name))
        if not path.startswith(os.path.normpath(STATIC)) or not os.path.isfile(path):
            self.send_error(404)
            return
        with open(path, "rb") as f:
            body = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        p = urllib.parse.urlparse(self.path).path
        if p in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
        elif p == "/api/state":
            self._json(cache.state())
        elif p == "/api/scenarios":
            self._json(SCENARIOS)
        elif p == "/api/health":
            self._json({"ok": True, "twins": len(cache.things), "updates": cache.updates})
        elif p.startswith("/static/"):
            ext = p.rsplit(".", 1)[-1]
            self._file(p[len("/static/"):], {"js": "text/javascript", "css": "text/css", "svg": "image/svg+xml"}.get(ext, "application/octet-stream"))
        else:
            self.send_error(404)

    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        if p != "/api/command":
            self.send_error(404)
            return
        n = int(self.headers.get("Content-Length", "0"))
        try:
            cmd = json.loads(self.rfile.read(n).decode() or "{}")
        except json.JSONDecodeError:
            self._json({"ok": False, "error": "bad json"}, 400)
            return
        if not isinstance(cmd, dict) or "action" not in cmd:
            self._json({"ok": False, "error": "need {action: ...}"}, 400)
            return
        if mqtt_client is None:
            self._json({"ok": False, "error": "mqtt not connected"}, 503)
            return
        mqtt_client.publish(COMMAND_TOPIC, json.dumps(cmd), qos=1)
        log.info("command sent: %s", cmd)
        self._json({"ok": True, "sent": cmd})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    threading.Thread(target=ditto_loader, daemon=True).start()
    mqtt_setup()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    log.info("mine map on http://0.0.0.0:%d", PORT)
    server.serve_forever()


if __name__ == "__main__":
    main()
