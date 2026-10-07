"""Пользовательские исключения и перевод ошибок БД в человекочитаемый вид."""

import re

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
        file_path: str | None = None,
        table_name: str | None = None,
        column: str | None = None,
        expected: str | None = None,
        received: str | None = None,
        row_number: int | None = None,
        hint: str | None = None,
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
    """Разбирает сообщение asyncpg про неверный аргумент запроса.

    Возвращает (position_1_based, element_index | None, received, expected) или None.
    """
    match = _ARG_PATTERN.search(message)
    if not match:
        return None
    arg_pos = int(match.group(1))
    element = int(match.group(2)) if match.group(2) else None
    received = (match.group(3) or "").strip()
    expected = (match.group(4) or "").strip()
    return arg_pos, element, received, expected


def _column_for_arg(arg_pos: int, columns: list[str]) -> str | None:
    """Возвращает имя колонки по 1-индексной позиции аргумента (без id)."""
    return columns[arg_pos - 1] if 1 <= arg_pos <= len(columns) else None


def build_load_error(
    exc: Exception,
    *,
    file_path: str | None = None,
    table_name: str | None = None,
    columns: list[str] | None = None,
) -> ReportLoadError:
    """Переводит любое исключение в человекочитаемый ReportLoadError."""
    columns = columns or []
    base_hint = "Проверьте формат данных в исходном файле и типы колонок в TOML-конфигурации / в таблице."

    # 1. Неверный тип аргумента:
    #    "invalid input for query argument $N [in element #M of executemany() sequence]:
    #     1.0 (expected str, got float)"
    parsed = _parse_executemany_message(str(exc))
    if parsed:
        arg_pos, element, received, expected = parsed
        row_number = element + 1 if element is not None else None
        msg = (
            "несоответствие типа данных при вставке строки"
            + (f" ~#{row_number}" if row_number else "")
            + f": значение {received} не подходит для типа колонки."
        )
        return ReportLoadError(
            msg,
            file_path=file_path,
            table_name=table_name,
            column=_column_for_arg(arg_pos, columns),
            expected=expected or None,
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
