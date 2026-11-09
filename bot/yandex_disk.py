"""Скачивание публичного файла с Яндекс.Диска с retry-политикой."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Callable

import aiohttp
from .secure_fs import open_private_directory

YANDEX_API = "https://cloud-api.yandex.net/v1/disk/public/resources/download"
YANDEX_METADATA_API = "https://cloud-api.yandex.net/v1/disk/public/resources"

RETRY_ATTEMPTS = 3
RETRY_INTERVAL = 300        # 5 минут, согласно ТЗ
HTTP_TIMEOUT_API = 30
HTTP_TIMEOUT_DOWNLOAD = 120
SOURCE_CHECK_SECONDS = 1800


@dataclass
class _SourceRequest:
    force: bool
    attempts: int
    interval: int


_flights: dict[tuple, tuple[asyncio.Task, _SourceRequest]] = {}


class YandexRequestError(RuntimeError):
    def __init__(self, status: int, retry_after: str | None = None):
        super().__init__(f"Яндекс.Диск вернул HTTP {status}")
        self.status = status
        self.delay = 0.0
        if retry_after:
            try:
                self.delay = max(0.0, float(retry_after))
            except ValueError:
                try:
                    self.delay = max(0.0, (
                        parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)
                    ).total_seconds())
                except (ValueError, TypeError, OverflowError):
                    self.delay = 0.0


def _read_private(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
        or info.st_uid != os.geteuid()
    ):
        raise PermissionError(f"Файл расписания небезопасен: {path}")
    return path.read_bytes()


def _write_private(path: Path, content: bytes) -> None:
    directory_fd = open_private_directory(path.parent, create=True)
    os.close(directory_fd)
    _read_private(path)
    fd, name = tempfile.mkstemp(prefix=f".{path.name}-", dir=path.parent)
    temp = Path(name)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = -1
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        temp.unlink(missing_ok=True)


def source_version(public_url: str, root: Path) -> str | None:
    key = hashlib.sha256(public_url.encode()).hexdigest()
    content = _read_private(root / f"{key}.json")
    if content is None:
        return None
    manifest = json.loads(content)
    if not isinstance(manifest, dict):
        raise ValueError("Повреждена версия Excel-кандидата")
    digest = manifest.get("digest")
    if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
        raise ValueError("Повреждена версия Excel-кандидата")
    return digest


async def _request_json(session, url: str, public_url: str) -> dict:
    async with session.get(
        url, params={"public_key": public_url},
        timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_API),
    ) as response:
        if response.status != 200:
            raise YandexRequestError(response.status, response.headers.get("Retry-After"))
        payload = await response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("Некорректный ответ Яндекс.Диска")
    return payload


async def _download_bytes(session, public_url: str) -> bytes:
    link = await _request_json(session, YANDEX_API, public_url)
    href = link.get("href")
    if not isinstance(href, str) or not href:
        raise RuntimeError("В ответе Yandex API нет поля 'href'")
    async with session.get(href) as response:
        if response.status != 200:
            raise YandexRequestError(response.status, response.headers.get("Retry-After"))
        return await response.read()


async def _retry_or_raise(exc, attempt: int, request: _SourceRequest, logger) -> None:
    retryable = (
        not isinstance(exc, YandexRequestError)
        or exc.status == 429 or exc.status >= 500
    )
    logger.warning(
        "Обновление Excel: попытка %s/%s: %s",
        attempt, request.attempts, type(exc).__name__,
    )
    if not retryable or attempt >= request.attempts:
        raise RuntimeError(
            f"Не удалось обновить Excel за {attempt} попыток: {exc}"
        ) from exc
    await asyncio.sleep(max(request.interval, getattr(exc, "delay", 0)))


async def _cached_source(
    public_url: str, root: Path, logger,
    validate: Callable[[Path], object] | None, request: _SourceRequest,
) -> bytes:
    # Same-turn clients can upgrade a cached request to a forced check.
    await asyncio.sleep(0)
    key = hashlib.sha256(public_url.encode()).hexdigest()
    manifest_path = root / f"{key}.json"
    cooldown_path = root / f"{key}.retry"
    raw = _read_private(manifest_path)
    manifest = None
    if raw is not None:
        try:
            manifest = json.loads(raw)
            if not isinstance(manifest, dict):
                raise ValueError("manifest must be an object")
            digest = manifest["digest"]
            if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("invalid digest")
            if not isinstance(manifest.get("checked_at"), (int, float)):
                raise ValueError("invalid check time")
            content = _read_private(root / f"{key}-{digest}.xlsx")
            if content is None:
                manifest = None
            elif hashlib.sha256(content).hexdigest() != digest:
                logger.warning("Checksum локального Excel не совпал; требуется обновление")
                manifest = None
        except (ValueError, KeyError, TypeError):
            logger.warning("Повреждён кэш источника Excel; требуется обновление")
            manifest = None
    if manifest is not None and not request.force and (
        0 <= datetime.now(timezone.utc).timestamp() - manifest["checked_at"] < SOURCE_CHECK_SECONDS
    ):
        return content
    cooldown = _read_private(cooldown_path)
    if cooldown is not None:
        delay = max(0.0, float(cooldown) - datetime.now(timezone.utc).timestamp())
        if delay:
            await asyncio.sleep(delay)
    attempt = 0
    while True:
        attempt += 1
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=HTTP_TIMEOUT_DOWNLOAD)
            ) as session:
                meta = await _request_json(session, YANDEX_METADATA_API, public_url)
                fingerprint = None
                size = meta.get("size")
                for algorithm, length in (("sha256", 64), ("md5", 32)):
                    checksum = meta.get(algorithm)
                    if (
                        isinstance(checksum, str)
                        and re.fullmatch(rf"[a-fA-F0-9]{{{length}}}", checksum)
                        and isinstance(size, int) and not isinstance(size, bool) and size >= 0
                    ):
                        fingerprint = [algorithm, checksum.lower(), size]
                        break
                if fingerprint is None:
                    logger.info("У источника Excel нет checksum/size; файл будет проверен скачиванием")
                if manifest is not None and fingerprint is not None and (
                    fingerprint == manifest.get("fingerprint")
                ):
                    candidate = root / f"{key}-{manifest['digest']}.xlsx"
                else:
                    content = await _download_bytes(session, public_url)
                    if fingerprint is not None:
                        algorithm, checksum, size = fingerprint
                        if len(content) != size or hashlib.new(algorithm, content).hexdigest() != checksum:
                            raise RuntimeError("Файл Excel изменился во время скачивания; повторите загрузку.")
                    digest = hashlib.sha256(content).hexdigest()
                    candidate = root / f"{key}-{digest}.xlsx"
                    _write_private(candidate, content)
                try:
                    if validate is not None:
                        await asyncio.to_thread(validate, candidate)
                except Exception:
                    if manifest is None or candidate.name != f"{key}-{manifest['digest']}.xlsx":
                        candidate.unlink(missing_ok=True)
                    raise
                new_manifest = {
                    "digest": hashlib.sha256(content).hexdigest(),
                    "fingerprint": fingerprint,
                    "checked_at": datetime.now(timezone.utc).timestamp(),
                }
                _write_private(manifest_path, json.dumps(new_manifest).encode())
                cooldown_path.unlink(missing_ok=True)
                if manifest is not None and manifest["digest"] != new_manifest["digest"]:
                    (root / f"{key}-{manifest['digest']}.xlsx").unlink(missing_ok=True)
                return content
        except (aiohttp.ClientError, TimeoutError, YandexRequestError) as exc:
            if isinstance(exc, YandexRequestError) and exc.status == 429:
                _write_private(cooldown_path, str(
                    datetime.now(timezone.utc).timestamp()
                    + max(request.interval, exc.delay)
                ).encode())
            await _retry_or_raise(exc, attempt, request, logger)


async def download_public(
    public_url: str,
    dest: Path,
    debug_logger: logging.Logger,
    attempts: int = RETRY_ATTEMPTS,
    interval: int = RETRY_INTERVAL,
    on_change: Callable[[], None] | None = None,
    *,
    cache_dir: Path | None = None,
    validate: Callable[[Path], object] | None = None,
    force: bool = False,
) -> Path:
    """Скачивает xlsx по публичной ссылке в dest. Возвращает dest."""
    if attempts < 1 or interval < 0:
        raise ValueError("Число попыток должно быть положительным, интервал — неотрицательным")
    if cache_dir is not None:
        directory_fd = open_private_directory(cache_dir, create=True)
        os.close(directory_fd)
        key = (asyncio.get_running_loop(), cache_dir.resolve(), public_url)
        flight = _flights.get(key)
        if flight is None or flight[0].done():
            request = _SourceRequest(force, attempts, interval)
            task = asyncio.create_task(_cached_source(
                public_url, cache_dir, debug_logger, validate, request,
            ))
            _flights[key] = (task, request)

            def finished(done):
                current = _flights.get(key)
                if current is not None and current[0] is done:
                    _flights.pop(key, None)
                if not done.cancelled():
                    done.exception()

            task.add_done_callback(finished)
        else:
            task, request = flight
            request.force |= force
            request.attempts = max(request.attempts, attempts)
            request.interval = max(request.interval, interval)
        content = await asyncio.shield(task)
        previous = _read_private(dest)
        _write_private(dest, content)
        if on_change is not None and previous != content:
            on_change()
        return dest
    timeout = aiohttp.ClientTimeout(total=HTTP_TIMEOUT_DOWNLOAD)
    request = _SourceRequest(force, attempts, interval)

    async with aiohttp.ClientSession(timeout=timeout) as session:
        for attempt in range(1, attempts + 1):
            try:
                content = await _download_bytes(session, public_url)
                previous = _read_private(dest)
                _write_private(dest, content)
                if on_change is not None and previous != content:
                    on_change()
                return dest
            except (aiohttp.ClientError, TimeoutError, YandexRequestError) as exc:
                await _retry_or_raise(exc, attempt, request, debug_logger)
    raise RuntimeError("Не удалось скачать файл Excel")
