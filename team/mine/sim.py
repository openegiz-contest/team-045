"""
Sarybulak open-pit simulator: haul cycle with queues and a dispatcher, block
model with ore control and blending, drill-and-blast preparation, crusher,
mill and flotation, energy and diesel accounting, fleet reliability.

The simulator is pure Python with no I/O: `Mine.step(dt)` advances the state,
`Mine.snapshot()` returns the features of every twin, `Mine.command(cmd)`
applies a what-if scenario. run_sim.py wires it to MQTT.
"""

import heapq
import math
import random
from collections import deque

from . import config as C
from .model import build_blocks

# Excavator diesel burn: Komatsu PC2000 class, 180-260 L/h digging, ~40 L/h
# idling (Komatsu PC2000-11 spec sheet; operator fuel reports).
EXCAVATOR_LPH = {"loading": 240.0, "idle": 40.0, "waiting": 40.0}

# Drill and blast: 25 x 25 m block, 15 m bench + 1.5 m sub-drill, pattern
# 5.5 x 6.5 m -> ~16 holes x 16.5 m = 264 m of drilling; rotary rig 25-35 m/h
# in porphyry (Atlas Copco DM45 field data) -> ~9 h drilling + 3 h charging
# and a blast. Fragmentation grows with hardness (Kuz-Ram trend): P80 ~ 180 mm
# at Wi 12 to ~320 mm at Wi 18 (ASSUMPTION, order of magnitude from El Aljibe
# drone fragmentation dataset, Zenodo 10666378: P80 150-400 mm).
DRILL_H = 9.0
CHARGE_H = 3.0


def frag_p80_mm(hardness, rng):
    return round((180.0 + (hardness - 12.0) * 23.0) * rng.uniform(0.9, 1.1), 0)


class Segment:
    def __init__(self, spec):
        self.id = spec["id"]
        self.a, self.b = spec["a"], spec["b"]
        self.length = float(spec["length_m"])
        self.grade = float(spec["grade_pct"])
        self.poly = [tuple(p) for p in spec["polyline"]]
        self.open = True
        self.trucks = 0
        self.trips = 0
        # cumulative drawing length along the polyline for interpolation
        self.cum = [0.0]
        for (x0, y0), (x1, y1) in zip(self.poly, self.poly[1:]):
            self.cum.append(self.cum[-1] + math.hypot(x1 - x0, y1 - y0))

    def xy(self, frac, forward=True):
        """Map a fraction of the segment length to map coordinates."""
        if not forward:
            frac = 1.0 - frac
        target = max(0.0, min(1.0, frac)) * self.cum[-1]
        for i in range(1, len(self.cum)):
            if target <= self.cum[i] or i == len(self.cum) - 1:
                span = self.cum[i] - self.cum[i - 1] or 1.0
                t = (target - self.cum[i - 1]) / span
                (x0, y0), (x1, y1) = self.poly[i - 1], self.poly[i]
                return (x0 + (x1 - x0) * t, y0 + (y1 - y0) * t)
        return self.poly[-1]


class Network:
    def __init__(self):
        self.segments = {s["id"]: Segment(s) for s in C.SEGMENTS}
        self.adj = {n: [] for n in C.NODES}
        for s in self.segments.values():
            self.adj[s.a].append((s.b, s, True))
            self.adj[s.b].append((s.a, s, False))

    @staticmethod
    def speed_kmh(loaded, grade_in_direction):
        t = C.TRUCK
        if abs(grade_in_direction) < 2.0:
            return t["speed_loaded_flat_kmh"] if loaded else t["speed_empty_flat_kmh"]
        if grade_in_direction > 0:  # climbing
            return t["speed_loaded_ramp_kmh"] if loaded else t["speed_empty_ramp_kmh"] + 4.0
        return 20.0 if loaded else t["speed_empty_ramp_kmh"]  # descending, retarder-limited

    def travel_s(self, seg, forward, loaded):
        grade = seg.grade if forward else -seg.grade
        return seg.length / (self.speed_kmh(loaded, grade) / 3.6)

    def route(self, src, dst, loaded):
        """Dijkstra on open segments. Returns [(segment, forward)], or None."""
        if src == dst:
            return []
        dist = {src: 0.0}
        prev = {}
        heap = [(0.0, src)]
        while heap:
            d, n = heapq.heappop(heap)
            if n == dst:
                break
            if d > dist.get(n, math.inf):
                continue
            for m, seg, fwd in self.adj[n]:
                if not seg.open:
                    continue
                nd = d + self.travel_s(seg, fwd, loaded)
                if nd < dist.get(m, math.inf):
                    dist[m] = nd
                    prev[m] = (n, seg, fwd)
                    heapq.heappush(heap, (nd, m))
        if dst not in prev:
            return None
        path = []
        n = dst
        while n != src:
            p, seg, fwd = prev[n]
            path.append((seg, fwd))
            n = p
        path.reverse()
        return path

    def route_time(self, src, dst, loaded):
        r = self.route(src, dst, loaded)
        if r is None:
            return math.inf
        return sum(self.travel_s(s, f, loaded) for s, f in r)


class Block:
    def __init__(self, spec):
        self.__dict__.update(spec)
        self.remaining = float(spec["tonnes"])
        self.status = "insitu"      # insitu -> drilling -> charged -> blasted -> mining -> mined
        self.prep_timer = 0.0
        self.p80 = 0.0
        self.dirty = True


