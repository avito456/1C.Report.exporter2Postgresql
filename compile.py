"""Сборка exe через PyInstaller: версия берётся из pyproject.toml и пишется в app/_version.py.

Запуск: `uv run --group dev python compile.py`.
"""

import logging
import tomllib
from pathlib import Path

import PyInstaller.__main__

pyproject_path = Path("./pyproject.toml")
with pyproject_path.open("rb") as file:
    data = tomllib.load(file)

VERSION = data["project"]["version"]
PROGRAM_NAME = data["project"]["name"]

logging.warning("\n" * 5)
logging.warning("-" * 100)
logging.warning(f">>> Compile: PROGRAM_NAME={PROGRAM_NAME}, VERSION={VERSION}")
logging.warning("-" * 100 + "\n")

version_file = Path("./app/_version.py")
version_file.parent.mkdir(parents=True, exist_ok=True)
version_file.write_text(f'__version__ = "{VERSION}"\n', encoding="utf-8")
logging.warning(f"Updated {version_file} with version {VERSION}")


def compile_python_script(script_path: str) -> None:
    icon_path = Path("./icon.ico")
    if icon_path.exists():
        logging.info(f"Icon found: {icon_path.resolve()}")
        icon_arg = ["--icon", str(icon_path.resolve())]
    else:
        logging.warning(f"Icon not found: {icon_path.resolve()}")
        icon_arg = []

    PyInstaller.__main__.run(
        [
            "--name",
            PROGRAM_NAME.replace(" ", "_"),
            script_path,
            *icon_arg,
            "--add-data",
            f"{pyproject_path.resolve()}{';'}.",
            "--distpath",
            f"dist/{PROGRAM_NAME}_{VERSION.replace('.', '_')}",
            "--workpath",
            f"build/{PROGRAM_NAME}_{VERSION.replace('.', '_')}",
            "--specpath",
            f".build/{PROGRAM_NAME}_{VERSION.replace('.', '_')}",
        ]
    )


script_path = "./app/__main__.py"
compile_python_script(script_path)

exe_name = PROGRAM_NAME.replace(" ", "_")
logging.warning(
    f"\n\n{'-' * 100}\n"
    f"Compiled: dist/{PROGRAM_NAME}_{VERSION.replace('.', '_')}/{exe_name}.exe "
    f"(version {VERSION} from pyproject.toml)\n"
    f"{'-' * 100}\n"
)
