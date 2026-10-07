import tomllib
import unittest
import uuid
from pathlib import Path

import polars as pl

from app.settings import env
from app.settings.tables_settings import (
    Column,
    ReportConfig,
    load_config,
    parse_config,
    save_config,
)
from scripts.migrate_toml import convert

LEGACY_TOML = '''[reports_export_settings."My Report"]
use = true
table_name = "t1"
schema_name = "marts"
event_time = "d"
comment = "c"

[[reports_export_settings."My Report".columns]]
name = "A"
alias = "a"
type = "string"
comment = "ca"

[[reports_export_settings."My Report".indexes]]
use = false
name = "i"
columns = ["a"]
unique = false
'''

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
        cfg = parse_config(tomllib.loads(FLAT_TOML), "My Report")

        self.assertIsInstance(cfg, ReportConfig)
        self.assertTrue(cfg.use)
        self.assertEqual(cfg.table_name, "t1")
        self.assertEqual(cfg.columns[0].alias, "a")
        self.assertEqual(cfg.indexes[0].name, "i")

    def test_legacy_format_is_unwrapped(self):
        cfg = parse_config(tomllib.loads(LEGACY_TOML), "My Report")

        self.assertIsInstance(cfg, ReportConfig)
        self.assertTrue(cfg.use)
        self.assertEqual(cfg.table_name, "t1")
        self.assertEqual(len(cfg.columns), 1)

    def test_legacy_section_stem_mismatch_falls_back_to_section(self):
        cfg = parse_config(tomllib.loads(LEGACY_TOML), "Other Name")

        self.assertIsNotNone(cfg)
        self.assertEqual(cfg.table_name, "t1")

    def test_legacy_without_sections_returns_none(self):
        cfg = parse_config({"reports_export_settings": {}}, "My Report")

        self.assertIsNone(cfg)


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

    def test_load_legacy_file(self):
        self.path.write_text(LEGACY_TOML, encoding="utf-8")

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


class MigrateConvertTest(unittest.TestCase):
    def test_legacy_to_flat_semantically_equal(self):
        new_text, warnings = convert(LEGACY_TOML, "My Report")

        self.assertEqual(warnings, [])
        self.assertNotIn("reports_export_settings", new_text)
        self.assertEqual(tomllib.loads(new_text), tomllib.loads(FLAT_TOML))

    def test_flat_file_is_untouched(self):
        new_text, warnings = convert(FLAT_TOML, "My Report")

        self.assertEqual(new_text, FLAT_TOML)
        self.assertEqual(warnings, [])

    def test_stem_mismatch_produces_warning(self):
        new_text, warnings = convert(LEGACY_TOML, "Other Name")

        self.assertTrue(warnings)
        self.assertEqual(tomllib.loads(new_text), tomllib.loads(FLAT_TOML))

    def test_conversion_is_idempotent(self):
        once, _ = convert(LEGACY_TOML, "My Report")
        twice, warnings = convert(once, "My Report")

        self.assertEqual(once, twice)
        self.assertEqual(warnings, [])

    def test_broken_syntax_raises(self):
        broken = LEGACY_TOML.replace("use = true", "use = true.", 1)

        with self.assertRaises(Exception):
            convert(broken, "My Report")


if __name__ == "__main__":
    unittest.main()
