"""Точка входа: инициализирует логирование и запускает веб-сервер с файловым монитором.

Запуск: `uv run report-exporter` (или `python -m app`). Сборка в exe: `compile.py`.
"""

import asyncio

from loguru import logger

from app.logger.init_logger import init_logger
from app.server import run_server
from app.settings.env import config, get_version


def main() -> None:
    init_logger(config)
    try:
        logger.info(f"Application version: {get_version()}")
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception:
        logger.exception("Критическая ошибка")


if __name__ == "__main__":
    main()
