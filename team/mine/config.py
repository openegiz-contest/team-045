"""
Sarybulak open-pit copper mine: layout, fleet and process parameters.

Everything the simulator, the twins and the map share lives here. Each number
carries its source in a comment: an equipment data sheet, a reference book,
a public dataset or an explicit engineering assumption (marked ASSUMPTION).

Coordinates are metres in a local grid; x grows east, y grows south (screen
orientation), origin at the north-west corner of the site. The pit lies in the
west, the plant in the east.
"""

NAMESPACE = "org.openegiz.sarybulak"
MINE_ID = "mine"

# ---------------------------------------------------------------------------
# Geology: a porphyry copper deposit of Kazakh type.
#   Average grades of Kazakh porphyry mines: Bozshakol 0.36 % Cu, Aktogay
#   0.33 % Cu (USGS Open-File Report 2010-1255, Mineral Facilities of Northern
#   and Central Eurasia; company annual reports). Our block grades scatter
#   around 0.4 % Cu with a richer core, cut-off 0.25 % Cu (ASSUMPTION, typical
#   for low-grade open-pit copper).
#   Bond work index of porphyry copper ore 12-18 kWh/t (Bond, 1961; GeoMet
#   dataset, Zenodo 7051975 gives hardness classes). Rock density 2.7 t/m3.
# ---------------------------------------------------------------------------
CUTOFF_GRADE = 0.25          # % Cu, ore / waste boundary
ROCK_DENSITY = 2.7           # t/m3
BLOCK_SIZE_M = 25.0          # block side, m (25 x 25 x 15 m bench height)
BENCH_HEIGHT_M = 15.0
BLOCK_TONNES = BLOCK_SIZE_M * BLOCK_SIZE_M * BENCH_HEIGHT_M * ROCK_DENSITY  # ~25 300 t
SWELL_FACTOR = 1.0           # tonnes are tonnes; swell only matters for volume

# Benches: top of the pit is bench 545, mining goes down.
BENCHES = [
    # id, elevation m, origin (x, y) of the block grid, grid columns x rows
    {"id": "bench-545", "elevation": 545, "origin": (140, 380), "cols": 5, "rows": 3},
    {"id": "bench-530", "elevation": 530, "origin": (200, 640), "cols": 5, "rows": 3},
]

# Grade field: a smooth "ore body" bulge plus deterministic noise, so blocks
# near the centre of the pit are richer. Evaluated in blocks.py.
GRADE_CENTER = (330, 560)    # x, y of the richest point
GRADE_PEAK = 1.10            # % Cu at the centre
GRADE_FLOOR = 0.10           # % Cu far from the centre
GRADE_RADIUS = 130.0         # m, fall-off scale
HARDNESS_MIN, HARDNESS_MAX = 12.0, 18.0   # Bond Wi, kWh/t

# ---------------------------------------------------------------------------
# Fleet
#   Excavator: Komatsu PC2000-11 class, 12 m3 bucket, 22 t per pass in blasted
#   rock, 30-35 s pass cycle (Komatsu PC2000-11 spec sheet; Caterpillar
#   Performance Handbook, hydraulic shovel cycle times).
#   Haul truck: Caterpillar 777G, nominal payload 98.6 t, operated at 91 t
#   (CAT 777G spec sheet); loaded speed on an 8 % ramp 15-20 km/h, empty on
#   the flat 35-40 km/h (CAT 777G rimpull-speed curves); fuel burn 777 class:
#   hauling loaded 65-80 L/h, empty 35-45 L/h, idle 8-12 L/h (Caterpillar
#   Performance Handbook fuel consumption tables, medium-high duty).
#   OpenMines north_pit_mine.json (MIT; team/data/openmines) gives a real pit
#   with 71 trucks of 35-77 t at 25 km/h average and 3-5 km hauls, 20 t
#   shovels at 1.5 min per load: our hauls (2.4-4.2 km) and cycle times sit
#   inside that envelope.
# ---------------------------------------------------------------------------
EXCAVATORS = [
    {"id": "excavator-01", "name": "Экскаватор ЭКГ-1 (PC2000)", "bench": "bench-545", "bucket_t": 22.0, "pass_s": 32.0},
    {"id": "excavator-02", "name": "Экскаватор ЭКГ-2 (PC2000)", "bench": "bench-530", "bucket_t": 22.0, "pass_s": 32.0},
]

TRUCK_COUNT = 6
TRUCK = {
    "model": "CAT 777G",
    "capacity_t": 91.0,
    "tank_l": 1137.0,                 # CAT 777G fuel tank
    "speed_loaded_ramp_kmh": 17.0,    # 8 % ramp, loaded
    "speed_loaded_flat_kmh": 32.0,
    "speed_empty_ramp_kmh": 28.0,     # downhill empty, retarder-limited
    "speed_empty_flat_kmh": 38.0,
    "fuel_lph": {"hauling": 72.0, "returning": 40.0, "idle": 10.0, "loading": 12.0, "dumping": 12.0},
    "dump_s": 55.0,                   # body raise + lower, 777 class (Cat handbook ~0.9 min)
    "spot_s": 25.0,                   # spotting at the shovel
    "refuel_below_l": 180.0,          # ASSUMPTION: refuel when < ~16 % of tank
    "refuel_rate_lpm": 400.0,         # high-flow diesel nozzle 300-500 L/min
}

