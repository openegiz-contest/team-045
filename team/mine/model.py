"""
Static model of the Sarybulak mine: the objects that become twins in Ditto.

build_twins() returns an ordered dict {thingId: thing body}. Parents come
before children so the hierarchy attribute `_parents` always points to an
existing twin. Attributes hold what does not change (geometry, ratings,
data-sheet parameters); features hold what the simulator updates.
"""

import math
import random
from collections import OrderedDict

from . import config as C

POLICY = "default:basic_policy"


def tid(name):
    return f"{C.NAMESPACE}:{name}"


def grade_at(x, y, rng):
    """Smooth ore-body bulge plus noise; deterministic per block (seeded)."""
    dx, dy = x - C.GRADE_CENTER[0], y - C.GRADE_CENTER[1]
    r = math.hypot(dx, dy) / C.GRADE_RADIUS
    g = C.GRADE_FLOOR + (C.GRADE_PEAK - C.GRADE_FLOOR) * math.exp(-r * r)
    g *= rng.uniform(0.75, 1.25)
    return round(max(0.05, g), 3)


def build_blocks():
    """Block model: one block = 25 x 25 x 15 m, ~25 300 t."""
    rng = random.Random(7)
    blocks = []
    for bench in C.BENCHES:
        ox, oy = bench["origin"]
        for row in range(bench["rows"]):
            for col in range(bench["cols"]):
                x = ox + col * C.BLOCK_SIZE_M + C.BLOCK_SIZE_M / 2
                y = oy + row * C.BLOCK_SIZE_M + C.BLOCK_SIZE_M / 2
                g = grade_at(x, y, rng)
                blocks.append({
                    "id": f"block-{bench['elevation']}-{row + 1}{chr(ord('A') + col)}",
                    "bench": bench["id"],
                    "elevation": bench["elevation"],
                    "x": x, "y": y, "row": row, "col": col,
                    "grade": g,
                    "hardness": round(rng.uniform(C.HARDNESS_MIN, C.HARDNESS_MAX), 1),
                    "tonnes": round(C.BLOCK_TONNES),
                    "is_ore": g >= C.CUTOFF_GRADE,
                })
    return blocks


def feature(value, unit=None):
    p = {"value": value}
    if unit:
        p["unit"] = unit
    return {"properties": p}


