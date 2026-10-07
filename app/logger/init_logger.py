from loguru import logger
import sys
from pathlib import Path


def init_logger(config):
    """Loguru с цветами и защитой от PyInstaller проблем."""
    logger.remove()

    # 1. Консольный логгер (всегда работает)
    logger.add(
        sys.stdout,
        format=(
            "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<white>{message}</white>"
        ),
        level=config.LOG_FILE_LEVEL.upper(),
        colorize=True,
        enqueue=True  # Безопасно для PyInstaller
    )

    # 2. Файловый логгер — только если LOG_FILE_DIR явно задан
    log_dir = (config.LOG_FILE_DIR or '').strip()
    if not log_dir:
        return

    log_path = Path(config.APP_PATH or ".") / Path(log_dir) / "app.log"

    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if log_path.parent.exists() and log_path.parent.is_dir():
            logger.add(
                str(log_path),
                rotation="5 MB",
                retention="7 days",
                compression="zip",
                level=config.LOG_FILE_LEVEL.upper(),
                format="{time:YYYY-MM-DD HH:mm:ss.SSS} | {level:<8} | {name}:{function}:{line} | {message}",
                enqueue=True  # Важно для PyInstaller!
            )
            logger.info(f"📝 Логи пишутся в: {log_path}")
        else:
            logger.warning(f"⚠️  Не удалось создать папку логов: {log_path.parent}")

    except Exception as e:
        logger.warning(f"⚠️  Ошибка настройки файла логов: {e}")

