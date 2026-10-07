"""Пользовательские исключения и перевод ошибок БД в человекочитаемый вид."""

import re
from typing import List, Optional

import asyncpg


class ReportLoadError(Exception):
    """Ошибка загрузки отчёта с человекочитаемым описанием.

    Содержит структурированные поля для диагностики, а текст самого
    исключения — готовое к показу пользователю сообщение.
    """

    def __init__(
        self,
        message: str,
        *,
        file_path: Optional[str] = None,
        table_name: Optional[str] = None,
        column: Optional[str] = None,
        expected: Optional[str] = None,
        received: Optional[str] = None,
        row_number: Optional[int] = None,
        hint: Optional[str] = None,
    ):
        super().__init__(message)
        self.message = message
        self.file_path = file_path
        self.table_name = table_name
        self.column = column
        self.expected = expected
        self.received = received
        self.row_number = row_number
        self.hint = hint

    def format_full(self) -> str:
        """Полный многострочный блок с деталями (для лога)."""
        lines = ["Не удалось загрузить файл."]
        if self.file_path:
            lines.append(f"Файл: {self.file_path}")
        if self.table_name:
            lines.append(f"Таблица: {self.table_name}")
        lines.append(f"Причина: {self.message}")
        if self.column:
            lines.append(f"Колонка: {self.column}")
        if self.expected:
            lines.append(f"Ожидаемый тип: {self.expected}")
        if self.received:
            lines.append(f"Фактическое значение: {self.received}")
        if self.row_number is not None:
            lines.append(f"Номер строки (примерно): {self.row_number}")
        if self.hint:
            lines.append(f"Рекомендация: {self.hint}")
        return "\n".join(lines)


_ARG_PATTERN = re.compile(
    r"invalid input for query argument \$(\d+)"
    r"(?: in element #(\d+))?"
    r"(?: of executemany\(\) sequence)?:\s*(.+?)\s*"
    r"\(expected (.+?), got (.+?)\)\.?\s*$",
    re.IGNORECASE | re.DOTALL,
)


def _parse_executemany_message(message: str):
    """Разбирает сообщение asyncpg про неверный аргумент executemany.

    Возвращает (position_1_based, element_index, received, expected) или None.
    """
    match = _ARG_PATTERN.search(message)
    if not match:
        return None
    arg_pos = int(match.group(1))
    element = int(match.group(2)) if match.group(2) else 0
    received = (match.group(3) or "").strip()
    expected = (match.group(4) or "").strip()
    return arg_pos, element, received, expected


def _describe_value(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, float):
        return repr(value)
    if isinstance(value, (int, bool)):
        return str(value)
    return repr(value)


def _column_for_arg(arg_pos: int, columns: List[str]) -> Optional[str]:
    """Возвращает имя колонки по 1-индексной позиции аргумента (без id)."""
    if arg_pos < 1:
        return None
    if 1 <= arg_pos <= len(columns):
        return columns[arg_pos - 1]
    return None


_COLUMN_TYPE_HINT = {
    "TEXT": "строковый (символьный) столбец — файл должен содержать текст",
    "UUID": "UUID-столбец — значение должно быть в формате UUID",
    "DATE": "дата в формате ДД.ММ.ГГГГ или ГГГГ-ММ-ДД",
    "TIMESTAMP": "дата-время в формате ДД.ММ.ГГГГ ЧЧ:ММ:СС",
    "BIGINT": "целочисленный столбец",
    "DOUBLE PRECISION": "числовой столбец (дробное число)",
}


def build_load_error(
    exc: Exception,
    *,
    file_path: Optional[str] = None,
    table_name: Optional[str] = None,
    columns: Optional[List[str]] = None,
) -> ReportLoadError:
    """Переводит любое исключение в человекочитаемый ReportLoadError."""
    columns = columns or []
    base_hint = (
        "Проверьте формат данных в исходном файле и типы колонок "
        "в TOML-конфигурации / в таблице."
    )

    # 1. Типичная ошибка неверного типа аргумента в executemany:
    #    "invalid input for query argument $N ... (expected str, got float)"
    parsed = _parse_executemany_message(str(exc))
    if parsed:
        arg_pos, element, received, _expected = parsed
        column = _column_for_arg(arg_pos, columns)
        row_number = element + 1 if element is not None else None
        msg = (
            f"несоответствие типа данных при вставке строки"
            + (f" ~#{row_number}" if row_number else "")
            + f": значение {received} не подходит для типа колонки."
        )
        return ReportLoadError(
            msg,
            file_path=file_path,
            table_name=table_name,
            column=column,
            expected=_expected or None,
            received=received or None,
            row_number=row_number,
            hint=base_hint,
        )

    # 2. Ошибки asyncpg/PostgreSQL с полезными полями.
    if isinstance(exc, asyncpg.PostgresError):
        message = str(exc) or "ошибка PostgreSQL"
        col = getattr(exc, "column_name", None)
        detail = getattr(exc, "detail", None) or ""
        hint = getattr(exc, "hint", None) or base_hint
        if hasattr(exc, "sqlstate"):
            return ReportLoadError(
                message,
                file_path=file_path,
                table_name=table_name,
                column=col or None,
                received=detail or None,
                hint=hint,
            )

    return ReportLoadError(
        str(exc) or "неизвестная ошибка",
        file_path=file_path,
        table_name=table_name,
        hint=base_hint,
    )
