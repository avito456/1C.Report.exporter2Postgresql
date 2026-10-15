from pathlib import Path

import pytest

from app.parsers.mxl_reader import MxlFormatError, parse_mxl, read_mxl

EXAMPLE = Path(__file__).resolve().parent.parent / "EXAMPLE"
REPORTS = ["СВЕРКА_DWH_ТоварыВПути", "СВЕРКА_DWH_ТоварыНаКомиссии"]


def _grid_lines(sheet) -> list[str]:
    lines = [
        "\t".join(sheet.cells.get((r, c), "") for c in range(sheet.col_count)).rstrip("\t")
        for r in range(sheet.row_count)
    ]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _txt_lines(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8-sig").replace("\r", "")
    lines = [line.rstrip("\t") for line in text.split("\n")]
    while lines and not lines[0]:
        lines.pop(0)
    while lines and not lines[-1]:
        lines.pop()
    return lines


@pytest.mark.parametrize("report", REPORTS)
def test_grid_matches_txt_export(report):
    mxl = EXAMPLE / f"{report} - выгрузка (MXL).mxl"
    txt = EXAMPLE / f"{report} - выгрузка (TXT).txt"
    if not (mxl.exists() and txt.exists()):
        pytest.skip("нет образцов в EXAMPLE")
    assert _grid_lines(read_mxl(mxl)) == _txt_lines(txt)


def test_merges_are_read():
    mxl = EXAMPLE / f"{REPORTS[0]} - выгрузка (MXL).mxl"
    if not mxl.exists():
        pytest.skip("нет образцов в EXAMPLE")
    merges = read_mxl(mxl).merges
    assert (6, 0, 6, 6) in merges  # stock_date на 7 колонок
    assert (6, 7, 7, 7) in merges  # stock_amount_way на 2 строки


def test_not_mxl_raises():
    with pytest.raises(MxlFormatError):
        parse_mxl(b"just text\tfile\n")