class Excavator:
    def __init__(self, spec):
        self.id = spec["id"]
        self.bench = spec["bench"]
        self.face = C.FACE_OF_BENCH[spec["bench"]]
        self.bucket = spec["bucket_t"]
        self.pass_s = spec["pass_s"]
        self.state = "idle"         # idle | loading | waiting | down | service | no_block
        self.block = None
        self.passes = 0
        self.tonnes = 0.0
        self.queue = deque()        # trucks waiting at this face
        self.loading_truck = None
        self.timer = 0.0
        self.hours = 0.0
        self.since_service = 0.0
        self.busy_s = 0.0
        self.total_s = 0.0
        self.forced_down = False
        self.down_timer = 0.0
        self.failures = 0


class Truck:
    def __init__(self, idx, rng):
        self.id = f"truck-{idx:02d}"
        self.idx = idx
        self.state = "idle"
        self.node = "fuel"          # current node when not on a road
        self.path = []              # remaining [(segment, forward)]
        self.seg = None
        self.fwd = True
        self.dist = 0.0             # metres along current segment
        self.xy = C.NODES["fuel"]["xy"]
        self.payload = 0.0
        self.material = ""
        self.grade = 0.0
        self.hardness = 0.0
        self.speed = 0.0
        self.fuel = C.TRUCK["tank_l"] * rng.uniform(0.5, 1.0)
        self.fuel_rate = 0.0
        self.trips = 0
        self.hours = 0.0
        self.since_service = 0.0
        self.health = 100.0
        self.excavator = None
        self.destination = ""
        self.timer = 0.0
        self.cycle_start = 0.0
        self.wait_s = 0.0
        self.forced_parked = False
        self.down_timer = 0.0
        self.failures = 0

    @property
    def loaded(self):
        return self.payload > 1.0


