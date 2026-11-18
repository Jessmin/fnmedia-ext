"""Passive measurement must reuse only bytes from the exact delivered stream."""
import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import measured_quality as quality


def ts_bytes(value=0):
    return (b"\x47\x1f\xff\x10" + bytes([value]) * 184) * 16


async def pending_done(measurements):
    await asyncio.gather(*measurements._pending.values())


class MeasuredQualityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.measurements = quality.Measurements()
        self.addAsyncCleanup(self.measurements.close)

    async def test_cache_is_exact_and_expires(self):
        a, b, c = ("title", 0, "a"), ("title", 1, "a"), ("title", 0, "b")
        low = quality.MeasuredQuality(1280, 720, "720p")
        high = quality.MeasuredQuality(3840, 1600, "4K")
        clock = SimpleNamespace(monotonic=Mock(return_value=10.0))
        with patch.object(quality, "time", clock), patch.object(
                quality, "_inspect", new=AsyncMock(side_effect=[low, high])) as inspect:
            self.measurements.submit(a, ts_bytes())
            self.measurements.submit(b, ts_bytes(1))
            await pending_done(self.measurements)
            self.assertEqual(self.measurements.get(a), low)
            self.assertEqual(self.measurements.get(b), high)
            self.assertIsNone(self.measurements.get(c))
            self.assertIsNone(self.measurements.reserve(a))
            self.assertEqual(inspect.await_count, 2)
            clock.monotonic.return_value += quality._MEASUREMENT_TTL + 1
            self.assertIsNone(self.measurements.get(a))
            self.assertIsNotNone(self.measurements.reserve(a))

    async def test_failure_cools_down_identical_bytes_only(self):
        name = ("title", 0, "a")
        high = quality.MeasuredQuality(3840, 2160, "4K")
        with patch.object(quality, "_inspect", new=AsyncMock(side_effect=[None, high])) as inspect:
            self.measurements.submit(name, ts_bytes())
            await pending_done(self.measurements)
            self.measurements.submit(name, ts_bytes())
            await pending_done(self.measurements)
            self.assertEqual(inspect.await_count, 1)
            self.assertIsNone(self.measurements.get(name))
            self.measurements.submit(name, ts_bytes(1))
            await pending_done(self.measurements)
            self.assertEqual(inspect.await_count, 2)
            self.assertEqual(self.measurements.get(name), high)

    async def test_bounded_collection_cancellation_and_probe_slots(self):
        entered = asyncio.Event()

        async def inspect(data):
            self.assertLessEqual(len(data), quality.MAX_SAMPLE_BYTES)
            self.assertEqual(len(data) % 188, 0)
            entered.set()
            await asyncio.Event().wait()

        with patch.object(quality, "_inspect", new=AsyncMock(side_effect=inspect)) as probe:
            a = self.measurements.reserve("a")
            b = self.measurements.reserve("b")
            self.assertIsNone(self.measurements.reserve("a"))
            self.assertIsNone(self.measurements.reserve("c"))
            b.feed(ts_bytes())
            b.cancel()
            b.finish()
            probe.assert_not_awaited()
            a.feed(ts_bytes() * 200)
            a.feed(ts_bytes())
            self.assertEqual(len(a._data), quality.MAX_SAMPLE_BYTES)
            a.finish()
            a.finish()
            await asyncio.wait_for(entered.wait(), 1.0)
            self.assertIsNone(self.measurements.reserve("a"))
            c = self.measurements.reserve("c")
            self.assertIsNotNone(c)
            self.assertIsNone(self.measurements.reserve("d"))
            await self.measurements.close()
            c.finish()
            self.assertFalse(self.measurements._pending)
            self.assertIsNotNone(self.measurements.reserve("a"))
            probe.assert_awaited_once()

    async def test_rejects_non_ts_and_short_sample(self):
        with patch.object(quality, "_inspect", new=AsyncMock()) as probe:
            self.measurements.submit("image", b"\x89PNG\r\n\x1a\n" + ts_bytes())
            self.measurements.submit("fmp4", b"\x00\x00\x00\x20ftyp" + ts_bytes())
            self.measurements.submit("short", ts_bytes()[:188])
            await pending_done(self.measurements)
        probe.assert_not_awaited()

    def test_ignores_attached_picture_and_conflicting_video_tracks(self):
        video = {"codec_type": "video", "codec_name": "hevc", "width": 1920, "height": 1080}
        other = {**video, "width": 3840, "height": 2160}
        cover = {**other, "disposition": {"attached_pic": 1}}

        def inspect(*streams):
            return quality._quality_from_probe(json.dumps({"streams": streams}).encode())

        self.assertEqual(inspect(cover, video), quality.MeasuredQuality(1920, 1080, "1080p"))
        self.assertIsNone(inspect(cover))
        self.assertIsNone(inspect(video, other))

    async def test_probe_timeout_kills_and_drains_pipe(self):
        async def communicate(data=None):
            if data is not None:
                await asyncio.Event().wait()
            return b"", b""

        proc = SimpleNamespace(communicate=AsyncMock(side_effect=communicate),
                               kill=Mock(), returncode=None)
        with patch.object(quality.shutil, "which", return_value="ffprobe"), patch.object(
                quality.asyncio, "create_subprocess_exec", new=AsyncMock(return_value=proc)
        ) as spawn, patch.object(quality, "_PROBE_TIMEOUT", 0.01):
            self.measurements.submit("title", ts_bytes())
            await pending_done(self.measurements)
        args = spawn.call_args.args
        self.assertEqual(args[args.index("-protocol_whitelist") + 1], "pipe")
        self.assertEqual(args[args.index("-i") + 1], "pipe:0")
        proc.kill.assert_called_once()
        self.assertEqual(proc.communicate.await_count, 2)
        self.assertIsNone(self.measurements.get("title"))

    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"),
                         "real fixture needs ffmpeg and ffprobe")
    async def test_real_avc_and_hevc_transport_streams(self):
        fixtures = [
            ("4K-name-only", "1280x720", "libx264", [], quality.MeasuredQuality(1280, 720, "720p")),
            ("unnamed-uhd", "3840x1600", "libx265",
             ["-x265-params", "log-level=error:pools=none:frame-threads=1"],
             quality.MeasuredQuality(3840, 1600, "4K")),
        ]
        for key, size, encoder, extra, expected in fixtures:
            with self.subTest(encoder=encoder):
                rendered = await asyncio.to_thread(
                    subprocess.run,
                    ["ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi",
                     "-i", f"color=black:s={size}:r=1", "-frames:v", "1", "-an", "-c:v",
                     encoder, "-preset", "ultrafast", "-threads", "1", *extra, "-f",
                     "mpegts", "pipe:1"], capture_output=True, timeout=15,
                )
                if b"Unknown encoder" in rendered.stderr:
                    self.skipTest(f"ffmpeg has no {encoder}")
                self.assertEqual(rendered.returncode, 0, rendered.stderr)
                self.measurements.submit(key, rendered.stdout)
                await pending_done(self.measurements)
                self.assertEqual(self.measurements.get(key), expected)


if __name__ == "__main__":
    unittest.main()
