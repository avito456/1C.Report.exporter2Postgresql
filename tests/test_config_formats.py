"""Тесты плоского формата TOML: разбор, load_config, автогенерация и save_config."""

import tomllib
import unittest
import uuid

import polars as pl

from app.settings import env
from app.settings.tables_settings import (
    Column,
    ReportConfig,
    infer_string_column_type,
    load_config,
    save_config,
)

FLAT_TOML = """use = true
table_name = "t1"
schema_name = "marts"
event_time = "d"
comment = "c"

[[columns]]
name = "A"
alias = "a"
type = "string"
comment = "ca"

[[indexes]]
use = false
name = "i"
columns = ["a"]
unique = false
"""


class ParseConfigTest(unittest.TestCase):
    def test_flat_format(self):
        cfg = ReportConfig.model_validate(tomllib.loads(FLAT_TOML))

        self.assertIsInstance(cfg, ReportConfig)
        self.assertTrue(cfg.use)
        self.assertEqual(cfg.table_name, "t1")
        self.assertEqual(cfg.columns[0].alias, "a")
        self.assertEqual(cfg.indexes[0].name, "i")


class LoadConfigFileTest(unittest.TestCase):
    def setUp(self):
        self.filename = f"__test_cfg_{uuid.uuid4().hex}.toml"
        self.path = env.get_project_root() / self.filename

    def tearDown(self):
        self.path.unlink(missing_ok=True)

    def test_load_flat_file(self):
        self.path.write_text(FLAT_TOML, encoding="utf-8")

        cfg = load_config(self.filename)

        self.assertIsNotNone(cfg)
        self.assertTrue(cfg.use)
        self.assertEqual(cfg.table_name, "t1")

    def test_missing_file_autogenerates_flat(self):
        df = pl.DataFrame({"Col One": ["1"], "Col Two": ["x"]})

        cfg = load_config(self.filename, df=df)

        self.assertIsNotNone(cfg)
        self.assertTrue(self.path.exists())
        text = self.path.read_text(encoding="utf-8")
        self.assertNotIn("reports_export_settings", text)
        reparsed = tomllib.loads(text)
        self.assertIn("columns", reparsed)


class FromDatasetTest(unittest.TestCase):
    def test_generates_flat_config(self):
        df = pl.DataFrame({"A": ["1"], "B": ["x"]})

        cfg = ReportConfig.from_dataset("rep", df)

        self.assertFalse(cfg.use)
        self.assertEqual(cfg.table_name, "rep_table")
        self.assertEqual([c.name for c in cfg.columns], ["A", "B"])

    def test_save_config_writes_flat_toml(self):
        cfg = ReportConfig(
            use=True,
            table_name="t1",
            columns=[Column(name="A", alias="a", type="string")],
        )
        path = env.get_project_root() / f"__test_cfg_{uuid.uuid4().hex}.toml"

        try:
            save_config(cfg, str(path))
            text = path.read_text(encoding="utf-8")
            self.assertNotIn("reports_export_settings", text)
            reparsed = tomllib.loads(text)
            self.assertEqual(reparsed["table_name"], "t1")
            self.assertEqual(reparsed["columns"][0]["alias"], "a")
        finally:
            path.unlink(missing_ok=True)


class InferStringColumnTypeTest(unittest.TestCase):
    def _infer(self, values):
        return infer_string_column_type(pl.Series(values, dtype=pl.String))

    def test_integers(self):
        self.assertEqual(self._infer(["1", "-15", "+7", "0", None]), "Int64")

    def test_integers_with_thousand_separators(self):
        self.assertEqual(self._infer(["1 234", "12\xa0345 678", "5"]), "Int64")

    def test_decimals_with_comma_or_dot(self):
        self.assertEqual(self._infer(["1,5", "2", "-0,25"]), "float64")
        self.assertEqual(self._infer(["1.5", "1 234,56"]), "float64")

    def test_codes_with_leading_zeros_stay_string(self):
        self.assertEqual(self._infer(["007", "123"]), "string")

    def test_mixed_and_text_stay_string(self):
        self.assertEqual(self._infer(["12", "abc"]), "string")
        self.assertEqual(self._infer(["01.02.2026", "03.04.2026"]), "string")
        self.assertEqual(self._infer(["", None]), "string")

    def test_too_big_integer_becomes_float(self):
        self.assertEqual(self._infer(["99999999999999999999"]), "float64")

    def test_from_dataset_uses_inferred_types(self):
        df = pl.DataFrame({"Кол": ["1", "2"], "Сумма": ["1 000,50", "2"], "Имя": ["a", "b"]})

        cfg = ReportConfig.from_dataset("rep", df)

        self.assertEqual([c.type for c in cfg.columns], ["Int64", "float64", "string"])


if __name__ == "__main__":
    unittest.main()
