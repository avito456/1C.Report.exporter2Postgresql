"""Тесты apply_schema_to_dataset: переименование, отбрасывание колонок, приведение типов."""

import unittest

import polars as pl

from app.settings.tables_settings import (
    Column,
    ReportConfig,
    apply_schema_to_dataset,
)


class ApplySchemaToDatasetTest(unittest.TestCase):
    def _make_config(self, columns):
        return ReportConfig(
            use=True,
            table_name="report01",
            columns=columns,
        )

    def test_drops_extra_columns_and_renames_aliases(self):
        df = pl.DataFrame(
            {
                "A": ["1", "2"],
                "B": ["x", "y"],
                "Extra": ["9", "9"],
            }
        )
        config = self._make_config(
            [
                Column(name="A", alias="a_col", type="Int64"),
                Column(name="B", alias="b_col", type="string"),
            ]
        )

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["a_col", "b_col"])
        self.assertEqual(result["a_col"].to_list(), [1, 2])
        self.assertEqual(result["b_col"].to_list(), ["x", "y"])

    def test_missing_configured_column_is_skipped(self):
        df = pl.DataFrame({"A": ["1"]})
        config = self._make_config(
            [
                Column(name="A", alias="a_col", type="Int64"),
                Column(name="Missing", alias="missing_col", type="string"),
            ]
        )

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["a_col"])

    def test_column_without_alias_keeps_name(self):
        df = pl.DataFrame({"A": ["1"]})
        config = self._make_config(
            [
                Column(name="A", type="string"),
            ]
        )

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["A"])

    def test_datetime_and_date_columns_are_parsed(self):
        df = pl.DataFrame(
            {
                "T": ["01.02.2026 10:30:00", "03.04.2026"],
                "D": ["01.02.2026 10:30:00", "03.04.2026"],
            }
        )
        config = self._make_config(
            [
                Column(name="T", type="datetime64[ns]"),
                Column(name="D", type="date"),
            ]
        )

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result["T"].dtype, pl.Datetime)
        self.assertEqual(result["T"].to_list()[0].hour, 10)
        self.assertEqual(result["D"].dtype, pl.Date)
        self.assertEqual(result["D"].to_list()[1].day, 3)


if __name__ == "__main__":
    unittest.main()
