"""Measure already delivered MPEG-TS bytes without requesting any media."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass
import hashlib
import json
import shutil
import time


MAX_SAMPLE_BYTES = 512 * 1024
_MAX_ACTIVE = 2
_MAX_ENTRIES = 256
_MEASUREMENT_TTL = 15 * 60.0
_FAILURE_TTL = 30.0
_PROBE_TIMEOUT = 3.0
_TS_PACKET_BYTES = 188


@dataclass(frozen=True)
class MeasuredQuality:
    width: int
    height: int
    label: str


def _quality_from_probe(output: bytes) -> MeasuredQuality | None:
    streams = json.loads(output).get("streams", [])
    sizes = set()
    for stream in streams:
        if (stream.get("codec_type") != "video"
                or stream.get("codec_name") not in ("h264", "hevc")
                or (stream.get("disposition") or {}).get("attached_pic")):
            continue
        width, height = stream.get("width"), stream.get("height")
        if (type(width) is int and 320 <= width <= 16384
                and type(height) is int and 240 <= height <= 16384):
            sizes.add((width, height))
    # A multiplex with conflicting video tracks cannot identify the one in use.
    if len(sizes) != 1:
        return None
    width, height = sizes.pop()
    long_edge, short_edge = max(width, height), min(width, height)
    if long_edge >= 7600 and short_edge >= 3000:
        label = "8K"
    elif long_edge >= 3800 and short_edge >= 1440:
        label = "4K"
    else:
        label = f"{height}p"
    return MeasuredQuality(width, height, label)


def _is_mpeg_ts(data: bytearray) -> bool:
    return len(data) >= 3 * _TS_PACKET_BYTES and all(
        data[offset] == 0x47 and data[offset + 3] & 0x30
        for offset in range(0, 3 * _TS_PACKET_BYTES, _TS_PACKET_BYTES)
    )


async def _inspect(data: bytearray) -> MeasuredQuality | None:
    binary = shutil.which("ffprobe")
    if not binary:
        return None
    proc = await asyncio.create_subprocess_exec(
        binary, "-v", "error", "-max_alloc", str(32 * 1024 * 1024),
        "-protocol_whitelist", "pipe", "-f", "mpegts",
        "-probesize", str(MAX_SAMPLE_BYTES), "-analyzeduration", "1000000",
        "-i", "pipe:0", "-show_entries",
        "stream=codec_type,codec_name,width,height:stream_disposition=attached_pic",
        "-of", "json", stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(proc.communicate(data), _PROBE_TIMEOUT)
        return _quality_from_probe(output) if proc.returncode == 0 else None
    finally:
        if proc.returncode is None:
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            await proc.communicate()


class Sample:
    """One bounded sample. Finish only after successful delivery."""

    def __init__(self, owner: Measurements, key: Hashable):
        self._owner = owner
        self._key = key
        self._data: bytearray | None = bytearray()

    def feed(self, data: bytes) -> None:
        if self._data is None:
            return
        remaining = MAX_SAMPLE_BYTES - len(self._data)
        if remaining:
            self._data.extend(memoryview(data)[:remaining])

    def finish(self) -> None:
        data, self._data = self._data, None
        if data is not None:
            self._owner._finish(self, data)

    def cancel(self) -> None:
        if self._data is not None:
            self._data = None
            self._owner._release(self)


class Measurements:
    """Bounded process-local results keyed by the caller's exact episode and line."""

    def __init__(self):
        self._cache: OrderedDict[Hashable, tuple[float, MeasuredQuality]] = OrderedDict()
        self._failures: OrderedDict[tuple[Hashable, bytes], float] = OrderedDict()
        self._collecting: dict[Hashable, Sample] = {}
        self._pending: dict[Hashable, asyncio.Task] = {}
        self._closing = False

    def get(self, key: Hashable) -> MeasuredQuality | None:
        hit = self._cache.get(key)
        if hit is not None and hit[0] > time.monotonic():
            self._cache.move_to_end(key)
            return hit[1]
        self._cache.pop(key, None)
        return None

    def reserve(self, key: Hashable) -> Sample | None:
        if self._closing or key in self._collecting or key in self._pending:
            return None
        if len(self._collecting) + len(self._pending) >= _MAX_ACTIVE or self.get(key):
            return None
        sample = Sample(self, key)
        self._collecting[key] = sample
        return sample

    def submit(self, key: Hashable, data: bytes) -> None:
        sample = self.reserve(key)
        if sample is not None:
            sample.feed(data)
            sample.finish()

    def _release(self, sample: Sample) -> bool:
        if self._collecting.get(sample._key) is not sample:
            return False
        del self._collecting[sample._key]
        return True

    def _finish(self, sample: Sample, data: bytearray) -> None:
        if not self._release(sample) or self._closing or not _is_mpeg_ts(data):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        key = sample._key
        task = loop.create_task(self._record(key, data))
        self._pending[key] = task

        def finished(done: asyncio.Task) -> None:
            if self._pending.get(key) is done:
                del self._pending[key]

        task.add_done_callback(finished)

    async def _record(self, key: Hashable, data: bytearray) -> None:
        # A failed sample should not block a different segment of the same line.
        # The digest avoids retaining media bytes or source credentials in the cache.
        del data[len(data) - len(data) % _TS_PACKET_BYTES:]
        failed_key = (key, hashlib.blake2b(data, digest_size=16).digest())
        until = self._failures.pop(failed_key, 0.0)
        if until > time.monotonic():
            self._failures[failed_key] = until
            return
        try:
            info = await _inspect(data)
        except Exception:
            info = None
        if info is not None:
            self._cache[key] = (time.monotonic() + _MEASUREMENT_TTL, info)
            self._cache.move_to_end(key)
            if len(self._cache) > _MAX_ENTRIES:
                self._cache.popitem(last=False)
        else:
            self._failures[failed_key] = time.monotonic() + _FAILURE_TTL
            if len(self._failures) > _MAX_ENTRIES:
                self._failures.popitem(last=False)

    async def close(self) -> None:
        self._closing = True
        try:
            for sample in tuple(self._collecting.values()):
                sample.cancel()
            tasks = tuple(self._pending.values())
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            self._pending.clear()
            self._cache.clear()
            self._failures.clear()
        finally:
            self._closing = False


measurements = Measurements()
