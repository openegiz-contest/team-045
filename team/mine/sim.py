"""
Симулятор горизонта ВЖР: вентиляционная сеть (Харди–Кросс), аэрогазовый
режим камер, горное давление и сейсмичность, цикл камеры (бурение, заряжание,
взрыв, проветривание, оборка, крепление, выпуск), люди и техника, наряд смены
с вердиктом по каждой камере, рудоспуски, дробилка и подъём.

Чистый Python без ввода-вывода: Mine.step(dt), Mine.snapshot(), Mine.command().
"""

import math
import random
from collections import deque

from . import config as C
from .model import build_chambers, pillar_strength_mpa, pillar_stress_mpa, airway_resistance

R_CLOSED = 5000.0        # сопротивление закрытой двери / завала, Н·с²/м⁸ (практически непроницаемо)
R_REGULATOR_OPEN = 0.0
SURFACE = "surface"


# ============================================================ вентиляция
class Airway:
    def __init__(self, spec):
        self.id = spec["id"]
        self.a, self.b = spec["a"], spec["b"]
        self.L, self.A = spec["L"], spec["A"]
        self.R0 = airway_resistance(spec)
        self.kind = spec["kind"]
        self.door = spec.get("door", "")
        self.panel = spec.get("panel")
        self.dead_end = spec.get("dead_end", False)
        self.blocked = False
        self.door_open = self.door == "regulator"      # регулятор в норме открыт, дверь закрыта
        self.regulator_extra = 0.0                     # дополнительное сопротивление регулятора
        self.q = 0.0                                   # м3/с, + от a к b
        self.co = 0.0
        self.no2 = 0.0
        self.temp = C.AIR["rock_temp_c"]
        self.people = 0
        self.vehicles = 0

    @property
    def R(self):
        if self.blocked:
            return R_CLOSED
        if self.door == "door" and not self.door_open:
            return R_CLOSED
        if self.door == "regulator" and not self.door_open:
            return R_CLOSED
        return self.R0 + self.regulator_extra

    @property
    def velocity(self):
        return abs(self.q) / self.A


class VentNetwork:
    """Сеть выработок + поверхность. Вентилятор на стволе: p = p0·(1 − (Q/Qmax)²)."""

    def __init__(self):
        self.airways = {a["id"]: Airway(a) for a in C.AIRWAYS}
        # стволы: поверхность — узел ствола, малое сопротивление
        for sid in ("shaft-cage", "shaft-skip", "shaft-vent"):
            self.airways[f"shaft:{sid}"] = Airway({"id": f"shaft:{sid}", "a": SURFACE, "b": sid, "L": C.DEPTH_M, "A": 20.0, "P": 16.0, "kind": "ствол"})
        self.fan_speed = 1.0      # доля номинала
        self.fan_on = True
        self.fan_q = 0.0
        self.fan_p = 0.0
        self.nodes = set()
        for aw in self.airways.values():
            self.nodes.update((aw.a, aw.b))
        self.meshes = self._find_meshes()

    def _find_meshes(self):
        """Базовые циклы: остовное дерево из ветвей с наименьшим сопротивлением (Краскал),
        остальные ветви — хорды. Так закрытые двери и завалы попадают в хорды, и метод
        Харди–Кросса сходится быстро."""
        parent_uf = {n: n for n in self.nodes}

        def find(x):
            while parent_uf[x] != x:
                parent_uf[x] = parent_uf[parent_uf[x]]
                x = parent_uf[x]
            return x

        tree, chords = [], []
        for aw in sorted(self.airways.values(), key=lambda a: a.R):
            ra, rb = find(aw.a), find(aw.b)
            if ra == rb:
                chords.append(aw)
            else:
                parent_uf[ra] = rb
                tree.append(aw)
        adj = {}
        for aw in tree:
            adj.setdefault(aw.a, []).append((aw.b, aw))
            adj.setdefault(aw.b, []).append((aw.a, aw))
        parent = {SURFACE: None}
        stack = [SURFACE]
        while stack:
            n = stack.pop()
            for m, aw in adj.get(n, []):
                if m not in parent:
                    parent[m] = (n, aw)
                    stack.append(m)

        def path(n):
            p = []
            while parent.get(n):
                pn, paw = parent[n]
                p.append((paw, n))
                n = pn
            return p

        meshes = []
        for aw in chords:
            pa, pb = path(aw.a), path(aw.b)
            sa = {x[0].id for x in pa}
            sb = {x[0].id for x in pb}
            mesh = [(aw, 1)]
            for paw, child in pb:                                     # от b вверх к общему предку
                if paw.id in sa:
                    break
                mesh.append((paw, 1 if paw.a == child else -1))
            for paw, child in pa:                                     # от общего предка вниз к a
                if paw.id in sb:
                    break
                mesh.append((paw, -1 if paw.a == child else 1))
            meshes.append(mesh)
        return meshes

    def fan_pressure(self, q):
        if not self.fan_on or self.fan_speed <= 0:
            return 0.0
        qmax = C.FAN["q_nominal_m3s"] * 1.6 * self.fan_speed
        p0 = C.FAN["p_nominal_pa"] * 1.5 * self.fan_speed ** 2
        return max(0.0, p0 * (1 - (q / qmax) ** 2))

    def solve(self, iterations=120):
        """Харди–Кросс. Вентилятор всасывающий на ветви shaft:shaft-vent: напор действует от
        ствола (b) к поверхности (a), то есть против направления ветви."""
        fan = self.airways["shaft:shaft-vent"]
        self.meshes = self._find_meshes()          # топология меняется (двери, завалы)
        qmax = C.FAN["q_nominal_m3s"] * 1.6 * max(0.05, self.fan_speed)
        p0 = C.FAN["p_nominal_pa"] * 1.5 * self.fan_speed ** 2 if self.fan_on else 0.0
        # начальное приближение, удовлетворяющее балансу узлов: небольшой расход по каждому контуру
        for aw in self.airways.values():
            aw.q = 0.0
        for mesh in self.meshes:
            for aw, sgn in mesh:
                aw.q += 5.0 * sgn
        for _ in range(iterations):
            for mesh in self.meshes:
                num = den = 0.0
                for aw, sgn in mesh:
                    q = aw.q * sgn
                    num += aw.R * q * abs(q)
                    den += 2 * aw.R * abs(q)
                    if aw is fan and p0 > 0:
                        qf = abs(aw.q)
                        h = max(0.0, p0 * (1 - (qf / qmax) ** 2))
                        num += sgn * h                    # напор направлен b->a: в контуре со знаком sgn даёт -(-sgn)·h
                        den += 2 * p0 * qf / qmax ** 2
                if den < 1e-9:
                    continue
                dq = max(-40.0, min(40.0, -num / den))
                for aw, sgn in mesh:
                    aw.q += dq * sgn
        self.fan_q = -fan.q
        self.fan_p = max(0.0, p0 * (1 - (abs(fan.q) / qmax) ** 2)) if p0 > 0 else 0.0

    def flow_into(self, node):
        return sum(aw.q for aw in self.airways.values() if aw.b == node and aw.q > 0) + \
            sum(-aw.q for aw in self.airways.values() if aw.a == node and aw.q < 0)

    def path_open(self, src, dst):
        seen, stack = {src}, [src]
        while stack:
            n = stack.pop()
            if n == dst:
                return True
            for aw in self.airways.values():
                if aw.blocked or aw.id.startswith("shaft:"):
                    continue
                m = aw.b if aw.a == n else aw.a if aw.b == n else None
                if m and m not in seen:
                    seen.add(m)
                    stack.append(m)
        return False


