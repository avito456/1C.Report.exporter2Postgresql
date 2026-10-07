# План: переход на плоский формат TOML-настроек отчётов

**Дата:** 2026-10-07
**Статус:** выполняется

## Проблема

- Секции вида `[reports_export_settings."лдАнализПродажПродавцов_PBI (TXT)"]` — избыточная обёртка: имя отчёта дублируется 4 раза (имя `.txt` → имя `.toml` → ключ секции → база `table_name`), путь секции повторяется в начале каждой `[[...columns]]` (в `Orders.toml` — 47 раз).
- `Config.from_dataset` всегда кладёт ровно 1 запись → фактически «1 файл = 1 отчёт».
- Расхождение имени секции и stem файла = рантаймовый `Missing TOML section` или тихий автоген нового файла.

## Согласованные решения

| Вопрос | Решение |
|---|---|
| Структура TOML | Плоский формат, 1 файл = 1 отчёт, без секции-обёртки |
| Миграция | Одноразовый скрипт `scripts/migrate_toml.py` (dry-run → apply) |
| Сироты (underscore-файл, `app/`, `_Тест`, `Orders`/`table_settings` без txt) | **Не трогать** — конвертируется только формат внутри файлов |
| Нормализация имён отчётов | Нет, имена как есть |
| Fallback и чистка | Dual-format чтение в `load_config` + мелкая чистка |
| Документация | Краткий раздел в README.md |

## Целевой формат

**Было:**

```toml
[reports_export_settings."лдАнализПродажПродавцов_PBI (TXT)"]
use = false
table_name = "..."
schema_name = "marts"

[[reports_export_settings."лдАнализПродажПродавцов_PBI (TXT)".columns]]
name = "Код магазина"
alias = "код_магазина"
type = "string"
```

**Станет:**

```toml
use = false
table_name = "лдАнализПродажПродавцов_PBI (TXT)_table"
schema_name = "marts"
event_time = ""
comment = ""

[[columns]]
name = "Код магазина"
alias = "код_магазина"
type = "string"
comment = ""

[[indexes]]
use = false
name = "default_idx"
columns = ["код_магазина"]
unique = false
```

## Этапы

### Этап 1 — `app/settings/tables_settings.py`
1. Плоская модель верхнего уровня (`ReportConfig`): `use`, `table_name`, `schema_name`, `event_time`, `comment`, `columns`, `indexes`. `Config`-словарь — только для чтения legacy.
2. `load_config()`: автоопределение формата; legacy-ключ `reports_export_settings` → распаковка единственной записи + **warning, если ключ секции ≠ stem файла**.
3. `save_config()` / `from_dataset()` → всегда плоский формат.
4. Чистка: default `event_time` → `None`; удалить мёртвый `process_dataset_with_schema`; `apply_schema_to_dataset` принимает плоские настройки.

### Этап 2 — `app/server.py`
5. Убрать `.get(toml_file)` и ветку `Missing TOML section`; передавать настройки напрямую.

### Этап 3 — `scripts/migrate_toml.py`
6. Обход `*.toml` корня (кроме `pyproject.toml`), конвертер legacy → плоский через `tomlkit`, dry-run по умолчанию, `--apply` для записи. Отчёт: файл → действие → предупреждения. Сироты не переименовывать/удалять.
7. Запуск dry-run → отчёт → `--apply` → коммит.

### Этап 4 — Тесты
8. Новые: `load_config` legacy/плоский/рассинхрон секции, `from_dataset` → плоский, round-trip миграции; обновить `tests/test_apply_schema.py`.
9. Прогон: `.venv\Scripts\python.exe -B -m unittest discover -s tests -v` ( baseline: 8 tests OK).

### Этап 5 — Документация
10. Краткий раздел в `README.md`: целевой формат, связь `txt ↔ toml`, правила миграции, поведение fallback.

## Проверка результата

- Тесты зелёные.
- `python -c "import app.server"` без ошибок.
- Dry-run миграции без ошибок на всех TOML.

## Коммиты

Каждый успешно пройденный этап закоммичен отдельно.
