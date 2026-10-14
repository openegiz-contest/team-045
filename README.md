# ML: карьер «Сарыбұлақ» — цифровой рудник на OpenEgiz

*English summary at the end.*

Цифровой двойник медного карьера на платформе OpenEgiz v1.0.4: 62 двойника в Eclipse Ditto
(уступы, блочная модель, экскаваторы, самосвалы, дороги, дробилка, склад, отвал, фабрика,
подстанция, диспетчер), симулятор горных работ с диспетчеризацией, рудоконтролем и шихтовкой,
БВР, переработкой, энергетикой и надёжностью техники, живая 2D-карта карьера и дашборды Grafana,
сценарии «что если» с отменой. Описание решения: [SUBMISSION.md](SUBMISSION.md),
презентация: [presentation.pdf](presentation.pdf), устройство кода: [team/README.md](team/README.md).

## Запуск

Требования: Linux с Docker Engine и Compose v2 (или macOS / Windows WSL2 с Docker Desktop),
`git`, `make`, 6 ГБ памяти для Docker, доступ в интернет для загрузки образов.
Проверено на чистой Ubuntu 24.04 с Docker 29 (окружение судьи).

```bash
sudo apt install -y make        # на чистой Ubuntu make не установлен
make mine
```

`make mine` поднимает платформу OpenEgiz (первый запуск скачивает около 2,3 ГБ образов и
собирает два образа, 10–20 минут), создаёт двойники карьера в Ditto, запускает симулятор,
карту и подключает дашборды. В конце печатаются адреса и пароли. Ручных действий не требуется.

Проверка, что всё работает от начала до конца (двойники → телеметрия → InfluxDB → Grafana →
карта → сценарий):

```bash
make mine-check
```

Остановить и запустить снова: `make down`, `make up` (рудник поднимается вместе с платформой).
Удалить всё вместе с данными и паролями: `make clean`.

## Что открыть

| Что | Где |
|---|---|
| **Карта карьера и сценарии «что если»** | http://localhost:8095/ |
| Дашборды Grafana (папка **Sarybulak Mine**: обзор, транспорт, фабрика и энергетика, карта) | http://localhost:3000/dashboards?query=Sarybulak — логин `admin`, пароль печатает `make mine` (хранится в `deploy/compose/.env`) |
| Двойники и их иерархия | Grafana → приложение **OpenEgiz** → **Twins**, пространство имён `org.openegiz.sarybulak` |
| Ditto API | http://localhost:8080/api/2/search/things?namespaces=org.openegiz.sarybulak — логин `ditto`, пароль из `deploy/compose/.env` |

Симуляция идёт в 10 раз быстрее реального времени (`SIM_SPEEDUP` в `team/compose.yml`):
за минуту на стенде проходит 10 минут смены, полный цикл самосвала занимает около 1,5 минут.

## Сценарии «что если»

Кнопки в правой панели карты. Каждая кнопка включает сценарий, повторное нажатие отменяет его,
«Сбросить все сценарии» возвращает штатный режим. Активные сценарии видны в шапке карты и в
feature `active_scenarios` двойника `org.openegiz.sarybulak:mine`. Подробности и ожидаемые
последствия: [SUBMISSION.md](SUBMISSION.md#модули-и-сценарии-что-если).

Те же команды можно отправить без браузера:

```bash
curl -XPOST localhost:8095/api/command -H 'Content-Type: application/json' \
  -d '{"action":"excavator_down","target":"excavator-01","on":true}'
```

## Состав репозитория

| Путь | Содержимое |
|---|---|
| `team/` | Код команды: двойники, симулятор, карта, дашборды, скрипты, данные ([team/README.md](team/README.md)) |
| `Makefile` | Добавлены цели `mine`, `mine-stop`, `mine-check` |
| `SUBMISSION.md`, `presentation.pdf` | Описание и презентация решения |
| `OPENEGIZ.md`, `OPENEGIZ.ru.md`, `deploy/`, `examples/`, … | Платформа OpenEgiz v1.0.4 без изменений |

## Если что-то не так

| Симптом | Что делать |
|---|---|
| `Ports already in use` | Занят один из портов 3000, 8080, 8085, 8086, 8090, 1883, 8095: остановите программу или поменяйте порт в `deploy/compose/.env` и повторите `make mine` |
| `make mine` упал на `init` или `mine-setup` | Ditto не успел подняться: выполните `make mine` ещё раз |
| Карта пустая | Подождите 10–20 с после старта (Ditto индексирует двойники); `make logs S=mine-control` |
| Нет точек на дашбордах | Telegraf пишет раз в 10 с; `make logs S=mine-sim` должен показывать `sim … h` |

## Лицензия

MIT, см. [LICENSE](LICENSE). Платформа OpenEgiz: MIT; части, производные от OpenTwins, Apache 2.0
(см. NOTICE). Данные OpenMines (`team/data/openmines`): MIT, текст лицензии приложен.

---

**English.** Digital twin of an open-pit copper mine on OpenEgiz v1.0.4: 62 Ditto twins
(benches, block model, excavators, haul trucks, roads, crusher, stockpile, dump, plant,
substation, dispatcher), a mining simulator with dispatching, ore control and blending,
drill-and-blast, processing, energy and reliability, a live 2D map and Grafana dashboards,
what-if scenarios with undo. Run: `sudo apt install -y make && make mine`; verify: `make mine-check`.
Open the map at http://localhost:8095/ and the Grafana folder *Sarybulak Mine* at
http://localhost:3000/ (admin password is printed by `make mine`).
