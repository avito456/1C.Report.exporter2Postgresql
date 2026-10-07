"""Тесты вывода схемы таблицы из DataFrame и идемпотентности create_table (без БД)."""

import asyncio
import unittest
from datetime import datetime

import polars as pl

from app.db.db_uploader import AsyncDatasetToPostgres


def make_uploader(df: pl.DataFrame) -> AsyncDatasetToPostgres:
    return AsyncDatasetToPostgres(db_params={}, dataframe=df, table_name="t", schema="s")


class InferSchemaTest(unittest.TestCase):
    def test_maps_polars_dtypes_to_postgres(self):
        df = pl.DataFrame(
            {
                "i": [1, 2],
                "f": [1.5, 2.5],
                "b": [True, False],
                "d": [datetime(2026, 1, 1), datetime(2026, 1, 2)],
                "t": ["a", "b"],
                "u": ["123e4567-e89b-12d3-a456-426614174000"] * 2,
                "mixed": ["123e4567-e89b-12d3-a456-426614174000", "not-uuid"],
            }
        )

        schema = make_uploader(df)._infer_schema()

        self.assertEqual(schema["i"], "BIGINT")
        self.assertEqual(schema["f"], "DOUBLE PRECISION")
        self.assertEqual(schema["b"], "BOOLEAN")
        self.assertEqual(schema["d"], "TIMESTAMP")
        self.assertEqual(schema["t"], "TEXT")
        self.assertEqual(schema["u"], "UUID")
        self.assertEqual(schema["mixed"], "TEXT")
        self.assertEqual(schema["date_download"], "TIMESTAMP")

    def test_id_column_is_excluded(self):
        uploader = make_uploader(pl.DataFrame({"id": [1], "a": ["x"]}))

        self.assertNotIn("id", uploader.columns)


class CreateTableOnceTest(unittest.TestCase):
    def test_create_table_is_noop_when_ready(self):
        uploader = make_uploader(pl.DataFrame({"a": ["x"]}))
        uploader._table_ready = True

        # Без БД: если бы DDL выполнялся, упала бы попытка создать пул соединений.
        asyncio.run(uploader.create_table())


if __name__ == "__main__":
    unittest.main()
