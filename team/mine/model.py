"""
Статическая модель горизонта ВЖР: объекты, которые становятся двойниками Ditto.

build_twins() возвращает упорядоченный словарь {thingId: тело вещи}. Родители
идут раньше детей, чтобы `_parents` всегда указывал на существующий двойник.
Атрибуты — то, что не меняется (геометрия, паспорта, нормы); features — то,
что обновляет симулятор.
"""

import math
from collections import OrderedDict

from . import config as C

POLICY = "default:basic_policy"


def tid(name):
    return f"{C.NAMESPACE}:{name}"


def f(value, unit=None):
    p = {"value": value}
    if unit:
        p["unit"] = unit
    return {"properties": p}


def airway_resistance(aw):
    """Сопротивление Аткинсона R = α·P·L / A³, Н·с²/м⁸."""
    return C.FAN["alpha"] * aw["P"] * aw["L"] / (aw["A"] ** 3)


def build_chambers():
    """Камеры и целики панелей на сетке 20 м. Начальное состояние по progress панели."""
    chambers, pillars = [], []
    for panel in C.PANELS:
        ox, oy = panel["origin"]
        n = panel["cols"] * panel["rows"]
        done = int(round(panel["progress"] * n * C.LAYERS_PER_CHAMBER))   # отработанных слоёв в панели
        idx = 0
        for row in range(panel["rows"]):
            for col in range(panel["cols"]):
                cx = ox + col * C.GRID_M
                cy = oy + row * C.GRID_M
                cid = f"{panel['id'].replace('panel-', 'k')}-{row + 1}{col + 1}"       # k1-11 … k3-36
                layers_done = min(C.LAYERS_PER_CHAMBER, max(0, done - idx * C.LAYERS_PER_CHAMBER))
                idx += 1
                state = "mined_out" if layers_done >= C.LAYERS_PER_CHAMBER else ("idle" if layers_done > 0 else "virgin")
                chambers.append({
                    "id": cid, "panel": panel["id"], "x": cx, "y": cy, "w": C.CHAMBER_W, "l": C.CHAMBER_L,
                    "row": row, "col": col, "layer": layers_done, "state": state,
                    "grade": round(C.ORE_GRADE_PCT * (0.85 + 0.3 * ((col * 7 + row * 3) % 5) / 4), 2),
                })
                pillars.append({
                    "id": cid.replace("k", "c", 1),          # c1-11: целик при камере k1-11
                    "panel": panel["id"], "chamber": cid,
                    "x": cx + C.CHAMBER_W, "y": cy + (C.GRID_M - C.PILLAR_W) / 2, "w": C.PILLAR_W,
                    "h": C.LAYER_H * max(1, layers_done),
                    "edge": col == 0 or col == panel["cols"] - 1 or row == 0 or row == panel["rows"] - 1,
                })
    return chambers, pillars


def pillar_strength_mpa(w, h):
    """Хедли–Грант: S = K · w^0.5 / h^0.75."""
    return C.PILLAR_K_MPA * math.sqrt(w) / (h ** 0.75)


def pillar_stress_mpa(extraction):
    """Теория площадей влияния: σ = ρ g H / (1 − e)."""
    sigma_v = C.ROCK_DENSITY * 1000 * 9.81 * C.DEPTH_M / 1e6
    return sigma_v / max(0.05, 1.0 - extraction)


