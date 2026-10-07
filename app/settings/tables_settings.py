"""Настройки экспорта отчёта: один TOML-файл = один отчёт (имя совпадает с именем .txt).

ReportConfig описывает таблицу, колонки (name -> alias, тип, комментарий), индексы
и колонку периода `event_time`. load_config читает TOML или автогенерирует его из
DataFrame, apply_schema_to_dataset переименовывает колонки и приводит типы.
"""

import re
import tomllib
from pathlib import Path

import polars as pl
import tomlkit
from loguru import logger
from pydantic import BaseModel, ConfigDict, Field

from app.settings import env

# Дата/время в текстовом виде: dd.MM.yyyy [HH:mm:ss]
DATE_STRING_RE = re.compile(r"^\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?$")
DATETIME_TYPE = "datetime64[ns]"


def infer_polars_type(dtype: pl.DataType) -> str:
    """Тип колонки в TOML по типу polars."""
    if dtype.is_integer():
        return "Int64"
    if dtype.is_float():
        return "float64"
    if dtype == pl.Datetime:
        return DATETIME_TYPE
    if dtype == pl.Boolean:
        return "boolean"
    return "string"


# Числа в выгрузках 1С: «1 234 567,89», «-15», «0,5». Целая часть без ведущих нулей
# (иначе это код вроде «007», который должен остаться строкой). \s в Unicode-режиме
# regex-движка polars покрывает и неразрывный пробел (0xA0) — разделитель разрядов.
_INT_PART = r"(?:0|[1-9]\d*|[1-9]\d{0,2}(?:\s\d{3})+)"
INT_RE = rf"^[+-]?{_INT_PART}$"
FLOAT_RE = rf"^[+-]?{_INT_PART}[.,]\d+$"


def infer_string_column_type(series: pl.Series) -> str:
    """Определяет тип текстовой колонки по значениям: Int64, float64 или string.

    Пустые значения игнорируются. Колонка считается числовой, только если ВСЕ
    непустые значения — числа. Целые, не помещающиеся в Int64, считаются дробными.
    """
    values = series.drop_nulls().str.strip_chars()
    values = values.filter(values.str.len_chars() > 0)
    if values.is_empty():
        return "string"

    is_int = values.str.contains(INT_RE)
    if is_int.all():
        digits = values.str.replace_all(r"\s", "")
        return "Int64" if digits.cast(pl.Int64, strict=False).null_count() == 0 else "float64"
    if (is_int | values.str.contains(FLOAT_RE)).all():
        return "float64"
    return "string"


def _column_type(series: pl.Series) -> str:
    """Тип колонки для TOML: по dtype, а для текстовых колонок — по содержимому."""
    col_type = infer_polars_type(series.dtype)
    if col_type == "string" and series.dtype == pl.String:
        return infer_string_column_type(series)
    return col_type


def _parse_dates(col: str, dtype: type[pl.Date] | type[pl.Datetime]) -> pl.Expr:
    """Разбирает текстовую колонку в дату/дату-время (форматы с временем и без)."""
    text = pl.col(col).cast(pl.Utf8, strict=False)
    return (
        text.str.strptime(dtype, "%d.%m.%Y %H:%M:%S", strict=False)
        .fill_null(text.str.strptime(dtype, "%d.%m.%Y", strict=False))
        .alias(col)
    )


class Column(BaseModel):
    name: str
    alias: str | None = None
    type: str = "string"
    comment: str | None = None


class Index(BaseModel):
    use: bool = False
    name: str | None = None
    columns: list[str] = Field(default_factory=list)
    unique: bool = False


class ReportConfig(BaseModel):
    """Плоские настройки экспорта одного отчёта: 1 файл TOML = 1 отчёт."""

    model_config = ConfigDict(extra="allow")
    use: bool = False
    table_name: str = "report01"
    schema_name: str = "marts"
    event_time: str | None = None  # alias колонки с датой операции
    comment: str | None = None  # комментарий таблицы для PostgreSQL
    columns: list[Column] = Field(default_factory=list)
    indexes: list[Index] = Field(default_factory=list)

    @classmethod
    def from_dataset(cls, name: str, df: pl.DataFrame, event_time: str | None = None) -> "ReportConfig":
        """Получает настройки экспорта таблицы из df.
        Заголовки берутся из df.columns (заголовки DataFrame), а не из первой строки данных."""

        if df.is_empty():
            logger.warning(f"Пустой Dataframe: {name}")
            return cls()

        # Берем заголовки из df.columns (это заголовки DataFrame, а не данные)
        generated_columns = [
            Column(
                name=col_name,  # Оригинальное имя колонки из заголовков DataFrame
                type=_column_type(df[col_name]),
                alias=col_name.lower().replace(" ", "_").replace(".", "_"),
            )
            for col_name in df.columns  # df.columns - это заголовки таблицы
        ]

        # Если event_time не указан, ищем первую колонку с типом datetime
        if event_time is None:
            for col in generated_columns:
                if col.type == DATETIME_TYPE:
                    event_time = col.alias
                    logger.info(f"📅 Автоматически выбран event_time: {event_time}")
                    break

        # Фолбек: детект дата-колонок по паттерну строк (dd.MM.yyyy [HH:mm:ss])
        if event_time is None:
            for col in generated_columns:
                if col.type != "string":
                    continue
                try:
                    sample = df[col.name].drop_nulls().head(20).to_list()
                    if sample and all(DATE_STRING_RE.match(str(v).strip()) for v in sample if v is not None):
                        col.type = DATETIME_TYPE
                        event_time = col.alias
                        logger.info(f"📅 Дата-колонка найдена по образцу: {event_time}")
                        break
                except Exception:
                    continue

        # Используем alias для индекса (имена колонок в БД)
        fake_index = Index(
            name="default_idx", columns=[generated_columns[0].alias] if generated_columns else ["column1"]
        )

        logger.info(f"📋 Автогенерация настроек для {name}")
        return cls(
            use=False,
            table_name=f"{name}_table",
            columns=generated_columns,
            indexes=[fake_index],
            event_time=event_time,  # Теперь всегда будет в TOML (даже если None)
        )


