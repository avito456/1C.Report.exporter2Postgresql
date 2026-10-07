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
        df = pl.DataFrame({
            "A": ["1", "2"],
            "B": ["x", "y"],
            "Extra": ["9", "9"],
        })
        config = self._make_config([
            Column(name="A", alias="a_col", type="Int64"),
            Column(name="B", alias="b_col", type="string"),
        ])

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["a_col", "b_col"])
        self.assertEqual(result["a_col"].to_list(), [1, 2])
        self.assertEqual(result["b_col"].to_list(), ["x", "y"])

    def test_missing_configured_column_is_skipped(self):
        df = pl.DataFrame({"A": ["1"]})
        config = self._make_config([
            Column(name="A", alias="a_col", type="Int64"),
            Column(name="Missing", alias="missing_col", type="string"),
        ])

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["a_col"])

    def test_column_without_alias_keeps_name(self):
        df = pl.DataFrame({"A": ["1"]})
        config = self._make_config([
            Column(name="A", type="string"),
        ])

        result = apply_schema_to_dataset(df, config)

        self.assertEqual(result.columns, ["A"])


if __name__ == "__main__":
    unittest.main()