# ============================================================ объекты
class Chamber:
    STAGES = ["drilling", "charging", "charged", "ventilating", "scaling", "bolting", "mucking"]

    def __init__(self, spec, panel):
        self.__dict__.update(spec)
        self.panel = panel
        self.layer = spec["layer"]
        self.state = spec["state"]                  # virgin | idle | <stage> | mined_out | closed
        self.remaining = 0.0 if self.state == "mined_out" else C.CHAMBER_TONNES
        self.timer = 0.0
        self.crew = None
        self.lhd = None
        self.jumbo = None
        self.aux_fan = False
        self.aux_fan_failed = False
        self.air = 0.0                              # м3/мин фактически
        self.air_required = 0.0
        self.co = 0.0                               # % об.
        self.no2 = 0.0
        self.temp = C.AIR["rock_temp_c"]
        self.blast_t = None                         # время взрыва
        self.verdict = "—"
        self.reason = ""
        self.restrict_diesel = False     # ограничение: без дизельной техники (воздух)
        self.restrict_blast = False      # ограничение: без новых взрывов (целики, сейсмика)
        self.pillar = None
        self.dirty = True

    @property
    def cx(self):
        return self.x + self.w / 2

    @property
    def cy(self):
        return self.y + self.l / 2

    @property
    def people(self):
        return self.crew.size if self.crew and self.crew.state == "в камере" else 0

    @property
    def diesel_kw(self):
        return C.LHD["kw"] if (self.lhd and self.lhd.state == "выпуск") else 0.0

    @property
    def air_required_full(self):
        """Потребность камеры, если её ПДМ работает: по ней планируется наряд (без дребезга)."""
        return C.AIR["per_person_m3min"] * C.CREW_SIZE + (C.AIR["per_diesel_kw_m3min"] * C.LHD["kw"] if self.lhd else 0.0)

    @property
    def occupied(self):
        return self.crew is not None and self.crew.state in ("в камере", "спуск")

    def has_work(self):
        return self.state not in ("mined_out", "closed")


class Pillar:
    def __init__(self, spec, chamber):
        self.__dict__.update(spec)
        self.chamber = chamber
        self.extracted = False
        self.damage = 0.0            # 0..1 накопленное повреждение (сейсмика, время)
        self.convergence = 0.2
        self.status = "устойчив"
        self.sf = 3.0
        self.strength = 0.0
        self.stress = 0.0
        self.dirty = True


class Panel:
    def __init__(self, spec):
        self.__dict__.update(spec)
        self.in_node, self.out_node = spec["in"], spec["out"]
        self.seismic_class_initial = spec["seismic_class"]
        self.chambers = []
        self.pillars = []
        self.q_in = 0.0
        self.seismic_class = spec["seismic_class"]
        self.bumps_shift = 0
        self.energy_shift = 0.0
        self.max_bump = 0.0
        self.hazard = "низкая"
        self.evacuated_until = 0.0
        self.extra_diesel_kw = 0.0   # сценарий: самосвал/техника в панели
        self.ore_pass_level = 0.0
        self.ore_pass_total = 0.0
        self.layers_mined = 0
        self.status = "работа"


class Crew:
    def __init__(self, cid):
        self.id = cid
        self.name = f"звено-{cid.split('-')[1]}"
        self.size = C.CREW_SIZE
        self.chamber = None
        self.task = ""
        self.state = "на поверхности"      # на поверхности | спуск | в камере | выведено | подъём
        self.xy = C.NODES["shaft-cage"]["xy"]
        self.exposure = 0.0
        self.timer = 0.0


class Machine:
    def __init__(self, mid, kind, xy):
        self.id = mid
        self.kind = kind                   # lhd | jumbo
        self.chamber = None
        self.state = "стоянка"
        self.xy = xy
        self.tonnes_shift = 0.0
        self.buckets = 0
        self.rounds = 0
        self.hours = 0.0
        self.timer = 0.0


