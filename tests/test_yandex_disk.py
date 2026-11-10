import asyncio
import hashlib
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from bot import yandex_disk


class Response:
    def __init__(self, value=None, *, status=200, headers=None, gate=None):
        self.value = value
        self.status = status
        self.headers = headers or {}
        self.gate = gate

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def json(self):
        if self.gate is not None:
            await self.gate.wait()
        return self.value

    async def read(self):
        return self.value


class SourceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.cache = self.root / "source"
        self.logger = MagicMock()
        self.session = MagicMock()
        self.session.__aenter__ = AsyncMock(return_value=self.session)
        self.session.__aexit__ = AsyncMock(return_value=False)
        self.patch = patch.object(
            yandex_disk.aiohttp, "ClientSession", return_value=self.session
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.validate = MagicMock()

    @staticmethod
    def meta(content):
        return {"md5": hashlib.md5(content).hexdigest(), "size": len(content)}

    def responses(self, content):
        return [
            Response(self.meta(content)), Response({"href": "https://example.invalid/file"}),
            Response(content),
        ]

    async def get(self, *, force=False, name="out.xlsx", url="https://example.invalid/source"):
        return await yandex_disk.download_public(
            url, self.root / name, self.logger, attempts=1,
            cache_dir=self.cache, validate=self.validate, force=force,
        )

    async def test_unchanged_force_restart_and_missing_local_file(self):
        content = b"valid workbook"
        self.session.get.side_effect = self.responses(content)
        self.assertEqual((await self.get()).read_bytes(), content)
        self.assertEqual(self.session.get.call_count, 3)
        await self.get(name="another.xlsx")
        self.assertEqual(self.session.get.call_count, 3)
        self.session.get.side_effect = [Response(self.meta(content))]
        await self.get(force=True)
        self.assertEqual(self.session.get.call_count, 4)
        # A new caller uses the persisted manifest rather than an in-memory cache.
        await self.get(name="after-restart.xlsx")
        self.assertEqual(self.session.get.call_count, 4)
        source_key = hashlib.sha256(b"https://example.invalid/source").hexdigest()
        (self.cache / f"{source_key}-{hashlib.sha256(content).hexdigest()}.xlsx").unlink()
        self.session.get.side_effect = self.responses(content)
        await self.get()
        self.assertEqual(self.session.get.call_count, 7)

    async def test_bad_candidate_does_not_replace_valid_candidate(self):
        self.session.get.side_effect = self.responses(b"good")
        await self.get()
        version = yandex_disk.source_version("https://example.invalid/source", self.cache)
        self.validate.side_effect = ValueError("invalid workbook")
        self.session.get.side_effect = self.responses(b"broken")
        with self.assertRaisesRegex(ValueError, "invalid workbook"):
            await self.get(force=True)
        self.assertEqual(yandex_disk.source_version(
            "https://example.invalid/source", self.cache
        ), version)
        self.assertEqual((self.root / "out.xlsx").read_bytes(), b"good")

    async def test_changed_source_and_missing_checksum_download_bytes(self):
        self.session.get.side_effect = self.responses(b"first")
        await self.get()
        self.session.get.side_effect = [
            Response({"modified": "unchanged"}), Response({"href": "https://example.invalid/file"}),
            Response(b"second"),
        ]
        self.assertEqual((await self.get(force=True)).read_bytes(), b"second")
        self.session.get.side_effect = self.responses(b"third")
        self.assertEqual((await self.get(url="https://example.invalid/other")).read_bytes(), b"third")

    async def test_checksum_mismatch_keeps_old_version(self):
        self.session.get.side_effect = self.responses(b"first")
        await self.get()
        self.session.get.side_effect = [
            Response(self.meta(b"new")), Response({"href": "https://example.invalid/file"}),
            Response(b"different"),
        ]
        with self.assertRaisesRegex(RuntimeError, "изменился"):
            await self.get(force=True)
        self.assertEqual((self.root / "out.xlsx").read_bytes(), b"first")

    async def test_concurrent_download_survives_one_cancelled_client(self):
        gate = asyncio.Event()
        responses = self.responses(b"shared")
        responses[0].gate = gate
        self.session.get.side_effect = responses
        first = asyncio.create_task(self.get(name="first.xlsx"))
        second = asyncio.create_task(self.get(name="second.xlsx", force=True))
        await asyncio.sleep(0)
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        gate.set()
        self.assertEqual((await second).read_bytes(), b"shared")
        self.assertEqual(self.session.get.call_count, 3)
        self.validate.assert_called_once()

    async def test_source_change_during_download_retries_with_fresh_metadata(self):
        self.session.get.side_effect = [
            Response(self.meta(b"old")), Response({"href": "https://example.invalid/file"}),
            Response(b"new"),
        ] + self.responses(b"new")
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()) as sleep:
            path = await yandex_disk.download_public(
                "https://example.invalid/source", self.root / "out.xlsx",
                self.logger, attempts=3, interval=300, cache_dir=self.cache,
                validate=self.validate,
            )
        self.assertEqual(path.read_bytes(), b"new")
        self.assertEqual(self.session.get.call_count, 6)
        self.assertIn(unittest.mock.call(300), sleep.await_args_list)
        self.validate.assert_called_once()

    async def test_invalid_workbook_is_not_retried(self):
        self.session.get.side_effect = self.responses(b"invalid")
        self.validate.side_effect = RuntimeError("invalid workbook")
        with self.assertRaisesRegex(RuntimeError, "invalid workbook"):
            await yandex_disk.download_public(
                "https://example.invalid/source", self.root / "out.xlsx",
                self.logger, attempts=3, cache_dir=self.cache,
                validate=self.validate,
            )
        self.assertEqual(self.session.get.call_count, 3)
        self.assertIsNone(yandex_disk.source_version(
            "https://example.invalid/source", self.cache,
        ))

    async def test_force_joining_cached_request_still_checks_metadata(self):
        self.session.get.side_effect = self.responses(b"cached")
        await self.get()
        self.session.get.side_effect = [Response(self.meta(b"cached"))]
        await asyncio.gather(
            self.get(name="first.xlsx"),
            self.get(name="second.xlsx", force=True),
        )
        self.assertEqual(self.session.get.call_count, 4)

    async def test_exhausted_retry_after_is_respected_by_next_request(self):
        self.session.get.side_effect = [
            Response(status=429, headers={"Retry-After": "600"}),
        ]
        with self.assertRaises(RuntimeError):
            await self.get(force=True)
        self.session.get.side_effect = self.responses(b"recovered")
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()) as sleep:
            await self.get(force=True)
        self.assertTrue(any(
            call.args[0] > 590 for call in sleep.await_args_list
        ))

    async def test_timeout_retry_budget_and_http_date(self):
        until = datetime.now(timezone.utc) + timedelta(minutes=10)
        error = yandex_disk.YandexRequestError(429, format_datetime(until, usegmt=True))
        self.assertGreater(error.delay, 590)
        self.session.get.side_effect = TimeoutError("request timeout")
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "3 попыток"):
                await yandex_disk.download_public(
                    "https://example.invalid/source", self.root / "out.xlsx",
                    self.logger, attempts=3, cache_dir=self.cache,
                )
        self.assertEqual(self.session.get.call_count, 3)

    async def test_background_client_upgrades_shared_retry_budget(self):
        self.session.get.side_effect = [Response(status=503)] * 3
        url = "https://example.invalid/source"
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()):
            results = await asyncio.gather(
                yandex_disk.download_public(
                    url, self.root / "interactive.xlsx", self.logger,
                    attempts=1, interval=0, cache_dir=self.cache,
                ),
                yandex_disk.download_public(
                    url, self.root / "background.xlsx", self.logger,
                    attempts=3, interval=300, cache_dir=self.cache,
                ),
                return_exceptions=True,
            )
        self.assertEqual(self.session.get.call_count, 3)
        for result in results:
            self.assertIsInstance(result, RuntimeError)
            self.assertIn("3 попыток", str(result))

    async def test_python310_asyncio_timeout_retries_cached_and_uncached(self):
        class LegacyAsyncTimeout(Exception):
            pass

        for cache_dir in (None, self.cache):
            with self.subTest(cache_dir=cache_dir):
                self.session.get.reset_mock()
                self.session.get.side_effect = LegacyAsyncTimeout("timeout")
                with patch.object(
                    yandex_disk.asyncio, "TimeoutError", LegacyAsyncTimeout
                ), patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()):
                    with self.assertRaisesRegex(RuntimeError, "3 попыток"):
                        await yandex_disk.download_public(
                            "https://example.invalid/source", self.root / "out.xlsx",
                            self.logger, attempts=3, cache_dir=cache_dir,
                        )
                self.assertEqual(self.session.get.call_count, 3)

    async def test_retry_after_and_terminal_errors(self):
        responses = [Response(status=429, headers={"Retry-After": "600"})]
        self.session.get.side_effect = responses + self.responses(b"retry")
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()) as sleep:
            await yandex_disk.download_public(
                "https://example.invalid/source", self.root / "out.xlsx",
                self.logger, attempts=2, interval=300, cache_dir=self.cache,
                validate=self.validate,
            )
        self.assertIn(unittest.mock.call(600.0), sleep.await_args_list)
        self.session.get.side_effect = [Response(status=404)]
        with self.assertRaisesRegex(RuntimeError, "1 попыток"):
            await self.get(force=True)
        self.session.get.side_effect = [Response(status=503)] * 3
        with patch.object(yandex_disk.asyncio, "sleep", new=AsyncMock()):
            with self.assertRaisesRegex(RuntimeError, "3 попыток"):
                await yandex_disk.download_public(
                    "https://example.invalid/source", self.root / "out.xlsx",
                    self.logger, attempts=3, cache_dir=self.cache, force=True,
                )
