"""Loopback T4 source: public Wogg catalog, then explicit single-file Quark playback."""
from __future__ import annotations

import asyncio
from collections import OrderedDict
from contextlib import asynccontextmanager
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import time
from urllib.parse import urljoin, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
import httpx

import quark_bridge
import wogg


PORT = int(os.environ.get("WOGG_T4_PORT", "18724"))
ORIGIN = f"http://127.0.0.1:{PORT}"
LISTEN_SOCK = os.environ.get("WOGG_FNM_SOCKET", "/var/run/trim_media.sock")
STATE = Path(os.environ.get("WOGG_STATE", "state"))
# Quark adds a signed URL to each segment of a long episode. Match fnmedia's
# 2 MiB playlist limit, then replace the long URLs with local resource links.
MAX_MANIFEST = 2 * 1024 * 1024
HLS_RESOURCE_TTL = 6 * 3600
MAX_EPISODES = 150
TTL = 300
REF = "https://pan.quark.cn/"
FORWARD_HEADERS = frozenset({"content-type", "content-length", "content-range",
                             "accept-ranges", "content-encoding"})


class Cache:
    def __init__(self, limit=64):
        self.limit = limit
        self.values = OrderedDict()

    def get(self, key):
        hit = self.values.pop(key, None)
        if hit and hit[0] > time.monotonic():
            self.values[key] = hit
            return hit[1]
        return None

    def put(self, key, value, ttl=TTL):
        self.values.pop(key, None)
        self.values[key] = time.monotonic() + ttl, value
        while len(self.values) > self.limit:
            self.values.popitem(last=False)
        return value


search_cache, titles, detail_cache = Cache(), Cache(512), Cache(24)
saved = Cache(256)
resources = Cache(8192)
work = asyncio.Semaphore(4)
listings = asyncio.Semaphore(2)
pending = {}


def connection():
    return sqlite3.connect(STATE / "episodes.sqlite3", timeout=3)


def register(scope, records):
    now = int(time.time())
    with connection() as db:
        db.executemany("INSERT OR REPLACE INTO scoped_episodes VALUES (?, ?, ?, ?, ?)",
                       [(scope, *row, now) for row in records])
        db.execute("DELETE FROM scoped_episodes WHERE touched < ?", (now - 14 * 86400,))
        db.execute("DELETE FROM scoped_episodes WHERE rowid IN (SELECT rowid FROM scoped_episodes "
                   "ORDER BY touched DESC, rowid DESC LIMIT -1 OFFSET 8192)")
        db.execute("DELETE FROM staged WHERE updated < ?", (now - 90 * 86400,))


def episode(scope, ref):
    if not re.fullmatch(r"[0-9a-f]{40}", ref):
        raise HTTPException(404, "选集已过期，请重新打开详情")
    with connection() as db:
        row = db.execute("SELECT share, fid FROM scoped_episodes WHERE scope = ? AND ref = ?",
                         (scope, ref)).fetchone()
    if not row:
        raise HTTPException(404, "选集已过期，请重新打开详情")
    return row


def saved_fid(scope, ref):
    cached = saved.get((scope, ref))
    if cached:
        return cached
    with connection() as db:
        row = db.execute("SELECT dest FROM staged WHERE scope = ? AND ref = ?",
                         (scope, ref)).fetchone()
    return saved.put((scope, ref), row[0], ttl=90) if row else None


def remember_fid(scope, ref, fid):
    saved.put((scope, ref), fid, ttl=90)
    with connection() as db:
        db.execute("INSERT OR REPLACE INTO staged VALUES (?, ?, ?, ?)",
                   (scope, ref, fid, int(time.time())))


def forget_fid(scope, ref):
    saved.values.pop((scope, ref), None)
    with connection() as db:
        db.execute("DELETE FROM staged WHERE scope = ? AND ref = ?", (scope, ref))


@asynccontextmanager
async def lifespan(app):
    wogg.catalog_origin()
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    with connection() as db:
        db.execute("CREATE TABLE IF NOT EXISTS scoped_episodes (scope TEXT, ref TEXT, share TEXT, "
                   "fid TEXT, touched INTEGER, PRIMARY KEY (scope, ref))")
        db.execute("CREATE TABLE IF NOT EXISTS staged (scope TEXT, ref TEXT, dest TEXT, "
                   "updated INTEGER, PRIMARY KEY (scope, ref))")
    async with (httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=LISTEN_SOCK),
                                  timeout=60, trust_env=False) as ext,
                httpx.AsyncClient(timeout=30, trust_env=False) as cdn):
        app.state.ext, app.state.cdn = ext, cdn
        yield


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


async def blocking(func, *args):
    async with work:
        return await asyncio.to_thread(func, *args)


async def list_share(url):
    async with listings:
        return await blocking(quark_bridge.list_videos, url)