# ============================================================ рудник
class Mine:
    def __init__(self, seed=42):
        self.rng = random.Random(seed)
        self.t = 0.0
        self.shift_no = 1
        self.shift_phase = "работа"       # работа | пересменка
        self.net = VentNetwork()
        self.panels = {p["id"]: Panel(p) for p in C.PANELS}
        chambers, pillars = build_chambers()
        self.chambers = {}
        self.pillars = {}
        for c in chambers:
            ch = Chamber(c, self.panels[c["panel"]])
            self.chambers[ch.id] = ch
            self.panels[c["panel"]].chambers.append(ch)
        for p in pillars:
            pl = Pillar(p, self.chambers[p["chamber"]])
            self.pillars[pl.id] = pl
            self.chambers[p["chamber"]].pillar = pl
            self.panels[p["panel"]].pillars.append(pl)
        self.crews = [Crew(c) for c in C.CREWS]
        self.lhds = [Machine(m, "lhd", C.NODES["j-main-1"]["xy"]) for m in C.LHDS]
        self.jumbos = [Machine(m, "jumbo", C.NODES["j-main-2"]["xy"]) for m in C.JUMBOS]
        self.crusher_bunker = 0.0
        self.crusher_tph = 0.0
        self.crusher_total = 0.0
        self.hoist_tph = 0.0
        self.hoist_total = 0.0
        self.hoist_buffer = 0.0
        self.skips = 0
        self.ore_shift = 0.0
        self.alarm = "нет"
        self.scenarios = {}
        self.manual_plan = {}             # crew id -> chamber id (наряд мастера)
        self.auto_plan = True
        self.rejected = ""
        self.last_change = ""
        self.events = deque(maxlen=60)
        self.events_total = 0
        self.sensors = {s["id"]: {"node": s["node"], "co": 0.0, "no2": 0.0, "temp": C.AIR["rock_temp_c"], "v": 0.0, "alarm": "норма"} for s in C.SENSORS}
        self.shift_elapsed = 0.0
        self._update_ground()
        self._ventilate(0.0)
        self._plan_shift(initial=True)

    # ------------------------------------------------------------ служебное
    def log(self, text):
        self.events.append((round(self.t / 3600, 2), text))
        self.events_total += 1

    def chamber_neighbors(self, ch):
        return [c for c in ch.panel.chambers if c is not ch and abs(c.row - ch.row) <= 1 and abs(c.col - ch.col) <= 1]

    # ------------------------------------------------------------ горное давление
    def _update_ground(self):
        for pl in self.pillars.values():
            ch = pl.chamber
            if pl.extracted:
                pl.sf, pl.status = 0.0, "извлечён"
                continue
            layers = max(1, ch.layer + (1 if ch.state in Chamber.STAGES and ch.state != "drilling" else 0))
            h = C.LAYER_H * layers
            # локальное извлечение: доля отработанных соседних камер и извлечённых целиков
            nb = self.chamber_neighbors(ch)
            mined_nb = sum(1 for c in nb if c.layer > 0 or c.state in Chamber.STAGES) / max(1, len(nb))
            extracted_nb = sum(1 for c in nb if c.pillar and c.pillar.extracted)
            e = C.EXTRACTION_RATIO * (0.55 + 0.45 * mined_nb) * (0.9 if pl.edge else 1.0)
            e = min(0.95, e + 0.06 * extracted_nb)
            strength = pillar_strength_mpa(pl.w, h) * (1 - 0.5 * pl.damage)
            stress = pillar_stress_mpa(e)
            pl.strength, pl.stress = strength, stress
            sf = strength / stress
            pl.sf = sf
            old = pl.status
            pl.status = "устойчив" if sf >= C.SF_OK else "наблюдение" if sf >= C.SF_WATCH else "критический"
            if pl.status != old:
                pl.dirty = True
                if pl.status == "критический":
                    self.log(f"Целик {pl.id[1:]}: SF {sf:.2f} < {C.SF_WATCH}, камеры рядом закрываются")
            # конвергенция растёт с падением запаса (ДОПУЩЕНИЕ: 0,2 мм/сут при SF 2,5, ~2 мм/сут при SF 1,2)
            pl.convergence = round(0.2 * (2.5 / max(0.6, sf)) ** 2.2 * (1 + pl.damage), 2)
            pl.h = h

    def _seismic_shift(self):
        """В начале смены: толчки за смену по статистике seismic-bumps."""
        for p in self.panels.values():
            cls = p.seismic_class if p.seismic_class in C.SEISMIC else "b"
            rate = C.SEISMIC[cls]["bumps_per_shift"]
            # больше вынутых слоёв и критических целиков -> больше толчков (ДОПУЩЕНИЕ)
            crit = sum(1 for pl in p.pillars if pl.status in ("критический", "извлечён"))
            rate *= 1 + 0.15 * crit
            n = 0
            lam = rate
            # Пуассон
            L, k, pr = math.exp(-lam), 0, 1.0
            while True:
                pr *= self.rng.random()
                if pr < L:
                    break
                k += 1
            n = k
            energies = [10 ** self.rng.uniform(1.5, 3.9 + (0.6 if cls != "a" else 0.0) + 0.3 * crit) for _ in range(n)]
            p.bumps_shift = n
            p.energy_shift = round(sum(energies))
            p.max_bump = round(max(energies)) if energies else 0
            if p.max_bump >= C.SEISMIC["stop_energy_j"]:
                p.evacuated_until = self.t + C.SEISMIC["evacuate_h"] * 3600
                p.hazard = "высокая"
                for pl in p.pillars:
                    if not pl.extracted:
                        pl.damage = min(1.0, pl.damage + 0.08)
                self.log(f"{p.name}: сейсмический толчок {p.max_bump:.0f} Дж, люди выводятся на {C.SEISMIC['evacuate_h']:.0f} ч")
            elif p.energy_shift >= C.SEISMIC["energy_p90_j"]:
                p.hazard = "повышенная"
            else:
                p.hazard = "низкая"
            p.seismic_class = "b" if p.energy_shift >= C.SEISMIC["energy_p90_j"] or crit else p.seismic_class_initial

    # ------------------------------------------------------------ вентиляция и газы
    def _ventilate(self, dt):
        self.net.solve()
        for p in self.panels.values():
            aw = next(a for a in self.net.airways.values() if a.panel == p.id)
            p.q_in = max(0.0, abs(aw.q))
            active = [c for c in p.chambers if c.aux_fan]
            # камеры панели проветриваются сквозной струёй: каждая работающая камера получает
            # долю расхода панели; ВМП добавляет не больше своей подачи и не больше струи
            budget = p.q_in * 60 * C.AIR["panel_share"]
            for c in p.chambers:
                c.air_required = C.AIR["per_person_m3min"] * c.people + C.AIR["per_diesel_kw_m3min"] * c.diesel_kw
            need = sum(c.air_required for c in active) or 1.0
            # струя панели делится между работающими камерами пропорционально потребности,
            # но не меньше 1/n на камеру (сквозное проветривание)
            for c in p.chambers:
                if not c.aux_fan or c.aux_fan_failed:
                    c.air = 0.0
                else:
                    c.air = budget * max(c.air_required / need, 1.0 / max(1, len(active)) * 0.5) if active else 0.0
            p.air_budget = budget
            for c in p.chambers:
                # газы в камере: стационарное выделение дизеля + затухание взрывных газов
                q_m3s = max(0.05, c.air / 60)
                emission_co = c.diesel_kw * C.DIESEL_EMISSION["co_g_per_kwh"] / 3600        # г/с
                emission_no2 = c.diesel_kw * C.DIESEL_EMISSION["no2_g_per_kwh"] / 3600
                ss_co = emission_co / q_m3s * 0.08 / 1000 * 1000      # г/м3 -> % об. (1 г/м3 CO ≈ 0,08 %)
                ss_no2 = emission_no2 / q_m3s * 0.052 / 1000 * 1000   # 1 г/м3 NO2 ≈ 0,052 %
                vol = C.CHAMBER_VOLUME * (10 if c.state == "ventilating" else 1)   # взрывные газы занимают камеру и прилегающие выработки
                tau = vol / q_m3s if c.air > 0 else 1e9
                k = 1 - math.exp(-dt / max(60.0, tau)) if dt > 0 else 0
                bg_co, bg_no2 = getattr(c, "co_bg", 0.0), getattr(c, "no2_bg", 0.0)
                c.co += (ss_co + bg_co - c.co) * k
                c.no2 += (ss_no2 + bg_no2 - c.no2) * k
                # тепло дизеля: 1,5 кВт на кВт мощности при коэффициенте загрузки 0,5; cp·ρ воздуха 1,2 кДж/(м3·К)
                heat_c = c.diesel_kw * C.DIESEL_EMISSION["heat_kw_per_kw"] * 0.5 / (1.2 * q_m3s) if c.diesel_kw else 0.0
                target_t = C.AIR["rock_temp_c"] + min(12.0, heat_c) + (0.8 * c.people / 5)
                c.temp += (target_t - c.temp) * (1 - math.exp(-dt / 900.0)) if dt > 0 else 0
            # панельная выработка: средневзвешенный CO исходящей
            aw.people = sum(c.people for c in p.chambers)
            aw.vehicles = sum(1 for c in p.chambers if c.diesel_kw > 0)
        # перенос газов по сети: концентрация в узле = смешение входящих струй по расходу,
        # в ветви = концентрация узла-истока + собственный источник (панели: дизель, взрывные газы)
        aws = [a for a in self.net.airways.values()]
        src = {}
        for p in self.panels.values():
            paw = next(a for a in aws if a.panel == p.id)
            q = max(0.5, p.q_in)
            co_src = sum(c.co * max(0, c.air) for c in p.chambers) / (q * 60) + p.extra_diesel_kw * C.DIESEL_EMISSION["co_g_per_kwh"] / 3600 / q * 0.08
            no2_src = sum(c.no2 * max(0, c.air) for c in p.chambers) / (q * 60) + p.extra_diesel_kw * C.DIESEL_EMISSION["no2_g_per_kwh"] / 3600 / q * 0.052
            heat = sum((c.temp - C.AIR["rock_temp_c"]) * max(0, c.air) for c in p.chambers) / (q * 60)
            src[paw.id] = (co_src, no2_src, heat)
        node_c = {n: (0.0, 0.0, 0.0) for n in self.net.nodes}
        for _ in range(8):
            for aw in aws:
                up = aw.a if aw.q >= 0 else aw.b
                base = node_c[up] if up != SURFACE else (0.0, 0.0, 0.0)
                add = src.get(aw.id, (0.0, 0.0, 0.0))
                aw.co, aw.no2 = base[0] + add[0], base[1] + add[1]
                aw.temp = C.AIR["rock_temp_c"] + base[2] + add[2]
            for n in self.net.nodes:
                if n == SURFACE:
                    continue
                inflow = [(abs(a.q), a) for a in aws if (a.b == n and a.q > 0) or (a.a == n and a.q < 0)]
                tot = sum(q for q, _ in inflow)
                if tot > 1e-6:
                    node_c[n] = (sum(q * a.co for q, a in inflow) / tot, sum(q * a.no2 for q, a in inflow) / tot,
                                 sum(q * (a.temp - C.AIR["rock_temp_c"]) for q, a in inflow) / tot)
        self.node_c = node_c
        # камеры дышат воздухом своей панели: фоновая концентрация на входе панели
        for p in self.panels.values():
            bg = node_c.get(p.in_node, (0.0, 0.0, 0.0))
            for c in p.chambers:
                c.co_bg, c.no2_bg = bg[0], bg[1]
        # датчики АГК
        for sid, s in self.sensors.items():
            node = s["node"]
            c = node_c.get(node, (0.0, 0.0, 0.0))
            near = [a for a in self.net.airways.values() if node in (a.a, a.b) and not a.id.startswith("shaft:")]
            s["co"], s["no2"], s["temp"] = c[0], c[1], C.AIR["rock_temp_c"] + c[2]
            s["v"] = max((a.velocity for a in near), default=0.0)
            s["alarm"] = ("CO" if s["co"] > C.AIR["co_limit_pct"] else "NO2" if s["no2"] > C.AIR["no2_limit_pct"]
                          else "температура" if s["temp"] > C.AIR["t_max_c"] else "нет струи" if s["v"] < 0.15 else "норма")

    # ------------------------------------------------------------ вердикты
    def _verdict(self, ch):
        """Можно ли ставить людей в камеру: (вердикт, причина)."""
        p = ch.panel
        if not ch.has_work():
            return ("—", "камера отработана" if ch.state == "mined_out" else "закрыта")
        reasons_forbid, reasons_restrict = [], []
        if self.t < p.evacuated_until:
            reasons_forbid.append(f"панель выведена после толчка до {p.evacuated_until / 3600:.1f} ч")
        if not self.net.path_open(p.in_node, "shaft-cage"):
            reasons_forbid.append("нет открытого пути к стволу")
        pl = ch.pillar
        near = [pl] + [c.pillar for c in self.chamber_neighbors(ch) if c.pillar]
        crit = [x for x in near if x.status == "критический"]
        watch = [x for x in near if x.status == "наблюдение"]
        if crit:
            reasons_forbid.append(f"целик {crit[0].id[1:]} критический (SF {crit[0].sf:.2f})")
        if pl.convergence >= C.CONVERGENCE_CLOSE_MM_D:
            reasons_forbid.append(f"конвергенция {pl.convergence} мм/сут")
        if ch.state == "ventilating":
            since = (self.t - ch.blast_t) / 60 if ch.blast_t else 0
            if since < C.AIR["blast_reentry_min"] or ch.co > C.AIR["co_limit_pct"]:
                reasons_forbid.append(f"проветривание после взрыва: CO {ch.co * 1000:.2f}‰, {since:.0f} мин")
        if ch.co > C.AIR["co_limit_pct"]:
            reasons_forbid.append(f"CO {ch.co:.4f} % > {C.AIR['co_limit_pct']} %")
        if ch.no2 > C.AIR["no2_limit_pct"]:
            reasons_forbid.append(f"NO2 {ch.no2:.5f} % > {C.AIR['no2_limit_pct']} %")
        if ch.temp > C.AIR["t_max_c"]:
            reasons_forbid.append(f"температура {ch.temp:.1f} °C")
        if p.q_in * 60 < C.AIR["per_person_m3min"] * C.CREW_SIZE * 1.5:
            reasons_forbid.append(f"в панели нет струи ({p.q_in:.1f} м3/с)")
        # воздух под людей и дизель
        # бюджет воздуха панели за вычетом полной потребности других занятых камер
        others = sum(c.air_required_full for c in p.chambers if c is not ch and c.aux_fan and not c.aux_fan_failed)
        avail = max(0.0, getattr(p, "air_budget", p.q_in * 60 * C.AIR["panel_share"]) - others)
        need_people = C.AIR["per_person_m3min"] * C.CREW_SIZE
        need_lhd = need_people + C.AIR["per_diesel_kw_m3min"] * C.LHD["kw"]
        restrict_diesel = restrict_blast = False
        if ch.aux_fan_failed:
            reasons_forbid.append("отказ ВМП, камера не проветривается")
        elif avail < need_people:
            reasons_forbid.append(f"воздуха {avail:.0f} м3/мин < {need_people:.0f} на звено")
        elif avail < need_lhd:
            reasons_restrict.append(f"воздуха {avail:.0f} м3/мин: без дизельной техники (ПДМ нужно {need_lhd:.0f})")
            restrict_diesel = True
        if watch:
            reasons_restrict.append(f"целик {watch[0].id[1:]} под наблюдением (SF {watch[0].sf:.2f}): без новых взрывов, осмотр кровли")
            restrict_blast = True
        if p.hazard == "повышенная":
            reasons_restrict.append("повышенная сейсмическая активность: без новых взрывов")
            restrict_blast = True
        if ch.state in ("charged",):
            reasons_restrict.append("камера заряжена, ждёт взрыва")
        ch.restrict_diesel, ch.restrict_blast = restrict_diesel, restrict_blast
        if reasons_forbid:
            return ("запрещено", "; ".join(reasons_forbid))
        if reasons_restrict:
            return ("ограничено", "; ".join(reasons_restrict))
        return ("разрешено", "воздух и целики в норме")

    def _update_verdicts(self):
        for ch in self.chambers.values():
            v, r = self._verdict(ch)
            if (v, r) != (ch.verdict, ch.reason):
                if v != ch.verdict and ch.verdict != "—" and v in ("запрещено",) and ch.crew:
                    self.log(f"Камера {ch.id[1:]}: {v} — {r}")
                ch.verdict, ch.reason = v, r
                ch.dirty = True

    def _air_allows_lhd(self, ch):
        p = ch.panel
        others = sum(c.air_required_full for c in p.chambers if c is not ch and c.aux_fan and not c.aux_fan_failed)
        budget = getattr(p, "air_budget", p.q_in * 60 * C.AIR["panel_share"])
        return budget - others >= C.AIR["per_person_m3min"] * C.CREW_SIZE + C.AIR["per_diesel_kw_m3min"] * C.LHD["kw"]

    # ------------------------------------------------------------ наряд
    def _plan_shift(self, initial=False):
        """Горный мастер: расставить звенья, ПДМ и СБУ по камерам по вердиктам."""
        keep = {}
        for crew in self.crews:
            ch = crew.chamber
            # преемственность: звено остаётся в своей камере, если там есть работа и наряд допустим
            if ch and ch.crew is crew and ch.has_work() and ch.state not in ("charged", "ventilating") and ch.verdict in ("разрешено", "ограничено"):
                keep[crew.id] = ch
            if ch and ch.crew is crew:
                ch.crew = None
            crew.chamber = None
        for m in self.lhds + self.jumbos:
            if m.chamber:
                if m.kind == "lhd":
                    m.chamber.lhd = None
                else:
                    m.chamber.jumbo = None
            m.chamber = None
            m.state = "стоянка"
        self._update_verdicts()
        # кандидаты: камеры с работой и допустимым вердиктом, приоритет — незавершённый цикл
        def prio(c):
            stage = Chamber.STAGES.index(c.state) if c.state in Chamber.STAGES else -1
            return (0 if c.verdict == "разрешено" else 1, -stage, -c.layer, c.panel.id, c.row, c.col)
        cands = [c for c in self.chambers.values() if c.has_work() and c.verdict in ("разрешено", "ограничено")
                 and c.state not in ("charged", "ventilating")
                 and not (c.restrict_blast and c.state in ("virgin", "idle", "drilling", "charging"))]
        cands.sort(key=prio)
        assigned = []
        # ручной наряд мастера имеет приоритет
        plan_lines = []
        for crew in self.crews:
            target = keep.get(crew.id)
            if target is not None and (target.crew is not None or target in assigned):
                target = None
            manual = self.manual_plan.get(crew.id)
            if manual and manual in self.chambers:
                mc = self.chambers[manual]
                if mc.verdict in ("разрешено", "ограничено") and mc.has_work() and mc.crew is None:
                    target = mc
                else:
                    self.rejected = f"{crew.name} → {manual[1:]}: {mc.verdict}, {mc.reason}"
                    self.log(f"Наряд отклонён: {self.rejected}")
                    self.manual_plan.pop(crew.id, None)
            if target is None and self.auto_plan:
                target = next((c for c in cands if c.crew is None and c not in assigned), None)
            if target is None:
                crew.state = "на поверхности" if initial else "резерв"
                crew.task = "резерв"
                plan_lines.append(f"{crew.name}: резерв")
                continue
            target.crew = crew
            crew.chamber = target
            assigned.append(target)
            crew.state = "спуск"
            crew.timer = 0.0
            if target.state in ("virgin", "idle"):
                target.state = "drilling"
                target.timer = 0.0
                target.dirty = True
            crew.task = target.state
            plan_lines.append(f"{crew.name} → камера {target.id[1:]} ({target.verdict}: {target.state})")
        # техника: СБУ в камеры бурения/крепления, ПДМ в камеры выпуска (если вердикт разрешает дизель)
        for c in assigned:
            if c.state in ("drilling", "bolting") and self.jumbos:
                jb = next((j for j in self.jumbos if j.chamber is None), None)
                if jb:
                    jb.chamber, c.jumbo, jb.state = c, jb, "переезд"
                    jb.timer = 0.0
            if c.state == "mucking" and c.verdict == "разрешено" and self._air_allows_lhd(c):
                lhd = next((l for l in self.lhds if l.chamber is None), None)
                if lhd:
                    lhd.chamber, c.lhd, lhd.state = c, lhd, "переезд"
                    lhd.timer = 0.0
        self.plan_text = "; ".join(plan_lines)
        if not initial:
            self.log(f"Смена {self.shift_no}: наряд — {self.plan_text}")

    # ------------------------------------------------------------ цикл камеры
    def _step_chamber(self, ch, dt):
        crew = ch.crew
        working = crew is not None and crew.state == "в камере"
        ch.aux_fan = (ch.occupied and ch.state in Chamber.STAGES and ch.state != "charged") or ch.state == "ventilating"
        if ch.state == "charged":
            return
        if ch.state == "ventilating":
            # взрывные газы рассеиваются (считается в _ventilate); вход по вердикту
            if ch.blast_t and (self.t - ch.blast_t) / 60 >= C.AIR["blast_reentry_min"] and ch.co <= C.AIR["co_limit_pct"]:
                ch.state, ch.timer, ch.dirty = "scaling", 0.0, True
                self.log(f"Камера {ch.id[1:]}: проветрена, CO {ch.co:.4f} %, можно заходить")
            return
        if not working:
            return
        if ch.verdict == "запрещено":
            return
        if ch.restrict_blast and ch.state in ("drilling", "charging"):
            return                                  # без новых взрывов: бурение и заряжание приостановлены
        if ch.restrict_diesel and ch.lhd:
            ch.lhd.state, ch.lhd.chamber, ch.lhd = "стоянка", None, None    # ПДМ уходит, воздуха на неё нет
        ch.timer += dt
        hours = ch.timer / 3600
        if ch.state == "drilling":
            if ch.jumbo and ch.jumbo.state == "переезд" and ch.jumbo.timer > 600:
                ch.jumbo.state = "бурение"
            if ch.jumbo and ch.jumbo.state == "бурение":
                ch.jumbo.timer += dt
            if hours >= C.CYCLE_H["drilling"]:
                ch.state, ch.timer, ch.dirty = "charging", 0.0, True
                if ch.jumbo:
                    ch.jumbo.rounds += 1
                    ch.jumbo.state = "стоянка"
                    ch.jumbo.chamber, ch.jumbo = None, None
        elif ch.state == "charging":
            if hours >= C.CYCLE_H["charging"]:
                ch.state, ch.timer, ch.dirty = "charged", 0.0, True
                self.log(f"Камера {ch.id[1:]}: заряжена, взрыв в пересменку")
        elif ch.state == "scaling":
            if ch.verdict == "ограничено" or ch.verdict == "разрешено":
                if hours >= C.CYCLE_H["scaling"]:
                    ch.state, ch.timer, ch.dirty = "bolting", 0.0, True
        elif ch.state == "bolting":
            if hours >= C.CYCLE_H["bolting"]:
                ch.state, ch.timer, ch.dirty = "mucking", 0.0, True
                ch.remaining = C.CHAMBER_TONNES
        elif ch.state == "mucking":
            lhd = ch.lhd
            if lhd is None:
                lhd = next((l for l in self.lhds if l.chamber is None), None)
                if lhd and ch.verdict == "разрешено" and self._air_allows_lhd(ch):
                    lhd.chamber, ch.lhd, lhd.state, lhd.timer = ch, lhd, "переезд", 0.0
                else:
                    lhd = None
            if lhd:
                lhd.timer += dt
                if lhd.state == "переезд" and lhd.timer > 600:
                    lhd.state = "выпуск"
                if lhd.state == "выпуск":
                    if ch.verdict != "разрешено":
                        lhd.state = "ожидание (вердикт)"
                    else:
                        lhd.hours += dt / 3600
                        buckets = dt / C.LHD["cycle_s"]
                        tons = min(ch.remaining, buckets * C.LHD["bucket_t"] * self.rng.uniform(0.9, 1.05))
                        ch.remaining -= tons
                        lhd.tonnes_shift += tons
                        lhd.buckets += buckets
                        ch.panel.ore_pass_level += tons
                        ch.panel.ore_pass_total += tons
                        self.ore_shift += tons
                elif lhd.state == "ожидание (вердикт)" and ch.verdict == "разрешено":
                    lhd.state = "выпуск"
            if ch.remaining <= 0.5:
                ch.layer += 1
                ch.panel.layers_mined += 1
                ch.remaining = 0.0
                if lhd:
                    lhd.state, lhd.chamber, ch.lhd = "стоянка", None, None
                ch.state = "mined_out" if ch.layer >= C.LAYERS_PER_CHAMBER else "idle"
                ch.dirty = True
                self.log(f"Камера {ch.id[1:]}: слой {ch.layer} выпущен" + (", камера отработана" if ch.state == "mined_out" else ""))
                if ch.state == "idle":
                    ch.state, ch.timer = "drilling", 0.0     # звено продолжает следующий слой
                    crew.task = ch.state
                else:
                    crew.task = "резерв"
                    crew.state = "выведено"
                    ch.crew, crew.chamber = None, None
        if crew:
            crew.task = ch.state

    # ------------------------------------------------------------ люди
    def _step_crews(self, dt):
        for crew in self.crews:
            ch = crew.chamber
            if crew.state == "спуск":
                crew.timer += dt
                crew.xy = C.NODES["shaft-cage"]["xy"] if crew.timer < 600 else ((ch.cx, ch.cy) if ch else crew.xy)
                if crew.timer >= 1200:
                    crew.state = "в камере" if ch else "резерв"
            elif crew.state == "в камере":
                if ch is None:
                    crew.state = "резерв"
                    continue
                crew.xy = (ch.cx, ch.cy)
                if ch.verdict == "запрещено":
                    crew.state = "выведено"
                    crew.timer = 0.0
                    self.log(f"{crew.name} выведено из камеры {ch.id[1:]}: {ch.reason}")
                crew.exposure += ch.co * dt / 3600
            elif crew.state == "выведено":
                crew.timer += dt
                node = ch.panel.in_node if ch else "j-main-1"
                crew.xy = C.NODES[node]["xy"]
                # вернуться, если камера снова разрешена; иначе ждать переназначения мастером
                if ch and ch.verdict in ("разрешено", "ограничено") and ch.has_work() and self.t >= ch.panel.evacuated_until:
                    crew.state = "в камере"
                elif ch and (not ch.has_work()):
                    ch.crew, crew.chamber = None, None
                    crew.state = "резерв"
            elif crew.state == "резерв":
                crew.xy = C.NODES["j-main-1"]["xy"]
                if ch is None and self.auto_plan:
                    # резервное звено берёт ближайшую разрешённую камеру
                    cand = [c for c in self.chambers.values() if c.has_work() and c.crew is None and c.verdict == "разрешено"
                            and c.state not in ("charged", "ventilating")]
                    if cand:
                        c = cand[0]
                        c.crew, crew.chamber, crew.state, crew.timer = crew, c, "спуск", 600.0
                        if c.state in ("virgin", "idle"):
                            c.state, c.timer, c.dirty = "drilling", 0.0, True
            elif crew.state == "на поверхности":
                crew.xy = C.NODES["shaft-cage"]["xy"]
        for m in self.lhds + self.jumbos:
            if m.chamber:
                m.xy = (m.chamber.cx, m.chamber.cy + 6)
            elif m.kind == "lhd":
                m.xy = (C.NODES["j-main-1"]["xy"][0] + 30, C.NODES["j-main-1"]["xy"][1] + 18 * self.lhds.index(m))
            else:
                m.xy = (C.NODES["j-main-2"]["xy"][0] + 30, C.NODES["j-main-2"]["xy"][1] + 18 * self.jumbos.index(m))

    # ------------------------------------------------------------ руда
    def _step_ore(self, dt):
        draw_total = 0.0
        for p in self.panels.values():
            draw = min(p.ore_pass_level, C.CRUSHER_TPH / 3 / 3600 * dt)
            p.ore_pass_level -= draw
            draw_total += draw
        self.crusher_bunker += draw_total
        crushed = min(self.crusher_bunker, C.CRUSHER_TPH / 3600 * dt)
        self.crusher_bunker -= crushed
        self.crusher_total += crushed
        self.crusher_tph = crushed / dt * 3600 if dt else 0
        self.hoist_buffer += crushed
        hoisted = min(self.hoist_buffer, C.HOIST_TPH / 3600 * dt)
        self.hoist_buffer -= hoisted
        self.hoist_total += hoisted
        self.hoist_tph = hoisted / dt * 3600 if dt else 0
        self.skips = int(self.hoist_total / 25)

    # ------------------------------------------------------------ смена
    def _shift_change(self):
        """Пересменка: взрывы заряженных камер, люди на поверхность, сейсмика, новый наряд."""
        self.shift_no += 1
        self.shift_elapsed = 0.0
        for ch in self.chambers.values():
            if ch.state == "charged":
                ch.state, ch.blast_t, ch.dirty = "ventilating", self.t, True
                # взрывные газы: объём CO = ВВ × 40 л/кг в объёме камеры + прилегающих выработок (×3)
                co_m3 = C.BLAST["explosive_kg"] * C.BLAST["co_l_per_kg"] / 1000
                ch.co = min(0.5, co_m3 / (C.CHAMBER_VOLUME * 10) * 100)
                ch.aux_fan = True
                self.log(f"Камера {ch.id[1:]}: взрыв, CO {ch.co:.3f} %, проветривание")
        for crew in self.crews:
            crew.state = "спуск"
            crew.timer = 0.0
        for m in self.lhds:
            m.tonnes_shift = 0.0
        self.ore_shift = 0.0
        self._seismic_shift()
        self._update_ground()
        self._plan_shift()

    # ------------------------------------------------------------ шаг
    def step(self, dt):
        self.t += dt
        self.shift_elapsed += dt
        if self.shift_elapsed >= C.SHIFT_H * 3600:
            self._shift_change()
        if int(self.t / dt) % 15 == 0:
            self._update_ground()
        self._ventilate(dt)
        self._update_verdicts()
        for ch in self.chambers.values():
            self._step_chamber(ch, dt)
        self._step_crews(dt)
        self._step_ore(dt)
        bad = [s for s in self.sensors.values() if s["alarm"] not in ("норма",)]
        forb_with_people = [c for c in self.chambers.values() if c.verdict == "запрещено" and c.people]
        self.alarm = ("АГК: " + ", ".join(f"{sid}:{s['alarm']}" for sid, s in self.sensors.items() if s["alarm"] != "норма")) if bad \
            else ("люди в запрещённой камере " + forb_with_people[0].id[1:]) if forb_with_people else "нет"

    def run(self, seconds, dt=10.0):
        for _ in range(int(seconds / dt)):
            self.step(dt)

    # ------------------------------------------------------------ сценарии
    def command(self, cmd):
        a = cmd.get("action")
        on = bool(cmd.get("on", True))
        net = self.net
        if a == "fan":
            pct = float(cmd.get("speed_pct", 100))
            net.fan_speed = max(0.0, min(1.2, pct / 100))
            net.fan_on = pct > 0
            if abs(pct - 100) < 1e-6:
                self.scenarios.pop("fan", None)
            else:
                self.scenarios["fan"] = f"{pct:.0f}%"
            self.log(f"Сценарий: ВГП {'остановлен' if pct <= 0 else f'на {pct:.0f} %'}")
        elif a == "door":
            aw = net.airways.get(cmd.get("target", "aw-door-3"))
            if not aw or not aw.door:
                return "unknown door"
            aw.door_open = on if aw.door == "door" else (not on)   # для регулятора on = закрыть
            key = f"door:{aw.id}"
            default_open = aw.door == "regulator"
            if aw.door_open == default_open:
                self.scenarios.pop(key, None)
            else:
                self.scenarios[key] = "открыта" if aw.door_open else "закрыт"
            self.log(f"Сценарий: {aw.id} {'открыта' if aw.door_open else 'закрыт(а)'}")
        elif a == "regulator":
            # прикрыть регулятор панели на pct %: добавочное сопротивление (ДОПУЩЕНИЕ: до 8·R0 при 100 %)
            aw = net.airways.get(cmd.get("target", "aw-p1-vent"))
            if not aw or aw.door != "regulator":
                return "unknown regulator"
            pct = max(0.0, min(95.0, float(cmd.get("closed_pct", 0))))
            aw.regulator_extra = aw.R0 * 8.0 * (pct / 100) / max(0.05, 1 - pct / 100)
            key = f"regulator:{aw.id}"
            if pct <= 0:
                self.scenarios.pop(key, None)
            else:
                self.scenarios[key] = f"{pct:.0f}%"
            self.log(f"Регулятор {aw.id}: прикрыт на {pct:.0f} %")
        elif a == "blocked":
            aw = net.airways.get(cmd.get("target"))
            if not aw:
                return "unknown airway"
            aw.blocked = on
            key = f"blocked:{aw.id}"
            if on:
                self.scenarios[key] = True
            else:
                self.scenarios.pop(key, None)
            self.log(f"Сценарий: {aw.id} {'завалена (обрушение)' if on else 'расчищена'}")
        elif a == "extra_diesel":
            p = self.panels.get(cmd.get("target", "panel-1"))
            if not p:
                return "unknown panel"
            p.extra_diesel_kw = float(cmd.get("kw", C.TRUCK["kw"])) if on else 0.0
            key = f"extra_diesel:{p.id}"
            if on:
                self.scenarios[key] = f"{p.extra_diesel_kw:.0f}kW"
            else:
                self.scenarios.pop(key, None)
            self.log(f"Сценарий: в {p.name} {'заехал самосвал ' + C.TRUCK['model'] if on else 'самосвал выехал'}")
        elif a == "seismic":
            p = self.panels.get(cmd.get("target", "panel-3"))
            if not p:
                return "unknown panel"
            e = float(cmd.get("energy", 120000))
            p.max_bump, p.energy_shift, p.bumps_shift = e, p.energy_shift + e, p.bumps_shift + 1
            p.evacuated_until = self.t + C.SEISMIC["evacuate_h"] * 3600
            p.hazard, p.seismic_class = "высокая", "b"
            for pl in p.pillars:
                if not pl.extracted:
                    pl.damage = min(1.0, pl.damage + 0.12)
            self._update_ground()
            self.scenarios[f"seismic:{p.id}"] = f"{e:.0f}J"
            self.log(f"Сценарий: толчок {e:.0f} Дж в {p.name}, вывод людей на {C.SEISMIC['evacuate_h']:.0f} ч")
        elif a == "pillar_extract":
            pl = self.pillars.get(cmd.get("target"))
            if not pl:
                return "unknown pillar"
            pl.extracted = on
            pl.dirty = True
            key = f"pillar_extract:{pl.id}"
            if on:
                self.scenarios[key] = True
            else:
                self.scenarios.pop(key, None)
                pl.damage = 0.0
            self._update_ground()
            self.log(f"Сценарий: целик {pl.id[1:]} {'извлечён' if on else 'восстановлен'}")
        elif a == "aux_fan_fail":
            ch = self.chambers.get(cmd.get("target"))
            if not ch:
                return "unknown chamber"
            ch.aux_fan_failed = on
            ch.dirty = True
            key = f"aux_fan_fail:{ch.id}"
            if on:
                self.scenarios[key] = True
            else:
                self.scenarios.pop(key, None)
            self.log(f"Сценарий: ВМП камеры {ch.id[1:]} {'отказал' if on else 'восстановлен'}")
        elif a == "assign":
            crew_id, ch_id = cmd.get("crew"), cmd.get("chamber")
            crew = next((c for c in self.crews if c.id == crew_id), None)
            if crew is None:
                return "unknown crew"
            if not ch_id:
                self.manual_plan.pop(crew_id, None)
                self.last_change = f"{crew.name}: снято с наряда"
            else:
                ch = self.chambers.get(ch_id)
                if ch is None:
                    return "unknown chamber"
                if ch.verdict == "запрещено" or not ch.has_work():
                    self.rejected = f"{crew.name} → {ch.id[1:]}: {ch.verdict} — {ch.reason}"
                    self.log(f"Наряд отклонён: {self.rejected}")
                    return "rejected: " + ch.reason
                self.manual_plan[crew_id] = ch_id
                self.last_change = f"{crew.name} → камера {ch.id[1:]} ({ch.verdict})"
                # применить сразу
                if crew.chamber and crew.chamber.crew is crew:
                    crew.chamber.crew = None
                if ch.crew and ch.crew is not crew:
                    ch.crew.chamber, ch.crew.state = None, "резерв"
                ch.crew, crew.chamber, crew.state, crew.timer = crew, ch, "спуск", 600.0
                if ch.state in ("virgin", "idle"):
                    ch.state, ch.timer, ch.dirty = "drilling", 0.0, True
                self.log(f"Наряд мастера: {self.last_change}")
        elif a == "auto_plan":
            self.auto_plan = on
            self.log(f"Автонаряд {'включён' if on else 'выключен'}")
        elif a == "reset":
            for key in list(self.scenarios):
                k, _, v = key.partition(":")
                if k == "door":
                    self.command({"action": "door", "target": v, "on": False if net.airways[v].door == "door" else False})
                    aw = net.airways[v]
                    aw.door_open = aw.door == "regulator"
                elif k == "blocked":
                    self.command({"action": "blocked", "target": v, "on": False})
                elif k == "regulator":
                    self.command({"action": "regulator", "target": v, "closed_pct": 0})
                elif k == "extra_diesel":
                    self.command({"action": "extra_diesel", "target": v, "on": False})
                elif k == "pillar_extract":
                    self.command({"action": "pillar_extract", "target": v, "on": False})
                elif k == "aux_fan_fail":
                    self.command({"action": "aux_fan_fail", "target": v, "on": False})
            net.fan_speed, net.fan_on = 1.0, True
            for p in self.panels.values():
                p.evacuated_until = 0.0
                p.hazard = "низкая"
                p.seismic_class = p.seismic_class_initial
                for pl in p.pillars:
                    pl.damage = 0.0
            self.manual_plan.clear()
            self.scenarios.clear()
            self._update_ground()
            self.log("Сценарии сброшены")
        else:
            return f"unknown action {a}"
        self._ventilate(0.0)
        self._update_verdicts()
        return "ok"

    # ------------------------------------------------------------ снимок
    def snapshot(self):
        f = lambda v, u=None: {"value": v, **({"unit": u} if u else {})}
        th = {}
        verdicts = [c.verdict for c in self.chambers.values()]
        th[C.MINE_ID] = {
            "sim_time_h": f(round(self.t / 3600, 2), "ч"), "shift_no": f(self.shift_no), "shift_phase": f(self.shift_phase),
            "people_underground": f(sum(c.size for c in self.crews if c.state not in ("на поверхности",)), "чел"),
            "ore_hoisted_t": f(round(self.hoist_total), "т"), "ore_shift_t": f(round(self.ore_shift), "т"),
            "chambers_allowed": f(verdicts.count("разрешено")), "chambers_restricted": f(verdicts.count("ограничено")),
            "chambers_forbidden": f(verdicts.count("запрещено")),
            "alarm": f(self.alarm),
            "active_scenarios": f(", ".join(f"{k}={v}" if v is not True else k for k, v in self.scenarios.items()) or "none"),
        }
        net = self.net
        th["fan-main"] = {"state": f("работает" if net.fan_on and net.fan_speed > 0 else "остановлен"),
                          "speed_pct": f(round(net.fan_speed * 100)), "q": f(round(net.fan_q, 1), "м3/с"),
                          "pressure": f(round(net.fan_p), "Па"), "power": f(round(C.FAN["power_kw"] * net.fan_speed ** 3 * (1 if net.fan_on else 0)), "кВт")}
        for nid in C.NODES:
            people = sum(c.size for c in self.crews if c.xy == C.NODES[nid]["xy"] and c.state not in ("на поверхности",))
            d = {"people": f(people, "чел")}
            if nid.startswith("shaft"):
                d["q"] = f(round(abs(net.airways[f"shaft:{nid}"].q), 1), "м3/с")
                d["mode"] = f("исходящая" if nid == "shaft-vent" else "свежая")
            if nid == "crusher":
                d.update({"state": f("дробит" if self.crusher_tph > 1 else "ожидание"), "throughput": f(round(self.crusher_tph), "т/ч"),
                          "bunker": f(round(self.crusher_bunker), "т"), "tonnes_total": f(round(self.crusher_total), "т")})
            th[nid] = d
        th["hoist"] = {"state": f("подъём" if self.hoist_tph > 1 else "ожидание"), "throughput": f(round(self.hoist_tph), "т/ч"),
                       "tonnes_total": f(round(self.hoist_total), "т"), "skips_total": f(self.skips)}
        for aw in net.airways.values():
            if aw.id.startswith("shaft:"):
                continue
            th[aw.id] = {"status": f("blocked" if aw.blocked else "open"), "q": f(round(abs(aw.q), 1), "м3/с"),
                         "velocity": f(round(aw.velocity, 2), "м/с"), "direction": f(1 if aw.q >= 0 else -1),
                         "co": f(round(aw.co, 5), "% об."), "no2": f(round(aw.no2, 6), "% об."), "temp": f(round(aw.temp, 1), "°C"),
                         "door_state": f(("открыта" if aw.door_open else "закрыта") if aw.door == "door" else ("открыт" if aw.door_open else "закрыт") if aw.door else ""),
                         "people": f(aw.people, "чел"), "vehicles": f(aw.vehicles)}
        for sid, s in self.sensors.items():
            th[sid] = {"co": f(round(s["co"], 5), "% об."), "no2": f(round(s["no2"], 6), "% об."), "co2": f(round(0.1 + s["co"] * 20, 3), "% об."),
                       "o2": f(round(20.9 - s["co"] * 30, 2), "% об."), "temp": f(round(s["temp"], 1), "°C"),
                       "velocity": f(round(s["v"], 2), "м/с"), "alarm": f(s["alarm"])}
        for p in self.panels.values():
            active = [c for c in p.chambers if c.aux_fan]
            th[p.id] = {"q_in": f(round(p.q_in, 1), "м3/с"), "chambers_active": f(len(active)),
                        "people": f(sum(c.people for c in p.chambers), "чел"), "diesel_kw": f(round(sum(c.diesel_kw for c in p.chambers) + p.extra_diesel_kw), "кВт"),
                        "air_required": f(round(sum(c.air_required for c in p.chambers) + C.AIR["per_diesel_kw_m3min"] * p.extra_diesel_kw), "м3/мин"),
                        "air_available": f(round(p.q_in * 60), "м3/мин"),
                        "seismic_class": f(p.seismic_class), "bumps_shift": f(p.bumps_shift), "seismic_energy_shift": f(p.energy_shift, "Дж"),
                        "max_bump_energy": f(p.max_bump, "Дж"), "hazard": f(p.hazard),
                        "evacuated_until_h": f(round(p.evacuated_until / 3600, 2) if p.evacuated_until > self.t else 0, "ч"),
                        "extraction_ratio": f(round(C.EXTRACTION_RATIO + 0.06 * sum(1 for pl in p.pillars if pl.extracted) / max(1, len(p.pillars)), 3)),
                        "layers_mined": f(p.layers_mined),
                        "status": f("эвакуация" if p.evacuated_until > self.t else "работа" if active else "простой")}
            th[p.ore_pass] = {"level": f(round(p.ore_pass_level), "т"), "state": f("выпуск" if p.ore_pass_level > 1 else "ожидание"),
                              "tonnes_total": f(round(p.ore_pass_total), "т")}
        for ch in self.chambers.values():
            stage_h = C.CYCLE_H.get(ch.state)
            prog = min(100, round(100 * ch.timer / 3600 / stage_h)) if stage_h else (round(100 * (1 - ch.remaining / C.CHAMBER_TONNES)) if ch.state == "mucking" else 0)
            th[ch.id] = {"state": f(ch.state), "layer": f(ch.layer), "tonnes_remaining": f(round(ch.remaining), "т"),
                         "crew": f(ch.crew.id if ch.crew else ""), "equipment": f(", ".join(m.id for m in (ch.lhd, ch.jumbo) if m)),
                         "people": f(ch.people, "чел"), "diesel_kw": f(round(ch.diesel_kw), "кВт"),
                         "air": f(round(ch.air), "м3/мин"), "air_required": f(round(ch.air_required), "м3/мин"),
                         "co": f(round(ch.co, 5), "% об."), "no2": f(round(ch.no2, 6), "% об."), "temp": f(round(ch.temp, 1), "°C"),
                         "aux_fan": f("отказ" if ch.aux_fan_failed else "вкл" if ch.aux_fan else "выкл"),
                         "verdict": f(ch.verdict), "reason": f(ch.reason),
                         "reentry_in": f(max(0, round(C.AIR["blast_reentry_min"] - (self.t - ch.blast_t) / 60)) if ch.state == "ventilating" and ch.blast_t else 0, "мин"),
                         "stage_progress": f(prog, "%")}
        for pl in self.pillars.values():
            th[pl.id] = {"height": f(round(pl.h, 1), "м"), "strength": f(round(pl.strength, 1), "МПа"), "stress": f(round(pl.stress, 1), "МПа"),
                         "sf": f(round(pl.sf, 2)), "convergence": f(pl.convergence, "мм/сут"), "status": f(pl.status), "extracted": f(pl.extracted)}
        for crew in self.crews:
            th[crew.id] = {"chamber": f(crew.chamber.id if crew.chamber else ""), "task": f(crew.task), "state": f(crew.state),
                           "people": f(crew.size, "чел"), "x": f(round(crew.xy[0], 1), "м"), "y": f(round(crew.xy[1], 1), "м"),
                           "exposure_co": f(round(crew.exposure, 5), "%·ч")}
        for m in self.lhds:
            th[m.id] = {"chamber": f(m.chamber.id if m.chamber else ""), "state": f(m.state),
                        "kw_running": f(C.LHD["kw"] if m.state == "выпуск" else 0, "кВт"), "tonnes_shift": f(round(m.tonnes_shift), "т"),
                        "buckets_total": f(int(m.buckets)), "hours": f(round(m.hours, 1), "ч"), "x": f(round(m.xy[0], 1), "м"), "y": f(round(m.xy[1], 1), "м")}
        for m in self.jumbos:
            th[m.id] = {"chamber": f(m.chamber.id if m.chamber else ""), "state": f(m.state),
                        "kw_running": f(C.JUMBO["kw_tramming"] if m.state == "переезд" else 0, "кВт"), "rounds_total": f(m.rounds),
                        "x": f(round(m.xy[0], 1), "м"), "y": f(round(m.xy[1], 1), "м")}
        th["shift-plan"] = {"shift_no": f(self.shift_no), "plan": f(getattr(self, "plan_text", "")), "auto": f(self.auto_plan),
                            "last_change": f(self.last_change), "rejected": f(self.rejected)}
        return th
