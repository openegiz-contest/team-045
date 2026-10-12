#!/usr/bin/env python3
"""
Generate the Grafana dashboards of the Sarybulak mine as JSON files in
team/grafana/dashboards/. They are provisioned by team/grafana/provisioning.yaml
(mounted by team/compose.yml). Run after editing:

    python3 team/grafana/build_dashboards.py

Data: InfluxDB bucket `default`, measurement `mqtt_consumer`, tag `thingId`,
field `value_<feature>_properties_value` (what Telegraf writes from Ditto's
change events). Datasource uid `opentwins-influxdb` is provisioned by OpenEgiz.
"""

import json
import os

NS = "org.openegiz.sarybulak"
NS_RE = NS.replace(".", "\\.")
DS = {"type": "influxdb", "uid": "opentwins-influxdb"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def flux_one(thing, feature):
    return (f'from(bucket: "default")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r.thingId == "{NS}:{thing}" and r._field == "value_{feature}_properties_value")\n'
            f'  |> keep(columns: ["_time", "_value"])')


def flux_many(regex, feature, label="twin"):
    return (f'from(bucket: "default")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r.thingId =~ /^{NS_RE}:{regex}/ and r._field == "value_{feature}_properties_value")\n'
            f'  |> map(fn: (r) => ({{_time: r._time, _value: r._value, {label}: r.thingId}}))\n'
            f'  |> group(columns: ["{label}"])\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)')


def flux_last_table(regex, features):
    fl = " or ".join(f'r._field == "value_{f}_properties_value"' for f in features)
    return (f'from(bucket: "default")\n  |> range(start: -5m)\n'
            f'  |> filter(fn: (r) => r.thingId =~ /^{NS_RE}:{regex}/ and ({fl}))\n'
            f'  |> group(columns: ["thingId", "_field"])\n  |> last()\n  |> group()\n'
            f'  |> pivot(rowKey: ["thingId"], columnKey: ["_field"], valueColumn: "_value")\n'
            f'  |> drop(columns: ["_start", "_stop", "_time", "_measurement", "parent", "originator", "correlationId", "topic", "host"])')


def target(q, ref="A"):
    return {"datasource": DS, "query": q, "refId": ref}


def stat(title, q, unit="", x=0, y=0, w=4, h=4, decimals=None, color="blue", thresholds=None):
    p = {"id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(q)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": False}, "colorMode": "value",
                     "graphMode": "area", "textMode": "value", "justifyMode": "auto"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color}}, "overrides": []}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if thresholds:
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return p


def ts(title, queries, unit="", x=0, y=0, w=12, h=8, stacked=False, min_=None, max_=None, legend_right=False):
    p = {"id": nid(), "type": "timeseries", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(q, chr(65 + i)) for i, q in enumerate(queries)],
         "options": {"legend": {"displayMode": "list", "placement": "right" if legend_right else "bottom", "showLegend": True},
                     "tooltip": {"mode": "multi", "sort": "desc"}},
         "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 12 if not stacked else 60,
                                                                  "stacking": {"mode": "normal" if stacked else "none"},
                                                                  "spanNulls": True}},
                         "overrides": []}}
    if min_ is not None:
        p["fieldConfig"]["defaults"]["min"] = min_
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
    return p


def table(title, q, x=0, y=0, w=24, h=9, renames=None):
    p = {"id": nid(), "type": "table", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(q)], "options": {"showHeader": True, "cellHeight": "sm"},
         "fieldConfig": {"defaults": {}, "overrides": []},
         "transformations": [{"id": "organize", "options": {"renameByName": renames or {}}}]}
    return p


