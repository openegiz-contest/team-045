#!/usr/bin/env python3
"""
Дашборды Grafana для горизонта ВЖР: JSON в team/grafana/dashboards/, которые
подключает team/grafana/provisioning/vzhr.yaml (монтируется team/compose.yml).
После правки:

    python3 team/grafana/build_dashboards.py

Данные: InfluxDB bucket `default`, measurement `mqtt_consumer`, тег `thingId`,
поле `value_<feature>_properties_value` (так Telegraf пишет события Ditto).
Источник данных uid `opentwins-influxdb` создаёт OpenEgiz.
"""

import json
import os
import time

NS = "org.openegiz.zhezkazgan"
NS_RE = NS.replace(".", "\\.")
DS = {"type": "influxdb", "uid": "opentwins-influxdb"}
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboards")
_id = [0]


def nid():
    _id[0] += 1
    return _id[0]


def flux_one(thing, feature, label=None):
    """Одна величина одного двойника; label подписывает линию на графике с несколькими запросами."""
    name = label or f"{thing}.{feature}"
    return (f'from(bucket: "default")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r.thingId == "{NS}:{thing}" and r._field == "value_{feature}_properties_value")\n'
            f'  |> map(fn: (r) => ({{_time: r._time, _value: r._value, _field: "{name}"}}))\n  |> group(columns: ["_field"])')


def flux_many(regex, feature, label="twin"):
    return (f'from(bucket: "default")\n  |> range(start: v.timeRangeStart, stop: v.timeRangeStop)\n'
            f'  |> filter(fn: (r) => r.thingId =~ /^{NS_RE}:{regex}/ and r._field == "value_{feature}_properties_value")\n'
            f'  |> map(fn: (r) => ({{_time: r._time, _value: r._value, {label}: r.thingId}}))\n'
            f'  |> group(columns: ["{label}"])\n  |> aggregateWindow(every: v.windowPeriod, fn: last, createEmpty: false)')


def flux_last_table(regex, features, kind="num"):
    """Последние значения features по двойникам в одну таблицу. Flux не умеет pivot со
    смешанными типами, поэтому числовые и текстовые поля идут в разные таблицы."""
    fl = " or ".join(f'r._field == "value_{f}_properties_value"' for f in features)
    typ = 'types.isNumeric(v: r._value)' if kind == "num" else '(types.isType(v: r._value, type: "string") or types.isType(v: r._value, type: "bool"))'
    conv = '' if kind == "num" else '  |> map(fn: (r) => ({r with _value: string(v: r._value)}))\n'
    return (f'import "types"\nfrom(bucket: "default")\n  |> range(start: -10m)\n'
            f'  |> filter(fn: (r) => r.thingId =~ /^{NS_RE}:{regex}/ and ({fl}))\n'
            f'  |> filter(fn: (r) => {typ})\n{conv}'
            f'  |> group(columns: ["thingId", "_field"])\n  |> last()\n  |> group()\n'
            f'  |> pivot(rowKey: ["thingId"], columnKey: ["_field"], valueColumn: "_value")\n'
            f'  |> drop(columns: ["_start", "_stop", "_time", "_measurement", "parent", "originator", "correlationId", "topic", "host"])')


def target(q, ref="A"):
    return {"datasource": DS, "query": q, "refId": ref}


def stat(title, q, unit="", x=0, y=0, w=4, h=4, decimals=None, color="blue", thresholds=None, text=False):
    p = {"id": nid(), "type": "stat", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(q)],
         "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "/.*/" if text else "", "values": False}, "colorMode": "none" if text else "value",
                     "graphMode": "none" if text else "area", "textMode": "value", "justifyMode": "auto"},
         "fieldConfig": {"defaults": {"unit": unit, "color": {"mode": "fixed", "fixedColor": color}}, "overrides": []}}
    if decimals is not None:
        p["fieldConfig"]["defaults"]["decimals"] = decimals
    if thresholds:
        p["fieldConfig"]["defaults"]["color"] = {"mode": "thresholds"}
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
    return p


def ts(title, queries, unit="", x=0, y=0, w=12, h=8, min_=None, max_=None, thresholds=None):
    p = {"id": nid(), "type": "timeseries", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
         "targets": [target(q, chr(65 + i)) for i, q in enumerate(queries)],
         "options": {"legend": {"displayMode": "list", "placement": "bottom", "showLegend": True}, "tooltip": {"mode": "multi", "sort": "desc"}},
         "fieldConfig": {"defaults": {"unit": unit, "custom": {"lineWidth": 2, "fillOpacity": 10, "spanNulls": True}}, "overrides": []}}
    if min_ is not None:
        p["fieldConfig"]["defaults"]["min"] = min_
    if max_ is not None:
        p["fieldConfig"]["defaults"]["max"] = max_
    if thresholds:
        p["fieldConfig"]["defaults"]["thresholds"] = {"mode": "absolute", "steps": thresholds}
        p["fieldConfig"]["defaults"]["custom"]["thresholdsStyle"] = {"mode": "line"}
    return p


