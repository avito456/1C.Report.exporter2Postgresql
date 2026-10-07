import asyncio

from loguru import logger

from app.logger.init_logger import init_logger
from app.server import run_server
from app.settings.env import config, get_version

init_logger(config)


if __name__ == "__main__":
    try:
        logger.info(f"Application version: {get_version()}")
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception:
        logger.exception("Критическая ошибка")
