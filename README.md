# ML: Восточно-Жезказганский рудник — цифровой двойник горизонта для горного мастера

*English summary at the end.*

Цифровой двойник добычного горизонта подземного медного рудника (ВЖР, Казахмыс, г. Сатпаев) на
платформе OpenEgiz v1.0.4. 166 двойников в Eclipse Ditto: вентиляционная сеть с ВГП, выработки,
двери и регуляторы, датчики аэрогазового контроля, три панели с 54 камерами и 54 целиками,
звенья горнорабочих, ПДМ и буровые установки, рудоспуски, дробилка, подъём, наряд смены.
Двойник отвечает горному мастеру на вопрос, в какие камеры можно ставить людей: хватает ли
воздуха и держат ли целики. Замысел и описание: [SUBMISSION.md](SUBMISSION.md), презентация:
[presentation.pdf](presentation.pdf), устройство кода: [team/README.md](team/README.md).

## Запуск

Требования: Linux с Docker Engine и Compose v2 (или macOS / Windows WSL2 с Docker Desktop),
`git`, `make`, 6 ГБ памяти для Docker, интернет для загрузки образов. Проверено на чистой
Ubuntu 24.04 с Docker 29 (окружение судьи) и на macOS.

```bash
sudo apt install -y make        # на чистой Ubuntu make не установлен
make mine
```

`make mine` поднимает платформу OpenEgiz (первый запуск скачивает около 2,3 ГБ образов и
собирает два образа, 10–20 минут), создаёт двойники горизонта в Ditto, запускает симулятор,
план горизонта и подключает дашборды. В конце печатаются адреса и пароли. Ручных действий не
требуется.

Сквозная проверка (двойники → телеметрия → InfluxDB → Grafana → план → сценарий и его отмена):

```bash
make mine-check
```

Остановить и запустить снова: `make down`, `make up` (рудник поднимается вместе с платформой).
Удалить всё вместе с данными и паролями: `make clean`.

## Что открыть

| Что | Где |
|---|---|
| **План горизонта, наряд смены, сценарии «что если»** | http://localhost:8095/ |
| Дашборды Grafana, папка **ВЖР**: обзор смены, воздух и газы, горное давление и целики, наряд и камеры, план | http://localhost:3000/dashboards?query=ВЖР — логин `admin`, пароль печатает `make mine` (хранится в `deploy/compose/.env`) |
| Двойники и их иерархия | Grafana → приложение **OpenEgiz** → **Twins**, пространство имён `org.openegiz.zhezkazgan` |
| Ditto API | http://localhost:8080/api/2/search/things?namespaces=org.openegiz.zhezkazgan — логин `ditto`, пароль из `deploy/compose/.env` |

Симуляция идёт в 20 раз быстрее реального времени (`SIM_SPEEDUP` в `team/compose.yml`): смена
8 часов проходит за 24 минуты на стенде; взрывы происходят в пересменку, проветривание занимает
около 1,5 минут стенда.

## Как пользоваться (5 минут)

1. Откройте план. Камеры зелёные — наряд разрешён, жёлтые — ограничения (без дизеля или без
   взрывов), красные — запрещено; причина в списке справа и во всплывающей подсказке.
2. В разделе «Наряд смены» выберите звену другую камеру и нажмите «→». Двойник примет наряд
   только в допустимую камеру; попытку поставить людей в запрещённую он отклонит с причиной.
3. Нажмите сценарий, например «ВГП: снижение подачи до 50 %», и смотрите, как струи, вердикты и
   список запрещённых камер меняются; повторное нажатие отменяет сценарий, «Сбросить все
   сценарии» возвращает штатный режим.
4. В Grafana откройте «ВЖР: воздух и газы» и «ВЖР: горное давление и целики», чтобы увидеть
   те же величины во времени.

Те же команды без браузера:

```bash
curl -XPOST localhost:8095/api/command -H 'Content-Type: application/json' -d '{"action":"fan","speed_pct":50}'
curl -XPOST localhost:8095/api/command -H 'Content-Type: application/json' -d '{"action":"assign","crew":"crew-1","chamber":"k1-23"}'
```

## Состав репозитория

| Путь | Содержимое |
|---|---|
| `team/` | Код команды: двойники, симулятор, план, дашборды, скрипты, данные ([team/README.md](team/README.md)) |
| `Makefile` | Добавлены цели `mine`, `mine-stop`, `mine-check` |
| `SUBMISSION.md`, `presentation.pdf` | Замысел, описание и презентация решения |
| `OPENEGIZ.md`, `OPENEGIZ.ru.md`, `deploy/`, `examples/`, … | Платформа OpenEgiz v1.0.4 без изменений |

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `Ports already in use` | Занят один из портов 3000, 8080, 8085, 8086, 8090, 1883, 8095: остановите программу или поменяйте порт в `deploy/compose/.env` и повторите `make mine` |
| `make mine` упал на `init` или `mine-setup` | Ditto не успел подняться: выполните `make mine` ещё раз |
| План пустой | Подождите 10–20 с после старта (Ditto индексирует двойники); `make logs S=mine-control` |
| Нет точек на дашбордах | Telegraf пишет раз в 10 с; `make logs S=mine-sim` должен показывать `sim … h` |

## Лицензия

MIT, см. [LICENSE](LICENSE). Платформа OpenEgiz: MIT; части, производные от OpenTwins, Apache 2.0
(см. NOTICE). Данные seismic-bumps (`team/data/seismic-bumps`): CC BY 4.0, атрибуция в файле рядом.

---

**English.** Digital twin of a production level of the East Zhezkazgan underground copper mine
(Kazakhmys, Satpayev) on OpenEgiz v1.0.4, built for the shift foreman: 166 Ditto twins
(ventilation network with the main fan, airways, doors and regulators, gas sensors, three
room-and-pillar panels with 54 chambers and 54 pillars, crews, LHDs and jumbos, ore passes,
crusher, hoist, shift plan). The twin answers where people may be sent this shift: is there enough
air and do the pillars hold. Run: `sudo apt install -y make && make mine`; verify: `make mine-check`.
Open the level plan at http://localhost:8095/ and the Grafana folder *ВЖР* at http://localhost:3000/
(admin password is printed by `make mine`).
