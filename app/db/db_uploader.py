import asyncio
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, Optional

import asyncpg
import pendulum
import polars as pl
from loguru import logger

uuid_pattern = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)


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


def resolve_existing_columns(existing: set[str], expected: Iterable[str]) -> Dict[str, str]:
    """Сопоставляет ожидаемое полное имя колонки с фактическим именем в БД.

    Postgres хранит длинные идентификаторы (более 63 байт) под усечённым именем,
    поэтому колонка, добавленная ранее, возвращается из information_schema уже
    обрезанной. Функция строит маппинг: полное имя -> фактическое имя в БД
    (точное совпадение либо 63-байтовая обрезка), чтобы не терять длинные
    колонки и не циклически их добавлять/удалять.
    """
    mapping: Dict[str, str] = {}
    for name in expected:
        if name in existing:
            mapping[name] = name
            continue
        truncated = pg_truncate_name(name)
        if truncated != name and truncated in existing:
            mapping[name] = truncated
    return mapping


def is_period_event_time(event_time: Optional[str]) -> bool:
    return str(event_time or "").strip().lower() == "period"


def missing_required_period_bounds(
    event_time: Optional[str],
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
    event_time: Optional[str],
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
        db_params: Dict[str, Any],
        dataframe: pl.DataFrame,
        table_name: str,
        schema: str = "public",
        event_time: Optional[str] = None,
        start_period: Optional[datetime] = None,
        end_period: Optional[datetime] = None,
        indexes: Optional[list[IndexDef] | list[dict[str, Any]]] = None,
        comment: Optional[str] = None,
        column_comments: Optional[Dict[str, str]] = None,
    ):
        self.db_params = db_params
        self.table_name = table_name
        self.schema = schema
        self.event_time = event_time
        self.start_period = self.parse_ru_date(start_period)
        self.end_period = self.parse_ru_date(end_period)
        self.indexes = self._normalize_indexes(indexes)
        self.comment = comment
        self.column_comments = column_comments or {}
        self.pool: Optional[asyncpg.Pool] = None
        #---Время загрузки "date_download"
        self._df = dataframe.with_columns(
            pl.lit(pendulum.now("Europe/Moscow").replace(tzinfo=None))
            .cast(pl.Datetime)
            .alias("date_download")
        )
        self._columns: Optional[list[str]] = None
        self._schema_def: Optional[Dict[str, str]] = None
        logger.debug(f"Init uploader for {schema}.{table_name}")

    @staticmethod
    def _normalize_indexes(raw_indexes: Optional[list[Any]]) -> list[IndexDef]:
        if not raw_indexes:
            return []

        normalized: list[IndexDef] = []
        for idx in raw_indexes:
            if isinstance(idx, IndexDef):
                normalized.append(idx)
                continue

            if isinstance(idx, dict):
                name = str(idx.get("name") or "").strip()
                columns = [str(col) for col in idx.get("columns", []) if str(col).strip()]
                normalized.append(
                    IndexDef(
                        name=name,
                        columns=columns,
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
            exclude = {"id", "ID", "Id"}
            self._columns = [col for col in self.df.columns if col not in exclude]
        return self._columns

    async def ensure_pool(self) -> None:
        if self.pool is None:
            self.pool = await asyncpg.create_pool(
                **self.db_params,
                min_size=4,
                max_size=25,
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

    def _infer_schema(self) -> Dict[str, str]:
        if self._schema_def:
            return self._schema_def

        schema: Dict[str, str] = {}
        for col in self.columns:
            dtype = self.df[col].dtype

            if dtype in [pl.Int8, pl.Int16, pl.Int32, pl.Int64, pl.UInt8, pl.UInt16, pl.UInt32, pl.UInt64]:
                schema[col] = "BIGINT"
            elif dtype in [pl.Float32, pl.Float64]:
                schema[col] = "DOUBLE PRECISION"
            elif dtype == pl.Boolean:
                schema[col] = "BOOLEAN"
            elif dtype in [pl.Datetime, pl.Datetime("ms"), pl.Datetime("us"), pl.Datetime("ns")]:
                schema[col] = "TIMESTAMP"
            elif dtype == pl.Date:
                schema[col] = "DATE"
            elif dtype in [pl.Utf8, pl.String]:
                try:
                    sample = self.df[col].drop_nulls().head(10).to_list()
                    if sample:
                        uuid_count = sum(1 for value in sample if uuid_pattern.match(str(value)))
                        if uuid_count >= len(sample) * 0.8:
                            for value in sample:
                                uuid.UUID(str(value))
                            schema[col] = "UUID"
                            continue
                except Exception:
                    pass
                schema[col] = "TEXT"
            else:
                schema[col] = "TEXT"

        self._schema_def = schema
        return schema

    async def create_table(self) -> None:
        """Создаёт таблицу (если её нет) и добирает недостающие колонки.

        На повторной загрузке, когда схема уже совпадает, никакого DDL
        не выполняется вообще. Весь DDL ограничен lock_timeout, чтобы не
        зависнуть на AccessExclusiveLock, занятой другой сессией
        (например, открытой транзакцией в BI-клиенте).
        """
        await self.ensure_pool()
        schema_def = self._infer_schema()

        columns_sql = ", ".join(f"{quote_identifier(k)} {v}" for k, v in schema_def.items())
        qualified_table = f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"
        sql = f"""
        CREATE TABLE IF NOT EXISTS {qualified_table} (
            id BIGSERIAL PRIMARY KEY,
            {columns_sql}
        )"""

        async with self.pool.acquire() as conn:
            await conn.execute("SET lock_timeout = '5s'")
            try:
                await conn.execute(sql)

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
                        f"ADD COLUMN IF NOT EXISTS {quote_identifier(col)} {schema_def[col]}"
                        for col in missing
                    )
                    await conn.execute(f"ALTER TABLE {qualified_table} {add_sql}")
                    logger.info(f"Columns added to {qualified_table}: {missing}")

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
                        escaped_comment = self.comment.replace("'", "''")
                        await conn.execute(
                            f"COMMENT ON TABLE {qualified_table} IS '{escaped_comment}'"
                        )

                expected_column_comments = {
                    col_name: col_comment
                    for col_name, col_comment in self.column_comments.items()
                    if col_comment
                }
                if expected_column_comments:
                    current_col_comments = {
                        row["attname"]: row["comment"]
                        for row in await conn.fetch(
                            """
                            SELECT pa.attname,
                                   col_description(pc.oid, pa.attnum) AS comment
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
                    for col_name, col_comment in expected_column_comments.items():
                        if current_col_comments.get(col_name) == col_comment:
                            continue
                        escaped = col_comment.replace("'", "''")
                        col_quoted = quote_identifier(col_name)
                        await conn.execute(
                            f"COMMENT ON COLUMN {qualified_table}.{col_quoted} IS '{escaped}'"
                        )
            finally:
                try:
                    await conn.execute("RESET lock_timeout")
                except Exception:
                    pass

        logger.info(f"Table is ready: [{self.schema}.{self.table_name}] ({self.comment})")

    async def sync_schema(self) -> None:
        """Приводит таблицу к схеме текущего DataFrame.

        Добавляет недостающие колонки и удаляет лишние (не описанные в TOML).
        DDL выполняется с lock_timeout, чтобы повторная загрузка не зависала
        на блокировке таблицы (ACCESS EXCLUSIVE), занятой другой сессией.
        """
        await self.ensure_pool()
        schema_def = self._infer_schema()

        qualified_table = f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"

        async with self.pool.acquire() as conn:
            await conn.execute("SET lock_timeout = '5s'")
            try:
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
                resolved_db = set(resolved.values()) | {"id", "ID", "Id"}

                for col, col_type in schema_def.items():
                    if col not in resolved:
                        await conn.execute(
                            f"ALTER TABLE {qualified_table} ADD COLUMN IF NOT EXISTS "
                            f"{quote_identifier(col)} {col_type}"
                        )
                        logger.info(f"Column '{col}' added to {qualified_table}")
                        continue

                    db_col = resolved[col]
                    actual_type = self._normalize_db_type(existing_types[db_col])
                    if actual_type == self._normalize_db_type(col_type):
                        continue
                    db_col_quoted = quote_identifier(db_col)
                    cast_expr = self._pg_cast_expr(db_col_quoted, col_type)
                    await conn.execute(
                        f"ALTER TABLE {qualified_table} ALTER COLUMN {db_col_quoted} "
                        f"TYPE {col_type} USING {cast_expr}"
                    )
                    logger.info(
                        f"Column '{col}' type changed from {existing_types[db_col]} to {col_type}"
                    )

                for col in existing_types:
                    if col in resolved_db:
                        continue
                    try:
                        await conn.execute(
                            f"ALTER TABLE {qualified_table} DROP COLUMN IF EXISTS "
                            f"{quote_identifier(col)}"
                        )
                        logger.warning(
                            f"Column '{col}' dropped from {qualified_table}: not described in TOML"
                        )
                    except asyncpg.exceptions.LockNotAvailableError:
                        logger.warning(
                            f"Column '{col}' NOT dropped from {qualified_table}: "
                            f"table is locked by another session"
                        )
            finally:
                try:
                    await conn.execute("RESET lock_timeout")
                except Exception:
                    pass

    async def load_data(self, chunk_size: int = 100_000, max_concurrency: int = 10) -> Dict[str, Any]:
        await self.ensure_pool()
        await self.create_table()

        total_rows = len(self.df)
        loaded = 0
        semaphore = asyncio.Semaphore(max_concurrency)

        async def process_chunk(start: int) -> int:
            async with semaphore:
                end = min(start + chunk_size, total_rows)
                chunk = self.df.slice(start, end - start)
                return await self._fast_copy(chunk)

        tasks = [process_chunk(i) for i in range(0, total_rows, chunk_size)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        for result in results:
            if isinstance(result, Exception):
                logger.error(f"Chunk failed: {result}")
            else:
                loaded += result

        async with self.pool.acquire() as conn:
            actual_count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"
            )

        logger.info(f"Loaded {loaded}/{total_rows}, actual rows in DB: {actual_count}")
        return {"planned": total_rows, "loaded": loaded, "actual": actual_count}

    async def _fast_copy(self, chunk: pl.DataFrame) -> int:
        if chunk.is_empty():
            return 0

        load_columns = [col for col in chunk.columns if col not in {"id", "ID", "Id"}]
        records = chunk.select(load_columns).rows()

        async with self.pool.acquire() as conn:
            await conn.copy_records_to_table(
                table_name=self.table_name,
                schema_name=self.schema,
                records=records,
                columns=load_columns,
            )
            logger.debug(f"COPY inserted rows: {len(records)}")
            return len(records)

    async def create_indexes(self) -> None:
        await self.ensure_pool()

        index_state_sql = """
            SELECT i.indisvalid
            FROM pg_index i
            JOIN pg_class c ON c.oid = i.indexrelid
            JOIN pg_class t ON t.oid = i.indrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = $1 AND t.relname = $2 AND c.relname = $3
        """

        async with self.pool.acquire() as conn:
            qualified_table = f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"
            # CREATE INDEX CONCURRENTLY ждёт завершения чужих транзакций/снятия
            # блокировок. Без lock_timeout он может зависнуть навсегда, если
            # таблица залочена другой сессией (например, открытой транзакцией BI).
            await conn.execute("SET lock_timeout = '5s'")
            try:
                for idx in self.indexes:
                    if not idx.use:
                        continue
                    if not idx.name or not idx.columns:
                        logger.warning(f"Index skipped due to invalid config: {idx}")
                        continue

                    state = await conn.fetchval(
                        index_state_sql,
                        self.schema,
                        self.table_name,
                        idx.name,
                    )

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
                        created = await conn.fetchval(
                            index_state_sql,
                            self.schema,
                            self.table_name,
                            idx.name,
                        )
                        if created is True:
                            logger.info(f"Index created: {idx.name}")
                        else:
                            logger.warning(
                                f"Index '{idx.name}' execute succeeded but index is INVALID"
                            )
            finally:
                try:
                    await conn.execute("RESET lock_timeout")
                except Exception:
                    pass

    async def delete_period(self, batch_size: int = 50_000, dry_run: bool = False) -> int:
        if not self.event_time:
            logger.info("Parameter [event_time] is not set, skip period deletion")
            return 0

        if self.event_time not in self.columns:
            logger.warning(
                f"Column [event_time] = '{self.event_time}' is not among loaded columns, "
                f"skip period deletion"
            )
            return 0

        validate_required_period_bounds(self.event_time, self.start_period, self.end_period)

        await self.create_table()
        await self.ensure_pool()

        qualified_table = f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"

        safe_col = quote_identifier(self.event_time)

        start_period = self.start_period.replace(tzinfo=None) if self.start_period and self.start_period.tzinfo else self.start_period
        end_period = self.end_period.replace(tzinfo=None) if self.end_period and self.end_period.tzinfo else self.end_period
        if not end_period and start_period:
            end_period = start_period

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
                where_clause = (
                    f"TO_DATE(SPLIT_PART({safe_col}, ' ', 1), 'DD.MM.YYYY') >= $1::date "
                    f"AND TO_DATE(SPLIT_PART({safe_col}, ' ', 1), 'DD.MM.YYYY') <= $2::date"
                )
                start_param = start_period if start_period else None
                end_param = end_period if end_period else None
            else:
                where_clause = f"{safe_col} >= $1 AND {safe_col} <= $2"
                start_param = start_period
                end_param = end_period

            count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {qualified_table} WHERE {where_clause}",
                start_param,
                end_param,
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
                    start_param,
                    end_param,
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
    def parse_ru_date(value: Any):
        if value is None or value == "":
            return None

        if isinstance(value, pendulum.DateTime):
            return value
        if isinstance(value, datetime):
            return pendulum.instance(value)

        date_str = str(value).strip()
        formats = [
            "DD.MM.YYYY HH:mm:ss",
            "DD.MM.YYYY",
            "DD.MM.YY HH:mm:ss",
            "YYYY-MM-DD HH:mm:ss",
            "YYYY-MM-DD",
        ]

        for fmt in formats:
            try:
                return pendulum.from_format(date_str, fmt)
            except ValueError:
                continue

        try:
            return pendulum.parse(date_str, day_first=True)
        except ValueError:
            return None

    @staticmethod
    def _db_type_to_pl(db_type: str) -> pl.DataType:
        t = db_type.upper()
        if t in ("BIGINT", "INTEGER", "SMALLINT"):
            return pl.Int64
        if t in ("DOUBLE PRECISION", "REAL", "NUMERIC", "DECIMAL"):
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
        t = data_type.upper()
        if "TIMESTAMP" in t:
            return "TIMESTAMP"
        if "DOUBLE PRECISION" in t or t in ("REAL", "DOUBLE"):
            return "DOUBLE PRECISION"
        if t in ("INTEGER", "INT", "SMALLINT", "BIGINT") or "SERIAL" in t:
            return "BIGINT"
        if "CHARACTER VARYING" in t or t in ("VARCHAR", "CHAR", "CHARACTER", "TEXT"):
            return "TEXT"
        if "NUMERIC" in t or "DECIMAL" in t:
            return "DOUBLE PRECISION"
        return t

    @staticmethod
    def _pg_cast_expr(col_quoted: str, db_type: str) -> str:
        if db_type == "TEXT":
            return f"{col_quoted}::text"
        if db_type == "BOOLEAN":
            return f"{col_quoted}::boolean"
        if db_type == "DATE":
            return f"{col_quoted}::date"
        if db_type == "TIMESTAMP":
            return f"{col_quoted}::timestamp"
        if db_type == "DOUBLE PRECISION":
            return f"{col_quoted}::double precision"
        if db_type == "BIGINT":
            return f"{col_quoted}::bigint"
        if db_type == "UUID":
            return f"{col_quoted}::uuid"
        return f"{col_quoted}::text"

    async def _insert_batch(self, chunk: pl.DataFrame) -> int:
        if chunk.is_empty():
            return 0

        chunk_clean = chunk.select(self.columns)

        schema_def = self._infer_schema()
        for col in self.columns:
            target_pl = self._db_type_to_pl(schema_def[col])
            if chunk_clean[col].dtype != target_pl:
                chunk_clean = chunk_clean.with_columns(
                    pl.col(col).cast(target_pl, strict=False)
                )

        records = chunk_clean.rows()

        placeholders = ", ".join(f"${i + 1}" for i in range(len(self.columns)))
        columns_sql = ", ".join(quote_identifier(col) for col in self.columns)
        qualified_table = f"{quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"
        sql = f"INSERT INTO {qualified_table} ({columns_sql}) VALUES ({placeholders})"

        async with self.pool.acquire() as conn:
            await conn.executemany(sql, records)

        logger.debug(f"INSERT rows: {len(records)}")
        return len(records)

    async def load_df(self, chunk_size: int = 50_000) -> Dict[str, Any]:
        await self.ensure_pool()
        await self.create_table()

        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = $1 AND table_name = $2",
                self.schema,
                self.table_name,
            )
            if count == 0:
                raise RuntimeError(f"Table {self.schema}.{self.table_name} was not created")

        df_to_load = self.df.select(self.columns)
        total_rows = len(df_to_load)
        loaded = 0

        for start in range(0, total_rows, chunk_size):
            end = min(start + chunk_size, total_rows)
            chunk = df_to_load.slice(start, end - start)
            loaded += await self._insert_batch(chunk)
            logger.debug(f"Batch {start}-{end} loaded")

        async with self.pool.acquire() as conn:
            actual_count = await conn.fetchval(
                f"SELECT COUNT(*) FROM {quote_identifier(self.schema)}.{quote_identifier(self.table_name)}"
            )

        logger.info(f"Loaded {loaded}/{total_rows}, actual rows in DB: {actual_count}")
        return {"planned": total_rows, "loaded": loaded, "actual": actual_count}
