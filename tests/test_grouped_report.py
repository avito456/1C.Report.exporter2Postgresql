"""Выгрузка 1С с группировкой разворачивается в плоскую таблицу; плоская — без изменений."""

from pathlib import Path

from app.parsers.report_loader import ReportHeaderParser

GROUPED = (
    "﻿\r\nПараметры:\tСП: 29.09.2026 - 30.09.2026\r\n\r\n"
    "Период, день\t\tКоличество Начальный остаток\tКоличество Приход\r\n"
    "Склад получатель\tНоменклатура\t\t\r\n"
    "29.09.2026 0:00:00\t\t5,00\t3,00\r\n"
    " 01 (ОБЩАК)  Ч\tБейсболка A\t2,00\t1,00\r\n"
    " 01 (ОБЩАК)  Ч\tБейсболка B\t3 000,00\t\r\n"
    "30.09.2026 0:00:00\t\t7,00\t3,00\r\n"
    " 02 (ТЕСТ)\tБейсболка A\t\t4,00\r\n"
)

FLAT = (
    "Параметры:\tПериод: 01.09.2026 - 30.09.2026\r\n\r\n"
    "Дата\tТовар\tКоличество\r\n"
    "01.09.2026\tA\t1,00\r\n"
    "02.09.2026\tB\t2,00\r\n"
    "Итого\t\t3,00\r\n"
)


def _load(tmp_path: Path, text: str):
    p = tmp_path / "r.txt"
    p.write_bytes(text.encode("utf-8"))
    return ReportHeaderParser.load_inventory_csv(p)


def test_grouped_is_flattened(tmp_path):
    df, header = _load(tmp_path, GROUPED)
    assert df.columns == [
        "Период, день",
        "Склад получатель",
        "Номенклатура",
        "Количество Начальный остаток",
        "Количество Приход",
    ]
    assert df.height == 3  # подытоги групп и вторая строка шапки отброшены
    assert df["Период, день"].to_list() == ["29.09.2026 0:00:00"] * 2 + ["30.09.2026 0:00:00"]
    assert df["Номенклатура"].to_list() == ["Бейсболка A", "Бейсболка B", "Бейсболка A"]
    assert df["Количество Приход"].to_list() == ["1,00", None, "4,00"]
    assert header["start_period"] == "29.09.2026"


def test_flat_is_unchanged(tmp_path):
    df, _ = _load(tmp_path, FLAT)
    assert df.columns == ["Дата", "Товар", "Количество"]
    assert df.height == 2
