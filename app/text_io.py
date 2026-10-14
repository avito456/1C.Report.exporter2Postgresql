"""Чтение текстовых выгрузок 1С с определением кодировки.

1С выгружает отчёты и рядом лежащие TOML-конфиги то в UTF-8 (иногда с BOM),
то в Windows-1251. Декодирование cp1251-байтов как UTF-8 падает с
UnicodeDecodeError, поэтому кодировки перебираются по очереди.
"""

from pathlib import Path

ENCODINGS = ("utf-8-sig", "cp1251")


def decode_text(data: bytes, encodings: tuple[str, ...] = ENCODINGS) -> str:
    """Декодирует байты, перебирая кодировки; последняя — с errors='replace'."""
    for encoding in encodings:
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode(encodings[-1], errors="replace")


def read_text_auto(file_path: str | Path, encodings: tuple[str, ...] = ENCODINGS) -> str:
    """Читает файл и декодирует его с автоопределением кодировки."""
    return decode_text(Path(file_path).read_bytes(), encodings)
