"""Разбор MXL-выгрузки отчёта 1С в polars.DataFrame.

Аналог ``ReportHeaderParser`` для формата MXL: сетка ячеек из ``mxl_reader``
делится на шапку отчёта (период) и таблицу; ступенчатая N-уровневая группировка
разворачивается в плоскую таблицу. Результат совпадает с разбором TXT-выгрузки того же
отчёта: ``(DataFrame из строковых колонок, {"start_period", "end_period"?})``.
"""

import time
from pathlib import Path
from typing import Any

import polars as pl
from loguru import logger

from app.parsers.mxl_reader import MxlSheet, parse_mxl
from app.parsers.report_loader import ReportHeaderParser

Grid = list[list[str]]


def _leading_filled(cells: list[str], limit: int) -> int:
    """Число ведущих непустых ячеек (в пределах limit)."""
    count = 0
    while count < limit and cells[count].strip():
        count += 1
    return count


def _build_grid(sheet: MxlSheet) -> Grid:
    """Плотная сетка строк листа; ведущие и замыкающие пустые строки отбрасываются."""
    grid: Grid = [[""] * sheet.col_count for _ in range(sheet.row_count)]
    for (row, col), text in sheet.cells.items():
        grid[row][col] = text
    start, end = 0, len(grid)
    while start < end and not any(c.strip() for c in grid[start]):
        start += 1
    while end > start and not any(c.strip() for c in grid[end - 1]):
        end -= 1
    return grid[start:end]


def _last_filled(row: list[str]) -> int:
    """Индекс последней непустой ячейки строки (-1, если строка пуста)."""
    for j in range(len(row) - 1, -1, -1):
        if row[j].strip():
            return j
    return -1


def _split_header_and_table(grid: Grid) -> tuple[Grid, Grid]:
    """Таблица начинается с первой строки, где заполнена ячейка в колонке №3 или правее.

    Это аналог HEADER_PATTERN/BODY_PATTERN (строка не менее чем с двумя табуляциями):
    название отчёта, «Параметры:» и «Отбор:» занимают не более двух колонок.
    """
    for index, row in enumerate(grid):
        if _last_filled(row) >= 2:
            return grid[:index], grid[index:]
    raise ValueError("❌ Не найдено тело таблицы в MXL")


def _header_text(header: Grid) -> str:
    """Шапка в виде, привычном для TXT-выгрузки (ячейки через табуляцию)."""
    return "\n".join("\t".join(row).rstrip("\t") for row in header)


