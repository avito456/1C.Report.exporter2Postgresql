"""Загрузка polars.DataFrame в PostgreSQL через asyncpg.

AsyncDatasetToPostgres выводит схему таблицы из DataFrame, создаёт таблицу и
недостающие колонки, приводит типы существующих колонок, удаляет строки за период
отчёта (`event_time`), заливает данные через COPY и создаёт индексы CONCURRENTLY.
Весь DDL выполняется с lock_timeout, чтобы не зависать на блокировках BI-клиентов.

Также содержит вспомогательные функции: экранирование идентификаторов, учёт
63-байтового усечения имён колонок в PostgreSQL и проверку границ периода.
"""

import re
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import asyncpg
import pendulum
import polars as pl
from loguru import logger

UUID_PATTERN = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

ID_COLUMNS = {"id", "ID", "Id"}  # служебный первичный ключ таблицы, в загрузку не входит
LOCK_TIMEOUT = "5s"


@dataclass(frozen=True)
class IndexDef:
    name: str
    columns: list[str]
    use: bool = True
    unique: bool = False


def quote_identifier(name: str) -> str:
    if not name:
        return ""
    return '"' + name.replace('"', '""') + '"'


def quote_literal(value: str) -> str:
    """Строковый литерал SQL (для COMMENT ON, не поддерживающего параметры)."""
    return "'" + value.replace("'", "''") + "'"


def pg_truncate_name(name: str) -> str:
    """Обрезает имя до 63 байт, как это делает PostgreSQL (NAMEDATALEN-1).

    Длинные идентификаторы (например, кириллические имена колонок) Postgres
    сохраняет под усечённым именем. Функция повторяет это правило на уровне
    байтов UTF-8, не разрезая многобайтовый символ.
    """
    encoded = name.encode("utf-8")
    if len(encoded) <= 63:
        return name
    encoded = encoded[:63]
    while encoded and (encoded[-1] & 0xC0) == 0x80:
        encoded = encoded[:-1]
    if encoded and (encoded[-1] & 0xC0) == 0xC0:
        encoded = encoded[:-1]
    return encoded.decode("utf-8")


def resolve_existing_columns(existing: set[str], expected: Iterable[str]) -> dict[str, str]:
    """Сопоставляет ожидаемое полное имя колонки с фактическим именем в БД.

    Postgres хранит длинные идентификаторы (более 63 байт) под усечённым именем,
    поэтому колонка, добавленная ранее, возвращается из information_schema уже
    обрезанной. Функция строит маппинг: полное имя -> фактическое имя в БД
    (точное совпадение либо 63-байтовая обрезка), чтобы не терять длинные
    колонки и не циклически их добавлять/удалять.
    """
    mapping: dict[str, str] = {}
    for name in expected:
        if name in existing:
            mapping[name] = name
            continue
        truncated = pg_truncate_name(name)
        if truncated != name and truncated in existing:
            mapping[name] = truncated
    return mapping


def is_period_event_time(event_time: str | None) -> bool:
    return str(event_time or "").strip().lower() == "period"


def missing_required_period_bounds(
    event_time: str | None,
    start_period: Any,
    end_period: Any,
) -> list[str]:
    if not is_period_event_time(event_time):
        return []

    missing = []
    if start_period is None:
        missing.append("start_period")
    if end_period is None and start_period is None:
        missing.append("end_period")
    return missing


def validate_required_period_bounds(
    event_time: str | None,
    start_period: Any,
    end_period: Any,
) -> None:
    missing = missing_required_period_bounds(event_time, start_period, end_period)
    if missing:
        raise ValueError(
            'Parameter [event_time] = "period" in toml file requires parsed '
            f"header dates: start_period and end_period. Missing: {', '.join(missing)}"
        )


