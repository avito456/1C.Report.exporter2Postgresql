# 1C.Report.exporter2Postgresql

Экспорт отчётов из 1С (TXT) в PostgreSQL с настройкой схемы через TOML.

## Настройки экспорта отчётов (TOML)

**1 файл TOML = 1 отчёт.** Файл лежит в корне проекта и называется
точно как исходный `.txt`-файл: `Зарплата.txt` → `Зарплата.toml`.
Секция-обёртка `[reports_export_settings."..."]` не используется —
все ключи расположены на верхнем уровне файла.

```toml
use = true                      # true — разрешена загрузка в БД
table_name = "int_rep__zp"      # имя таблицы PostgreSQL
schema_name = "intermediate"    # схема PostgreSQL
event_time = "period_month"     # alias колонки-даты (для DELETE периода)
comment = "Описание таблицы"    # COMMENT ON TABLE

[[columns]]
name = "Дата"                   # заголовок колонки в .txt (как в файле)
alias = "period_month"          # имя колонки в БД
type = "date"                   # string | int | float | date | datetime | boolean | uuid
comment = "Период месяц"        # COMMENT ON COLUMN

[[indexes]]
use = true
name = "zp_unique_idx"
columns = ["period_month"]
unique = true
```

Правила:

- Колонка файла, **не описана** в `[[columns]]` → отбрасывается из выгрузки.
- Колонка **описана**, но отсутствует в файле → пропуск + warning в логе.
- `use = false` → загрузка отчёта отменяется.
- Файла `.toml` нет → при первом запуске он **автоматически создаётся**
  из заголовков `.txt` (все типы `string`, `use = false`) — плоским форматом.

### Связь txt ↔ toml

Связь определяется **только совпадением имён**: имя `.txt` = имя `.toml`.
Имя файла `.toml` и его содержимое связаны формально: `load_config` читает
файл по имени исходника, плоский формат секции не содержит.

## Архитектура

```
watchdog (app/server.py) → asyncio-очередь → разбор TXT (app/parsers/report_loader.py)
  → настройки TOML (app/settings/tables_settings.py) → загрузка (app/db/db_uploader.py, COPY)
```

Каждый модуль начинается с docstring, описывающего его назначение.

## Разработка

```bash
uv sync --group dev                      # зависимости, включая pytest, ruff, pyinstaller
uv run pytest                            # тесты
uv run ruff check . && uv run ruff format .   # линтер и форматирование
uv run python compile.py                 # сборка exe (PyInstaller)
```
