import asyncio
import os
import queue
import time
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Dict

import uvicorn
from fastapi import FastAPI
from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers.polling import PollingObserver

from app.db import db_uploader
from app.db.errors import ReportLoadError, build_load_error
from app.parsers import report_loader
from app.settings import tables_settings
from app.settings.env import Config

config = Config()

monitor_status = {
    "is_running": True,
    "processed_files": 0,
    "processed_files_status": {},
    "last_processed": None,
}
status_lock = Lock()
shutdown_event = Event()

# Путь -> время начала обработки. Запись со слишком старым временем
# считается «зависшей» и выселяется, чтобы большой/блокируемый файл
# не «заглох» в обработке и не молча игнорировал последующие изменения.
files_in_processing: Dict[str, float] = {}
processing_lock = Lock()
PROCESS_STALE_SECONDS = 1800.0
READ_TIMEOUT_SECONDS = 300.0
FALLBACK_SCAN_INTERVAL = 5.0


def _is_blocked(path: str) -> bool:
    """True — событие для path нужно отбросить (уже обрабатывается).

    Если запись «зависла» дольше PROCESS_STALE_SECONDS, выселяем её
    (гарантированное освобождение) и принимаем текущее событие.
    """
    with processing_lock:
        started = files_in_processing.get(path)
        if started is None:
            return False
        if time.time() - started >= PROCESS_STALE_SECONDS:
            files_in_processing.pop(path, None)
            logger.warning(
                f"Stale processing entry evicted for {path} "
                f"(older than {PROCESS_STALE_SECONDS}s). Event accepted."
            )
            return False
        logger.warning(f"File already in processing: {path}")
        return True

_fallback_last_seen: Dict[str, float] = {}


def _fallback_scan(report_dir: str):
    """Фоновый сканер: ловит модификации, пропущенные PollingObserver."""
    try:
        dir_path = Path(report_dir)
        if not dir_path.exists():
            return

        now = time.time()
        for entry in dir_path.iterdir():
            if not entry.is_file():
                continue
            if not entry.name.lower().endswith(".txt"):
                continue

            try:
                mtime = entry.stat().st_mtime
            except (OSError, PermissionError):
                continue

            path_str = str(entry)

            prev = _fallback_last_seen.get(path_str)
            if prev is not None and abs(mtime - prev) < 0.1:
                continue
            _fallback_last_seen[path_str] = mtime

            if now - mtime > 10.0:
                continue

            if _is_blocked(path_str):
                continue

            logger.info(f"Fallback scan: file modified: {path_str}")
            try:
                file_queue.put_nowait(path_str)
            except queue.Full:
                logger.warning("File queue is full")
    except Exception as exc:
        logger.debug(f"Fallback scan error: {exc}")


def _start_fallback_scanner(report_dir: str):
    """Запускает daemon-поток периодического сканирования директории."""
    def _loop():
        while not shutdown_event.is_set():
            _fallback_scan(report_dir)
            shutdown_event.wait(FALLBACK_SCAN_INTERVAL)

    t = Thread(target=_loop, daemon=True, name="fallback-scanner")
    t.start()
    return t


file_queue: queue.Queue[str] = queue.Queue()
DATETIME_FORMATS = ("%Y-%m-%d %H:%M:%S", "%d.%m.%Y %H:%M:%S")
DATE_ONLY_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")
DATE_FORMATS = DATETIME_FORMATS + DATE_ONLY_FORMATS


def parse_header_date(raw_value: str | None) -> datetime | None:
    parsed_date, _ = parse_header_date_with_type(raw_value)
    return parsed_date


def parse_header_date_with_type(raw_value: str | None) -> tuple[datetime | None, bool]:
    if not raw_value:
        return None, False

    for fmt in DATETIME_FORMATS:
        try:
            return datetime.strptime(raw_value, fmt), True
        except ValueError:
            continue

    for fmt in DATE_ONLY_FORMATS:
        try:
            return datetime.strptime(raw_value, fmt), False
        except ValueError:
            continue

    return None, False


