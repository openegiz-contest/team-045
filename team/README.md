# team/ — цифровой двойник карьера «Сарыбұлақ»

Код команды ML. Решение построено на OpenEgiz v1.0.4 без изменения ядра платформы:
всё, что здесь лежит, подключается к стеку overlay-файлом `compose.yml` командой `make mine`.

```text
team/
├── compose.yml              overlay: mine-setup, mine-sim, mine-control, дашборды Grafana
├── Dockerfile, requirements.txt
├── mine/
│   ├── config.py            параметры карьера, парка, фабрики, энергетики — с источниками
│   ├── model.py             блочная модель и 62 двойника Ditto (атрибуты = геометрия и паспорта)
│   ├── setup_twins.py       PUT двойников в Ditto (идемпотентно)
│   ├── sim.py               симулятор: рейсы, очереди, диспетчер, рудоконтроль и шихтовка,
│   │                        БВР, дробилка → мельницы → флотация, энергия и дизель, отказы и ТО
│   ├── run_sim.py           MQTT: telemetry/<twin> (Ditto protocol), sarybulak/commands, sarybulak/events
│   └── control.py           карта карьера и панель сценариев (HTTP :8095), данные из Ditto
├── static/index.html        живая 2D-карта (SVG), строится из атрибутов и features двойников
├── grafana/                 build_dashboards.py → dashboards/*.json, provisioning.yaml
├── scripts/                 urls.sh, check.sh (сквозная проверка), dev_preview.py (без платформы)
└── data/openmines/          north_pit_mine.json (MIT) — калибровка транспортного модуля
```

## Поток данных

```text
mine-sim ──MQTT telemetry/<twin> (Ditto protocol modify)──▶ Ditto (состояние двойников)
                                                               │
          Ditto ──MQTT opentwins/twin/events/…──▶ Telegraf ──▶ InfluxDB ──▶ Grafana (дашборды)
                                  │
                                  └──▶ mine-control (кэш состояния) ──HTTP /api/state──▶ карта (браузер)

карта ──POST /api/command──▶ mine-control ──MQTT sarybulak/commands──▶ mine-sim (сценарий «что если»)
```

Карта и дашборды получают данные только из платформы: при старте `mine-control` читает
все двойники пространства имён `org.openegiz.sarybulak` через Ditto search API, затем
следит за событиями изменения, которые Ditto публикует в MQTT (`opentwins/twin/events/#`).
Симулятор не обращается ни к Ditto, ни к InfluxDB напрямую.

## Двойники (иерархия через `_parents`)

| Вид | Кол-во | Атрибуты (постоянные) | Features (живые) |
|---|---|---|---|
| mine | 1 | борт, цель шихтовки | добыча, вскрыша, концентрат, готовность парка, сценарии |
| bench → block | 2 → 30 | координаты, содержание Cu, крепость (Wi), тоннаж | статус (массив/бурение/заряжен/взорван/отработка/отработан), остаток, P80 |
| excavator | 2 | ковш, цикл, уступ | состояние, блок, содержание в забое, ковшей, очередь, загрузка, наработка |
| haul-truck | 6 | модель, грузоподъёмность, бак | состояние, x/y, дорога, груз, материал, скорость, топливо, рейсы, здоровье |
| road | 8 | узлы, длина, уклон, полилиния | open/closed, самосвалов на участке, проездов |
| face, junction, crusher, stockpile, waste-dump, fuel-station | 8 | координаты, паспортные данные | очереди, производительность, бункер, тонны, содержание |
| plant → mill, flotation | 3 | паспорт | производительность, удельная энергия, мощность, извлечение, концентрат |
| substation | 1 | мощность | нагрузка, энергия, дизель, CO₂ |
| dispatcher | 1 | политика | назначения, средний цикл, ожидание, режим шихтовки |

## Разработка

```bash
make mine            # поднять платформу и рудник
make mine-check      # сквозная проверка
make logs S=mine-sim # журнал симулятора
python3 team/grafana/build_dashboards.py   # пересобрать дашборды после правки
python3 team/scripts/dev_preview.py        # карта без платформы (только для разработки)
```
