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


# Трёхуровневая ступенчатая группировка (3-строчная шапка): дата → склад → номенклатура.
GROUPED_3LEVEL = (
    "Параметры:\tНа дату: 01.09.2026 0:00:00\r\n"
    "\r\n"
    "stock_date\t\t\t\t\tstock_amount_way\tstock_cost_way_rub\r\n"
    "wh_name\twh_guid\t\t\t\t\t\r\n"
    "item_name\titem_guid\tf(cost)\titem_type\tprice\t\t\r\n"
    "01.09.2026\t\t\t\t\t10\t100\r\n"
    "Склад A\tguid-a\t\t\t\t6\t60\r\n"
    "Товар 1\titem-1\tСТМ\tСТМ\t100,00\t2\t20\r\n"
    "Товар 2\titem-2\tСТМ\tСТМ\t100,00\t4\t40\r\n"
    "Склад B\tguid-b\t\t\t\t4\t40\r\n"
    "Товар 3\titem-3\tСТМ\tСТМ\t100,00\t4\t40\r\n"
    "02.09.2026\t\t\t\t\t5\t50\r\n"
    "Склад A\tguid-a\t\t\t\t5\t50\r\n"
    "Товар 1\titem-1\tСТМ\tСТМ\t100,00\t5\t50\r\n"
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


def test_three_level_grouped_is_flattened(tmp_path):
    df, _ = _load(tmp_path, GROUPED_3LEVEL)
    assert df.columns == [
        "stock_date",
        "wh_name",
        "wh_guid",
        "item_name",
        "item_guid",
        "f(cost)",
        "item_type",
        "price",
        "stock_amount_way",
        "stock_cost_way_rub",
    ]
    assert df.height == 4  # 3 строки шапки и подытоги (дата, склад) отброшены
    assert df["stock_date"].to_list() == ["01.09.2026"] * 3 + ["02.09.2026"]
    assert df["wh_name"].to_list() == ["Склад A", "Склад A", "Склад B", "Склад A"]
    assert df["wh_guid"].to_list() == ["guid-a", "guid-a", "guid-b", "guid-a"]
    assert df["item_name"].to_list() == ["Товар 1", "Товар 2", "Товар 3", "Товар 1"]
    assert df["stock_amount_way"].to_list() == ["2", "4", "4", "5"]
    assert df["stock_cost_way_rub"].to_list() == ["20", "40", "40", "50"]
    assert df["stock_date"].null_count() == 0
