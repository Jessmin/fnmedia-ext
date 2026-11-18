"""The same HLS response feeds the player and the optional quality display."""
from __future__ import annotations

import asyncio
from pathlib import Path
import shutil
import subprocess
import sys
import unittest
from unittest.mock import AsyncMock, patch

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "proxy"))
import app
import live


@unittest.skipUnless(shutil.which("ffprobe") and shutil.which("ffmpeg"),
                     "sample requires FFmpeg")
class MeasuredQualityIntegrationTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.segment = subprocess.check_output([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-f", "lavfi", "-i",
            "color=c=blue:s=3840x2160:r=24", "-frames:v", "2", "-c:v", "libx264",
            "-threads", "2", "-preset", "ultrafast", "-f", "mpegts", "pipe:1",
        ], timeout=15)

    async def asyncTearDown(self):
        await app.measurements.close()
        live.reset()

    async def test_delivery_keeps_bytes_and_reports_the_matching_line_only(self):
        current = {"online_guid": "title", "index": 4, "line": "src::hq",
                   "url": "https://feed.test/v.m3u8", "headers": {}, "label": "线路一"}
        other = dict(current, line="src2::auto", label="线路二")
        reqs = AsyncMock(return_value=httpx.Response(200, stream=httpx.ByteStream(self.segment)))
        with patch.object(app, "_pick_episode", return_value=current), \
             patch.object(app, "_await_pending_chart", new=AsyncMock()), \
             patch.object(app, "_live_title_for", return_value="第 5 集"), \
             patch.object(app, "_line_choices", return_value=[current, other]), \
             patch.object(app, "ext_get_stream_retry", new=reqs), \
             patch.object(app.linepredict, "record_seg") as record_seg:
            key = app._measured_quality_key(current)
            before = app._qualities(current)
            self.assertTrue(all("待检测" in item["resolution"] for item in before))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                         base_url="http://test") as client:
                response = await client.get("/v/media/online/sample/seg", params={
                    "u": app.b64e("https://feed.test/segment.ts"), "index": "4",
                    "line": "src::hq"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, self.segment)
                for _ in range(80):
                    if app.measurements.get(key):
                        break
                    await asyncio.sleep(0.05)
                snapshot = (await client.get("/v/api/ext/live")).json()["data"]
            self.assertEqual(reqs.await_count, 1)
            self.assertEqual(record_seg.call_args.kwargs["nbytes"], len(self.segment))
            self.assertEqual(snapshot["devices"][0]["session"]["measured_quality"],
                             {"width": 3840, "height": 2160, "label": "4K"})
            after = app._qualities(current)
            self.assertEqual([row["bitrate"] for row in before],
                             [row["bitrate"] for row in after])
            self.assertIn("实测 4K", after[0]["resolution"])
            self.assertIn("待检测", after[1]["resolution"])
            self.assertIn("待检测", app._quality_line_label(dict(current, index=5)))

    async def test_wrong_line_or_nonzero_range_stays_unverified(self):
        target = {"online_guid": "title", "index": 4, "line": "src::hq",
                  "url": "https://feed.test/v.m3u8", "headers": {}}
        responses = AsyncMock(side_effect=[
            httpx.Response(200, stream=httpx.ByteStream(self.segment)),
            httpx.Response(206, stream=httpx.ByteStream(self.segment),
                           headers={"Content-Range": "bytes 100-200/1000"}),
        ])
        with patch.object(app, "_pick_episode", return_value=target), \
             patch.object(app, "_await_pending_chart", new=AsyncMock()), \
             patch.object(app, "ext_get_stream_retry", new=responses), \
             patch.object(app.measurements, "reserve") as reserve, \
             patch.object(app.linepredict, "record_seg"):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app.app),
                                         base_url="http://test") as client:
                params = {"u": app.b64e("https://feed.test/segment.ts"), "index": "4"}
                await client.get("/v/media/online/sample/seg",
                                 params=dict(params, line="another::line"))
                await client.get("/v/media/online/sample/seg",
                                 params=dict(params, line="src::hq"))
            reserve.assert_not_called()