# ---------------------------------------------------------------------------
# Road network: nodes with coordinates; segments with length (m, measured along
# the switchbacks, hence longer than the straight line), gradient (%), and a
# polyline for drawing. Lengths are ASSUMPTIONS consistent with OpenMines
# haul distances (3-5 km) for a mid-sized pit.
# ---------------------------------------------------------------------------
NODES = {
    "face-545":  {"xy": (300, 440),  "name": "Забой 545"},
    "face-530":  {"xy": (360, 700),  "name": "Забой 530"},
    "ramp-top":  {"xy": (760, 560),  "name": "Выезд из карьера"},
    "junction":  {"xy": (1040, 560), "name": "Развязка"},
    "crusher":   {"xy": (1420, 400), "name": "Дробилка"},
    "stockpile": {"xy": (1340, 640), "name": "Рудный склад"},
    "dump":      {"xy": (1160, 960), "name": "Отвал"},
    "fuel":      {"xy": (900, 360),  "name": "АЗС"},
}

SEGMENTS = [
    # id, from, to, length m, grade % (positive = climbing from -> to), polyline
    {"id": "road-ramp-west", "a": "face-545", "b": "ramp-top", "length_m": 1250, "grade_pct": 8.0,
     "polyline": [(300, 440), (420, 400), (560, 430), (620, 520), (760, 560)]},
    {"id": "road-ramp-east", "a": "face-530", "b": "ramp-top", "length_m": 1650, "grade_pct": 8.0,
     "polyline": [(360, 700), (520, 760), (660, 700), (700, 620), (760, 560)]},
    {"id": "road-bench-link", "a": "face-545", "b": "face-530", "length_m": 520, "grade_pct": -3.0,
     "polyline": [(300, 440), (250, 560), (300, 660), (360, 700)]},
    {"id": "road-main", "a": "ramp-top", "b": "junction", "length_m": 300, "grade_pct": 0.0,
     "polyline": [(760, 560), (1040, 560)]},
    {"id": "road-crusher", "a": "junction", "b": "crusher", "length_m": 520, "grade_pct": 1.0,
     "polyline": [(1040, 560), (1200, 470), (1420, 400)]},
    {"id": "road-stockpile", "a": "junction", "b": "stockpile", "length_m": 340, "grade_pct": 0.0,
     "polyline": [(1040, 560), (1200, 620), (1340, 640)]},
    {"id": "road-dump", "a": "junction", "b": "dump", "length_m": 480, "grade_pct": 2.0,
     "polyline": [(1040, 560), (1080, 760), (1160, 960)]},
    {"id": "road-fuel", "a": "ramp-top", "b": "fuel", "length_m": 260, "grade_pct": 0.0,
     "polyline": [(760, 560), (820, 440), (900, 360)]},
]

FACE_OF_BENCH = {"bench-545": "face-545", "bench-530": "face-530"}

# ---------------------------------------------------------------------------
# Processing plant
#   Primary gyratory crusher 1 000 t/h rated (FLSmidth/Metso 54-75 class for a
#   ~25 kt/d plant); feed F80 400 mm -> product P80 150 mm.
#   SAG + ball milling to P80 150 um; flotation recovery 85-90 % at 0.4 % Cu
#   feed, falling at low grades (typical porphyry Cu metallurgy, Kaggle
#   "Quality Prediction in a Mining Process" shows the same grade/recovery
#   sensitivity); concentrate 24 % Cu.
#   Comminution energy by Bond's law: W = 10 Wi (1/sqrt(P80) - 1/sqrt(F80)),
#   kWh/t with sizes in micrometres (Bond, 1952).
# ---------------------------------------------------------------------------
CRUSHER = {"rated_tph": 1000.0, "f80_um": 400_000.0, "p80_um": 150_000.0, "no_load_kw": 150.0, "bin_capacity_t": 600.0}
MILL = {"rated_tph": 1000.0, "f80_um": 150_000.0, "p80_um": 150.0, "no_load_kw": 1200.0}
FLOTATION = {"base_recovery": 0.89, "grade_knee": 0.30, "recovery_at_floor": 0.70, "concentrate_grade": 24.0,
             "hardness_penalty_per_kwh": 0.004}  # recovery drops 0.4 % per kWh/t above 14 (coarser grind)
BLEND = {"target_grade": 0.42, "band": 0.08}    # keep crusher feed in 0.34-0.50 % Cu (ASSUMPTION: plant design range)
STOCKPILE = {"capacity_t": 60_000.0, "reclaim_tph": 300.0, "initial_t": 12_000.0, "initial_grade": 0.31}

# ---------------------------------------------------------------------------
# Power
#   Site base load (lighting, pumps, workshops) 1.5 MW; substation rated 20 MVA
#   (ASSUMPTION). Energy consumption profiles of a real mining and processing
#   plant (Zenodo 20647907, CC BY-SA 4.0) show the same order: 10-20 MW with a
#   mill-dominated share.
# ---------------------------------------------------------------------------
POWER = {"base_kw": 1500.0, "substation_mva": 20.0, "diesel_kwh_per_l": 10.0, "diesel_co2_kg_per_l": 2.68,
         "grid_co2_kg_per_kwh": 0.62}  # KZ grid, coal-heavy, ~0.6 kg CO2/kWh (IEA)

# ---------------------------------------------------------------------------
# Maintenance / reliability (module 07)
#   Mean time between failures and repair durations for 777-class trucks and
#   hydraulic shovels are ASSUMPTIONS in the range reported for open-pit
#   fleets (MTBF 60-150 h, MTTR 2-8 h; OpenMines uses lambda_repair 0.0083
#   per minute and 50-minute repairs for road events).
# ---------------------------------------------------------------------------
RELIABILITY = {
    "truck": {"mtbf_h": 110.0, "mttr_h": 3.0, "service_interval_h": 250.0, "service_h": 6.0},
    "excavator": {"mtbf_h": 150.0, "mttr_h": 5.0, "service_interval_h": 500.0, "service_h": 10.0},
}