def title_row(item):
    # A special episode in the update note can hide the entire season in fnmedia's search.
    return {"vod_id": item.id, "vod_name": item.title, "vod_pic": item.poster,
            "vod_year": item.year, "type_name": item.category, "vod_remarks": "玩偶哥哥 · 夸克",
            "t4_source": "玩偶哥哥"}


def episode_ref(scope: str, media_id: str, share: str, fid: str) -> str:
    return hashlib.sha256("\0".join((scope, media_id, share, fid)).encode()).hexdigest()[:40]


def clean_label(value: str, limit: int = 180) -> str:
    return re.sub(r"[#$\x00-\x1f]", " ", value).strip()[:limit]


async def detail(media_id: str):
    if not re.fullmatch(r"\d{1,12}", media_id):
        raise HTTPException(404, "影片编号无效")
    scope = local_quark_account()
    cache_key = (scope, media_id)
    hit = detail_cache.get(cache_key)
    if hit is not None:
        return hit
    try:
        shares = await blocking(wogg.detail_shares, media_id)
    except wogg.WoggError:
        raise HTTPException(502, "玩偶哥哥详情暂时不可用") from None
    shares = shares[:3]
    results = await asyncio.gather(*(list_share(share.url) for share in shares),
                                   return_exceptions=True)
    groups, flags, entries = [], [], []
    for i, (share, videos) in enumerate(zip(shares, results), 1):
        if isinstance(videos, BaseException):
            continue
        episodes = []
        for video in videos[:MAX_EPISODES]:
            name, fid = video["name"], video["fid"]
            ref = episode_ref(scope, media_id, share.url, fid)
            label = clean_label(Path(name).stem) or f"视频 {len(episodes) + 1}"
            width, height = video.get("width"), video.get("height")
            if width and height:
                label += f" 【源文件 {width}×{height}】"
            episodes.append(f"{label}${ref}")
            entries.append((ref, share.url, fid))
        if episodes:
            flag = clean_label(share.label, 24) or "夸克"
            flags.append(f"{flag} · 玩偶 {i}")
            groups.append("#".join(episodes))
    if not entries:
        raise HTTPException(502 if shares else 404,
                            "夸克分享暂不可读取" if shares else "该影片暂无夸克分享")
    if local_quark_account() != scope:
        raise HTTPException(409, "夸克账号已切换，请重新打开影片")
    register(scope, entries)
    row = dict(titles.get(media_id) or {"vod_id": media_id},
               vod_play_from="$$$".join(flags), vod_play_url="$$$".join(groups))
    return detail_cache.put(cache_key, row, ttl=180)


@app.get("/health")
async def health():
    return {"ok": True, "source": "玩偶哥哥", "local_only": True}


@app.get("/t4")
async def t4(wd: str = "", ids: str = "", play: str = ""):
    if play:
        episode(local_quark_account(), play)
        return {"parse": 0, "url": f"{ORIGIN}/media/{play}"}
    if ids:
        return {"list": [await detail(ids)]}
    keyword = wd.strip()
    if not keyword:
        return {"class": [], "list": [], "pagecount": 0, "total": 0}
    if len(keyword) > 100:
        return {"list": []}
    rows = search_cache.get(keyword)
    if rows is None:
        try:
            items = await blocking(wogg.search, keyword)
        except wogg.WoggError:
            raise HTTPException(502, "玩偶哥哥搜索暂时不可用") from None
        rows = [title_row(item) for item in items[:20]]
        for row in rows:
            titles.put(row["vod_id"], row, ttl=900)
        search_cache.put(keyword, rows)
    return {"list": rows}


async def stage(scope, ref):
    # Only media requests can transfer. Search, detail, and T4 player resolution are read-only.
    share, source_fid = episode(scope, ref)
    job = (scope, ref)
    task = pending.get(job)
    if task is None:
        if len(pending) >= 2:
            raise HTTPException(503, "夸克转存繁忙，请稍后重试")
        if local_quark_account() != scope:
            raise HTTPException(409, "夸克账号已切换，请重新打开影片")
        task = asyncio.create_task(blocking(quark_bridge.stage_video, share, source_fid, scope))
        pending[job] = task

        def done(result):
            pending.pop(job, None)
            if not result.cancelled() and result.exception() is None:
                remember_fid(scope, ref, result.result())

        task.add_done_callback(done)
    try:
        fid = await asyncio.shield(task)
        if local_quark_account() != scope:
            raise HTTPException(409, "夸克账号已切换，请重新打开影片")
        return fid
    except quark_bridge.QuarkAccountChanged as exc:
        raise HTTPException(409, str(exc)) from None
    except quark_bridge.QuarkBridgeError:
        raise HTTPException(502, "夸克无法转存这一集，请确认登录或稍后重试") from None


def range_headers(request: Request):
    return {k: request.headers[k] for k in ("Range", "If-Range") if k in request.headers}


