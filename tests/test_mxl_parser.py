from pathlib import Path

import pytest

from app.parsers.mxl_parser import MxlReportParser
from app.parsers.mxl_reader import MxlFormatError, MxlSheet
from app.parsers.report_loader import ReportHeaderParser

EXAMPLE = Path(__file__).resolve().parent.parent / "EXAMPLE"
REPORTS = ["СВЕРКА_DWH_ТоварыВПути", "СВЕРКА_DWH_ТоварыНаКомиссии"]


@pytest.mark.parametrize("report", REPORTS)
def test_mxl_equals_txt(report):
    mxl = EXAMPLE / f"{report} - выгрузка (MXL).mxl"
    txt = EXAMPLE / f"{report} - выгрузка (TXT).txt"
    if not (mxl.exists() and txt.exists()):
        pytest.skip("нет образцов в EXAMPLE")
    expected, expected_header = ReportHeaderParser.load_inventory_csv(txt)
    actual, header = MxlReportParser.load_mxl(mxl)
    assert header == expected_header
    assert actual.columns == expected.columns
    assert actual.equals(expected)


def _sheet(rows: list[list[str]]) -> MxlSheet:
    sheet = MxlSheet(row_count=len(rows), col_count=max(len(r) for r in rows))
    for r, row in enumerate(rows):
        for c, text in enumerate(row):
            if text:
                sheet.cells[(r, c)] = text
    return sheet


HEAD = [["Отчёт"], [], ["Параметры:", "Период: 01.03.2026 - 31.03.2026"], []]


def test_three_level_grouping():
    sheet = _sheet(
        [
            *HEAD,
            ["g1", "", "", "", "qty"],
            ["g2", "g2b", "", "", ""],
            ["d1", "d2", "d3", "d4", ""],
            ["A", "", "", "", "10"],
            ["A", "W1", "", "", "10"],
            ["x1", "x2", "x3", "x4", "10"],
            ["Итого", "", "", "", "10"],
        ]
    )
    df, header = MxlReportParser.parse_sheet(sheet)
    assert header == {"start_period": "01.03.2026", "end_period": "31.03.2026"}
    assert df.columns == ["g1", "g2", "g2b", "d1", "d2", "d3", "d4", "qty"]
    assert df.row(0) == ("A", "A", "W1", "x1", "x2", "x3", "x4", "10")


def test_flat_table_and_empty_cells():
    sheet = _sheet([*HEAD, ["a", "b", "c"], ["1", "", "3"], ["4", "5", "6"], ["Итого", "", "9"]])
    df, _ = MxlReportParser.parse_sheet(sheet)
    assert df.to_dict(as_series=False) == {"a": ["1", "4"], "b": [None, "5"], "c": ["3", "6"]}


def test_no_table_raises():
    with pytest.raises(ValueError):
        MxlReportParser.parse_sheet(_sheet([["Отчёт"], ["Параметры:", "x"]]))


def test_not_mxl_file(tmp_path):
    bad = tmp_path / "x.mxl"
    bad.write_text("a\tb\tc\n")
    with pytest.raises(MxlFormatError):
        MxlReportParser.load_mxl(bad)


def test_server_watches_mxl_and_txt():
    from app import server

    assert server.REPORT_EXTENSIONS == (".txt", ".mxl")
    assert "report.MXL".lower().endswith(server.REPORT_EXTENSIONS)