def table(title, q, x=0, y=0, w=24, h=9, renames=None):
    return {"id": nid(), "type": "table", "title": title, "datasource": DS, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "targets": [target(q)], "options": {"showHeader": True, "cellHeight": "sm"},
            "fieldConfig": {"defaults": {}, "overrides": []},
            "transformations": [{"id": "organize", "options": {"renameByName": renames or {}}}]}


def text(title, content, x=0, y=0, w=24, h=4, mode="markdown"):
    return {"id": nid(), "type": "text", "title": title, "gridPos": {"x": x, "y": y, "w": w, "h": h},
            "options": {"mode": mode, "content": content}}


def dashboard(uid, title, panels, refresh="5s", rng="30m"):
    return {"uid": uid, "title": title, "tags": ["vzhr", "openegiz"], "timezone": "browser", "editable": True,
            # version растёт при каждой сборке: иначе Grafana с allowUiUpdates не перечитывает изменённый файл
            "refresh": refresh, "schemaVersion": 39, "version": int(time.time()), "time": {"from": f"now-{rng}", "to": "now"},
            "panels": panels, "templating": {"list": []}, "annotations": {"list": []}, "links": [
                {"title": "План горизонта", "type": "link", "url": "http://localhost:8095/", "targetBlank": True},
                {"title": "Обзор смены", "type": "link", "url": "/d/vzhr-overview"},
                {"title": "Воздух и газы", "type": "link", "url": "/d/vzhr-air"},
                {"title": "Горное давление", "type": "link", "url": "/d/vzhr-ground"},
                {"title": "Наряд и камеры", "type": "link", "url": "/d/vzhr-plan"},
                {"title": "План в Grafana", "type": "link", "url": "/d/vzhr-map"}]}


CO_LIMIT, NO2_LIMIT = 0.0016, 0.00026
LIM_T = [{"color": "green", "value": None}, {"color": "red", "value": 26}]


def overview():
    p = [
        stat("Смена №", flux_one("east-mine", "shift_no"), "", 0, 0, 3, 4, 0, "text"),
        stat("Людей под землёй", flux_one("east-mine", "people_underground"), "", 3, 0, 3, 4, 0, "blue"),
        stat("Камер: разрешено", flux_one("east-mine", "chambers_allowed"), "", 6, 0, 3, 4, 0, "green"),
        stat("Камер: ограничено", flux_one("east-mine", "chambers_restricted"), "", 9, 0, 3, 4, 0, "yellow"),
        stat("Камер: запрещено", flux_one("east-mine", "chambers_forbidden"), "", 12, 0, 3, 4, 0, "red"),
        stat("ВГП, м³/с", flux_one("fan-main", "q"), "", 15, 0, 3, 4, 0, "blue",
             [{"color": "red", "value": None}, {"color": "yellow", "value": 150}, {"color": "green", "value": 220}]),
        stat("Выдано руды, т", flux_one("hoist", "tonnes_total"), "", 18, 0, 3, 4, 0, "orange"),
        stat("Сигнал АГК", flux_one("east-mine", "alarm"), "", 21, 0, 3, 4, None, "red", text=True),
        ts("Вердикты по камерам (разрешено / ограничено / запрещено)", [flux_one("east-mine", "chambers_allowed", "разрешено"), flux_one("east-mine", "chambers_restricted", "ограничено"), flux_one("east-mine", "chambers_forbidden", "запрещено")], "", 0, 4, 12, 8, min_=0),
        ts("Воздух в панелях: подача, м³/с", [flux_many("panel-", "q_in")], "", 12, 4, 12, 8, min_=0),
        ts("Люди и дизельная техника в панелях", [flux_many("panel-", "people"), flux_many("panel-", "diesel_kw")], "", 0, 12, 12, 8, min_=0),
        ts("Выдача руды: подъём, т/ч, и накоплено, т", [flux_one("hoist", "throughput", "подъём, т/ч"), flux_one("hoist", "tonnes_total", "выдано, т")], "", 12, 12, 12, 8, min_=0),
        ts("Сейсмическая энергия за смену по панелям, Дж", [flux_many("panel-", "seismic_energy_shift")], "", 0, 20, 12, 8, min_=0),
        ts("Потребность и наличие воздуха в панелях, м³/мин", [flux_many("panel-", "air_required"), flux_many("panel-", "air_available")], "", 12, 20, 12, 8, min_=0),
        stat("Активные сценарии", flux_one("east-mine", "active_scenarios"), "", 0, 28, 12, 3, None, "yellow", text=True),
        stat("Наряд смены", flux_one("shift-plan", "plan"), "", 12, 28, 12, 3, None, "text", text=True),
    ]
    return dashboard("vzhr-overview", "ВЖР: обзор смены", p)