class Mine:
    def __init__(self, seed=42):
        self.rng = random.Random(seed)
        self.t = 0.0                            # simulated seconds
        self.net = Network()
        self.blocks = [Block(b) for b in build_blocks()]
        self.blocks_by_bench = {}
        for b in self.blocks:
            self.blocks_by_bench.setdefault(b.bench, []).append(b)
        self.excavators = [Excavator(e) for e in C.EXCAVATORS]
        self.trucks = [Truck(i, self.rng) for i in range(1, C.TRUCK_COUNT + 1)]
        self.cutoff = C.CUTOFF_GRADE
        # facilities
        self.crusher_up = True
        self.crusher_forced_down = False
        self.crusher_bin = 0.0
        self.crusher_bin_grade_t = 0.0        # sum grade*t in bin
        self.crusher_bin_hard_t = 0.0
        self.crusher_tph = 0.0
        self.crusher_total = 0.0
        self.crusher_feed_log = deque()       # (t, tonnes, grade) dumps in last hour
        self.crusher_p80_factor = 1.0
        self.crusher_queue = deque()
        self.crusher_dumping = None
        self.stock_t = C.STOCKPILE["initial_t"]
        self.stock_grade_t = C.STOCKPILE["initial_t"] * C.STOCKPILE["initial_grade"]
        self.stock_reclaim_tph = 0.0
        self.stock_queue = deque()
        self.stock_dumping = None
        self.dump_t = 0.0
        self.dump_queue = deque()
        self.dump_dumping = None
        self.fuel_queue = deque()
        self.fuel_busy = None
        self.diesel_issued = 0.0
        # plant
        self.mill_tph = 0.0
        self.mill_total = 0.0
        self.mill_power = 0.0
        self.mill_specific = 0.0
        self.mill_grade = 0.0
        self.crusher_power = 0.0
        self.recovery = 0.0
        self.conc_tph = 0.0
        self.conc_total = 0.0
        self.cu_total = 0.0
        self.tail_grade = 0.0
        self.energy_kwh = 0.0
        self.diesel_l = 0.0
        self.diesel_rate = 0.0
        self.load_kw = 0.0
        # mine totals
        self.ore_t = 0.0
        self.waste_t = 0.0
        self.cycles = deque(maxlen=40)        # recent cycle durations, s
        self.waits = deque(maxlen=40)
        self.blend_mode = "normal"
        self.scenarios = {}                   # active what-if scenarios
        self.fleet_target = C.TRUCK_COUNT
        self.events = deque(maxlen=50)        # (t, text) for the UI
        self.events_total = 0                 # ever logged, so publishers can find the new ones
        self.dispatch_table = {}
        # the mine starts mid-operation: two blasted blocks ahead on each bench, a third being drilled
        for blocks in self.blocks_by_bench.values():
            for b in blocks[:2]:
                b.status, b.p80 = "blasted", frag_p80_mm(b.hardness, self.rng)
            if len(blocks) > 2:
                blocks[2].status, blocks[2].prep_timer = "drilling", self.rng.uniform(0, DRILL_H * 3600)
        for ex in self.excavators:
            self._next_block(ex)

    # ------------------------------------------------------------------ util
    def log(self, text):
        self.events.append((round(self.t / 3600, 2), text))
        self.events_total += 1

    def feed_grade_1h(self):
        tot = sum(w for _, w, _ in self.crusher_feed_log)
        if tot <= 0:
            return 0.0
        return sum(w * g for _, w, g in self.crusher_feed_log) / tot

    def stock_grade(self):
        return self.stock_grade_t / self.stock_t if self.stock_t > 1 else 0.0

    # ------------------------------------------------------------ geology
    def _next_block(self, ex):
        """Hand the excavator the next prepared block on its bench."""
        cand = [b for b in self.blocks_by_bench[ex.bench] if b.status == "blasted"]
        if not cand:
            ex.block = None
            ex.state = "no_block"
            return
        b = cand[0]
        b.status = "mining"
        b.dirty = True
        ex.block = b
        if ex.state == "no_block":
            ex.state = "idle"
        self.log(f"{ex.id}: начал отработку блока {b.id} ({b.grade} % Cu, {'руда' if b.grade >= self.cutoff else 'порода'})")

    def _step_prep(self, dt):
        """Drill-and-blast crew: keep two blasted blocks ahead on every bench."""
        for bench, blocks in self.blocks_by_bench.items():
            ready = sum(1 for b in blocks if b.status in ("blasted",))
            inprog = [b for b in blocks if b.status in ("drilling", "charged")]
            for b in inprog:
                b.prep_timer += dt
                if b.status == "drilling" and b.prep_timer >= DRILL_H * 3600:
                    b.status, b.prep_timer, b.dirty = "charged", 0.0, True
                elif b.status == "charged" and b.prep_timer >= CHARGE_H * 3600:
                    b.status, b.dirty = "blasted", True
                    b.p80 = frag_p80_mm(b.hardness, self.rng)
                    self.log(f"Взрыв блока {b.id}: P80 {b.p80:.0f} мм")
            if ready + len(inprog) < 2:
                nxt = next((b for b in blocks if b.status == "insitu"), None)
                if nxt:
                    nxt.status, nxt.prep_timer, nxt.dirty = "drilling", 0.0, True

    # ---------------------------------------------------------- dispatcher
    def _choose_excavator(self, truck):
        """Min expected wait, nudged by the blend target. Returns excavator or None."""
        best, best_score = None, math.inf
        feed = self.feed_grade_1h()
        lo, hi = C.BLEND["target_grade"] - C.BLEND["band"], C.BLEND["target_grade"] + C.BLEND["band"]
        for ex in self.excavators:
            if ex.state in ("down", "service", "no_block") or ex.block is None:
                continue
            travel = self.net.route_time(truck.node, ex.face, loaded=False)
            if travel == math.inf:
                continue
            load_s = ex.pass_s * math.ceil(C.TRUCK["capacity_t"] / ex.bucket) + C.TRUCK["spot_s"]
            inbound = sum(1 for t in self.trucks if t.excavator is ex and t.state in ("to_face", "queued_at_face", "spotting", "loading"))
            score = travel + inbound * load_s
            # blending: when the crusher feed drifts out of band, prefer the face that pulls it back
            if ex.block.grade >= self.cutoff and feed > 0:
                if feed > hi and ex.block.grade < feed:
                    score -= 240
                elif feed < lo and ex.block.grade > feed:
                    score -= 240
            if score < best_score:
                best, best_score = ex, score
        return best

    def _choose_destination(self, truck):
        """Ore control + blending: where does this load go?"""
        if truck.material == "waste":
            return "dump"
        feed = self.feed_grade_1h()
        hi = C.BLEND["target_grade"] + C.BLEND["band"]
        crusher_ok = self.crusher_up and self.crusher_bin < C.CRUSHER["bin_capacity_t"] - C.TRUCK["capacity_t"] \
            and len(self.crusher_queue) < 3
        if not crusher_ok:
            return "stockpile" if self.stock_t < C.STOCKPILE["capacity_t"] else "dump"
        # a very rich load on an already-rich feed goes to the stockpile for later blending
        if feed > hi and truck.grade > hi and self.stock_t < C.STOCKPILE["capacity_t"] * 0.9:
            self.blend_mode = "divert_rich_to_stockpile"
            return "stockpile"
        return "crusher"

    # ------------------------------------------------------------- trucks
    def _start_trip(self, truck, dst):
        path = self.net.route(truck.node, dst, truck.loaded)
        if path is None:
            truck.state = "blocked"
            truck.speed = 0.0
            return False
        truck.path = path
        truck.destination = dst
        truck.state = "hauling" if truck.loaded else "to_face" if dst in C.FACE_OF_BENCH.values() else "returning"
        if dst == "fuel":
            truck.state = "to_fuel"
        if not path:
            self._arrive(truck)
        else:
            self._enter_segment(truck)
        return True

    def _reroute(self, truck, dst):
        """Change destination while driving: finish the current segment, then follow a new route."""
        truck.destination = dst
        if truck.seg is None:
            self._start_trip(truck, dst)
            return
        end = truck.seg.b if truck.fwd else truck.seg.a
        rest = self.net.route(end, dst, truck.loaded)
        truck.path = rest if rest is not None else []
        if rest is None:
            truck.state = "blocked"

    def _enter_segment(self, truck):
        seg, fwd = truck.path.pop(0)
        truck.seg, truck.fwd, truck.dist = seg, fwd, 0.0
        seg.trucks += 1

    def _drive(self, truck, dt):
        seg = truck.seg
        grade = seg.grade if truck.fwd else -seg.grade
        truck.speed = self.net.speed_kmh(truck.loaded, grade) * self.rng.uniform(0.95, 1.05)
        truck.dist += truck.speed / 3.6 * dt
        if truck.dist >= seg.length:
            seg.trucks -= 1
            seg.trips += 1
            truck.node = seg.b if truck.fwd else seg.a
            truck.xy = C.NODES[truck.node]["xy"]
            if truck.path:
                self._enter_segment(truck)
            else:
                truck.seg = None
                truck.speed = 0.0
                self._arrive(truck)
        else:
            truck.xy = seg.xy(truck.dist / seg.length, truck.fwd)

    def _arrive(self, truck):
        dst = truck.destination
        if dst in C.FACE_OF_BENCH.values():
            ex = truck.excavator
            if ex is None or ex.state in ("down", "service", "no_block"):
                truck.state = "idle"          # arrived at a dead face: ask the dispatcher again
                truck.excavator = None
                return
            truck.state = "queued_at_face"
            truck.timer = 0.0
            ex.queue.append(truck)
        elif dst == "crusher":
            if not self.crusher_up:              # crusher went down while we were hauling
                self._start_trip(truck, "stockpile" if self.stock_t < C.STOCKPILE["capacity_t"] else "dump")
                return
            truck.state = "queued_at_crusher"
            self.crusher_queue.append(truck)
        elif dst == "stockpile":
            truck.state = "queued_at_stockpile"
            self.stock_queue.append(truck)
        elif dst == "dump":
            truck.state = "queued_at_dump"
            self.dump_queue.append(truck)
        elif dst == "fuel":
            truck.state = "queued_at_fuel"
            self.fuel_queue.append(truck)
        truck.timer = 0.0

    def _dispatch_empty(self, truck):
        """After dumping: refuel if needed, else go to the best excavator."""
        if truck.forced_parked:
            truck.state = "parked"
            if truck.node != "fuel":
                self._start_trip(truck, "fuel")
                truck.state = "to_fuel"
                truck.destination = "fuel"
            return
        if truck.fuel < C.TRUCK["refuel_below_l"]:
            self._start_trip(truck, "fuel")
            return
        ex = self._choose_excavator(truck)
        if ex is None:
            truck.state = "idle"
            truck.excavator = None
            truck.speed = 0.0
            return
        truck.excavator = ex
        truck.cycle_start = self.t
        truck.wait_s = 0.0
        self._start_trip(truck, ex.face)

    def _step_truck(self, truck, dt):
        s = truck.state
        burn = C.TRUCK["fuel_lph"]
        if s in ("down", "parked", "blocked", "service"):
            truck.fuel_rate = 0.0
            truck.speed = 0.0
            if s == "down":
                truck.down_timer -= dt
                if truck.down_timer <= 0:
                    truck.state = "idle"
                    self.log(f"{truck.id}: ремонт завершён")
                    if truck.seg is not None:
                        truck.state = "hauling" if truck.loaded else "returning"
                        if truck.destination in C.FACE_OF_BENCH.values():
                            truck.state = "to_face"
            elif s == "service":
                truck.down_timer -= dt
                if truck.down_timer <= 0:
                    truck.state = "idle"
                    truck.health = 100.0
                    truck.since_service = 0.0
            elif s == "blocked":
                # road was closed with no detour: retry every tick until a route exists
                if self.net.route(truck.node, truck.destination, truck.loaded) is not None:
                    self._start_trip(truck, truck.destination)
            elif s == "parked" and not truck.forced_parked:
                truck.state = "idle"
            return
        if s == "idle":
            truck.fuel_rate = burn["idle"]
            if truck.node == "fuel" and truck.since_service >= C.RELIABILITY["truck"]["service_interval_h"]:
                truck.state = "service"
                truck.down_timer = C.RELIABILITY["truck"]["service_h"] * 3600
                return
            self._dispatch_empty(truck)
        elif s in ("hauling", "returning", "to_face", "to_fuel"):
            truck.fuel_rate = burn["hauling"] if truck.loaded else burn["returning"]
            if s == "to_face" and (truck.excavator is None or truck.excavator.state in ("down", "service", "no_block")):
                # the excavator we were sent to is out: the dispatcher re-assigns on the fly
                alt = self._choose_excavator(truck)
                if alt is not None and alt is not truck.excavator:
                    truck.excavator = alt
                    self._reroute(truck, alt.face)
            self._drive(truck, dt)
        elif s == "queued_at_face":
            truck.fuel_rate = burn["idle"]
            truck.wait_s += dt
        elif s in ("spotting", "loading"):
            truck.fuel_rate = burn["loading"]
        elif s in ("queued_at_crusher", "queued_at_stockpile", "queued_at_dump", "queued_at_fuel"):
            truck.fuel_rate = burn["idle"]
            truck.wait_s += dt
        elif s == "dumping":
            truck.fuel_rate = burn["dumping"]
        elif s == "refueling":
            truck.fuel_rate = 0.0
            truck.fuel = min(C.TRUCK["tank_l"], truck.fuel + C.TRUCK["refuel_rate_lpm"] / 60 * dt)
            self.diesel_issued += C.TRUCK["refuel_rate_lpm"] / 60 * dt
            if truck.fuel >= C.TRUCK["tank_l"] - 1:
                self.fuel_busy = None
                truck.state = "idle"
        truck.fuel = max(0.0, truck.fuel - truck.fuel_rate / 3600 * dt)
        if truck.state not in ("idle", "parked"):
            truck.hours += dt / 3600
            truck.since_service += dt / 3600
            truck.health = max(0.0, 100.0 - 60.0 * truck.since_service / C.RELIABILITY["truck"]["service_interval_h"])
            # random failure, exponential in operating hours
            if self.rng.random() < dt / 3600 / C.RELIABILITY["truck"]["mtbf_h"] * (1.5 - truck.health / 200):
                truck.state = "down"
                truck.down_timer = C.RELIABILITY["truck"]["mttr_h"] * 3600 * self.rng.uniform(0.5, 1.5)
                truck.failures += 1
                truck.speed = 0.0
                self.log(f"{truck.id}: отказ, ремонт {truck.down_timer / 3600:.1f} ч")

    # ---------------------------------------------------------- excavators
    def _step_excavator(self, ex, dt):
        ex.total_s += dt
        if ex.state in ("down", "service"):
            ex.down_timer -= dt
            if ex.down_timer <= 0 and not ex.forced_down:
                ex.state = "idle" if ex.block else "no_block"
                if ex.state == "idle":
                    self.log(f"{ex.id}: снова в работе")
            return
        if ex.block is None:
            self._next_block(ex)
            if ex.block is None:
                return
        if ex.loading_truck is None:
            if ex.queue:
                truck = ex.queue.popleft()
                ex.loading_truck = truck
                truck.state = "spotting"
                truck.timer = 0.0
                ex.state = "loading"
            else:
                ex.state = "waiting"
                return
        truck = ex.loading_truck
        truck.timer += dt
        ex.busy_s += dt
        if truck.state == "spotting":
            if truck.timer >= C.TRUCK["spot_s"]:
                truck.state = "loading"
                truck.timer = 0.0
            return
        b = ex.block
        # pass time stretches with coarse fragmentation (Kuz-Ram trend, ASSUMPTION)
        pass_s = ex.pass_s * (1.0 + max(0.0, b.p80 - 200.0) / 600.0)
        while truck.timer >= pass_s and truck.payload < C.TRUCK["capacity_t"] - 1 and b.remaining > 0:
            truck.timer -= pass_s
            bucket = min(ex.bucket * self.rng.uniform(0.85, 1.05), C.TRUCK["capacity_t"] - truck.payload, b.remaining)
            truck.grade = (truck.grade * truck.payload + b.grade * bucket) / (truck.payload + bucket)
            truck.hardness = (truck.hardness * truck.payload + b.hardness * bucket) / (truck.payload + bucket)
            truck.payload += bucket
            b.remaining -= bucket
            b.dirty = True
            ex.passes += 1
            ex.tonnes += bucket
        if truck.payload >= C.TRUCK["capacity_t"] - 1 or (b.remaining <= 0 and truck.payload > 0):
            truck.material = "ore" if truck.grade >= self.cutoff else "waste"
            ex.loading_truck = None
            self.waits.append(truck.wait_s)
            dst = self._choose_destination(truck)
            self._start_trip(truck, dst)
        if b.remaining <= 0:
            b.status, b.remaining, b.dirty = "mined", 0.0, True
            self.log(f"Блок {b.id} отработан")
            self._next_block(ex)
        ex.hours += dt / 3600
        ex.since_service += dt / 3600
        if self.rng.random() < dt / 3600 / C.RELIABILITY["excavator"]["mtbf_h"]:
            ex.state = "down"
            ex.down_timer = C.RELIABILITY["excavator"]["mttr_h"] * 3600 * self.rng.uniform(0.5, 1.5)
            ex.failures += 1
            self._release_loading(ex)
            self.log(f"{ex.id}: отказ гидравлики, ремонт {ex.down_timer / 3600:.1f} ч")

    def _release_loading(self, ex):
        """An excavator went down: a half-loaded truck leaves with what it has."""
        truck = ex.loading_truck
        ex.loading_truck = None
        if truck is not None:
            if truck.payload > 10:
                truck.material = "ore" if truck.grade >= self.cutoff else "waste"
                self._start_trip(truck, self._choose_destination(truck))
            else:
                truck.payload = 0.0
                truck.state = "idle"
        for t in list(ex.queue):
            t.state = "idle"
            t.excavator = None
        ex.queue.clear()

    # ---------------------------------------------------------- facilities
    def _serve_dump_point(self, queue, current_attr, dt, on_done):
        cur = getattr(self, current_attr)
        if cur is None and queue:
            cur = queue.popleft()
            setattr(self, current_attr, cur)
            cur.state = "dumping"
            cur.timer = 0.0
        if cur is not None:
            cur.timer += dt
            if cur.timer >= C.TRUCK["dump_s"]:
                on_done(cur)
                cur.payload, cur.grade, cur.hardness, cur.material = 0.0, 0.0, 0.0, ""
                cur.trips += 1
                self.cycles.append(self.t - cur.cycle_start)
                setattr(self, current_attr, None)
                cur.state = "idle"
                cur.destination = ""

    def _step_facilities(self, dt):
        def to_crusher(tr):
            self.crusher_bin += tr.payload
            self.crusher_bin_grade_t += tr.payload * tr.grade
            self.crusher_bin_hard_t += tr.payload * tr.hardness
            self.crusher_feed_log.append((self.t, tr.payload, tr.grade))
            self.ore_t += tr.payload

        def to_stock(tr):
            self.stock_t += tr.payload
            self.stock_grade_t += tr.payload * tr.grade
            self.ore_t += tr.payload

        def to_dump(tr):
            self.dump_t += tr.payload
            self.waste_t += tr.payload

        if self.crusher_up:
            self._serve_dump_point(self.crusher_queue, "crusher_dumping", dt, to_crusher)
        self._serve_dump_point(self.stock_queue, "stock_dumping", dt, to_stock)
        self._serve_dump_point(self.dump_queue, "dump_dumping", dt, to_dump)
        # fuel station: one bay
        if self.fuel_busy is None and self.fuel_queue:
            self.fuel_busy = self.fuel_queue.popleft()
            self.fuel_busy.state = "refueling"
        # crusher feed window
        while self.crusher_feed_log and self.t - self.crusher_feed_log[0][0] > 3600:
            self.crusher_feed_log.popleft()

        # stockpile reclaim: blend or feed a starved crusher
        self.stock_reclaim_tph = 0.0
        feed = self.feed_grade_1h()
        lo, hi = C.BLEND["target_grade"] - C.BLEND["band"], C.BLEND["target_grade"] + C.BLEND["band"]
        sg = self.stock_grade()
        want = False
        if self.crusher_up and self.stock_t > 50:
            if self.crusher_bin < 100:
                want, self.blend_mode = True, "reclaim_starved_crusher"
            elif feed > hi and sg < feed - 0.02:
                want, self.blend_mode = True, "reclaim_low_grade_to_blend_down"
            elif 0 < feed < lo and sg > feed + 0.02:
                want, self.blend_mode = True, "reclaim_high_grade_to_blend_up"
        if want:
            tons = min(C.STOCKPILE["reclaim_tph"] / 3600 * dt, self.stock_t,
                       max(0.0, C.CRUSHER["bin_capacity_t"] - self.crusher_bin))
            if tons > 0:
                self.stock_reclaim_tph = tons / dt * 3600
                self.stock_t -= tons
                self.stock_grade_t -= tons * sg
                self.crusher_bin += tons
                self.crusher_bin_grade_t += tons * sg
                self.crusher_bin_hard_t += tons * 14.0
                self.crusher_feed_log.append((self.t, tons, sg))
        elif self.blend_mode != "normal" and lo <= feed <= hi:
            self.blend_mode = "normal"

        # crusher -> mill -> flotation
        if self.crusher_up and self.crusher_bin > 0:
            # coarse blast fragmentation lowers crusher throughput (ASSUMPTION: -15 % at P80 350 mm)
            p80s = [b.p80 for ex in self.excavators if (b := ex.block) and b.p80]
            p80 = sum(p80s) / len(p80s) if p80s else 220.0
            self.crusher_p80_factor = max(0.7, min(1.0, 1.15 - p80 / 1000.0))
            rate = C.CRUSHER["rated_tph"] * self.crusher_p80_factor
            tons = min(rate / 3600 * dt, self.crusher_bin)
            g = self.crusher_bin_grade_t / self.crusher_bin
            h = self.crusher_bin_hard_t / self.crusher_bin
            self.crusher_bin -= tons
            self.crusher_bin_grade_t -= tons * g
            self.crusher_bin_hard_t -= tons * h
            self.crusher_tph = tons / dt * 3600
            self.crusher_total += tons
            self.mill_tph = min(self.crusher_tph, C.MILL["rated_tph"])
            self.mill_grade = g
            self.mill_total += tons
            bond = lambda wi, f, p: 10 * wi * (1 / math.sqrt(p) - 1 / math.sqrt(f))
            self.crusher_power = C.CRUSHER["no_load_kw"] + bond(h, C.CRUSHER["f80_um"], C.CRUSHER["p80_um"]) * self.crusher_tph
            self.mill_specific = bond(h, C.MILL["f80_um"], C.MILL["p80_um"])
            self.mill_power = C.MILL["no_load_kw"] + self.mill_specific * self.mill_tph
            F = C.FLOTATION
            rec = F["base_recovery"]
            if g < F["grade_knee"]:
                rec -= (F["base_recovery"] - F["recovery_at_floor"]) * (F["grade_knee"] - g) / F["grade_knee"]
            rec -= F["hardness_penalty_per_kwh"] * max(0.0, h - 14.0)
            self.recovery = max(0.4, rec)
            cu_tph = self.mill_tph * g / 100 * self.recovery
            self.conc_tph = cu_tph / (F["concentrate_grade"] / 100)
            self.conc_total += self.conc_tph / 3600 * dt
            self.cu_total += cu_tph / 3600 * dt
            self.tail_grade = g * (1 - self.recovery)
        else:
            self.crusher_tph = self.mill_tph = self.conc_tph = 0.0
            self.crusher_power = C.CRUSHER["no_load_kw"] if self.crusher_up else 0.0
            self.mill_power = C.MILL["no_load_kw"]
            self.recovery = 0.0

        # power and diesel
        self.load_kw = C.POWER["base_kw"] + self.crusher_power + self.mill_power
        self.energy_kwh += self.load_kw / 3600 * dt
        rate = sum(t.fuel_rate for t in self.trucks)
        for ex in self.excavators:
            rate += EXCAVATOR_LPH.get(ex.state, 0.0)
        self.diesel_rate = rate
        self.diesel_l += rate / 3600 * dt

    # ------------------------------------------------------------ stepping
    def step(self, dt):
        self.t += dt
        self._step_prep(dt)
        for ex in self.excavators:
            self._step_excavator(ex, dt)
        for truck in self.trucks:
            self._step_truck(truck, dt)
        self._step_facilities(dt)

    def run(self, seconds, dt=2.0):
        n = int(seconds / dt)
        for _ in range(n):
            self.step(dt)

    # ---------------------------------------------------------- scenarios
    def command(self, cmd):
        """Apply a what-if scenario. cmd = {"action": ..., ...}. Returns a text."""
        a = cmd.get("action")
        on = bool(cmd.get("on", True))
        if a == "excavator_down":
            ex = next((e for e in self.excavators if e.id == cmd.get("target")), None)
            if ex is None:
                return "unknown excavator"
            ex.forced_down = on
            if on:
                ex.state, ex.down_timer = "down", 1e12
                self._release_loading(ex)
                self.scenarios[f"excavator_down:{ex.id}"] = True
            else:
                ex.down_timer = 0.0
                ex.state = "idle" if ex.block else "no_block"
                self.scenarios.pop(f"excavator_down:{ex.id}", None)
            self.log(f"Сценарий: {ex.id} {'остановлен' if on else 'возвращён в работу'}")
        elif a == "crusher_down":
            self.crusher_forced_down = on
            self.crusher_up = not on
            if on:
                self.scenarios["crusher_down"] = True
                self.crusher_dumping = None
                for t in list(self.crusher_queue):
                    self.crusher_queue.remove(t)
                    self._start_trip(t, "stockpile")
                for t in self.trucks:
                    if t.state == "hauling" and t.destination == "crusher":
                        self._reroute(t, "stockpile")
            else:
                self.scenarios.pop("crusher_down", None)
            self.log(f"Сценарий: дробилка {'остановлена' if on else 'запущена'}")
        elif a == "road_closed":
            seg = self.net.segments.get(cmd.get("target"))
            if seg is None:
                return "unknown road"
            seg.open = not on
            key = f"road_closed:{seg.id}"
            if on:
                self.scenarios[key] = True
                # trucks already on the segment finish it; new routes avoid it
                for t in self.trucks:
                    if t.path and any(s is seg for s, _ in t.path):
                        rest = self.net.route(t.seg.b if t.fwd else t.seg.a, t.destination, t.loaded) if t.seg else None
                        if rest is not None:
                            t.path = rest
                        else:
                            t.path = []
                            t.state = "blocked"
            else:
                self.scenarios.pop(key, None)
                for t in self.trucks:
                    if t.state == "blocked":
                        self._start_trip(t, t.destination)
            self.log(f"Сценарий: дорога {seg.id} {'закрыта' if on else 'открыта'}")
        elif a == "fleet":
            n = int(cmd.get("count", C.TRUCK_COUNT))
            n = max(1, min(C.TRUCK_COUNT, n))
            self.fleet_target = n
            for t in self.trucks:
                t.forced_parked = t.idx > n
                if not t.forced_parked and t.state == "parked":
                    t.state = "idle"
            if n == C.TRUCK_COUNT:
                self.scenarios.pop("fleet", None)
            else:
                self.scenarios["fleet"] = n
            self.log(f"Сценарий: в работе {n} самосвалов")
        elif a == "cutoff":
            v = float(cmd.get("value", C.CUTOFF_GRADE))
            self.cutoff = max(0.05, min(1.0, v))
            if abs(self.cutoff - C.CUTOFF_GRADE) < 1e-6:
                self.scenarios.pop("cutoff", None)
            else:
                self.scenarios["cutoff"] = self.cutoff
            for b in self.blocks:
                b.dirty = True
            self.log(f"Сценарий: бортовое содержание {self.cutoff} % Cu")
        elif a == "truck_down":
            t = next((x for x in self.trucks if x.id == cmd.get("target")), None)
            if t is None:
                return "unknown truck"
            if on and t.state not in ("down",):
                if t.excavator and t in t.excavator.queue:
                    t.excavator.queue.remove(t)
                if t.excavator and t.excavator.loading_truck is t:
                    t.excavator.loading_truck = None
                t.state, t.down_timer = "down", C.RELIABILITY["truck"]["mttr_h"] * 3600
                self.log(f"Сценарий: {t.id} выведен в ремонт")
        elif a == "reset":
            for key in list(self.scenarios):
                if key.startswith("excavator_down:"):
                    self.command({"action": "excavator_down", "target": key.split(":")[1], "on": False})
                elif key.startswith("road_closed:"):
                    self.command({"action": "road_closed", "target": key.split(":")[1], "on": False})
            if self.crusher_forced_down:
                self.command({"action": "crusher_down", "on": False})
            self.command({"action": "fleet", "count": C.TRUCK_COUNT})
            self.command({"action": "cutoff", "value": C.CUTOFF_GRADE})
            self.scenarios.clear()
            self.log("Сценарии сброшены")
        else:
            return f"unknown action {a}"
        return "ok"

    # ------------------------------------------------------------ snapshot
    def snapshot(self, all_blocks=False):
        """Features of every twin that changed. Blocks only when dirty (or all_blocks)."""
        f = lambda v, u=None: {"value": v, **({"unit": u} if u else {})}
        things = {}
        active = sum(1 for t in self.trucks if t.state not in ("down", "parked", "service"))
        avail = 100.0 * sum(1 for t in self.trucks if t.state not in ("down", "service")) / len(self.trucks)
        things[C.MINE_ID] = {
            "sim_time_h": f(round(self.t / 3600, 2), "h"),
            "ore_mined_t": f(round(self.ore_t), "t"),
            "waste_mined_t": f(round(self.waste_t), "t"),
            "strip_ratio": f(round(self.waste_t / self.ore_t, 2) if self.ore_t else 0, "t/t"),
            "concentrate_t": f(round(self.conc_total, 1), "t"),
            "copper_in_concentrate_t": f(round(self.cu_total, 2), "t Cu"),
            "trucks_active": f(active, "trucks"),
            "fleet_availability_pct": f(round(avail, 1), "%"),
            "active_scenarios": f(", ".join(f"{k}={v}" if v is not True else k for k, v in self.scenarios.items()) or "none"),
        }
        for ex in self.excavators:
            things[ex.id] = {
                "state": f(ex.state), "block": f(ex.block.id if ex.block else ""),
                "face_grade": f(ex.block.grade if ex.block else 0, "% Cu"),
                "passes_total": f(ex.passes, "passes"), "tonnes_loaded": f(round(ex.tonnes), "t"),
                "queue": f(len(ex.queue) + (1 if ex.loading_truck else 0), "trucks"),
                "utilization_pct": f(round(100 * ex.busy_s / ex.total_s, 1) if ex.total_s else 0, "%"),
                "hours": f(round(ex.hours, 1), "h"),
                "health_pct": f(round(max(0, 100 - 60 * ex.since_service / C.RELIABILITY["excavator"]["service_interval_h"]), 1), "%"),
            }
        for t in self.trucks:
            things[t.id] = {
                "state": f(t.state), "x": f(round(t.xy[0], 1), "m"), "y": f(round(t.xy[1], 1), "m"),
                "road": f(t.seg.id if t.seg else ""), "destination": f(t.destination),
                "payload": f(round(t.payload, 1), "t"), "material": f(t.material),
                "load_grade": f(round(t.grade, 3), "% Cu"), "speed": f(round(t.speed, 1), "km/h"),
                "fuel": f(round(t.fuel), "L"), "fuel_rate": f(round(t.fuel_rate, 1), "L/h"),
                "trips_total": f(t.trips, "trips"), "hours": f(round(t.hours, 1), "h"),
                "health_pct": f(round(t.health, 1), "%"), "assigned_excavator": f(t.excavator.id if t.excavator else ""),
            }
        for seg in self.net.segments.values():
            things[seg.id] = {"status": f("open" if seg.open else "closed"), "trucks_on_road": f(seg.trucks, "trucks"),
                              "trips_total": f(seg.trips, "trips")}
        for node_id in C.NODES:
            q = {"crusher": self.crusher_queue, "stockpile": self.stock_queue, "dump": self.dump_queue,
                 "fuel": self.fuel_queue}.get(node_id)
            if node_id in C.FACE_OF_BENCH.values():
                ex = next(e for e in self.excavators if e.face == node_id)
                things[node_id] = {"queue": f(len(ex.queue), "trucks")}
            elif q is not None:
                things[node_id] = {"queue": f(len(q), "trucks")}
        things["crusher"].update({
            "state": f("crushing" if self.crusher_up and self.crusher_tph > 0 else "idle" if self.crusher_up else "down"),
            "throughput": f(round(self.crusher_tph), "t/h"), "feed_grade": f(round(self.feed_grade_1h(), 3), "% Cu"),
            "bin_level": f(round(self.crusher_bin), "t"), "power": f(round(self.crusher_power), "kW"),
            "tonnes_total": f(round(self.crusher_total), "t")})
        things["stockpile"].update({"tonnes": f(round(self.stock_t), "t"), "grade": f(round(self.stock_grade(), 3), "% Cu"),
                                    "reclaiming": f(round(self.stock_reclaim_tph), "t/h")})
        things["dump"].update({"tonnes": f(round(self.dump_t), "t")})
        things["fuel"].update({"diesel_issued_l": f(round(self.diesel_issued), "L")})
        things["mill"] = {"state": f("grinding" if self.mill_tph > 0 else "idle"), "throughput": f(round(self.mill_tph), "t/h"),
                          "feed_grade": f(round(self.mill_grade, 3), "% Cu"), "specific_energy": f(round(self.mill_specific, 2), "kWh/t"),
                          "power": f(round(self.mill_power), "kW"), "tonnes_total": f(round(self.mill_total), "t")}
        things["flotation"] = {"recovery": f(round(100 * self.recovery, 1), "%"), "concentrate_tph": f(round(self.conc_tph, 1), "t/h"),
                               "concentrate_total": f(round(self.conc_total, 1), "t"), "tailings_grade": f(round(self.tail_grade, 3), "% Cu")}
        things["plant"] = {"state": f("running" if self.mill_tph > 0 else "starved" if self.crusher_up else "crusher_down")}
        co2 = self.diesel_l * C.POWER["diesel_co2_kg_per_l"] / 1000 + self.energy_kwh * C.POWER["grid_co2_kg_per_kwh"] / 1000
        things["substation"] = {"load": f(round(self.load_kw), "kW"), "load_pct": f(round(100 * self.load_kw / (C.POWER["substation_mva"] * 1000), 1), "%"),
                                "energy_total": f(round(self.energy_kwh), "kWh"), "diesel_rate": f(round(self.diesel_rate), "L/h"),
                                "diesel_total": f(round(self.diesel_l), "L"), "co2_total": f(round(co2, 1), "t CO2")}
        things["dispatcher"] = {
            "assignments": f("; ".join(f"{t.id}->{t.excavator.id}" for t in self.trucks if t.excavator) or "none"),
            "avg_cycle_min": f(round(sum(self.cycles) / len(self.cycles) / 60, 1) if self.cycles else 0, "min"),
            "avg_queue_min": f(round(sum(self.waits) / len(self.waits) / 60, 1) if self.waits else 0, "min"),
            "blend_mode": f(self.blend_mode)}
        for bench, blocks in self.blocks_by_bench.items():
            things[bench] = {"blocks_mined": f(sum(1 for b in blocks if b.status == "mined"), "blocks"),
                             "tonnes_remaining": f(round(sum(b.remaining for b in blocks)), "t")}
        for b in self.blocks:
            if b.dirty or all_blocks:
                things[b.id] = {"status": f(b.status), "tonnes_remaining": f(round(b.remaining), "t"),
                                "fragmentation_p80_mm": f(b.p80, "mm"),
                                "material": f("ore" if b.grade >= self.cutoff else "waste")}
                b.dirty = False
        return things