def build_twins():
    twins = OrderedDict()
    mine = tid(C.MINE_ID)
    twins[mine] = {
        "policyId": POLICY,
        "attributes": {"name": C.MINE_NAME, "kind": "mine", "_isType": False, "depth_m": C.DEPTH_M,
                       "system": "камерно-столбовая, слоевая выемка", "map_extent": C.MAP_EXTENT,
                       "norms": {"air_per_person_m3min": C.AIR["per_person_m3min"], "air_per_kw_m3min": C.AIR["per_diesel_kw_m3min"],
                                 "co_limit_pct": C.AIR["co_limit_pct"], "no2_limit_pct": C.AIR["no2_limit_pct"],
                                 "t_max_c": C.AIR["t_max_c"], "sf_ok": C.SF_OK, "sf_watch": C.SF_WATCH,
                                 "blast_reentry_min": C.AIR["blast_reentry_min"]}},
        "features": {"sim_time_h": f(0, "ч"), "shift_no": f(1), "shift_phase": f("работа"),
                     "people_underground": f(0, "чел"), "ore_hoisted_t": f(0, "т"), "ore_shift_t": f(0, "т"),
                     "chambers_allowed": f(0), "chambers_restricted": f(0), "chambers_forbidden": f(0),
                     "alarm": f("нет"), "active_scenarios": f("none")},
    }

    # Вентилятор главного проветривания --------------------------------------
    twins[tid("fan-main")] = {
        "policyId": POLICY,
        "attributes": {"name": C.FAN["name"], "kind": "fan", "_isType": False, "_parents": mine,
                       "q_nominal_m3s": C.FAN["q_nominal_m3s"], "p_nominal_pa": C.FAN["p_nominal_pa"],
                       "power_kw": C.FAN["power_kw"], "node": "shaft-vent", "x": C.NODES["shaft-vent"]["xy"][0], "y": C.NODES["shaft-vent"]["xy"][1]},
        "features": {"state": f("работает"), "speed_pct": f(100, "%"), "q": f(0, "м3/с"), "pressure": f(0, "Па"),
                     "power": f(0, "кВт")},
    }

    # Узлы сети (стволы, сопряжения, дробилка) ----------------------------------
    for nid, node in C.NODES.items():
        attrs = {"name": node["name"], "kind": node["kind"], "_isType": False, "_parents": mine,
                 "x": node["xy"][0], "y": node["xy"][1]}
        feats = {"people": f(0, "чел")}
        if node["kind"] == "crusher":
            attrs["rated_tph"] = C.CRUSHER_TPH
            feats.update({"state": f("ожидание"), "throughput": f(0, "т/ч"), "bunker": f(0, "т"), "tonnes_total": f(0, "т")})
        if node["kind"] == "shaft":
            feats.update({"q": f(0, "м3/с"), "mode": f("свежая" if nid != "shaft-vent" else "исходящая")})
        twins[tid(nid)] = {"policyId": POLICY, "attributes": attrs, "features": feats}

    twins[tid("hoist")] = {
        "policyId": POLICY,
        "attributes": {"name": "Скиповой подъём", "kind": "hoist", "_isType": False, "_parents": tid("shaft-skip"),
                       "rated_tph": C.HOIST_TPH},
        "features": {"state": f("ожидание"), "throughput": f(0, "т/ч"), "tonnes_total": f(0, "т"), "skips_total": f(0)},
    }

    # Выработки (ветви вентиляционной сети) ------------------------------------
    for aw in C.AIRWAYS:
        a, b = C.NODES[aw["a"]]["xy"], C.NODES[aw["b"]]["xy"]
        twins[tid(aw["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": aw["name"], "kind": "airway", "_isType": False, "_parents": mine,
                           "from": aw["a"], "to": aw["b"], "length_m": aw["L"], "area_m2": aw["A"], "perimeter_m": aw["P"],
                           "resistance": round(airway_resistance(aw), 5), "airway_kind": aw["kind"],
                           "door": aw.get("door", ""), "panel": aw.get("panel", ""), "dead_end": aw.get("dead_end", False),
                           "polyline": [list(a), list(b)]},
            "features": {"status": f("open"), "q": f(0, "м3/с"), "velocity": f(0, "м/с"), "direction": f(1),
                         "co": f(0, "% об."), "no2": f(0, "% об."), "temp": f(C.AIR["rock_temp_c"], "°C"),
                         "door_state": f("закрыта" if aw.get("door") == "door" else ("открыт" if aw.get("door") else "")),
                         "people": f(0, "чел"), "vehicles": f(0)},
        }

    # Датчики аэрогазового контроля -------------------------------------------
    for s in C.SENSORS:
        xy = C.NODES[s["node"]]["xy"]
        twins[tid(s["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": s["name"], "kind": "sensor", "_isType": False, "_parents": tid(s["node"]),
                           "node": s["node"], "x": xy[0], "y": xy[1] + 14,
                           "limits": {"co_pct": C.AIR["co_limit_pct"], "no2_pct": C.AIR["no2_limit_pct"], "t_max_c": C.AIR["t_max_c"]}},
            "features": {"co": f(0, "% об."), "no2": f(0, "% об."), "co2": f(0.1, "% об."), "o2": f(20.9, "% об."),
                         "temp": f(C.AIR["rock_temp_c"], "°C"), "velocity": f(0, "м/с"), "alarm": f("норма")},
        }

    # Панели, камеры, целики, рудоспуски ---------------------------------------
    chambers, pillars = build_chambers()
    for panel in C.PANELS:
        twins[tid(panel["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": panel["name"], "kind": "panel", "_isType": False, "_parents": mine,
                           "origin_x": panel["origin"][0], "origin_y": panel["origin"][1], "cols": panel["cols"], "rows": panel["rows"],
                           "grid_m": C.GRID_M, "in_node": panel["in"], "out_node": panel["out"], "ore_pass": panel["ore_pass"],
                           "seismic_class_initial": panel["seismic_class"]},
            "features": {"q_in": f(0, "м3/с"), "chambers_active": f(0), "people": f(0, "чел"), "diesel_kw": f(0, "кВт"),
                         "air_required": f(0, "м3/мин"), "air_available": f(0, "м3/мин"),
                         "seismic_class": f(panel["seismic_class"]), "bumps_shift": f(0), "seismic_energy_shift": f(0, "Дж"),
                         "max_bump_energy": f(0, "Дж"), "hazard": f("низкая"), "evacuated_until_h": f(0, "ч"),
                         "extraction_ratio": f(C.EXTRACTION_RATIO), "layers_mined": f(0), "status": f("работа")},
        }
        rs = panel["ore_pass"]
        twins[tid(rs)] = {
            "policyId": POLICY,
            "attributes": {"name": f"Рудоспуск {rs.upper()}", "kind": "ore-pass", "_isType": False, "_parents": tid(panel["id"]),
                           "x": C.ORE_PASSES[rs]["xy"][0], "y": C.ORE_PASSES[rs]["xy"][1], "capacity_t": C.ORE_PASS_CAPACITY_T},
            "features": {"level": f(0, "т"), "state": f("ожидание"), "tonnes_total": f(0, "т")},
        }
    for ch in chambers:
        twins[tid(ch["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": f"Камера {ch['id'][1:]}", "kind": "chamber", "_isType": False, "_parents": tid(ch["panel"]),
                           "x": ch["x"], "y": ch["y"], "w": ch["w"], "l": ch["l"], "layer_h": C.LAYER_H, "layers": C.LAYERS_PER_CHAMBER,
                           "tonnes_per_layer": round(C.CHAMBER_TONNES), "grade_pct": ch["grade"], "volume_m3": round(C.CHAMBER_VOLUME)},
            "features": {"state": f(ch["state"]), "layer": f(ch["layer"]), "tonnes_remaining": f(round(C.CHAMBER_TONNES) if ch["state"] != "mined_out" else 0, "т"),
                         "crew": f(""), "equipment": f(""), "people": f(0, "чел"), "diesel_kw": f(0, "кВт"),
                         "air": f(0, "м3/мин"), "air_required": f(0, "м3/мин"), "co": f(0, "% об."), "no2": f(0, "% об."),
                         "temp": f(C.AIR["rock_temp_c"], "°C"), "aux_fan": f("выкл"),
                         "verdict": f("—"), "reason": f(""), "reentry_in": f(0, "мин"), "stage_progress": f(0, "%")},
        }
    for p in pillars:
        extraction = C.EXTRACTION_RATIO * (0.9 if p["edge"] else 1.0)
        strength = pillar_strength_mpa(p["w"], p["h"])
        stress = pillar_stress_mpa(extraction)
        twins[tid(p["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": f"Целик {p['id'][1:]}", "kind": "pillar", "_isType": False, "_parents": tid(p["panel"]),
                           "chamber": p["chamber"], "x": p["x"], "y": p["y"], "w": p["w"], "edge": p["edge"],
                           "k_mpa": round(C.PILLAR_K_MPA, 1), "formula": "Hedley–Grant S=K·w^0.5/h^0.75; σ=ρgH/(1−e)"},
            "features": {"height": f(p["h"], "м"), "strength": f(round(strength, 1), "МПа"), "stress": f(round(stress, 1), "МПа"),
                         "sf": f(round(strength / stress, 2)), "convergence": f(0.2, "мм/сут"), "status": f("устойчив"),
                         "extracted": f(False)},
        }

    # Люди и техника ----------------------------------------------------------
    for crew in C.CREWS:
        twins[tid(crew)] = {
            "policyId": POLICY,
            "attributes": {"name": f"Звено {crew.split('-')[1]} ({C.CREW_SIZE} чел.)", "short": f"звено-{crew.split('-')[1]}", "kind": "crew", "_isType": False, "_parents": mine,
                           "size": C.CREW_SIZE},
            "features": {"chamber": f(""), "task": f(""), "state": f("на поверхности"), "people": f(C.CREW_SIZE, "чел"),
                         "x": f(C.NODES["shaft-cage"]["xy"][0], "м"), "y": f(C.NODES["shaft-cage"]["xy"][1], "м"), "exposure_co": f(0, "%·ч")},
        }
    for i, lhd in enumerate(C.LHDS):
        twins[tid(lhd)] = {
            "policyId": POLICY,
            "attributes": {"name": f"ПДМ-{i + 1} ({C.LHD['model']})", "kind": "lhd", "_isType": False, "_parents": mine,
                           "kw": C.LHD["kw"], "bucket_t": C.LHD["bucket_t"]},
            "features": {"chamber": f(""), "state": f("стоянка"), "kw_running": f(0, "кВт"), "tonnes_shift": f(0, "т"),
                         "buckets_total": f(0), "hours": f(0, "ч"), "x": f(C.NODES["j-main-1"]["xy"][0], "м"), "y": f(C.NODES["j-main-1"]["xy"][1] + 20 * i, "м")},
        }
    for i, jb in enumerate(C.JUMBOS):
        twins[tid(jb)] = {
            "policyId": POLICY,
            "attributes": {"name": f"СБУ-{i + 1} ({C.JUMBO['model']})", "kind": "jumbo", "_isType": False, "_parents": mine,
                           "kw_tramming": C.JUMBO["kw_tramming"]},
            "features": {"chamber": f(""), "state": f("стоянка"), "kw_running": f(0, "кВт"), "rounds_total": f(0),
                         "x": f(C.NODES["j-main-2"]["xy"][0], "м"), "y": f(C.NODES["j-main-2"]["xy"][1] + 20 * i, "м")},
        }

    # Наряд смены -------------------------------------------------------------
    twins[tid("shift-plan")] = {
        "policyId": POLICY,
        "attributes": {"name": "Наряд смены (горный мастер)", "kind": "shift-plan", "_isType": False, "_parents": mine,
                       "rule": "звено идёт в камеру только при вердикте «разрешено»; «ограничено» — без дизеля или с одной машиной"},
        "features": {"shift_no": f(1), "plan": f(""), "auto": f(True), "last_change": f(""), "rejected": f("")},
    }
    return twins


if __name__ == "__main__":
    import collections
    t = build_twins()
    print(len(t), "twins", collections.Counter(v["attributes"]["kind"] for v in t.values()))