def air():
    p = [
        stat("ВГП: подача, м³/с", flux_one("fan-main", "q"), "", 0, 0, 4, 4, 0, "blue"),
        stat("ВГП: депрессия, Па", flux_one("fan-main", "pressure"), "", 4, 0, 4, 4, 0, "blue"),
        stat("ВГП: обороты, %", flux_one("fan-main", "speed_pct"), "", 8, 0, 4, 4, 0, "blue"),
        stat("Общая исходящая CO, % об.", flux_one("agk-vent", "co"), "", 12, 0, 4, 4, 5, "green",
             [{"color": "green", "value": None}, {"color": "yellow", "value": CO_LIMIT / 2}, {"color": "red", "value": CO_LIMIT}]),
        stat("Общая исходящая NO2, % об.", flux_one("agk-vent", "no2"), "", 16, 0, 4, 4, 6, "green",
             [{"color": "green", "value": None}, {"color": "yellow", "value": NO2_LIMIT / 2}, {"color": "red", "value": NO2_LIMIT}]),
        stat("Скорость струи, общая исходящая, м/с", flux_one("agk-vent", "velocity"), "", 20, 0, 4, 4, 1, "blue"),
        ts("CO на датчиках АГК, % об. (ПДК 0,0016 %)", [flux_many("agk-", "co")], "", 0, 4, 12, 8, min_=0, max_=0.004,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": CO_LIMIT}]),
        ts("NO2 на датчиках АГК, % об. (ПДК 0,00026 %)", [flux_many("agk-", "no2")], "", 12, 4, 12, 8, min_=0, max_=0.0006,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": NO2_LIMIT}]),
        ts("Расход по выработкам, м³/с", [flux_many("aw-", "q")], "", 0, 12, 12, 8, min_=0),
        ts("Скорость струи по выработкам, м/с (0,25–8 м/с)", [flux_many("aw-", "velocity")], "", 12, 12, 12, 8, min_=0,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 8}]),
        ts("Температура на датчиках, °C (не выше 26)", [flux_many("agk-", "temp")], "", 0, 20, 12, 8, thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 26}]),
        ts("CO в работающих камерах, % об.", [flux_many("k[1-3]-", "co")], "", 12, 20, 12, 8, min_=0, max_=0.004,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": CO_LIMIT}]),
        table("Датчики АГК: последние показания", flux_last_table("agk-", ["co", "no2", "o2", "co2", "temp", "velocity"]), 0, 28, 16, 6,
              {"thingId": "Датчик", "value_co_properties_value": "CO, %", "value_no2_properties_value": "NO2, %", "value_o2_properties_value": "O2, %",
               "value_co2_properties_value": "CO2, %", "value_temp_properties_value": "t, °C", "value_velocity_properties_value": "v, м/с"}),
        table("Датчики АГК: сигналы", flux_last_table("agk-", ["alarm"], "str"), 16, 28, 8, 6, {"thingId": "Датчик", "value_alarm_properties_value": "Сигнал"}),
    ]
    return dashboard("vzhr-air", "ВЖР: воздух и газы", p)


