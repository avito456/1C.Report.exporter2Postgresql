"""Чтение табличного документа 1С (формат MXL) в сетку ячеек.

MXL — текстовая сериализация 1С в фигурных скобках с префиксом ``MOXCEL``.
Нас интересуют только тексты ячеек, их позиции и области объединения:

* ячейка — список ``{16|20, fmt, [{"#",GUID,{n}}], {1,1,{"#","текст"}}}``
  (пустая ячейка — ``{16, fmt, {1,0}, 0}``);
* число после ячейки — колонка следующей ячейки в строке, а если следом нет
  ячейки — маркер конца строки (индекс строки + 1);
* объединения — список ``{N, {c1,r1,c2,r2,0}, ...}``.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

MAGIC = b"MOXCEL"
_BOM = b"\xef\xbb\xbf"

_TOKEN = re.compile(r'"((?:[^"]|"")*)"|([{}])|([^,{}"\s]+)')
_CELL_TYPES = (16, 20)


class MxlFormatError(ValueError):
    """Файл не является MXL или имеет неподдерживаемую структуру."""


class _Bare(str):
    """Небуквальный токен, не являющийся числом (например, GUID)."""

    __slots__ = ()


@dataclass(frozen=True)
class _Cell:
    text: str


@dataclass
class MxlSheet:
    """Ячейки листа: ``cells[(row, col)]`` — текст, ``merges`` — ``(r1, c1, r2, c2)``."""

    cells: dict[tuple[int, int], str] = field(default_factory=dict)
    merges: list[tuple[int, int, int, int]] = field(default_factory=list)
    row_count: int = 0
    col_count: int = 0


def _atom(token: str) -> int | str:
    try:
        return int(token)
    except ValueError:
        return _Bare(token)


def _cell_text(node: list) -> str | None:
    """Текст ячейки, если ``node`` — запись ячейки; иначе ``None``."""
    if len(node) < 3 or node[0] not in _CELL_TYPES or not isinstance(node[0], int):
        return None
    if not isinstance(node[1], int):
        return None
    for child in node[2:]:
        if isinstance(child, list) and len(child) >= 2 and child[0] == 1 and isinstance(child[0], int):
            if child[1] == 0:
                return ""
            if (
                child[1] == 1
                and len(child) >= 3
                and isinstance(child[2], list)
                and len(child[2]) >= 2
                and child[2][0] == "#"
                and not isinstance(child[2][0], _Bare)
            ):
                return child[2][1]
    return None


def _root_items(text: str) -> list:
    """Разбирает сериализацию потоково: элементы корня, ячейки сразу сжимаются."""
    stack: list[list] = []
    root: list | None = None
    for m in _TOKEN.finditer(text):
        s, brace, bare = m.groups()
        if s is not None:
            value: object = s.replace('""', '"') if '""' in s else s
        elif bare is not None:
            value = _atom(bare)
        elif brace == "{":
            stack.append([])
            continue
        else:
            if not stack:
                raise MxlFormatError("Лишняя закрывающая скобка")
            node = stack.pop()
            if stack:
                if len(stack) == 1:
                    cell = _cell_text(node)
                    stack[0].append(_Cell(cell) if cell is not None else node)
                else:
                    stack[-1].append(node)
            else:
                root = node
            continue
        if stack:
            stack[-1].append(value)
        else:
            raise MxlFormatError("Значение вне корневого списка")
    if root is None or stack:
        raise MxlFormatError("Незавершённая структура MXL")
    return root


def _merges(root: list) -> list[tuple[int, int, int, int]]:
    for item in root:
        if not isinstance(item, list) or len(item) < 2 or not isinstance(item[0], int):
            continue
        n, rest = item[0], item[1:]
        if n < 1 or len(rest) != n:
            continue
        if all(
            isinstance(r, list)
            and len(r) == 5
            and all(isinstance(v, int) for v in r)
            and r[4] == 0
            and r[2] >= r[0] >= 0
            and r[3] >= r[1] >= 0
            for r in rest
        ):
            return [(r[1], r[0], r[3], r[2]) for r in rest]
    return []


def parse_mxl(data: bytes) -> MxlSheet:
    """Разбирает содержимое MXL-файла в :class:`MxlSheet`."""
    if not data.startswith(MAGIC):
        raise MxlFormatError("Файл не является MXL (нет сигнатуры MOXCEL)")
    start = data.find(b"{")
    if start < 0:
        raise MxlFormatError("В MXL не найдено содержимое")
    try:
        text = data[start:].decode("utf-8-sig")
    except UnicodeDecodeError as e:
        raise MxlFormatError(f"Неподдерживаемая кодировка MXL: {e}") from e
    if data[len(MAGIC) + 6 : len(MAGIC) + 6 + 3] != _BOM and b"\xef\xbb\xbf{" not in data[:32]:
        raise MxlFormatError("Неподдерживаемый вариант MXL (нет BOM после заголовка)")

    root = _root_items(text)
    sheet = MxlSheet(merges=_merges(root))

    # Ячейки колонтитулов предваряются разделителем ``{0,0}`` — пропускаем их.
    first = None
    for i, item in enumerate(root):
        if isinstance(item, _Cell) and not (i > 0 and root[i - 1] == [0, 0]):
            first = i
            break
    if first is None:
        return sheet

    n = len(root)
    i = first
    col = 0
    row = 0
    pending: list[tuple[int, str]] = []
    while i < n:
        item = root[i]
        if not isinstance(item, _Cell):
            break
        pending.append((col, item.text))
        i += 1
        # после последней ячейки листа возможен служебный список ``{1,0,GUID,0}``
        while i < n and isinstance(root[i], list):
            i += 1
        nxt = root[i] if i < n else None
        if not isinstance(nxt, int):
            raise MxlFormatError(f"Ожидалась позиция после ячейки, получено {nxt!r}")
        after = root[i + 1] if i + 1 < n else None
        i += 1
        if isinstance(after, _Cell):
            if nxt <= col:
                raise MxlFormatError(f"Колонка не возрастает: {col} -> {nxt}")
            col = nxt
            continue
        # конец строки: nxt — индекс строки + 1
        row = nxt - 1
        if row < 0:
            raise MxlFormatError(f"Недопустимый маркер строки: {nxt}")
        for c, t in pending:
            sheet.cells[(row, c)] = t
        sheet.col_count = max(sheet.col_count, pending[-1][0] + 1)
        sheet.row_count = max(sheet.row_count, row + 1)
        pending = []
        col = 0
        while i < n and not isinstance(root[i], (_Cell, list)):
            i += 1
    if pending:
        raise MxlFormatError("Строка MXL не завершена маркером конца")
    return sheet


def read_mxl(path: str | Path) -> MxlSheet:
    """Читает MXL-файл с диска."""
    return parse_mxl(Path(path).read_bytes())
