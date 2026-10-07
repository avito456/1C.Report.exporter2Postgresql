"""Тесты дедупликации событий файлов: watchdog и fallback-сканер не должны ставить файл дважды."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from app import server


class EnqueueDedupTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server._loop = asyncio.get_running_loop()
        server.file_queue = asyncio.Queue()
        server._pending.clear()
        server._processed_mtime.clear()
        server.files_in_processing.clear()
        self.tmp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.tmp.name) / "r.txt")
        Path(self.path).write_text("x", encoding="utf-8")

    async def asyncTearDown(self):
        self.tmp.cleanup()

    async def _drain(self) -> int:
        await asyncio.sleep(0.05)  # дать call_soon_threadsafe отработать
        return server.file_queue.qsize()

    async def test_duplicate_events_before_processing_are_collapsed(self):
        server._enqueue(self.path, "watchdog")
        server._enqueue(self.path, "fallback")

        self.assertEqual(await self._drain(), 1)

    async def test_event_after_processing_of_same_version_is_skipped(self):
        server._enqueue(self.path, "watchdog")
        await self._drain()
        # имитация начала и конца обработки
        server._pending.discard(self.path)
        server._processed_mtime[self.path] = server._mtime(self.path)

        server._enqueue(self.path, "fallback")

        self.assertEqual(await self._drain(), 1)  # новых элементов нет

    async def test_modified_file_is_queued_again(self):
        server._processed_mtime[self.path] = server._mtime(self.path) - 5

        server._enqueue(self.path, "watchdog")

        self.assertEqual(await self._drain(), 1)


if __name__ == "__main__":
    unittest.main()