async def transfer_response(upstream, request: Request):
    headers = {k: v for k, v in upstream.headers.items() if k.lower() in FORWARD_HEADERS}
    headers["Cache-Control"] = "no-store"
    if request.method == "HEAD" or upstream.status_code == 416:
        await upstream.aclose()
        return Response(status_code=upstream.status_code, headers=headers)

    async def pump():
        try:
            async for data in upstream.aiter_raw(chunk_size=128 * 1024):
                yield data
        finally:
            await upstream.aclose()

    return StreamingResponse(pump(), status_code=upstream.status_code, headers=headers)


def cdn_url(url):
    try:
        parts = urlsplit(url)
        domain = (parts.hostname or "").lower()
        return (parts.scheme == "https" and domain.endswith(".quark.cn")
                and parts.username is None and parts.password is None
                and parts.port in (None, 443))
    except ValueError:
        return False


def resource(url, scope):
    if not cdn_url(url):
        raise HTTPException(502, "夸克流地址不在允许的域名范围")
    key = hashlib.sha256((scope + "\0" + url).encode()).hexdigest()[:40]
    # A VOD playlist may never be reloaded. Match the outer T4 resource lifetime.
    resources.put(key, (scope, url), ttl=HLS_RESOURCE_TTL)
    return f"{ORIGIN}/chunk/{key}"


async def manifest(upstream, base: str | None, scope: str):
    body = bytearray()
    try:
        async for chunk in upstream.aiter_bytes():
            if len(body) + len(chunk) > MAX_MANIFEST:
                raise HTTPException(502, "夸克播放清单过大")
            body.extend(chunk)
    finally:
        await upstream.aclose()
    text = body.decode("utf-8-sig", errors="replace")
    if not text.lstrip().startswith("#EXTM3U"):
        raise HTTPException(502, "夸克播放清单格式不正确")
    lines = []
    for line in text.splitlines():
        value = line.strip()
        if value and not value.startswith("#"):
            line = resource(urljoin(base or "", value), scope)
        elif 'URI="' in line:
            line = re.sub(r'URI="([^\"]+)"',
                          lambda m: 'URI="' + resource(urljoin(base or "", m[1]), scope) + '"', line)
        lines.append(line)
    return Response("\n".join(lines) + "\n", media_type="application/vnd.apple.mpegurl",
                    headers={"Cache-Control": "no-store"})


@app.api_route("/media/{ref}", methods=["GET", "HEAD"])
async def media(ref: str, request: Request):
    scope = local_quark_account()
    fid = saved_fid(scope, ref)
    if not fid:
        if request.method == "HEAD":
            episode(scope, ref)
            return Response(status_code=503)
        fid = await stage(scope, ref)
    client = request.app.state.ext
    try:
        upstream = await client.send(client.build_request(
            request.method, "http://fnmedia/v/api/ext/pan/play", params={"fid": fid},
            headers=range_headers(request)), stream=True)
    except httpx.HTTPError:
        raise HTTPException(502, "飞牛网盘播放代理暂时不可用") from None
    if upstream.status_code not in (200, 206, 416):
        await upstream.aclose()
        # An explicit not-found can be a previously transferred file the user removed.
        # Don't resubmit on ambiguous/rate-limit errors or in the same media request.
        if upstream.status_code == 404:
            forget_fid(scope, ref)
        raise HTTPException(502, "夸克暂未提供可播放文件")
    if request.method == "GET" and "mpegurl" in upstream.headers.get("content-type", "").lower():
        return await manifest(upstream, None, scope)
    return await transfer_response(upstream, request)


def local_quark_account():
    try:
        return quark_bridge.account_scope()
    except quark_bridge.QuarkUnavailable as exc:
        raise HTTPException(503, str(exc)) from None
    except quark_bridge.QuarkLoginRequired as exc:
        raise HTTPException(401, str(exc)) from None


@app.api_route("/chunk/{key}", methods=["GET", "HEAD"])
async def chunk(key: str, request: Request):
    item = resources.get(key)
    if not item:
        raise HTTPException(410, "夸克流已过期，请重新播放")
    scope, url = item
    if scope != local_quark_account():
        raise HTTPException(409, "夸克账号已切换，请重新打开影片")
    try:
        cookie = await blocking(quark_bridge.playback_cookie, scope)
        client = request.app.state.cdn
        upstream = await client.send(client.build_request(
            request.method, url, headers={"Cookie": cookie, "Referer": REF,
                                          **range_headers(request)}), stream=True)
    except quark_bridge.QuarkAccountChanged as exc:
        raise HTTPException(409, str(exc)) from None
    except (httpx.HTTPError, quark_bridge.QuarkBridgeError):
        raise HTTPException(502, "夸克分片暂不可用") from None
    if upstream.status_code not in (200, 206, 416):
        await upstream.aclose()
        raise HTTPException(502, "夸克分片暂不可用")
    if request.method == "GET" and ("mpegurl" in upstream.headers.get("content-type", "").lower()
                                    or urlsplit(url).path.endswith(".m3u8")):
        return await manifest(upstream, url, scope)
    return await transfer_response(upstream, request)