def ground():
    p = [
        ts("Запас прочности целиков панели П-1 (SF; 1,3 и 1,5)", [flux_many("c1-", "sf")], "", 0, 0, 8, 9, min_=0,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 1.3}, {"color": "yellow", "value": 1.5}]),
        ts("Запас прочности целиков панели П-2", [flux_many("c2-", "sf")], "", 8, 0, 8, 9, min_=0,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 1.3}, {"color": "yellow", "value": 1.5}]),
        ts("Запас прочности целиков панели П-3", [flux_many("c3-", "sf")], "", 16, 0, 8, 9, min_=0,
           thresholds=[{"color": "transparent", "value": None}, {"color": "red", "value": 1.3}, {"color": "yellow", "value": 1.5}]),
        ts("Конвергенция, мм/сут (наблюдение 1,0; закрытие 2,5)", [flux_many("c[1-3]-", "convergence")], "", 0, 9, 12, 8, min_=0,
           thresholds=[{"color": "transparent", "value": None}, {"color": "yellow", "value": 1.0}, {"color": "red", "value": 2.5}]),
        ts("Сейсмика по панелям: толчков за смену и максимальная энергия, Дж", [flux_many("panel-", "bumps_shift"), flux_many("panel-", "max_bump_energy")], "", 12, 9, 12, 8, min_=0),
        ts("Напряжение и прочность целиков П-1, МПа", [flux_many("c1-", "stress"), flux_many("c1-", "strength")], "", 0, 17, 12, 8, min_=0),
        ts("Вынуто слоёв по панелям", [flux_many("panel-", "layers_mined")], "", 12, 17, 12, 8, min_=0),
        table("Целики: последнее состояние", flux_last_table("c[1-3]-", ["height", "strength", "stress", "sf", "convergence"]), 0, 25, 16, 12,
              {"thingId": "Целик", "value_height_properties_value": "Высота, м", "value_strength_properties_value": "Прочность, МПа", "value_stress_properties_value": "Напряжение, МПа",
               "value_sf_properties_value": "SF", "value_convergence_properties_value": "Конвергенция, мм/сут"}),
        table("Целики: статус", flux_last_table("c[1-3]-", ["status", "extracted"], "str"), 16, 25, 8, 12,
              {"thingId": "Целик", "value_status_properties_value": "Статус", "value_extracted_properties_value": "Извлечён"}),
    ]
    return dashboard("vzhr-ground", "ВЖР: горное давление и целики", p)


def plan():
    p = [
        table("Наряд: звенья", flux_last_table("crew-", ["chamber", "task", "state"], "str"), 0, 0, 12, 7,
              {"thingId": "Звено", "value_chamber_properties_value": "Камера", "value_task_properties_value": "Работа", "value_state_properties_value": "Состояние"}),
        table("Техника", flux_last_table("(pdm|sbu)-", ["chamber", "state"], "str"), 12, 0, 12, 7,
              {"thingId": "Машина", "value_chamber_properties_value": "Камера", "value_state_properties_value": "Состояние"}),
        table("Камеры: вердикт наряда", flux_last_table("k[1-3]-", ["state", "verdict", "reason", "crew", "equipment"], "str"), 0, 7, 24, 14,
              {"thingId": "Камера", "value_state_properties_value": "Стадия", "value_verdict_properties_value": "Вердикт", "value_reason_properties_value": "Причина",
               "value_crew_properties_value": "Звено", "value_equipment_properties_value": "Техника"}),
        table("Камеры: воздух и газы", flux_last_table("k[1-3]-", ["layer", "people", "diesel_kw", "air", "air_required", "co", "no2", "temp"]), 0, 21, 24, 10,
              {"thingId": "Камера", "value_layer_properties_value": "Слой", "value_people_properties_value": "Чел.", "value_diesel_kw_properties_value": "Дизель, кВт",
               "value_air_properties_value": "Воздух, м³/мин", "value_air_required_properties_value": "Нужно, м³/мин", "value_co_properties_value": "CO, %", "value_no2_properties_value": "NO2, %", "value_temp_properties_value": "t, °C"}),
        ts("Выпуск руды ПДМ за смену, т", [flux_many("pdm-", "tonnes_shift")], "", 0, 31, 12, 8, min_=0),
        ts("Уровень рудоспусков, т", [flux_many("rs-", "level")], "", 12, 31, 12, 8, min_=0),
    ]
    return dashboard("vzhr-plan", "ВЖР: наряд, камеры и техника", p)


def mapdash():
    p = [
        text("План горизонта (живой, из Ditto)",
             '<iframe src="http://localhost:8095/" style="width:100%;height:100%;border:0;background:#0f1419" title="VZHR level plan"></iframe>',
             0, 0, 24, 30, mode="html"),
        text("Как читать план",
             "План строится из атрибутов двойников (выработки, панели, камеры, целики, датчики) и их features (состояния). "
             "Струи воздуха движутся по направлению и скорости из Ditto; цвет струи — чистота воздуха; камеры окрашены по вердикту наряда "
             "(зелёный разрешено, жёлтый ограничено, красный запрещено); целики — по запасу прочности. Справа: наряд смены с назначением звеньев, "
             "список запрещённых камер с причинами, сценарии «что если». Если iframe не отображается, откройте http://localhost:8095/", 0, 30, 24, 4),
    ]
    return dashboard("vzhr-map", "ВЖР: план горизонта", p, refresh="")


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        if f.endswith(".json"):
            os.remove(os.path.join(OUT, f))
    for d in (overview(), air(), ground(), plan(), mapdash()):
        path = os.path.join(OUT, f"{d['uid']}.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=1)
        print("wrote", path, len(d["panels"]), "panels")


if __name__ == "__main__":
    main()