def complete_end_period(
    event_time: str | None,
    start_period: datetime | None,
    start_period_is_datetime: bool,
    end_period: datetime | None,
) -> datetime | None:
    if not event_time or start_period is None or end_period is not None:
        return end_period

    if start_period_is_datetime:
        return start_period.replace(hour=23, minute=59, second=59, microsecond=0)

    return start_period


class FileHandler(FileSystemEventHandler):
    def on_created(self, event):
        if event.is_directory or shutdown_event.is_set():
            return

        if not event.src_path.lower().endswith(".txt"):
            return

        logger.debug(f"on_created: {event.src_path}")
        if _is_blocked(event.src_path):
            return

        logger.info(f"New file: {event.src_path}")
        try:
            file_queue.put_nowait(event.src_path)
        except queue.Full:
            logger.warning("File queue is full")

    def on_modified(self, event):
        if event.is_directory or shutdown_event.is_set():
            return

        if not event.src_path.lower().endswith(".txt"):
            return

        logger.debug(f"on_modified: {event.src_path}")
        if _is_blocked(event.src_path):
            return

        logger.info(f"\n{'=' * 100}\nFile modified: {event.src_path}\n{'=' * 100}\n")
        try:
            file_queue.put_nowait(event.src_path)
        except queue.Full:
            logger.warning("File queue is full")


class FileProcessor:
    async def process_files(self):
        while not shutdown_event.is_set():
            path = None
            try:
                loop = asyncio.get_running_loop()
                path = await loop.run_in_executor(None, lambda: file_queue.get(timeout=0.1))
                await self._process_single_file(path)
            except queue.Empty:
                continue
            except Exception as exc:
                logger.error(f"Queue processing error: {exc}")
            finally:
                if path is not None:
                    file_queue.task_done()

    async def _process_single_file(self, path: str):
        if shutdown_event.is_set():
            return

        with processing_lock:
            files_in_processing[path] = time.time()

        with status_lock:
            monitor_status["processed_files"] += 1
            monitor_status["last_processed"] = Path(path).name

        logger.debug(f"Asynchronous processing: {path}")
        loader = None
        ts: dict | None = None

        try:
            parser = report_loader.ReportHeaderParser()
            loop = asyncio.get_running_loop()
            df, header = await asyncio.wait_for(
                loop.run_in_executor(None, parser.load_inventory_csv, path),
                timeout=READ_TIMEOUT_SECONDS,
            )

            start_period, start_period_is_datetime = parse_header_date_with_type(header.get("start_period"))
            end_period = parse_header_date(header.get("end_period"))

            toml_file, _ = os.path.splitext(os.path.basename(path))
            report_settings = tables_settings.load_config(toml_file=f"{toml_file}.toml", df=df)
            if report_settings is None:
                logger.error(f"Could not load TOML config for {toml_file}")
                return

            table_config = report_settings.reports_export_settings.get(toml_file)
            if table_config is None:
                logger.error(f"Missing TOML section for {toml_file}")
                return

            ts = table_config.model_dump(exclude_none=True)

            if not ts.get("use", False):
                logger.warning(
                    f"\n{'!' * 100}\nParameter [use] = false. Upload to DB canceled\n{'!' * 100}\n"
                )
                return

            event_time = ts.get("event_time")
            end_period = complete_end_period(event_time, start_period, start_period_is_datetime, end_period)
            db_uploader.validate_required_period_bounds(event_time, start_period, end_period)

            df = tables_settings.apply_schema_to_dataset(
                df=df,
                report_settings=report_settings,
                toml_file=toml_file,
            )

            db_params = {
                "host": config.DB_HOST,
                "port": config.DB_PORT,
                "database": config.DB_NAME,
                "user": config.DB_USER,
                "password": config.DB_PWD,
            }

            column_comments = {
                col["alias"]: col["comment"]
                for col in ts.get("columns", [])
                if col.get("alias") and col.get("comment") and col["alias"] in df.columns
            }

            loader = db_uploader.AsyncDatasetToPostgres(
                db_params=db_params,
                dataframe=df,
                table_name=ts["table_name"],
                schema=ts["schema_name"],
                event_time=event_time,
                indexes=ts.get("indexes", []),
                start_period=start_period,
                end_period=end_period,
                comment=ts.get("comment"),
                column_comments=column_comments,
            )

            await loader.create_table()
            await loader.sync_schema()

            await loader.delete_period()
            await loader.load_df()
            await loader.create_indexes()

            with status_lock:
                monitor_status["processed_files_status"][Path(path).name] = datetime.now()

            logger.info(
                f"\n{'-' * 100}\n"
                f"File processed successfully: {path}"
                f"\n{'-' * 100}"
            )
        except Exception as exc:
            table_name = ts.get("table_name") if isinstance(ts, dict) else None
            columns_list = loader.columns if loader is not None else None
            err = build_load_error(
                exc,
                file_path=path,
                table_name=table_name,
                columns=columns_list,
            )
            logger.error(f"\n{'!' * 100}\n{err.format_full()}\n{'!' * 100}\n")
            with status_lock:
                monitor_status["processed_files_status"][Path(path).name] = f"ERROR: {err.message[:200]}"
            logger.warning(
                f"\n{'!' * 100}\n"
                f"File {path} skipped due to error. It will be retried after the next modification."
                f"\n{'!' * 100}\n"
            )
        finally:
            if loader is not None:
                await loader.close()
            with processing_lock:
                files_in_processing.pop(path, None)