def text(title, content, x=0, y=0, w=24, h=4, mode="markdown"):
    return {"id": nid(), "type": "text", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": mode, "content": content}}


def dashboard(uid, title, panels, refresh="5s", rng="30m", tags=None):
    return {"uid": uid, "title": title, "tags": tags or ["sarybulak", "openegiz"], "timezone": "browser", "editable": True,
            "refresh": refresh, "schemaVersion": 39, "version": 1, "time": {"from": f"now-{rng}", "to": "now"},
            "panels": panels, "templating": {"list": []}, "annotations": {"list": []}, "links": [
                {"title": "Карта карьера", "type": "link", "url": "http://localhost:8095/", "targetBlank": True},
                {"title": "Обзор", "type": "link", "url": "/d/sarybulak-overview"},
                {"title": "Парк", "type": "link", "url": "/d/sarybulak-fleet"},
                {"title": "Фабрика и энергия", "type": "link", "url": "/d/sarybulak-plant-energy"},
                {"title": "Карта в Grafana", "type": "link", "url": "/d/sarybulak-map"}]}


def overview():
    p = [
        stat("Руда добыта", flux_one("mine", "ore_mined_t"), "t", 0, 0, 4, 4, 0, "orange"),
        stat("Порода", flux_one("mine", "waste_mined_t"), "t", 4, 0, 4, 4, 0, "text"),
        stat("Коэффициент вскрыши", flux_one("mine", "strip_ratio"), "", 8, 0, 4, 4, 2, "text"),
        stat("Концентрат", flux_one("mine", "concentrate_t"), "t", 12, 0, 4, 4, 1, "green"),
        stat("Медь в концентрате", flux_one("mine", "copper_in_concentrate_t"), "t", 16, 0, 4, 4, 2, "green"),
        stat("Активные сценарии", flux_one("mine", "active_scenarios"), "", 20, 0, 4, 4, None, "yellow"),
        stat("Дробилка, т/ч", flux_one("crusher", "throughput"), "", 0, 4, 4, 4, 0, "blue",
             [{"color": "red", "value": None}, {"color": "yellow", "value": 300}, {"color": "green", "value": 700}]),
        stat("Питание фабрики, % Cu", flux_one("crusher", "feed_grade"), "", 4, 4, 4, 4, 3, "blue",
             [{"color": "red", "value": None}, {"color": "green", "value": 0.34}, {"color": "red", "value": 0.50}]),
        stat("Извлечение, %", flux_one("flotation", "recovery"), "", 8, 4, 4, 4, 1, "green"),
        stat("Нагрузка подстанции, кВт", flux_one("substation", "load"), "", 12, 4, 4, 4, 0, "purple"),
        stat("Дизель, л/ч", flux_one("substation", "diesel_rate"), "", 16, 4, 4, 4, 0, "orange"),
        stat("Готовность парка, %", flux_one("mine", "fleet_availability_pct"), "", 20, 4, 4, 4, 0, "green",
             [{"color": "red", "value": None}, {"color": "yellow", "value": 70}, {"color": "green", "value": 90}]),
        ts("Дробилка: производительность и питание", [flux_one("crusher", "throughput"), flux_one("crusher", "feed_grade")], "", 0, 8, 12, 8),
        ts("Очереди у забоев и дробилки (самосвалов)", [flux_many("excavator-", "queue"), flux_one("crusher", "queue")], "", 12, 8, 12, 8),
        ts("Содержание в забоях, % Cu", [flux_many("excavator-", "face_grade")], "", 0, 16, 12, 8),
        ts("Рудный склад: тонны и содержание", [flux_one("stockpile", "tonnes"), flux_one("stockpile", "grade")], "", 12, 16, 12, 8),
        ts("Цикл самосвала и ожидание, мин", [flux_one("dispatcher", "avg_cycle_min"), flux_one("dispatcher", "avg_queue_min")], "", 0, 24, 12, 8),
        ts("Добыча нарастающим итогом, т", [flux_one("mine", "ore_mined_t"), flux_one("mine", "waste_mined_t")], "t", 12, 24, 12, 8),
    ]
    # fix the stat with a string value: text mode
    p[5]["options"]["textMode"] = "value"
    p[5]["options"]["graphMode"] = "none"
    p[5]["options"]["colorMode"] = "none"
    p[1]["fieldConfig"]["defaults"]["color"] = {"mode": "fixed", "fixedColor": "text"}
    return dashboard("sarybulak-overview", "Sarybulak: обзор карьера", p)


def fleet():
    p = [
        table("Парк самосвалов: последнее состояние", flux_last_table("truck-", ["state", "payload", "speed", "fuel", "fuel_rate", "trips_total", "hours", "health_pct", "destination", "assigned_excavator"]), 0, 0, 24, 9,
              {"thingId": "Самосвал", "value_state_properties_value": "Состояние", "value_payload_properties_value": "Груз, т",
               "value_speed_properties_value": "Скорость, км/ч", "value_fuel_properties_value": "Топливо, л",
               "value_fuel_rate_properties_value": "Расход, л/ч", "value_trips_total_properties_value": "Рейсов",
               "value_hours_properties_value": "Наработка, ч", "value_health_pct_properties_value": "Состояние, %",
               "value_destination_properties_value": "Куда", "value_assigned_excavator_properties_value": "Экскаватор"}),
        ts("Скорость самосвалов, км/ч", [flux_many("truck-", "speed")], "", 0, 9, 12, 8),
        ts("Груз в кузове, т", [flux_many("truck-", "payload")], "t", 12, 9, 12, 8),
        ts("Топливо в баке, л", [flux_many("truck-", "fuel")], "", 0, 17, 12, 8, min_=0),
        ts("Рейсов, нарастающим итогом", [flux_many("truck-", "trips_total")], "", 12, 17, 12, 8),
        ts("Экскаваторы: загрузка, %", [flux_many("excavator-", "utilization_pct")], "percent", 0, 25, 12, 8, min_=0, max_=100),
        ts("Экскаваторы: очередь самосвалов", [flux_many("excavator-", "queue")], "", 12, 25, 12, 8),
        ts("Трафик по дорогам (самосвалов на участке)", [flux_many("road-", "trucks_on_road")], "", 0, 33, 12, 8),
        ts("Техническое состояние техники, %", [flux_many("truck-", "health_pct"), flux_many("excavator-", "health_pct")], "percent", 12, 33, 12, 8, min_=0, max_=100),
        table("Экскаваторы", flux_last_table("excavator-", ["state", "block", "face_grade", "passes_total", "tonnes_loaded", "queue", "utilization_pct", "hours"]), 0, 41, 24, 5,
              {"thingId": "Экскаватор", "value_state_properties_value": "Состояние", "value_block_properties_value": "Блок",
               "value_face_grade_properties_value": "Содержание, % Cu", "value_passes_total_properties_value": "Ковшей",
               "value_tonnes_loaded_properties_value": "Погружено, т", "value_queue_properties_value": "Очередь",
               "value_utilization_pct_properties_value": "Загрузка, %", "value_hours_properties_value": "Наработка, ч"}),
    ]
    return dashboard("sarybulak-fleet", "Sarybulak: транспорт и диспетчеризация", p)


def plant_energy():
    p = [
        stat("Мельницы, т/ч", flux_one("mill", "throughput"), "", 0, 0, 4, 4, 0, "blue"),
        stat("Удельная энергия измельчения, кВт·ч/т", flux_one("mill", "specific_energy"), "", 4, 0, 4, 4, 2, "purple"),
        stat("Извлечение, %", flux_one("flotation", "recovery"), "", 8, 0, 4, 4, 1, "green"),
        stat("Концентрат, т/ч", flux_one("flotation", "concentrate_tph"), "", 12, 0, 4, 4, 1, "green"),
        stat("Хвосты, % Cu", flux_one("flotation", "tailings_grade"), "", 16, 0, 4, 4, 3, "text"),
        stat("Электроэнергия всего, кВт·ч", flux_one("substation", "energy_total"), "", 20, 0, 4, 4, 0, "purple"),
        ts("Мощность: дробилка, мельницы, подстанция, кВт", [flux_one("crusher", "power"), flux_one("mill", "power"), flux_one("substation", "load")], "kwatt", 0, 4, 12, 8),
        ts("Питание фабрики и извлечение", [flux_one("mill", "feed_grade"), flux_one("flotation", "recovery")], "", 12, 4, 12, 8),
        ts("Дизель: расход парка, л/ч", [flux_one("substation", "diesel_rate")], "", 0, 12, 12, 8),
        ts("Расход топлива по самосвалам, л/ч", [flux_many("truck-", "fuel_rate")], "", 12, 12, 12, 8),
        ts("Загрузка подстанции, %", [flux_one("substation", "load_pct")], "percent", 0, 20, 12, 8, min_=0, max_=100),
        ts("CO₂ нарастающим итогом, т", [flux_one("substation", "co2_total")], "", 12, 20, 12, 8),
        ts("Бункер дробилки, т", [flux_one("crusher", "bin_level")], "t", 0, 28, 12, 8, min_=0),
        ts("Концентрат нарастающим итогом, т", [flux_one("flotation", "concentrate_total")], "t", 12, 28, 12, 8),
    ]
    return dashboard("sarybulak-plant-energy", "Sarybulak: фабрика и энергетика", p)


def mapdash():
    p = [
        text("Карта карьера (живая, из Ditto)",
             '<iframe src="http://localhost:8095/" style="width:100%;height:100%;border:0;background:#0f1419" title="Sarybulak mine map"></iframe>',
             0, 0, 24, 30, mode="html"),
        text("Как читать карту",
             "Карта строится из атрибутов двойников (геометрия уступов, блоков, дорог, объектов) и их features (состояния). "
             "Самосвалы движутся по координатам из Ditto; цвет блока — содержание меди, штриховка — бурение и заряжание, "
             "белая рамка — отрабатываемый блок. Кнопки справа запускают сценарии «что если» и отменяют их. "
             "Если iframe не отображается, откройте карту напрямую: http://localhost:8095/", 0, 30, 24, 4),
    ]
    return dashboard("sarybulak-map", "Sarybulak: карта карьера", p, refresh="")


def main():
    os.makedirs(OUT, exist_ok=True)
    for d in (overview(), fleet(), plant_energy(), mapdash()):
        path = os.path.join(OUT, f"{d['uid']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(d, f, ensure_ascii=False, indent=1)
        print("wrote", path, len(d["panels"]), "panels")


if __name__ == "__main__":
    main()
