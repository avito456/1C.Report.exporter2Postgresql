"""Разбор TXT-выгрузки отчёта 1С в polars.DataFrame.

ReportHeaderParser делит файл на шапку и тело таблицы (табуляция-разделитель),
извлекает из шапки период (`start_period` / `end_period`) тремя шаблонами
и читает тело в DataFrame (все колонки — строки, строки «Итого» отбрасываются).
Чтение файла повторяется, пока его не освободит писатель (сетевая шара).
"""

import io
import re
import time
from pathlib import Path
from typing import Any

import polars as pl
from loguru import logger


class ReportHeaderParser:
    # Получает шапку из текстового содержания отчета (многострочный текст)
    # Захватывает все до первой строки с минимум 2 табуляциями
    HEADER_PATTERN = re.compile(r"^(.*?)(?=^[^\n]*\t[^\n]*\t)", re.DOTALL | re.MULTILINE)

    # Получает строки таблицы из текстового содержания отчета (многострочный текст)
    # Захватывает все строки начиная с первой строки с минимум 2 табуляциями
    BODY_PATTERN = re.compile(r"(^[^\n]*\t[^\n]*\t.*)", re.DOTALL | re.MULTILINE)

    # Параметры: Период: 16.03.2026 - 22.03.2026 | 01.01.2026 0:00:00 - 22.04.2026 23:59:59
    PERIOD_RANGE_PATTERN_1 = re.compile(
        r"Параметры:[\s\S]*?:\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)\s*-\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)",
        re.IGNORECASE,
    )

    # Параметры:	Начало периода: 01.03.2022 0:00:00
    #     Конец периода: 20.03.2026 23:59:59
    #
    # Параметры:	Начало периода: 01.03.2022
    #    Конец периода: 20.03.2026
    PERIOD_RANGE_PATTERN_2 = re.compile(
        r":\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)\s*\n*.*:\s*(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)",
        re.IGNORECASE,
    )

    # Параметры:	На дату: 01.03.2026 0:00:00
    # Параметры:	На дату: 01.03.2026
    # Параметры:	Период: 01.03.2026 23:59:59
    # Параметры:	XXXXXX: 01.03.2026 23:59:59
    # Параметры:	XXXXXX: 01.03.2026
    PERIOD_PATTERN = re.compile(
        r"Параметры:.*?(\d{2}\.\d{2}\.\d{4}(?:\s+\d{1,2}:\d{2}:\d{2})?)", re.IGNORECASE
    )

    @classmethod
    def extract_period_from_header(cls, header_text: str) -> dict[str, str]:
        """
        Извлекает период или дату из шапки отчета, применяя паттерны по очереди.
        Значения возвращаются как в отчёте (с временем или только дата).

        Args:
            header_text: Текст шапки отчета

        Returns:
            Dict[str, str]: {'start_period': '...', 'end_period': '...'} или {'start_period': '...'}
        """
        result = {}

        # Паттерн 1:
        # Период: 16.03.2026 - 22.03.2026 | 16.03.2026 0:00:00 - 22.03.2026 0:00:00
        match = cls.PERIOD_RANGE_PATTERN_1.search(header_text)
        if match:
            result["start_period"] = match.group(1).strip()
            result["end_period"] = match.group(2).strip()
            logger.info(f"✅ Найден период (паттерн 1): {result['start_period']} - {result['end_period']}")
            return result

        # Паттерн 2
        # Период: Начало периода: 01.01.2026 0:00:00 | 01.01.2026
        #     Конец периода: 01.01.2026 0:00:00 | 01.01.2026
        match = cls.PERIOD_RANGE_PATTERN_2.search(header_text)
        if match:
            result["start_period"] = match.group(1).strip()
            result["end_period"] = match.group(2).strip()
            logger.debug(f"✅ Найден период (паттерн 2): {result['start_period']} - {result['end_period']}")
            return result

        # Паттерн 3:
        # На дату: ...
        match = cls.PERIOD_PATTERN.search(header_text)
        if match:
            result["start_period"] = match.group(1).strip()
            logger.info(f"✅ Найдена дата (паттерн 3): {result['start_period']}")
            return result

        logger.warning("⚠️ Период не найден ни одним паттерном")
        return result

    @classmethod
    def load_body_to_dataframe(cls, body_text: str) -> pl.DataFrame:
        """
        Загружает тело отчета в Polars DataFrame.
        Первая строка тела должна быть заголовками таблицы.

        Args:
            body_text: Текст тела отчета (табличные данные)

        Returns:
            pl.DataFrame: Загруженные данные
        """
        if not body_text.strip():
            raise ValueError("❌ Тело отчета пустое")

        lines = body_text.split("\n")

        # Фильтруем пустые строки и строки с "Итого" в первом поле
        data_lines = []
        for line in lines:
            line_stripped = line.strip()
            if not line_stripped:
                continue
            # Проверяем что есть хотя бы одна табуляция
            if "\t" not in line:
                continue
            # Пропускаем строки итогов (Итого в первом tab-поле)
            first_field = line.split("\t")[0].strip()
            if first_field.lower().startswith("итого"):
                continue
            data_lines.append(line)

        if len(data_lines) < 2:
            raise ValueError(f"❌ Недостаточно данных в теле отчета: {len(data_lines)} строк")

        grouped = cls._flatten_grouped(data_lines)
        if grouped is not None:
            logger.info(f"✅ Выгрузка с группировкой разложена в плоскую таблицу: {grouped.shape}")
            return grouped

        # Создаем буфер для чтения CSV
        csv_buffer = io.StringIO("\n".join(data_lines))

        # Загружаем в DataFrame с явным указанием что первая строка - заголовки
        df = pl.read_csv(
            csv_buffer,
            separator="\t",
            infer_schema_length=0,
            ignore_errors=True,
            has_header=True,  # Первая строка = заголовки
        )

        # Очищаем названия колонок (убираем пробелы по краям)
        df = df.rename({col: col.strip() for col in df.columns})

        logger.debug(f"✅ DataFrame загружен: {len(df)} строк × {len(df.columns)} колонок")
        return df

    @staticmethod
    def _split_cells(line: str, width: int) -> list[str]:
        cells = line.rstrip("\r\n").split("\t")
        return cells + [""] * (width - len(cells))

    @staticmethod
    def _leading_filled(cells: list[str], limit: int) -> int:
        """Число ведущих непустых ячеек (в пределах limit)."""
        count = 0
        while count < limit and cells[count].strip():
            count += 1
        return count

    @classmethod
    def _flatten_grouped(cls, data_lines: list[str]) -> pl.DataFrame | None:
        """Раскладывает ступенчатую N-уровневую группировку 1С в плоскую таблицу.

        Шапка сгруппированного отчёта — многострочная «лестница» (2..4 строки); правый
        блок ячеек — меры, заполненные только в верхней строке шапки:

            stock_date                              stock_amount_way  stock_cost_way_rub   (h1)
            wh_name   wh_guid                                                             (h2)
            item_name item_guid f(cost) item_type price                                   (h3)

        В зоне измерений каждый уровень занимает ведущие колонки, столбец 0 — общий
        «якорь»: у подытога в нём значение группы, у детальной строки — первое детальное
        измерение. Подытог закрывает уровень длиной в число заполненных ведущих ячеек,
        его значения запоминаются в стеке и проставляются в последующие детальные строки;
        сама строка отбрасывается. None — файл не является ступенчато-сгруппированным.
        """
        if len(data_lines) < 2:
            return None
        width = max(len(line.split("\t")) for line in data_lines[:4])

        def _cells(index: int) -> list[str]:
            return [c.strip() for c in cls._split_cells(data_lines[index], width)]

        first = _cells(0)
        second = _cells(1)

        # Зона мер: первый столбец, где верхняя строка шапки заполнена, а вторая — пуста.
        measure_start = next((j for j in range(1, width) if first[j] and not second[j]), None)
        if measure_start is None or not all(first[measure_start:]):
            return None
        dim_count = measure_start

        # Длина шапки: до первой строки, у которой заполнена хоть одна ячейка мер.
        header = [first, second]
        row_index = 2
        while row_index < len(data_lines):
            row = _cells(row_index)
            if any(row[j] for j in range(measure_start, width)):
                break
            header.append(row)
            row_index += 1

        # Уровни группировки — все строки шапки, кроме нижней (детальной).
        spans = [cls._leading_filled(row, dim_count) for row in header[:-1]]
        detail_cols = [j for j in range(dim_count) if header[-1][j]]
        grouped = (
            len(spans) >= 1
            and all(span > 0 for span in spans)
            and all(a < b for a, b in zip(spans, spans[1:], strict=False))  # строго возрастают
            and spans[-1] < dim_count  # над детальными измерениями есть групповой уровень
            and len(detail_cols) > spans[-1]
        )
        if not grouped:
            return None

        detail_only = [j for j in detail_cols if j >= spans[-1]]
        if not detail_only:
            # fallback: use all detail_cols if degenerate
            detail_only = detail_cols

        level_cols = [range(span) for span in spans]
        columns = (
            [header[r][j] for r in range(len(spans)) for j in level_cols[r]]
            + [header[-1][j] for j in detail_cols]
            + first[measure_start:]
        )

        stack: list[list[str | None]] = [[None] * span for span in spans]
        rows: list[list[str | None]] = []

        meas_count_total = len(first[measure_start:]) if measure_start < len(first) else 0
        for line in data_lines[row_index:]:
            cells = cls._split_cells(line, width)
            if len(cells) != width:
                continue  # пропускаем некорректные строки с лишними/недостающими табами
            dim_cells = cells[:dim_count]
            if any(dim_cells[j].strip() for j in detail_only):
                flat = [value for level in stack for value in level]
                rows.append([v or None for v in [*flat, *dim_cells, *cells[dim_count:]]])
                continue
            # Подытог уровня: ведущих заполненных ячеек ровно span уровня.
            filled = cls._leading_filled(dim_cells, dim_count)
            if filled in spans:
                r = spans.index(filled)
                stack[r] = [dim_cells[j].strip() for j in level_cols[r]]
                for deeper in range(r + 1, len(stack)):
                    stack[deeper] = [None] * spans[deeper]

        if not rows:
            raise ValueError("❌ В выгрузке с группировкой нет детальных строк")
        return pl.DataFrame(
            {name: [row[i] for row in rows] for i, name in enumerate(columns)},
            schema={name: pl.Utf8 for name in columns},
        )

    @classmethod
    def load_inventory_csv(
        cls, file_path: str | Path, encoding: str = "utf-8-sig"
    ) -> tuple[pl.DataFrame, dict[str, Any]]:
        file_path = Path(file_path)
        logger.info(f"📂 Загрузка и парсинг: {file_path.name}")

        text_content = cls._read_file_with_retries(file_path, encoding)

        # Используем HEADER_PATTERN для парсинга шапки
        header_info = cls._parse_header_lines(text_content)

        # Используем BODY_PATTERN для извлечения тела таблицы
        body_match = cls.BODY_PATTERN.search(text_content)
        if not body_match:
            raise ValueError("❌ Не найдено тело таблицы по BODY_PATTERN")

        body_text = body_match.group(1)

        # Загружаем тело в DataFrame
        df = cls.load_body_to_dataframe(body_text)

        logger.info(f"✅ DataFrame: {len(df)}×{len(df.columns)}")
        logger.info(
            f"✅ Период: {header_info.get('start_period', 'N/A')} → {header_info.get('end_period', 'N/A')}"
        )

        return df, header_info

    @classmethod
    def _read_file_with_retries(
        cls,
        file_path: Path,
        encoding: str,
        retries: int = 10,
        delay: float = 1.0,
    ) -> str:
        """Читает файл, повторяя попытку, пока писатель держит файл залоченным.

        Большой отчёт 1С пишется на сетевую шару не мгновенно: в момент
        срабатывания watchdog файл может быть ещё открыт/заблокирован.
        Без ожидания повторный open().read() падал бы на PermissionError/OSError
        и путь «зависал» в files_in_processing.
        """
        last_error: Exception | None = None
        for attempt in range(1, retries + 1):
            try:
                with open(file_path, encoding=encoding, errors="ignore") as f:
                    return f.read()
            except (PermissionError, OSError) as exc:
                last_error = exc
                logger.warning(
                    f"⚠️ Файл занят писателем ({file_path.name}), "
                    f"повтор {attempt}/{retries} через {delay}s: {exc}"
                )
                if attempt < retries:
                    time.sleep(delay)
        raise last_error if last_error else RuntimeError(f"Не удалось прочитать файл: {file_path}")

    @classmethod
    def _parse_header_lines(cls, text: str) -> dict[str, Any]:
        """
        Парсинг шапки используя HEADER_PATTERN - извлекает период (start_period/end_period).

        Args:
            text: Полный текст файла или только шапка

        Returns:
            Dict[str, Any]: Словарь с параметрами из шапки
        """
        result = {}

        # ---Парсим шапку---------------------------------------------
        # Используем HEADER_PATTERN для извлечения шапки
        # ------------------------------------------------------------
        header_match = cls.HEADER_PATTERN.search(text)

        if not header_match:
            logger.warning("⚠️ Шапка не найдена по HEADER_PATTERN")
            return result

        header_text = header_match.group(1)

        # Применяем паттерны для поиска периодов
        result.update(cls.extract_period_from_header(header_text))

        logger.debug(f"✅ Шапка распарсена: {len(result)} параметров")
        return result