class MxlReportParser:
    """Разбор MXL-выгрузки; публичная точка входа — :meth:`load_mxl`."""

    @classmethod
    def load_mxl(cls, file_path: str | Path) -> tuple[pl.DataFrame, dict[str, Any]]:
        file_path = Path(file_path)
        logger.info(f"📂 Загрузка и парсинг MXL: {file_path.name}")
        sheet = parse_mxl(cls._read_file_with_retries(file_path))
        df, header_info = cls.parse_sheet(sheet)
        logger.info(f"✅ DataFrame: {len(df)}×{len(df.columns)}")
        logger.info(
            f"✅ Период: {header_info.get('start_period', 'N/A')} → {header_info.get('end_period', 'N/A')}"
        )
        return df, header_info

    @classmethod
    def parse_sheet(cls, sheet: MxlSheet) -> tuple[pl.DataFrame, dict[str, Any]]:
        header, table = _split_header_and_table(_build_grid(sheet))
        header_info = ReportHeaderParser.extract_period_from_header(_header_text(header))
        return cls.table_to_dataframe(table, sheet.merges, len(header)), header_info

    @classmethod
    def table_to_dataframe(
        cls,
        table: Grid,
        merges: list[tuple[int, int, int, int]] | None = None,
        table_offset: int = 0,
    ) -> pl.DataFrame:
        """Строки таблицы (с шапкой) -> DataFrame; ``table_offset`` — число строк шапки отчёта."""
        rows = [
            row
            for row in table
            if any(c.strip() for c in row) and not row[0].strip().lower().startswith("итого")
        ]
        if len(rows) < 2:
            raise ValueError(f"❌ Недостаточно данных в теле отчета: {len(rows)} строк")
        width = max(_last_filled(row) for row in rows[:4]) + 1

        grouped = cls._flatten_grouped(rows, width, merges or [], table_offset)
        if grouped is not None:
            logger.info(f"✅ Выгрузка с группировкой разложена в плоскую таблицу: {grouped.shape}")
            return grouped
        return cls._flat_table(rows, width)

    @staticmethod
    def _flat_table(rows: Grid, width: int) -> pl.DataFrame:
        """Плоская таблица: первая строка — заголовки, остальные — данные."""
        columns = [c.strip() for c in rows[0][:width]]
        body = [[(c or None) for c in row[:width]] for row in rows[1:]]
        return pl.DataFrame(
            {name: [r[i] for r in body] for i, name in enumerate(columns)},
            schema={name: pl.Utf8 for name in columns},
        )

    @staticmethod
    def _header_height(
        measure_start: int,
        width: int,
        merges: list[tuple[int, int, int, int]],
        table_offset: int,
    ) -> int | None:
        """Высота шапки таблицы по вертикальным объединениям колонок мер (None — нет данных)."""
        heights = {
            r2 - r1 + 1
            for r1, c1, r2, c2 in merges
            if r1 == table_offset + 0 and r2 > r1 and measure_start <= c1 <= c2 < width
        }
        # Данные согласованы, только если у всех мер одинаковая высота.
        return heights.pop() if len(heights) == 1 else None

    @classmethod
    def _flatten_grouped(
        cls,
        rows: Grid,
        width: int,
        merges: list[tuple[int, int, int, int]],
        table_offset: int,
    ) -> pl.DataFrame | None:
        """Раскладывает ступенчатую N-уровневую группировку в плоскую таблицу.

        Правила те же, что у ``ReportHeaderParser._flatten_grouped``: шапка — «лестница»
        из 2..N строк, правый блок колонок — меры (заполнены только в верхней строке шапки);
        каждый уровень занимает ведущие колонки зоны измерений, подытог уровня задаёт
        значения группы в стеке и сам отбрасывается, детальная строка получает значения
        всех уровней. Высота шапки берётся из вертикальных объединений мер, а если их нет —
        определяется по первой строке со значением в зоне мер.
        """
        cells = [[c.strip() for c in row[:width]] + [""] * (width - len(row)) for row in rows]
        first, second = cells[0], cells[1]

        measure_start = next((j for j in range(1, width) if first[j] and not second[j]), None)
        if measure_start is None or not all(first[measure_start:]):
            return None
        dim_count = measure_start

        height = cls._header_height(measure_start, width, merges, table_offset)
        if height is None or not 2 <= height < len(rows):
            height = 2
            while height < len(rows) and not any(cells[height][measure_start:]):
                height += 1
        header = cells[:height]

        spans = [_leading_filled(row, dim_count) for row in header[:-1]]
        detail_cols = [j for j in range(dim_count) if header[-1][j]]
        grouped = (
            len(spans) >= 1
            and all(span > 0 for span in spans)
            and all(a < b for a, b in zip(spans, spans[1:], strict=False))
            and spans[-1] < dim_count
            and len(detail_cols) > spans[-1]
        )
        if not grouped:
            return None

        detail_only = [j for j in detail_cols if j >= spans[-1]] or detail_cols
        level_cols = [range(span) for span in spans]
        columns = (
            [header[r][j] for r in range(len(spans)) for j in level_cols[r]]
            + [header[-1][j] for j in detail_cols]
            + first[measure_start:]
        )

        stack: list[list[str | None]] = [[None] * span for span in spans]
        out: list[list[str | None]] = []
        for row in rows[height:]:
            row = row[:width] + [""] * (width - len(row))
            dim_cells = row[:dim_count]
            if any(dim_cells[j].strip() for j in detail_only):
                flat = [value for level in stack for value in level]
                out.append([v or None for v in [*flat, *dim_cells, *row[dim_count:]]])
                continue
            filled = _leading_filled(dim_cells, dim_count)
            if filled in spans:
                r = spans.index(filled)
                stack[r] = [dim_cells[j].strip() for j in level_cols[r]]
                for deeper in range(r + 1, len(stack)):
                    stack[deeper] = [None] * spans[deeper]

        if not out:
            raise ValueError("❌ В выгрузке с группировкой нет детальных строк")
        return pl.DataFrame(
            {name: [row[i] for row in out] for i, name in enumerate(columns)},
            schema={name: pl.Utf8 for name in columns},
        )

    @staticmethod
    def _read_file_with_retries(file_path: Path, retries: int = 10, delay: float = 1.0) -> bytes:
        """Читает файл, повторяя попытку, пока писатель держит его залоченным."""
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                return file_path.read_bytes()
            except (PermissionError, OSError) as exc:
                last_error = exc
                logger.warning(
                    f"⚠️ Файл занят писателем ({file_path.name}), "
                    f"повтор {attempt}/{retries} через {delay}s: {exc}"
                )
                if attempt < retries:
                    time.sleep(delay)
        raise last_error if last_error else RuntimeError(f"Не удалось прочитать файл: {file_path}")