class AsyncDatasetToPostgres:
    def __init__(
        self,
        db_params: dict[str, Any],
        dataframe: pl.DataFrame,
        table_name: str,
        schema: str = "public",
        event_time: str | None = None,
        start_period: datetime | None = None,
        end_period: datetime | None = None,
        indexes: list[IndexDef] | list[dict[str, Any]] | None = None,
        comment: str | None = None,
        column_comments: dict[str, str] | None = None,
    ):
        self.db_params = db_params
        self.table_name = table_name
        self.schema = schema
        self.event_time = event_time
        self.start_period = self._naive(start_period)
        self.end_period = self._naive(end_period)
        self.indexes = self._normalize_indexes(indexes)
        self.comment = comment
        self.column_comments = column_comments or {}
        self.pool: asyncpg.Pool | None = None
        # Время загрузки "date_download" (московское, без часового пояса)
        self._df = dataframe.with_columns(
            pl.lit(pendulum.now("Europe/Moscow").replace(tzinfo=None))
            .cast(pl.Datetime)
            .alias("date_download")
        )
        self._columns: list[str] | None = None
        self._schema_def: dict[str, str] | None = None
        self._table_ready = False
        logger.debug(f"Init uploader for {schema}.{table_name}")

    @staticmethod
    def _naive(value: datetime | None) -> datetime | None:
        return value.replace(tzinfo=None) if value is not None else None

    @staticmethod
    def _normalize_indexes(raw_indexes: list[Any] | None) -> list[IndexDef]:
        normalized: list[IndexDef] = []
        for idx in raw_indexes or []:
            if isinstance(idx, IndexDef):
                normalized.append(idx)
            elif isinstance(idx, dict):
                normalized.append(
                    IndexDef(
                        name=str(idx.get("name") or "").strip(),
                        columns=[str(col) for col in idx.get("columns", []) if str(col).strip()],
                        use=bool(idx.get("use", True)),
                        unique=bool(idx.get("unique", False)),
                    )
                )
        return normalized

    @property
    def df(self) -> pl.DataFrame:
        return self._df

    @property
    def columns(self) -> list[str]:
        if self._columns is None:
            self._columns = [col for col in self.df.columns if col not in ID_COLUMNS]
        return self._columns

    @property
    def qualified_table(self) -> str:
        return f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"

    async def ensure_pool(self) -> None:
        if self.pool is None:
            # Загрузка одного файла последовательная — большой пул не нужен.
            self.pool = await asyncpg.create_pool(
                **self.db_params,
                min_size=1,
                max_size=2,
                max_queries=50000,
                max_inactive_connection_lifetime=1800,
                command_timeout=120,
                server_settings={
                    "default_transaction_read_only": "off",
                    "search_path": self.schema,
                },
            )
            logger.debug("Connection pool created")

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    @asynccontextmanager
    async def _ddl_connection(self) -> AsyncIterator[asyncpg.Connection]:
        """Соединение с lock_timeout для DDL.

        Без lock_timeout ALTER/CREATE INDEX могут зависнуть навсегда на блокировке,
        занятой другой сессией (например, открытой транзакцией в BI-клиенте).
        """
        await self.ensure_pool()
        async with self.pool.acquire() as conn:
            await conn.execute(f"SET lock_timeout = '{LOCK_TIMEOUT}'")
            try:
                yield conn
            finally:
                try:
                    await conn.execute("RESET lock_timeout")
                except Exception:
                    pass

    @staticmethod
    def _pg_type(dtype: pl.DataType, series: pl.Series) -> str:
        if dtype.is_integer():
            return "BIGINT"
        if dtype.is_float():
            return "DOUBLE PRECISION"
        if dtype == pl.Boolean:
            return "BOOLEAN"
        if dtype == pl.Datetime:
            return "TIMESTAMP"
        if dtype == pl.Date:
            return "DATE"
        if dtype == pl.String:
            # UUID, если все значения первых 10 непустых строк имеют формат UUID
            sample = series.drop_nulls().head(10).to_list()
            if sample and all(UUID_PATTERN.match(str(value)) for value in sample):
                return "UUID"
        return "TEXT"

    def _infer_schema(self) -> dict[str, str]:
        if self._schema_def is None:
            self._schema_def = {col: self._pg_type(self.df[col].dtype, self.df[col]) for col in self.columns}
        return self._schema_def

    async def create_table(self) -> None:
        """Создаёт таблицу (если её нет) и добирает недостающие колонки и комментарии.

        Выполняется один раз за жизнь загрузчика (повторные вызовы — no-op).
        """
        if self._table_ready:
            return

        schema_def = self._infer_schema()
        columns_sql = ", ".join(f"{quote_identifier(k)} {v}" for k, v in schema_def.items())
        qualified_table = self.qualified_table

        async with self._ddl_connection() as conn:
            await conn.execute(
                f"CREATE TABLE IF NOT EXISTS {qualified_table} (id BIGSERIAL PRIMARY KEY, {columns_sql})"
            )

            existing_rows = await conn.fetch(
                """
                SELECT column_name FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                """,
                self.schema,
                self.table_name,
            )
            existing = {row["column_name"] for row in existing_rows}
            resolved = resolve_existing_columns(existing, schema_def)

            if "date_download" not in existing:
                await conn.execute(
                    f"ALTER TABLE {qualified_table} "
                    "ADD COLUMN IF NOT EXISTS date_download TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
                )

            missing = [col for col in schema_def if col not in resolved]
            if missing:
                add_sql = ", ".join(
                    f"ADD COLUMN IF NOT EXISTS {quote_identifier(col)} {schema_def[col]}" for col in missing
                )
                await conn.execute(f"ALTER TABLE {qualified_table} {add_sql}")
                logger.info(f"Columns added to {qualified_table}: {missing}")

            await self._sync_comments(conn)

        self._table_ready = True
        logger.info(f"Table is ready: [{self.schema}.{self.table_name}] ({self.comment})")

    async def _sync_comments(self, conn: asyncpg.Connection) -> None:
        """Приводит комментарии таблицы и колонок в БД к значениям из TOML."""
        if self.comment:
            current_comment = await conn.fetchval(
                """
                SELECT obj_description(c.oid, 'pg_class')
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = $1 AND c.relname = $2
                """,
                self.schema,
                self.table_name,
            )
            if current_comment != self.comment:
                await conn.execute(
                    f"COMMENT ON TABLE {self.qualified_table} IS {quote_literal(self.comment)}"
                )

        expected = {col: text for col, text in self.column_comments.items() if text}
        if not expected:
            return

        current = {
            row["attname"]: row["comment"]
            for row in await conn.fetch(
                """
                SELECT pa.attname, col_description(pc.oid, pa.attnum) AS comment
                FROM pg_class pc
                JOIN pg_namespace pn ON pn.oid = pc.relnamespace
                JOIN pg_attribute pa ON pa.attrelid = pc.oid
                WHERE pn.nspname = $1 AND pc.relname = $2
                  AND pa.attnum > 0 AND NOT pa.attisdropped
                """,
                self.schema,
                self.table_name,
            )
        }
        for col, text in expected.items():
            if current.get(col) != text:
                column = f"{self.qualified_table}.{quote_identifier(col)}"
                await conn.execute(f"COMMENT ON COLUMN {column} IS {quote_literal(text)}")

    async def sync_schema(self) -> None:
        """Приводит таблицу к схеме текущего DataFrame.

        Добавляет недостающие колонки, меняет тип расходящихся и удаляет лишние
        (не описанные в TOML).
        """
        schema_def = self._infer_schema()
        qualified_table = self.qualified_table

        async with self._ddl_connection() as conn:
            existing_rows = await conn.fetch(
                """
                SELECT column_name, data_type FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2
                """,
                self.schema,
                self.table_name,
            )
            existing_types = {row["column_name"]: row["data_type"] for row in existing_rows}

            # Сопоставляем полные имена колонок с фактическими именами в БД,
            # учитывая 63-байтовую обрезку длинных идентификаторов Postgres.
            resolved = resolve_existing_columns(set(existing_types), self.columns)
            resolved_db = set(resolved.values()) | ID_COLUMNS

            for col, col_type in schema_def.items():
                if col not in resolved:
                    await conn.execute(
                        f"ALTER TABLE {qualified_table} ADD COLUMN IF NOT EXISTS "
                        f"{quote_identifier(col)} {col_type}"
                    )
                    logger.info(f"Column '{col}' added to {qualified_table}")
                    continue

                db_col = resolved[col]
                if self._normalize_db_type(existing_types[db_col]) == self._normalize_db_type(col_type):
                    continue
                db_col_quoted = quote_identifier(db_col)
                await conn.execute(
                    f"ALTER TABLE {qualified_table} ALTER COLUMN {db_col_quoted} "
                    f"TYPE {col_type} USING {db_col_quoted}::{col_type}"
                )
                logger.info(f"Column '{col}' type changed from {existing_types[db_col]} to {col_type}")

            for col in existing_types:
                if col in resolved_db:
                    continue
                try:
                    await conn.execute(
                        f"ALTER TABLE {qualified_table} DROP COLUMN IF EXISTS {quote_identifier(col)}"
                    )
                    logger.warning(f"Column '{col}' dropped from {qualified_table}: not described in TOML")
                except asyncpg.exceptions.LockNotAvailableError:
                    logger.warning(
                        f"Column '{col}' NOT dropped from {qualified_table}: "
                        f"table is locked by another session"
                    )

    async def _count_rows(self) -> int:
        async with self.pool.acquire() as conn:
            return await conn.fetchval(f"SELECT COUNT(*) FROM {self.qualified_table}")

    async def load_df(self, chunk_size: int = 50_000) -> dict[str, Any]:
        """Загружает DataFrame в таблицу через COPY порциями по chunk_size строк."""
        await self.create_table()

        df_to_load = self.df.select(self.columns)
        total_rows = len(df_to_load)
        loaded = 0

        async with self.pool.acquire() as conn:
            for start in range(0, total_rows, chunk_size):
                chunk = df_to_load.slice(start, chunk_size)
                loaded += await self._copy_chunk(conn, chunk)
                logger.debug(f"Batch {start}-{start + len(chunk)} loaded")

        actual_count = await self._count_rows()
        logger.info(f"Loaded {loaded}/{total_rows}, actual rows in DB: {actual_count} --> [{self.qualified_table}]")
        return {"planned": total_rows, "loaded": loaded, "actual": actual_count}

    async def _copy_chunk(self, conn: asyncpg.Connection, chunk: pl.DataFrame) -> int:
        """Приводит порцию к типам схемы таблицы и заливает её через COPY."""
        if chunk.is_empty():
            return 0

        schema_def = self._infer_schema()
        for col in self.columns:
            target = self._db_type_to_pl(schema_def[col])
            if chunk[col].dtype != target:
                chunk = chunk.with_columns(pl.col(col).cast(target, strict=False))

        records = chunk.rows()
        await conn.copy_records_to_table(
            table_name=self.table_name,
            schema_name=self.schema,
            records=records,
            columns=self.columns,
        )
        logger.debug(f"COPY rows: {len(records)}")
        return len(records)

    async def create_indexes(self) -> None:
        index_state_sql = """
            SELECT i.indisvalid
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = $1 AND t.relname = $2 AND c.relname = $3
        """
        qualified_table = self.qualified_table

        async with self._ddl_connection() as conn:
            for idx in self.indexes:
                if not idx.use:
                    continue
                if not idx.name or not idx.columns:
                    logger.warning(f"Index skipped due to invalid config: {idx}")
                    continue

                state = await conn.fetchval(index_state_sql, self.schema, self.table_name, idx.name)
                if state is True:
                    logger.info(f"Index already exists: {idx.name}")
                    continue
                if state is False:
                    logger.warning(
                        f"Index '{idx.name}' on {qualified_table} is INVALID "
                        f"(aborted CONCURRENTLY build), dropping and recreating"
                    )
                    await conn.execute(
                        f"DROP INDEX CONCURRENTLY IF EXISTS "
                        f"{quote_identifier(self.schema)}.{quote_identifier(idx.name)}"
                    )

                cols_sql = ", ".join(quote_identifier(col) for col in idx.columns)
                unique_clause = "UNIQUE " if idx.unique else ""
                sql = (
                    f"CREATE {unique_clause}INDEX CONCURRENTLY "
                    f"{quote_identifier(idx.name)} ON {qualified_table} USING btree ({cols_sql})"
                )

                try:
                    await conn.execute(sql)
                except asyncpg.exceptions.DuplicateObjectError:
                    logger.warning(
                        f"Index '{idx.name}' NOT created: name already taken in schema "
                        f"'{self.schema}' by another table (index names must be unique)"
                    )
                except asyncpg.exceptions.LockNotAvailableError:
                    logger.warning(
                        f"Index '{idx.name}' NOT created: table is locked by "
                        f"another session (lock_timeout exceeded)"
                    )
                except Exception as exc:
                    logger.warning(f"Index '{idx.name}' failed: {exc}")
                else:
                    created = await conn.fetchval(index_state_sql, self.schema, self.table_name, idx.name)
                    if created is True:
                        logger.info(f"Index created: {idx.name}")
                    else:
                        logger.warning(f"Index '{idx.name}' execute succeeded but index is INVALID")

    async def delete_period(self, batch_size: int = 50_000, dry_run: bool = False) -> int:
        """Удаляет из таблицы строки за период [start_period, end_period] по колонке event_time."""
        if not self.event_time:
            logger.info("Parameter [event_time] is not set, skip period deletion")
            return 0

        if self.event_time not in self.columns:
            logger.warning(
                f"Column [event_time] = '{self.event_time}' is not among loaded columns, skip period deletion"
            )
            return 0

        validate_required_period_bounds(self.event_time, self.start_period, self.end_period)

        await self.create_table()

        qualified_table = self.qualified_table
        safe_col = quote_identifier(self.event_time)
        start_period = self.start_period
        end_period = self.end_period or start_period

        async with self.pool.acquire() as conn:
            col_type = await conn.fetchval(
                """
                SELECT data_type FROM information_schema.columns
                WHERE table_schema = $1 AND table_name = $2 AND column_name = $3
                """,
                self.schema,
                self.table_name,
                self.event_time,
            )

            if col_type and col_type.upper() in ("TEXT", "CHARACTER VARYING", "VARCHAR", "CHAR"):
                # Для TEXT-колонок используем TO_DATE, т.к. лексикографическое
                # сравнение dd.MM.yyyy некорректно (10.04.2026 > 03.07.2026)
                date_expr = f"TO_DATE(SPLIT_PART({safe_col}, ' ', 1), 'DD.MM.YYYY')"
                where_clause = f"{date_expr} >= $1::date AND {date_expr} <= $2::date"
            else:
                where_clause = f"{safe_col} >= $1 AND {safe_col} <= $2"

            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {qualified_table} WHERE {where_clause}",
                start_period,
                end_period,
            )
            logger.info(f"Rows to delete in period: {count} from [{start_period} <= and <= {end_period}]")

            if dry_run or count == 0:
                return count

            deleted = 0
            iteration = 0
            no_progress = 0

            while deleted < count and iteration < 1000:
                iteration += 1
                result = await conn.execute(
                    f"""
                    WITH del_rows AS (
                        SELECT ctid FROM {qualified_table}
                        WHERE {where_clause}
                        LIMIT $3
                    )
                    DELETE FROM {qualified_table} WHERE ctid IN (SELECT ctid FROM del_rows)
                    """,
                    start_period,
                    end_period,
                    batch_size,
                )

                try:
                    batch_deleted = int(result.split()[-1])
                except (ValueError, IndexError):
                    batch_deleted = 0

                deleted += batch_deleted
                logger.debug(f"Delete batch {iteration}: {batch_deleted}, total: {deleted}")

                if batch_deleted == 0:
                    no_progress += 1
                    if no_progress >= 3:
                        logger.warning("Delete stopped due to no progress")
                        break
                else:
                    no_progress = 0

            await conn.execute(f"VACUUM ANALYZE {qualified_table}")
            logger.info(f"Deleted rows: {deleted} of {count}")
            return deleted

    @staticmethod
    def _db_type_to_pl(db_type: str) -> pl.DataType:
        """Тип polars, в который приводятся данные перед COPY в колонку PostgreSQL."""
        t = db_type.upper()
        if t == "BIGINT":
            return pl.Int64
        if t == "DOUBLE PRECISION":
            return pl.Float64
        if t == "BOOLEAN":
            return pl.Boolean
        if t == "TIMESTAMP":
            return pl.Datetime
        if t == "DATE":
            return pl.Date
        return pl.Utf8

    @staticmethod
    def _normalize_db_type(data_type: str) -> str:
        """Приводит имя типа из information_schema к виду, используемому в схеме загрузчика."""
        t = data_type.upper()
        if "TIMESTAMP" in t:
            return "TIMESTAMP"
        if "DOUBLE PRECISION" in t or "NUMERIC" in t or "DECIMAL" in t or t in ("REAL", "DOUBLE"):
            return "DOUBLE PRECISION"
        if t in ("INTEGER", "INT", "SMALLINT", "BIGINT") or "SERIAL" in t:
            return "BIGINT"
        if "CHARACTER VARYING" in t or t in ("VARCHAR", "CHAR", "CHARACTER", "TEXT"):
            return "TEXT"
        return t
