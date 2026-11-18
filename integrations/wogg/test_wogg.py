import os
import unittest
from unittest.mock import patch

from wogg import SearchResult, Share, WoggError, catalog_origin, detail_shares, parse_detail_shares, search


class PublicCatalogTests(unittest.TestCase):
    def test_search_cards_keep_season_and_metadata(self):
        html = """
        <div class="module-search-item">
          <div class="video-cover">
            <img data-src="https://posters.example.org/gimg?b=2&amp;src=cover.webp">
          </div>
          <div class="video-info-header">
            <a class="video-serial" href="/voddetail/100.html">更新至第 6 期</a>
            <h3><a href="/voddetail/100.html">示例节目 <em>第四季</em></a></h3>
            <a class="tag-link" href="/vodtype/4.html"><span>综艺</span></a>
            <a href="/vodsearch/-------------2026.html">2026</a>
          </div>
        </div>
        <div class="module-search-item">
          <img src="https://unrelated.example/img.png">
          <h3><a href="/voddetail/101.html">示例节目 第三季</a></h3>
        </div>
        <div class="module-search-item">
          <h3><a href="https://unrelated.example/voddetail/102.html">外链</a></h3>
        </div>
        """
        origin = "https://catalog.example.org"
        settings = {"WOGG_ORIGIN": "HTTPS://Catalog.Example.org:443/",
                    "WOGG_POSTER_HOSTS": "posters.example.org"}
        with patch.dict(os.environ, settings, clear=True), patch("wogg._get", return_value=html) as get:
            rows = search("示例 节目")
            get.assert_called_once_with(origin, "/index.php/vodsearch/-------------.html?wd="
                                               "%E7%A4%BA%E4%BE%8B+%E8%8A%82%E7%9B%AE")
        self.assertEqual(rows, [
            SearchResult("100", "示例节目 第四季", "https://posters.example.org/gimg?b=2&src=cover.webp",
                         "2026", "综艺", "更新至第 6 期"),
            SearchResult("101", "示例节目 第三季"),
        ])

    def test_no_origin_default_or_unsafe_fallback(self):
        for value in ("", "http://catalog.example.org", "https://catalog.example.org/path",
                      "https://catalog.example.org/?", "https://catalog.example.org/#",
                      "https://user:password@catalog.example.org", "https://catalog.example.org:444",
                      "https://127.0.0.1", "https://[::1]", "https://localhost", "https://foo_bar.example.org",
                      "https://catalog.example.org\n"):
            with (self.subTest(value=value), patch.dict(os.environ, {"WOGG_ORIGIN": value}, clear=True),
                  patch("wogg._get") as get, self.assertRaises(WoggError)):
                search("示例")
            get.assert_not_called()
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(WoggError):
            catalog_origin()

    def test_same_origin_and_poster_sites_are_separate(self):
        html = """
        <div class="module-search-item">
          <img src="https://posters.example.org/other.webp" data-src="/cover.webp">
          <h3><a href="https://catalog.example.org/voddetail/101.html">同站链接</a></h3>
        </div>
        <div class="module-search-item">
          <h3><a href="https://another.example.org/voddetail/102.html">其他站点</a></h3>
        </div>
        """
        settings = {"WOGG_ORIGIN": "https://catalog.example.org"}
        with patch.dict(os.environ, settings, clear=True), patch("wogg._get", return_value=html):
            self.assertEqual(search("示例"), [SearchResult("101", "同站链接", settings["WOGG_ORIGIN"] + "/cover.webp")])

    def test_detail_uses_configured_origin(self):
        origin = "https://another.example.org"
        with patch.dict(os.environ, {"WOGG_ORIGIN": origin}, clear=True), patch("wogg._get", return_value="") as get:
            self.assertEqual(detail_shares("101"), [])
            get.assert_called_once_with(origin, "/voddetail/101.html")

    def test_detail_keeps_only_canonical_quark_share_links(self):
        html = """
        <a class="module-row-text copy" data-clipboard-text="https://pan.quark.cn/s/Demo1234">
          <h4>示例节目</h4>
        </a>
        <a class="module-row-text copy"
           data-clipboard-text="https://pan.quark.cn/s/Demo1234?from=other#/list/share?pwd=AbC1">
          <h4>示例节目 &lt;完整版&gt;</h4>
        </a>
        <a class="module-row-text" href="https://pan.quark.cn.unrelated.example/s/Fake1234">
          <h4>外链</h4>
        </a>
        <a class="module-row-text" href="https://pan.quark.cn@unrelated.example/s/Fake1234">
          <h4>外链</h4>
        </a>
        <a class="module-row-text" href="https://pan.quark.cn/s/Demo5678?pwd=AbC1#pwd=AbC2">
          <h4>提取码有冲突</h4>
        </a>
        <a href="https://pan.quark.cn/s/Ignored1">非资源行</a>
        """
        self.assertEqual(parse_detail_shares(html), [
            Share("示例节目 <完整版>", "https://pan.quark.cn/s/Demo1234?pwd=AbC1"),
        ])


if __name__ == "__main__":
    unittest.main()
