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
