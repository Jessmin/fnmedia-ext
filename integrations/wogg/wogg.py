"""Read the public Wogg catalog and its published Quark share links."""

from __future__ import annotations

from dataclasses import dataclass
from html.parser import HTMLParser
import http.client
import os
import re
import time
from urllib.parse import parse_qs, urlencode, urljoin, urlsplit
from urllib.request import build_opener, HTTPRedirectHandler, ProxyHandler, Request


_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST = rf"(?:{_LABEL}\.)+[a-z](?:[a-z0-9-]{{0,61}}[a-z0-9])?"
_ORIGIN = re.compile(rf"https://({_HOST})(?::443)?/?", re.ASCII | re.IGNORECASE)
_POSTER_HOST = re.compile(_HOST, re.ASCII | re.IGNORECASE)
_DETAIL_ID = re.compile(r"/(?:index\.php/)?voddetail/([0-9]{1,12})\.html")
_YEAR = re.compile(r"/(?:index\.php/)?vodsearch/-{13}((?:19|20)[0-9]{2})\.html")
_CATEGORY = re.compile(r"/(?:index\.php/)?vodtype/[0-9]+\.html")
_SHARE_ID = re.compile(r"/s/([A-Za-z0-9]{4,32})/?")
_CODE = re.compile(r"[A-Za-z0-9]{1,6}")
_MAX_HTML = 512 * 1024
_MAX_RESULTS = 32


class WoggError(RuntimeError):
    """A public catalog request failed or its response could not be used."""


def catalog_origin() -> str:
    value = os.environ.get("WOGG_ORIGIN", "")
    match = _ORIGIN.fullmatch(value)
    if not match or len(match[1]) > 253:
        raise WoggError("请设置 WOGG_ORIGIN，填写仅含 HTTPS 域名的站点地址")
    return "https://" + match[1].lower()


def _poster_hosts(origin: str) -> set[str]:
    hosts = {urlsplit(origin).netloc}
    for entry in os.environ.get("WOGG_POSTER_HOSTS", "").split(","):
        host = entry.strip().lower()
        if not host:
            continue
        if not _POSTER_HOST.fullmatch(host) or len(host) > 253:
            raise WoggError("WOGG_POSTER_HOSTS 需填写逗号分隔的域名")
        hosts.add(host)
    return hosts


@dataclass(frozen=True)
class SearchResult:
    id: str
    title: str
    poster: str = ""
    year: str = ""
    category: str = ""
    remarks: str = ""


@dataclass(frozen=True)
class Share:
    label: str
    url: str


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _get(origin: str, path: str) -> str:
    request = Request(origin + path, headers={
        "User-Agent": "Mozilla/5.0",
        "Accept": "text/html",
        "Accept-Encoding": "identity",
    })
    deadline = time.monotonic() + 8
    try:
        with build_opener(ProxyHandler({}), _NoRedirect()).open(request, timeout=4) as response:
            if response.headers.get("cf-mitigated") == "challenge":
                raise WoggError("玩偶哥哥站点要求访问验证")
            if response.headers.get_content_type() != "text/html":
                raise WoggError("玩偶哥哥返回了非网页响应")
            if response.headers.get("Content-Encoding", "identity") != "identity":
                raise WoggError("玩偶哥哥返回了不支持的编码")
            raw = bytearray()
            while time.monotonic() < deadline:
                chunk = response.read1(min(32768, _MAX_HTML + 1 - len(raw)))
                if not chunk:
                    return raw.decode("utf-8", errors="replace")
                raw.extend(chunk)
                if len(raw) > _MAX_HTML:
                    raise WoggError("玩偶哥哥网页超过读取上限")
            raise WoggError("读取玩偶哥哥网页超时")
    except (OSError, http.client.HTTPException) as exc:
        if getattr(exc, "code", None) in (401, 403, 429):
            raise WoggError("玩偶哥哥站点暂时拒绝请求") from exc
        raise WoggError("无法读取玩偶哥哥公开网页") from exc


def _safe_url(raw: str, allowed: set[str], origin: str = ""):
    try:
        raw = raw.strip()
        url = urlsplit(urljoin(origin + "/", raw) if origin else raw)
        if (url.scheme == "https" and url.hostname in allowed
                and url.username is None and url.password is None and url.port in (None, 443)):
            return url
    except ValueError:
        pass
    return None


def _clean(value: str, limit: int = 160) -> str:
    return " ".join(value.split())[:limit]


