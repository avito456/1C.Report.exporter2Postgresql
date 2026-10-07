# Миграция с Pandas на Polars

## Что было сделано

Проект успешно мигрирован с **pandas 3.0.0** на **Polars 0.20+** для решения проблемы зависания при импорте pandas на сервере.

## Преимущества Polars

- ⚡ **Быстрый импорт** - загружается мгновенно (решает проблему зависания)
- 🚀 **В 5-10 раз быстрее** pandas в обработке данных
- 💾 **Меньше памяти** - эффективное использование ресурсов
- 🔄 **Похожий API** - легкая миграция кода

## Измененные файлы

### 1. `pyproject.toml`
```toml
# Было: "pandas>=3.0.0"
# Стало: "polars>=0.20.0"
```

### 2. `app/parsers/report_loader.py`
- Импорт: `import pandas as pd` → `import polars as pl`
- Чтение CSV: `pd.read_csv()` → `pl.read_csv()`
- Переименование колонок: `.columns.str.method()` → `.rename()`

### 3. `app/db/db_uploader.py`
- Импорт: `import pandas as pd` → `import polars as pl`
- Удален: `import numpy as np`, `import pandas.api.types as pdt`
- Типы DataFrame: `pd.DataFrame` → `pl.DataFrame`
- Проверка типов: `pdt.is_*_dtype()` → прямое сравнение с `pl.Int64`, `pl.Float64` и т.д.
- Проверка на пустоту: `.empty` → `.is_empty()`
- Индексация: `.iloc[start:end]` → `.slice(start, length)`
- Замена NaN: `.replace({np.nan: None})` → `.fill_null(None)`
- Получение строк: `.itertuples()` → `.rows()`
- Удаление NaN: `.dropna()` → `.drop_nulls()`
- Получение списка: `.to_list()` вместо `.tolist()`

### 4. `app/settings/tables_settings.py`
- Импорт: `import pandas as pd` → `import polars as pl`
- Типы DataFrame: `pd.DataFrame` → `pl.DataFrame`
- Проверка на пустоту: `.empty` → `.is_empty()`
- Копирование: `.copy()` → `.clone()`
- Переименование: `.rename(columns={})` → `.rename({})`
- Изменение колонок: `df[col] = ...` → `df = df.with_columns(...)`
- Приведение типов: `.astype()` → `.cast()`
- Работа со строками: `.str.method()` → `.str.method()` (аналогично)
- Парсинг дат: `pd.to_datetime()` → `.str.strptime()`
- Числовые преобразования: `pd.to_numeric()` → `.cast(pl.Float64)`

## Установка зависимостей

```bash
# Удалить старые зависимости
uv pip uninstall pandas numpy

# Установить новые зависимости
uv sync

# Или вручную
uv pip install polars>=0.20.0
```

## Основные отличия API

| Операция | Pandas | Polars |
|----------|--------|--------|
| Импорт | `import pandas as pd` | `import polars as pl` |
| Чтение CSV | `pd.read_csv(sep='\t')` | `pl.read_csv(separator='\t')` |
| Пустой DF | `df.empty` | `df.is_empty()` |
| Копирование | `df.copy()` | `df.clone()` |
| Индексация | `df.iloc[0:10]` | `df.slice(0, 10)` |
| Выбор колонок | `df[['col1', 'col2']]` | `df.select(['col1', 'col2'])` |
| Изменение колонки | `df['col'] = ...` | `df = df.with_columns(...)` |
| Удаление NaN | `df.dropna()` | `df.drop_nulls()` |
| Замена NaN | `df.fillna(0)` | `df.fill_null(0)` |
| Проверка NaN | `pd.isna(x)` | `x is None` |
| Приведение типа | `df['col'].astype(int)` | `df['col'].cast(pl.Int64)` |
| Строки в список | `df['col'].tolist()` | `df['col'].to_list()` |
| Строки как кортежи | `df.itertuples()` | `df.rows()` |

## Типы данных Polars

| Pandas | Polars |
|--------|--------|
| `int64` | `pl.Int64` |
| `float64` | `pl.Float64` |
| `bool` | `pl.Boolean` |
| `datetime64[ns]` | `pl.Datetime` |
| `object` / `string` | `pl.Utf8` / `pl.String` |
| `date` | `pl.Date` |

## Тестирование

После миграции рекомендуется протестировать:

1. Чтение CSV файлов
2. Загрузку данных в PostgreSQL
3. Определение типов данных
4. Обработку дат в русском формате

## Откат (если нужно)

Если возникнут проблемы, можно откатиться:

```bash
# В pyproject.toml заменить обратно
"polars>=0.20.0" → "pandas>=3.0.0"

# Установить
uv sync
```

Затем откатить изменения в коде через git:
```bash
git checkout HEAD -- app/parsers/report_loader.py app/db/db_uploader.py app/settings/tables_settings.py
```

## Поддержка

Документация Polars: https://pola-rs.github.io/polars/