file_processor = FileProcessor()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("Starting file monitor...")

    report_dir = Path(config.REPORT_DIR)

    if not report_dir.exists():
        logger.error(
            f"Report directory not found: {report_dir}. "
            f"Check REPORT_DIR in .env or ensure the network share is accessible."
        )
        monitor_status["is_running"] = False
        yield
        return

    if not os.access(report_dir, os.R_OK):
        logger.error(
            f"Report directory is not readable: {report_dir}. "
            f"Check file permissions or network share access rights."
        )
        monitor_status["is_running"] = False
        yield
        return

    handler = FileHandler()
    observer = PollingObserver()
    observer.schedule(handler, str(report_dir), recursive=True)
    observer.start()

    app.state.observer = observer
    app.state.handler = handler
    app.state.processor_task = asyncio.create_task(file_processor.process_files())
    app.state.fallback_thread = _start_fallback_scanner(str(report_dir))

    logger.info(f"Monitoring started for directory: {report_dir}")
    yield

    logger.info("Graceful shutdown...")
    shutdown_event.set()

    with status_lock:
        monitor_status["is_running"] = False

    if hasattr(app.state, "processor_task"):
        app.state.processor_task.cancel()
        try:
            await app.state.processor_task
        except asyncio.CancelledError:
            pass

    if hasattr(app.state, "observer"):
        app.state.observer.stop()
        app.state.observer.join(timeout=5.0)

    logger.info("Graceful shutdown completed")


app = FastAPI(title="File Monitor Status", lifespan=lifespan)


@app.get("/status")
async def get_status():
    with status_lock:
        return {
            "is_running": monitor_status["is_running"] and not shutdown_event.is_set(),
            "processed_files": monitor_status["processed_files"],
            "processed_files_status": monitor_status["processed_files_status"],
            "last_processed": monitor_status["last_processed"],
            "watch_dir": config.REPORT_DIR,
            "queue_size": file_queue.qsize(),
        }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": "file-monitor"}


async def run_server():
    uv_config = uvicorn.Config(
        app,
        host=config.UV_HOST,
        port=config.UV_PORT,
        log_level=config.LOG_LEVEL,
    )
    server = uvicorn.Server(uv_config)
    await server.serve()
