"""Тесты плоского формата TOML: разбор, load_config, автогенерация и save_config."""

import tomllib
import unittest
import uuid

import polars as pl

from app.settings import env
from app.settings.tables_settings import (
    Column,
    ReportConfig,
    load_config,
    save_config,
)

FLAT_TOML = '''use = true
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
'''


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


if __name__ == "__main__":
    unittest.main()