def build_twins():
    twins = OrderedDict()
    mine = tid(C.MINE_ID)

    twins[mine] = {
        "policyId": POLICY,
        "attributes": {
            "name": "Карьер Сарыбұлақ (медно-порфировое месторождение)",
            "kind": "mine", "_isType": False,
            "cutoff_grade_pct": C.CUTOFF_GRADE,
            "blend_target_pct": C.BLEND["target_grade"], "blend_band_pct": C.BLEND["band"],
            "map_extent": {"w": 1700, "h": 1100},
        },
        "features": {
            "sim_time_h": feature(0, "h"),
            "ore_mined_t": feature(0, "t"),
            "waste_mined_t": feature(0, "t"),
            "strip_ratio": feature(0, "t/t"),
            "concentrate_t": feature(0, "t"),
            "copper_in_concentrate_t": feature(0, "t Cu"),
            "trucks_active": feature(0, "trucks"),
            "fleet_availability_pct": feature(100, "%"),
            "active_scenarios": feature(""),
        },
    }

    # Facilities (nodes) ----------------------------------------------------
    facility_kind = {"crusher": "crusher", "stockpile": "stockpile", "dump": "waste-dump", "fuel": "fuel-station",
                     "ramp-top": "junction", "junction": "junction", "face-545": "face", "face-530": "face"}
    for node_id, node in C.NODES.items():
        kind = facility_kind[node_id]
        attrs = {"name": node["name"], "kind": kind, "_isType": False, "_parents": mine,
                 "x": node["xy"][0], "y": node["xy"][1]}
        feats = {}
        if kind == "crusher":
            attrs.update({"rated_tph": C.CRUSHER["rated_tph"], "f80_mm": C.CRUSHER["f80_um"] / 1000,
                          "p80_mm": C.CRUSHER["p80_um"] / 1000, "bin_capacity_t": C.CRUSHER["bin_capacity_t"]})
            feats = {"state": feature("idle"), "throughput": feature(0, "t/h"), "feed_grade": feature(0, "% Cu"),
                     "bin_level": feature(0, "t"), "power": feature(0, "kW"), "tonnes_total": feature(0, "t"),
                     "queue": feature(0, "trucks")}
        elif kind == "stockpile":
            attrs.update({"capacity_t": C.STOCKPILE["capacity_t"], "reclaim_tph": C.STOCKPILE["reclaim_tph"]})
            feats = {"tonnes": feature(C.STOCKPILE["initial_t"], "t"), "grade": feature(C.STOCKPILE["initial_grade"], "% Cu"),
                     "reclaiming": feature(0, "t/h"), "queue": feature(0, "trucks")}
        elif kind == "waste-dump":
            feats = {"tonnes": feature(0, "t"), "queue": feature(0, "trucks")}
        elif kind == "fuel-station":
            attrs.update({"rate_lpm": C.TRUCK["refuel_rate_lpm"]})
            feats = {"diesel_issued_l": feature(0, "L"), "queue": feature(0, "trucks")}
        elif kind == "face":
            feats = {"queue": feature(0, "trucks")}
        twins[tid(node_id)] = {"policyId": POLICY, "attributes": attrs, "features": feats}

    # Road segments ---------------------------------------------------------
    for seg in C.SEGMENTS:
        twins[tid(seg["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": f"Дорога {C.NODES[seg['a']]['name']} → {C.NODES[seg['b']]['name']}",
                           "kind": "road", "_isType": False, "_parents": mine,
                           "from": seg["a"], "to": seg["b"], "length_m": seg["length_m"],
                           "grade_pct": seg["grade_pct"], "polyline": [list(p) for p in seg["polyline"]]},
            "features": {"status": feature("open"), "trucks_on_road": feature(0, "trucks"),
                         "trips_total": feature(0, "trips")},
        }

    # Benches and blocks ----------------------------------------------------
    for bench in C.BENCHES:
        twins[tid(bench["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": f"Уступ {bench['elevation']} м", "kind": "bench", "_isType": False, "_parents": mine,
                           "elevation_m": bench["elevation"], "origin_x": bench["origin"][0], "origin_y": bench["origin"][1],
                           "cols": bench["cols"], "rows": bench["rows"], "block_size_m": C.BLOCK_SIZE_M,
                           "face_node": C.FACE_OF_BENCH[bench["id"]]},
            "features": {"blocks_mined": feature(0, "blocks"), "tonnes_remaining": feature(0, "t")},
        }
    for b in build_blocks():
        twins[tid(b["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": b["id"], "kind": "block", "_isType": False, "_parents": tid(b["bench"]),
                           "x": b["x"], "y": b["y"], "elevation_m": b["elevation"], "size_m": C.BLOCK_SIZE_M,
                           "grade_pct": b["grade"], "hardness_kwh_t": b["hardness"], "tonnes_insitu": b["tonnes"],
                           "material": "ore" if b["is_ore"] else "waste"},
            "features": {"status": feature("insitu"), "tonnes_remaining": feature(b["tonnes"], "t"),
                         "fragmentation_p80_mm": feature(0, "mm"),
                         "material": feature("ore" if b["is_ore"] else "waste")},
        }

    # Excavators ------------------------------------------------------------
    for ex in C.EXCAVATORS:
        twins[tid(ex["id"])] = {
            "policyId": POLICY,
            "attributes": {"name": ex["name"], "kind": "excavator", "_isType": False, "_parents": mine,
                           "bench": tid(ex["bench"]), "bucket_t": ex["bucket_t"], "pass_s": ex["pass_s"],
                           "mtbf_h": C.RELIABILITY["excavator"]["mtbf_h"]},
            "features": {"state": feature("idle"), "block": feature(""), "face_grade": feature(0, "% Cu"),
                         "passes_total": feature(0, "passes"), "tonnes_loaded": feature(0, "t"),
                         "queue": feature(0, "trucks"), "utilization_pct": feature(0, "%"),
                         "hours": feature(0, "h"), "health_pct": feature(100, "%")},
        }

    # Trucks ----------------------------------------------------------------
    for i in range(1, C.TRUCK_COUNT + 1):
        twins[tid(f"truck-{i:02d}")] = {
            "policyId": POLICY,
            "attributes": {"name": f"Самосвал HT-{i:02d} ({C.TRUCK['model']})", "kind": "haul-truck", "_isType": False,
                           "_parents": mine, "capacity_t": C.TRUCK["capacity_t"], "tank_l": C.TRUCK["tank_l"],
                           "model": C.TRUCK["model"]},
            "features": {"state": feature("idle"), "x": feature(0, "m"), "y": feature(0, "m"),
                         "road": feature(""), "destination": feature(""), "payload": feature(0, "t"),
                         "material": feature(""), "load_grade": feature(0, "% Cu"), "speed": feature(0, "km/h"),
                         "fuel": feature(C.TRUCK["tank_l"], "L"), "fuel_rate": feature(0, "L/h"),
                         "trips_total": feature(0, "trips"), "hours": feature(0, "h"),
                         "health_pct": feature(100, "%"), "assigned_excavator": feature("")},
        }

    # Plant -----------------------------------------------------------------
    plant = tid("plant")
    twins[plant] = {
        "policyId": POLICY,
        "attributes": {"name": "Обогатительная фабрика", "kind": "plant", "_isType": False, "_parents": mine,
                       "x": 1560, "y": 300},
        "features": {"state": feature("running")},
    }
    twins[tid("mill")] = {
        "policyId": POLICY,
        "attributes": {"name": "Измельчение (SAG + шаровая)", "kind": "mill", "_isType": False, "_parents": plant,
                       "rated_tph": C.MILL["rated_tph"], "p80_um": C.MILL["p80_um"]},
        "features": {"state": feature("idle"), "throughput": feature(0, "t/h"), "feed_grade": feature(0, "% Cu"),
                     "specific_energy": feature(0, "kWh/t"), "power": feature(0, "kW"), "tonnes_total": feature(0, "t")},
    }
    twins[tid("flotation")] = {
        "policyId": POLICY,
        "attributes": {"name": "Флотация", "kind": "flotation", "_isType": False, "_parents": plant,
                       "concentrate_grade_pct": C.FLOTATION["concentrate_grade"]},
        "features": {"recovery": feature(0, "%"), "concentrate_tph": feature(0, "t/h"),
                     "concentrate_total": feature(0, "t"), "tailings_grade": feature(0, "% Cu")},
    }
    twins[tid("substation")] = {
        "policyId": POLICY,
        "attributes": {"name": "Подстанция 110/6 кВ", "kind": "substation", "_isType": False, "_parents": mine,
                       "rated_mva": C.POWER["substation_mva"], "x": 1620, "y": 520},
        "features": {"load": feature(0, "kW"), "load_pct": feature(0, "%"), "energy_total": feature(0, "kWh"),
                     "diesel_rate": feature(0, "L/h"), "diesel_total": feature(0, "L"), "co2_total": feature(0, "t CO2")},
    }
    twins[tid("dispatcher")] = {
        "policyId": POLICY,
        "attributes": {"name": "Диспетчер", "kind": "dispatcher", "_isType": False, "_parents": mine,
                       "policy": "min-wait + blend"},
        "features": {"assignments": feature(""), "avg_cycle_min": feature(0, "min"),
                     "avg_queue_min": feature(0, "min"), "blend_mode": feature("normal")},
    }
    return twins


if __name__ == "__main__":
    import json
    t = build_twins()
    print(json.dumps(t, ensure_ascii=False, indent=1)[:3000])
    print(len(t), "twins")