class _SearchParser(HTMLParser):
    def __init__(self, origin: str, poster_hosts: set[str]):
        super().__init__()
        self.origin = origin
        self.catalog_hosts = {urlsplit(origin).netloc}
        self.poster_hosts = poster_hosts
        self.rows: dict[str, SearchResult] = {}
        self.card: dict | None = None
        self.div_depth = 0
        self.in_heading = False
        self.anchor: tuple[str, list[str]] | None = None

    def handle_starttag(self, tag, attrs):
        attrs = {key: value or "" for key, value in attrs}
        classes = attrs.get("class", "").split()
        if tag == "div":
            if self.card is not None:
                self.div_depth += 1
            elif "module-search-item" in classes and len(self.rows) < _MAX_RESULTS:
                self.card = {}
                self.div_depth = 1
        if self.card is None:
            return
        if tag == "h3":
            self.in_heading = True
        if tag == "img" and not self.card.get("poster"):
            for key in ("data-src", "src"):
                value = attrs.get(key, "")
                poster = _safe_url(value, self.poster_hosts, self.origin) if value else None
                if poster and poster.path not in ("", "/") and len(value) <= 2048:
                    self.card["poster"] = poster._replace(fragment="").geturl()
                    break
        if tag != "a":
            return
        href = attrs.get("href", "")
        url = _safe_url(href, self.catalog_hosts, self.origin) if href else None
        if not url:
            return
        if self.in_heading and (match := _DETAIL_ID.fullmatch(url.path)):
            self.card["id"] = match[1]
            self.anchor = ("title", [])
        elif "video-serial" in classes and _DETAIL_ID.fullmatch(url.path):
            self.anchor = ("remarks", [])
        elif match := _YEAR.fullmatch(url.path):
            self.card["year"] = match[1]
        elif "tag-link" in classes and _CATEGORY.fullmatch(url.path):
            self.anchor = ("category", [])

    def handle_data(self, data):
        if self.anchor:
            self.anchor[1].append(data)

    def handle_endtag(self, tag):
        if self.card is None:
            return
        if tag == "a" and self.anchor:
            key, text = self.anchor
            self.card[key] = _clean("".join(text))
            self.anchor = None
        if tag == "h3":
            self.in_heading = False
        if tag == "div":
            self.div_depth -= 1
            if self.div_depth == 0:
                if self.card.get("id") and self.card.get("title"):
                    self.rows.setdefault(self.card["id"], SearchResult(**self.card))
                self.card = None
                self.anchor = None
                self.in_heading = False


def _quark_share(raw: str) -> tuple[str, str] | None:
    url = _safe_url(raw, {"pan.quark.cn"})
    match = _SHARE_ID.fullmatch(url.path) if url else None
    if not match:
        return None
    try:
        values = parse_qs(url.query, max_num_fields=24)
        fragment = parse_qs(url.fragment.split("?", 1)[-1], max_num_fields=24)
    except ValueError:
        return None
    codes = {item for pair in (values, fragment) for key in ("pwd", "passcode")
             for item in pair.get(key, ())}
    if len(codes) > 1 or any(not _CODE.fullmatch(item) for item in codes):
        return None
    code = next(iter(codes), "")
    share = "https://pan.quark.cn/s/" + match[1]
    if code:
        share += "?pwd=" + code
    return match[1], share


class _ShareParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: dict[str, Share] = {}
        self.entry: tuple[str, str] | None = None
        self.label: list[str] = []
        self.in_label = False

    def handle_starttag(self, tag, attrs):
        if tag == "a" and len(self.rows) < _MAX_RESULTS:
            attrs = dict(attrs)
            if "module-row-text" in (attrs.get("class") or "").split():
                raw = attrs.get("data-clipboard-text") or attrs.get("href") or ""
                if len(raw) <= 2048:
                    self.entry = _quark_share(raw)
                    self.label = []
        if self.entry and tag == "h4":
            self.in_label = True

    def handle_data(self, data):
        if self.in_label:
            self.label.append(data)

    def handle_endtag(self, tag):
        if tag == "h4":
            self.in_label = False
        if tag == "a" and self.entry:
            share_id, url = self.entry
            label = _clean("".join(self.label)) or "夸克网盘"
            previous = self.rows.get(share_id)
            if previous is None or ("?pwd=" in url and "?pwd=" not in previous.url):
                self.rows[share_id] = Share(label, url)
            self.entry = None
            self.in_label = False


def parse_search(html: str, origin: str, poster_hosts: set[str]) -> list[SearchResult]:
    parser = _SearchParser(origin, poster_hosts)
    parser.feed(html)
    parser.close()
    return list(parser.rows.values())


def parse_detail_shares(html: str) -> list[Share]:
    parser = _ShareParser()
    parser.feed(html)
    parser.close()
    return list(parser.rows.values())


def search(keyword: str) -> list[SearchResult]:
    keyword = " ".join(keyword.split())
    if not keyword or len(keyword) > 80:
        return []
    origin = catalog_origin()
    poster_hosts = _poster_hosts(origin)
    path = "/index.php/vodsearch/-------------.html?" + urlencode({"wd": keyword})
    return parse_search(_get(origin, path), origin, poster_hosts)


def detail_shares(media_id: str) -> list[Share]:
    if not re.fullmatch(r"[0-9]{1,12}", media_id):
        raise ValueError("无效的影片编号")
    return parse_detail_shares(_get(catalog_origin(), f"/voddetail/{media_id}.html"))
