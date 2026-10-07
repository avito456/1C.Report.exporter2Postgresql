"""Одноразовая миграция legacy TOML-настроек отчётов в плоский формат.

Legacy-формат:
    [reports_export_settings."имя отчёта"]
    use = false
    [[reports_export_settings."имя отчёта".columns]]
    ...

Плоский формат (1 файл = 1 отчёт):
    use = false
    [[columns]]
    ...

Конвертация текстовая (заголовки секций), поэтому комментарии и порядок ключей
сохраняются байт-в-байт. Результат валидируется: парсится и семантически
сравнивается с содержимым legacy-секции.

Использование:
    python scripts/migrate_toml.py            # dry-run: отчёт, ничего не пишет
    python scripts/migrate_toml.py --apply    # запись мигрированных файлов

Имена файлов не меняются, сироты не переименовываются и не удаляются —
конвертируется только формат содержимого.
"""
import argparse
import re
import sys
import tomllib
from pathlib import Path

LEGACY_KEY = "reports_export_settings"
SKIP_FILES = {"pyproject.toml"}

_ANY_LEGACY = re.compile(rf"^\[\[?{LEGACY_KEY}[.\]]", re.M)


def _wrapper_header_re(section_key: str) -> re.Pattern:
    """Заголовок-обёртка строго для выбранной секции: [reports_export_settings."key"]."""
    escaped = re.escape(section_key)
    return re.compile(
        rf"^\[{LEGACY_KEY}\.(?:\"{escaped}\"|{escaped})\][ \t]*(#[^\r\n]*)?(?P<eol>\r?\n)",
        re.M,
    )


def project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def convert(text: str, stem: str) -> tuple[str, list[str]]:
    """Конвертирует legacy TOML в плоский.

    Возвращает (новый_текст, список_предупреждений).
    Уже плоский файл возвращается без изменений.
    """
    warnings: list[str] = []

    if not _ANY_LEGACY.search(text):
        return text, warnings

    # Семантический разбор legacy-файла (заодно ловим синтаксические ошибки)
    data = tomllib.loads(text)
    legacy = data.get(LEGACY_KEY)
    if not isinstance(legacy, dict) or not legacy:
        raise ValueError(f"нет секций [{LEGACY_KEY}]")

    keys = list(legacy)
    if stem in legacy:
        if len(keys) > 1:
            extras = [k for k in keys if k != stem]
            warnings.append(f"лишние секции {extras} — проигнорированы")
        section_key = stem
    else:
        section_key = keys[0]
        warnings.append(
            f"ключ секции '{section_key}' не совпадает с именем файла '{stem}.toml' — "
            f"использован ключ секции"
        )
        if len(keys) > 1:
            warnings.append(f"в файле несколько секций {keys} — взята '{section_key}'")

    extra_top = [k for k in data if k != LEGACY_KEY]
    if extra_top:
        raise ValueError(f"лишние ключи верхнего уровня {extra_top} — автоматическая миграция невозможна")

    # Текстовая конвертация заголовков
    new_text, wrapper_count = _wrapper_header_re(section_key).subn(_replace_wrapper, text, count=1)
    if wrapper_count == 0:
        raise ValueError(f"заголовок секции [{LEGACY_KEY}].{section_key} не найден в тексте")

    escaped_key = re.escape(section_key)
    nested = re.compile(
        rf"^(\[\[){LEGACY_KEY}\.(?:\"{escaped_key}\"|{escaped_key})\.",
        re.M,
    )
    new_text, nested_count = nested.subn(r"\1", new_text)
    if nested_count == 0 and "columns" in legacy[section_key]:
        raise ValueError(f"не найдены заголовки [[{LEGACY_KEY}....columns]]")

    leftover = _ANY_LEGACY.search(new_text)
    if leftover:
        line_no = new_text[: leftover.start()].count("\n") + 1
        raise ValueError(f"остались ссылки на {LEGACY_KEY} (строка {line_no})")

    # Нормализация пустых строк после удалённого заголовка
    new_text = re.sub(r"\n{3,}", "\n\n", new_text)

    # Семантическая валидация: результат должен совпасть с legacy-секцией
    new_data = tomllib.loads(new_text)
    expected = legacy[section_key]
    if new_data != expected:
        raise ValueError("результат конвертации семантически не совпал с исходной секцией")

    return new_text, warnings


def _replace_wrapper(match: re.Match) -> str:
    """Удаляет строку-обёртку [reports_export_settings."X"], сохраняя
    однострочный комментарий (если был) отдельной строкой."""
    trailing_comment = match.group(1)
    if trailing_comment:
        return trailing_comment + match.group("eol")
    return ""


def migrate_file(path: Path, apply: bool) -> str:
    stem = path.stem
    text = path.read_text(encoding="utf-8")

    try:
        new_text, warnings = convert(text, stem)
    except Exception as e:
        return f"❌ {path.name}: {e}"

    if new_text == text:
        line = f"•  {path.name}: уже плоский — пропущен"
    else:
        line = f"→  {path.name}: legacy → плоский"
        if apply:
            path.write_text(new_text, encoding="utf-8", newline="\n")
            line += " [ЗАПИСАН]"
        else:
            line += " [dry-run]"

    for w in warnings:
        line += f"\n   ⚠️  {w}"
    return line


def main(argv: list[str] | None = None) -> int:
    reconfigure = getattr(sys.stdout, "reconfigure", None)
    if reconfigure is not None:
        reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Миграция legacy TOML-настроек в плоский формат")
    parser.add_argument("--apply", action="store_true",
                        help="записать изменения (по умолчанию dry-run)")
    args = parser.parse_args(argv)

    root = project_root()
    files = sorted(p for p in root.glob("*.toml") if p.name not in SKIP_FILES)

    mode = "APPLY: запись изменений" if args.apply else "DRY-RUN: изменения не записываются"
    print(f"Миграция TOML в {root}\n{mode}\n")

    errors = 0
    for path in files:
        result = migrate_file(path, apply=args.apply)
        print(result)
        if result.startswith("❌"):
            errors += 1

    print(f"\nГотово: файлов проверено {len(files)}, ошибок {errors}")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