def save_config(config_table: BaseModel, toml_file: str):
    """Сохраняет конфиг в TOML через json промежуточный шаг"""
    path = Path(toml_file)
    path.parent.mkdir(parents=True, exist_ok=True)

    # Pydantic -> dict -> TOML
    config_dict = config_table.model_dump(exclude_none=False)

    # Заменяем None на пустую строку для совместимости с TOML
    def replace_none(obj):
        if isinstance(obj, dict):
            return {k: replace_none(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [replace_none(item) for item in obj]
        elif obj is None:
            return ""
        return obj

    config_dict = replace_none(config_dict)

    with open(path, "w", encoding="utf-8") as f:
        # tomllib не умеет dump — пишем через tomlkit
        tomlkit.dump(config_dict, f, sort_keys=False)

    logger.info(f"🎯 Сохранен TOML: {path}")


def load_config(
    toml_file: str | Path,
    df: pl.DataFrame | None = None,
    event_col: str | None = None,
) -> ReportConfig | None:
    """Загружает конфигурацию отчёта из плоского TOML (автогенерирует, если файла нет)."""
    path_toml_file = env.get_project_root() / Path(toml_file)

    if not path_toml_file.exists():
        if df is None:
            logger.error("❌ DataFrame нужен при отсутствии TOML!")
            return None
        logger.warning(f"⚠️  TOML не найден: {path_toml_file}. Генерируем из df...")
        config = ReportConfig.from_dataset(Path(path_toml_file).stem, df, event_col)
        save_config(config, path_toml_file)
        return config

    with open(path_toml_file, "rb") as f:  # tomllib требует bytes
        toml_data = tomllib.load(f)

    return ReportConfig.model_validate(toml_data)


def apply_schema_to_dataset(df: pl.DataFrame, report_settings: ReportConfig) -> pl.DataFrame:
    """Применяет схему к DataFrame: переименовывает name->alias, кастует типы
    и отбрасывает колонки, не описанные в TOML."""
    df = df.clone()

    ts = report_settings.model_dump(exclude_none=True)
    original_columns = list(df.columns)

    # Маппинг настроенных колонок: name -> db_name (alias при наличии, иначе name)
    name_to_db_name: dict[str, str] = {}
    for col in ts.get("columns", []):
        name = col.get("name")
        if not name:
            logger.warning("⚠️ Запись в [columns] без поля name пропущена")
            continue
        alias = col.get("alias")
        name_to_db_name[name] = alias if alias else name

    # Настроенные колонки, отсутствующие в файле -> warning (неточность)
    for name, db_name in name_to_db_name.items():
        if name not in original_columns:
            logger.warning(
                f"⚠️ Неточность: колонка '{name}' (alias '{db_name}') не найдена в исходном файле — пропущена"
            )

    # Переименовываем только существующие колонки файла
    existing_renames = {k: v for k, v in name_to_db_name.items() if k in df.columns}
    df = df.rename(existing_renames)

    # Колонки, реально оставшиеся после переименования (порядок колонок файла)
    kept = [name_to_db_name[k] for k in original_columns if k in name_to_db_name]

    # Лишние колонки файла, не описанные в TOML -> отбрасываем
    dropped = [c for c in original_columns if c not in name_to_db_name]
    if dropped:
        logger.info(f"🗑️ Отброшены колонки файла (не описаны в TOML): {dropped}")

    df = df.select(kept)

    # Приведение типов только для оставшихся (kept) колонок
    for col_config in ts.get("columns", []):
        name = col_config.get("name")
        alias = col_config.get("alias")
        col_name = alias if alias else name
        col_type = col_config.get("type")
        if not name or col_name not in df.columns:
            continue
        try:
            if col_type in ("Int64", "int", "integer"):
                df = df.with_columns(
                    pl.col(col_name)
                    .cast(pl.Utf8)
                    .str.replace_all(r"[\s\xA0]+", "")
                    .str.replace(",", ".")
                    .str.replace(r"\.\d+$", "")
                    .cast(pl.Int64, strict=False)
                    .fill_null(0)
                )

            elif col_type in ("float64", "float"):
                df = df.with_columns(
                    pl.col(col_name)
                    .cast(pl.Utf8)
                    .str.replace_all(r"[\s\xA0]+", "")
                    .str.replace(",", ".")
                    .cast(pl.Float64, strict=False)
                    .fill_null(0.0)
                )

            elif col_type in (DATETIME_TYPE, "datetime"):
                df = df.with_columns(_parse_dates(col_name, pl.Datetime))
            elif col_type == "date":
                df = df.with_columns(_parse_dates(col_name, pl.Date))
            elif col_type == "boolean":
                df = df.with_columns(pl.col(col_name).cast(pl.Boolean, strict=False))
            elif col_type == "string":
                df = df.with_columns(pl.col(col_name).cast(pl.Utf8))
            # ✅ UUID
            elif col_type in ("UUID", "uuid"):
                # Очистка строк и валидация UUID
                df = df.with_columns(pl.col(col_name).cast(pl.Utf8).str.strip_chars().str.replace('"', ""))

        except Exception as e:
            logger.warning(f"⚠️ Тип {col_type} для {col_name}: {e}")

    return df
