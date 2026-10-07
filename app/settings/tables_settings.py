import tomllib  # Python 3.11+
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field, ConfigDict
import polars as pl
from loguru import logger
from app.settings import env


class Column(BaseModel):
    name: str
    alias: Optional[str] = None
    type: str = "string"
    comment: Optional[str] = None


class Index(BaseModel):
    use: bool = False
    name: Optional[str] = None
    columns: List[str] = Field(default_factory=list)
    unique: bool = False


class ReportConfig(BaseModel):
    """Плоские настройки экспорта одного отчёта: 1 файл TOML = 1 отчёт."""
    model_config = ConfigDict(extra='allow')
    use: bool = False
    table_name: str = "report01"
    schema_name: str = "marts"
    event_time: Optional[str] = None   # alias колонки с датой операции
    comment: Optional[str] = None      # комментарий таблицы для PostgreSQL
    columns: List[Column] = Field(default_factory=list)
    indexes: List[Index] = Field(default_factory=list)

    @classmethod
    def from_dataset(cls, name: str, df: pl.DataFrame,
                     event_time: Optional[str] = None) -> 'ReportConfig':
        """Получает настройки экспорта таблицы из df.
        Заголовки берутся из df.columns (заголовки DataFrame), а не из первой строки данных."""

        if df.is_empty():
            logger.warning(f"Пустой Dataframe: {name}")
            return cls()

        def infer_polars_type(dtype) -> str:
            if dtype in [pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64]:
                return "Int64"
            elif dtype in [pl.Float32, pl.Float64]:
                return "float64"
            elif dtype in [pl.Datetime, pl.Datetime("ms"), pl.Datetime("us"), pl.Datetime("ns")]:
                return "datetime64[ns]"
            elif dtype == pl.Boolean:
                return "boolean"
            return "string"

        # Берем заголовки из df.columns (это заголовки DataFrame, а не данные)
        generated_columns = [
            Column(
                name=col_name,  # Оригинальное имя колонки из заголовков DataFrame
                type=infer_polars_type(df[col_name].dtype),
                alias=col_name.lower().replace(' ', '_').replace('.', '_')
            )
            for col_name in df.columns  # df.columns - это заголовки таблицы
        ]

        # Если event_time не указан, ищем первую колонку с типом datetime
        if event_time is None:
            for col in generated_columns:
                if col.type == "datetime64[ns]":
                    event_time = col.alias
                    logger.info(f"📅 Автоматически выбран event_time: {event_time}")
                    break

        # Фолбек: детект дата-колонок по паттерну строк (dd.MM.yyyy [HH:mm:ss])
        if event_time is None:
            import re as _re
            _date_re = _re.compile(r'^\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?$')
            for col in generated_columns:
                if col.type != "string":
                    continue
                try:
                    sample = df[col.name].drop_nulls().head(20).to_list()
                    if sample and all(
                        _date_re.match(str(v).strip())
                        for v in sample
                        if v is not None
                    ):
                        col.type = "datetime64[ns]"
                        event_time = col.alias
                        logger.info(f"📅 Дата-колонка найдена по образцу: {event_time}")
                        break
                except Exception:
                    continue

        # Используем alias для индекса (имена колонок в БД)
        fake_index = Index(name='default_idx',
                           columns=[generated_columns[0].alias] if generated_columns else ['column1'])

        logger.info(f'📋 Автогенерация настроек для {name}')
        return cls(
            use=False,
            table_name=f"{name}_table",
            columns=generated_columns,
            indexes=[fake_index],
            event_time=event_time  # Теперь всегда будет в TOML (даже если None)
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
        # tomllib не умеет dump, используем tomlkit или ручной дамп
        import tomlkit
        tomlkit.dump(config_dict, f, sort_keys=False)

    logger.info(f'🎯 Сохранен TOML: {path}')


def load_config(toml_file: str | Path, df: Optional[pl.DataFrame] = None, event_col: Optional[str] = None) -> ReportConfig | None:
    """Загружает конфигурацию отчёта из плоского TOML (автогенерирует, если файла нет)."""
    path_toml_file = env.get_project_root() / Path(toml_file)

    if not path_toml_file.exists():
        if df is None:
            logger.error("❌ DataFrame нужен при отсутствии TOML!")
            return None
        logger.warning(f'⚠️  TOML не найден: {path_toml_file}. Генерируем из df...')
        config = ReportConfig.from_dataset(Path(path_toml_file).stem, df, event_col)
        save_config(config, path_toml_file)
        return config

    with open(path_toml_file, 'rb') as f:  # tomllib требует bytes
        toml_data = tomllib.load(f)

    return ReportConfig.model_validate(toml_data)


def apply_schema_to_dataset(df: pl.DataFrame, report_settings: ReportConfig) -> pl.DataFrame:
    """Применяет схему к DataFrame: переименовывает name->alias, кастует типы
    и отбрасывает колонки, не описанные в TOML."""
    df = df.clone()

    ts = report_settings.model_dump(exclude_none=True)
    original_columns = list(df.columns)

    # Маппинг настроенных колонок: name -> db_name (alias при наличии, иначе name)
    name_to_db_name: Dict[str, str] = {}
    for col in ts.get('columns', []):
        name = col.get('name')
        if not name:
            logger.warning("⚠️ Запись в [columns] без поля name пропущена")
            continue
        alias = col.get('alias')
        name_to_db_name[name] = alias if alias else name

    # Настроенные колонки, отсутствующие в файле -> warning (неточность)
    for name, db_name in name_to_db_name.items():
        if name not in original_columns:
            logger.warning(
                f"⚠️ Неточность: колонка '{name}' (alias '{db_name}') "
                f"не найдена в исходном файле — пропущена"
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
    for col_config in ts.get('columns', []):
        name = col_config.get('name')
        alias = col_config.get('alias')
        col_name = alias if alias else name
        col_type = col_config.get('type')
        if not name or col_name not in df.columns:
            continue
        try:
            if col_type in ("Int64", "int", "integer"):
                df = df.with_columns(
                    pl.col(col_name).cast(pl.Utf8)
                    .str.replace_all(r'[\s\xA0]+', '')
                    .str.replace(',', '.')
                    .str.replace(r'\.\d+$', '')
                    .cast(pl.Int64, strict=False)
                    .fill_null(0)
                )

            elif col_type in ("float64", "float"):
                df = df.with_columns(
                    pl.col(col_name).cast(pl.Utf8)
                    .str.replace_all(r'[\s\xA0]+', '')
                    .str.replace(',', '.')
                    .cast(pl.Float64, strict=False)
                    .fill_null(0.0)
                )

            elif col_type in ("datetime64[ns]", "datetime"):
                # Приводим к строке и парсим даты
                df = df.with_columns(
                    pl.col(col_name)
                    .cast(pl.Utf8, strict=False)
                    .alias(col_name)
                )
                # Парсим с двумя форматами
                df = df.with_columns(
                    pl.col(col_name)
                    .str.strptime(pl.Datetime, "%d.%m.%Y %H:%M:%S", strict=False)
                    .fill_null(
                        pl.col(col_name).str.strptime(pl.Datetime, "%d.%m.%Y", strict=False)
                    )
                    .alias(col_name)
                )
            elif col_type in ("date", "datetime"):
                # Приводим к строке и парсим даты
                df = df.with_columns(
                    pl.col(col_name)
                    .cast(pl.Utf8, strict=False)
                    .alias(col_name)
                )
                # Парсим с двумя форматами
                df = df.with_columns(
                    pl.col(col_name)
                    .str.strptime(pl.Date, "%d.%m.%Y %H:%M:%S", strict=False)
                    .fill_null(
                        pl.col(col_name).str.strptime(pl.Date, "%d.%m.%Y", strict=False)
                    )
                    .alias(col_name)
                )
            elif col_type == "boolean":
                df = df.with_columns(pl.col(col_name).cast(pl.Boolean, strict=False))
            elif col_type == "string":
                df = df.with_columns(pl.col(col_name).cast(pl.Utf8))
            # ✅ UUID
            elif col_type in ("UUID", "uuid"):
                # Очистка строк и валидация UUID
                df = df.with_columns(
                    pl.col(col_name).cast(pl.Utf8).str.strip_chars().str.replace('"', '')
                )

        except Exception as e:
            logger.warning(f"⚠️ Тип {col_type} для {col_name}: {e}")

    return df
