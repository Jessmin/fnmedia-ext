"""T4 workflow and destination checks without touching Quark storage."""
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import httpx

import bridge


class ByteStream(httpx.AsyncByteStream):
    async def __aiter__(self):
        yield b"mp4 sample"


class BridgeTests(unittest.TestCase):
    def setUp(self):
        for cache in (bridge.search_cache, bridge.titles, bridge.detail_cache,
                      bridge.saved, bridge.resources):
            cache.values.clear()
        self.tmp = self.enterContext(TemporaryDirectory())
        self.enterContext(patch.object(bridge, "STATE", Path(self.tmp)))
        self.enterContext(patch.dict(os.environ, {"WOGG_ORIGIN": "https://catalog.example.org"}))
        self.client = self.enterContext(TestClient(bridge.app))

    def test_only_getting_selected_media_transfers_the_exact_file(self):
        show = SimpleNamespace(id="2326", title="测试 第四季", poster="", year="2026",
                               category="综艺", remarks="更新至特辑")
        share = SimpleNamespace(url="https://pan.quark.cn/s/example?pwd=abcd", label="夸克")
        rows = [{"fid": "ep01", "name": "第1期 720p.mp4", "size": 100},
                {"fid": "ep02", "name": "第2期 2160p.mkv", "size": 250,
                 "width": 3840, "height": 2160}]
        async def send(request, stream):
            self.assertEqual(request.url.params["fid"], "saved02")
            self.assertEqual(request.headers["range"], "bytes=0-9")
            return httpx.Response(206, headers={"content-range": "bytes 0-9/50",
                                               "content-type": "video/x-matroska"},
                                  stream=ByteStream())

        with patch.object(bridge.wogg, "search", return_value=[show]), \
             patch.object(bridge.wogg, "detail_shares", return_value=[share]), \
             patch.object(bridge.quark_bridge, "list_videos", return_value=rows), \
             patch.object(bridge.quark_bridge, "stage_video", return_value="saved02") as stage, \
             patch.object(bridge, "local_quark_account", return_value="account-one"), \
             patch.object(bridge.app.state.ext, "send", new=AsyncMock(side_effect=send)):
            search = self.client.get("/t4", params={"wd": "测试"}).json()
            self.assertEqual(search["list"][0]["t4_source"], "玩偶哥哥")
            self.assertEqual(search["list"][0]["vod_remarks"], "玩偶哥哥 · 夸克")
            detail = self.client.get("/t4", params={"ids": "2326", "ac": "detail"}).json()
            entries = detail["list"][0]["vod_play_url"].split("#")
            ref = entries[1].split("$")[1]
            self.assertNotIn("quark.cn", str(detail))
            self.assertNotIn("源文件", entries[0])
            self.assertIn("2160p 【源文件 3840×2160】", entries[1])
            play = self.client.get("/t4", params={"play": ref}).json()
            self.assertTrue(play["url"].endswith(f"/media/{ref}"))
            stage.assert_not_called()

            media = self.client.get(f"/media/{ref}", headers={"Range": "bytes=0-9"})
            self.assertEqual(media.status_code, 206)
            self.assertEqual(media.headers["Content-Range"], "bytes 0-9/50")
            self.assertEqual(media.content, b"mp4 sample")
            stage.assert_called_once_with(share.url, "ep02", "account-one")

    def test_arbitrary_urls_and_unknown_references_do_not_stage(self):
        with patch.object(bridge.quark_bridge, "stage_video") as stage, \
             patch.object(bridge, "local_quark_account", return_value="account-one"):
            self.assertEqual(self.client.get("/t4", params={"play": "x"}).status_code, 404)
            self.assertEqual(self.client.get("/media/" + "1" * 40).status_code, 404)
            self.assertFalse(bridge.cdn_url("https://files.quark.cn.example/a"))
            self.assertFalse(bridge.cdn_url("https://files.quark.cn@localhost/a"))
            self.assertFalse(bridge.cdn_url("http://files.quark.cn/a"))
            self.assertTrue(bridge.cdn_url("https://files.quark.cn/a"))
            stage.assert_not_called()

    def test_episode_list_cache_is_scoped_to_the_active_login(self):
        share = SimpleNamespace(url="https://pan.quark.cn/s/example", label="夸克")
        first = [{"fid": "ep01", "name": "第一集.mp4", "size": 100}]
        second = [{"fid": "ep02", "name": "第二集.mp4", "size": 200}]
        with patch.object(bridge.wogg, "detail_shares", return_value=[share]), \
             patch.object(bridge.quark_bridge, "list_videos", side_effect=[first, second]) as listing, \
             patch.object(bridge, "local_quark_account", return_value="account-one") as account, \
             patch.object(bridge.quark_bridge, "stage_video") as stage:
            url = "/t4?ids=100"
            play_url = self.client.get(url).json()["list"][0]["vod_play_url"]
            self.assertIn("第一集", play_url)
            ref = play_url.split("$")[1]
            self.assertEqual(self.client.get(url).status_code, 200)
            account.return_value = "account-two"
            self.assertIn("第二集", self.client.get(url).json()["list"][0]["vod_play_url"])
            self.assertEqual(self.client.get("/t4", params={"play": ref}).status_code, 404)
            self.assertEqual(self.client.get("/media/" + ref).status_code, 404)
            self.assertEqual(listing.call_count, 2)
            stage.assert_not_called()

    def test_lite_without_quark_module_returns_a_clear_error(self):
        with patch.object(bridge.quark_bridge.importlib, "import_module", side_effect=ModuleNotFoundError):
            response = self.client.get("/t4?ids=100")
        self.assertEqual(response.status_code, 503)
        self.assertIn("官方完整 FPK", response.json()["detail"])

    def test_long_quark_playlist_rewrites_pollable_segments(self):
        segment_count = 1113
        signed_query = "x" * 620
        urls = [f"https://files.quark.cn/{i}.ts?signature={signed_query}"
                for i in range(segment_count)]
        playlist = "#EXTM3U\n#EXT-X-TARGETDURATION:6\n" + "".join(
            f"#EXTINF:6,\n{url}\n" for url in urls) + "#EXT-X-ENDLIST\n"
        self.assertGreater(len(playlist), 512 * 1024)
        upstream = httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                                  content=playlist)
        with patch.object(bridge, "local_quark_account", return_value="account-one"), \
             patch.object(bridge, "saved_fid", return_value="saved-episode"), \
             patch.object(bridge.app.state.ext, "send", new=AsyncMock(return_value=upstream)):
            response = self.client.get("/media/" + "1" * 40)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("signature=", response.text)
        links = [line for line in response.text.splitlines() if line.startswith(bridge.ORIGIN)]
        self.assertEqual(len(links), segment_count)
        near_end_of_episode = time.monotonic() + segment_count * 6 - 1
        with patch.object(bridge.time, "monotonic", return_value=near_end_of_episode):
            for i in (0, segment_count // 2, segment_count - 1):
                key = links[i].rsplit("/", 1)[-1]
                self.assertEqual(bridge.resources.get(key), ("account-one", urls[i]))

    def test_playlist_above_the_bounded_limit_is_rejected(self):
        upstream = httpx.Response(200, headers={"content-type": "application/vnd.apple.mpegurl"},
                                  content=b"#EXTM3U\n" + b"x" * bridge.MAX_MANIFEST)
        with patch.object(bridge, "local_quark_account", return_value="account-one"), \
             patch.object(bridge, "saved_fid", return_value="saved-episode"), \
             patch.object(bridge.app.state.ext, "send", new=AsyncMock(return_value=upstream)):
            response = self.client.get("/media/" + "1" * 40)

        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"], "夸克播放清单过大")
