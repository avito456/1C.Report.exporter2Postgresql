import asyncio
from loguru import logger
from app.server import run_server
from app.settings.env import Config, get_version
from app.logger.init_logger import init_logger

config = Config()
init_logger(config)


if __name__ == "__main__":
    try:
        logger.info(f"Application version: {get_version()}")
        asyncio.run(run_server())
    except KeyboardInterrupt:
        logger.info("Программа остановлена пользователем")
    except Exception as e:
        logger.error(f"Критическая ошибка: {e}")
