"""List and stage one selected file using fnmedia-ext's saved Quark session."""
from __future__ import annotations

from collections import OrderedDict, deque
from dataclasses import dataclass, field
import hashlib
import importlib
import re
import threading
import time
from urllib.parse import parse_qs, urlsplit


class QuarkBridgeError(RuntimeError):
    """A credential-safe diagnostic suitable for the caller to display."""


class QuarkUnavailable(QuarkBridgeError):
    """The installed extension does not expose the required Quark integration."""


class QuarkLoginRequired(QuarkBridgeError):
    """No usable Quark login is active in the extension."""


class QuarkAccountChanged(QuarkBridgeError):
    """The request refers to a different login from the active account."""


_ID = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_PATH = re.compile(r"/s/([A-Za-z0-9]{1,128})/?\Z")
_CODE = re.compile(r"[A-Za-z0-9]{1,6}\Z")
_MAX_DIRECTORIES = 14
_MAX_DEPTH = 3
_MAX_PAGES = 4
_PAGE_SIZE = 100
_MAX_VIDEOS = 400
_MAX_CACHE_ENTRIES = 96
_LIST_TTL = 120.0
_LIST_BUDGET = 10.0
_REQUEST_TIMEOUT = 4.0
_POLL_BUDGET = 7.0
_POLL_ATTEMPTS = 5
_STAGED_TTL = 1800.0
_PENDING_TTL = 600.0
_RETRY_TTL = 60.0


@dataclass(frozen=True)
class _Video:
    fid: str
    name: str
    size: int
    parent: str
    width: int = field(default=0, compare=False)
    height: int = field(default=0, compare=False)


@dataclass(frozen=True)
class _Transfer:
    status: str
    value: str = ""
    folder: str = ""


_directories: OrderedDict = OrderedDict()
_transfers: OrderedDict = OrderedDict()
_cache_lock = threading.Lock()
_listing_slots = threading.BoundedSemaphore(2)
_transfer_slot = threading.Lock()
_session_lock = threading.Lock()
_refreshed_session = None


def _share(share_url: str) -> tuple[str, str]:
    try:
        if not isinstance(share_url, str) or len(share_url) > 2048:
            raise ValueError
        parts = urlsplit(share_url.strip())
        path = _PATH.fullmatch(parts.path)
        if parts.scheme not in ("https", "http") or parts.netloc.lower() != "pan.quark.cn" or not path:
            raise ValueError
        fragment_query = parts.fragment.partition("?")[2]
        if parts.fragment.startswith(("pwd=", "passcode=")):
            fragment_query = parts.fragment
        codes = set()
        for query in (parts.query, fragment_query):
            params = parse_qs(query, keep_blank_values=True, max_num_fields=24)
            for key in ("pwd", "passcode"):
                codes.update(param for param in params.get(key, ()) if param)
        if len(codes) > 1:
            raise ValueError
        passcode = next(iter(codes), "")
        if passcode and not _CODE.fullmatch(passcode):
            raise ValueError
        return path[1], passcode
    except ValueError:
        raise QuarkBridgeError("夸克分享链接或提取码格式无效") from None


def _account(pan) -> tuple[str, str]:
    try:
        info = pan.account_info_safe()
        scope = (str(info.get("uid") or ""), str(info.get("saved_ts") or ""))
    except Exception:
        raise QuarkUnavailable("无法读取飞牛扩展的夸克登录状态") from None
    if not info.get("logged_in") or not any(scope):
        raise QuarkLoginRequired("请先在飞牛影视在线扩展内登录夸克")
    return scope


def _pan_module():
    try:
        return importlib.import_module("pan")
    except Exception:
        raise QuarkUnavailable("未找到夸克模块，请确认已安装官方完整 FPK 并配置 PYTHONPATH") from None


def account_scope() -> str:
    """Return an opaque key for the active Quark login."""
    return _account_scope(_account(_pan_module()))


def _account_scope(account: tuple[str, str]) -> str:
    return hashlib.sha256("\0".join(account).encode()).hexdigest()


def _ensure_account(account: tuple[str, str], expected_scope: str | None) -> None:
    if expected_scope is not None and _account_scope(account) != expected_scope:
        raise QuarkAccountChanged("夸克账号已切换，请重新打开资源")


def _session(share_url: str):
    share_id, passcode = _share(share_url)
    pan = _pan_module()
    account = _account(pan)
    cache_key = hashlib.sha256("\0".join((*account, share_id, passcode)).encode()).digest()
    return pan, account, share_id, passcode, cache_key


def _cookie(pan, account):
    global _refreshed_session
    try:
        state = pan._load()
        source = state.get("cookie") or ""
        scope = (str(state.get("uid") or ""), str(state.get("saved_ts") or ""))
        if not source or scope != account:
            raise QuarkBridgeError("夸克账号已切换，请重新打开资源")
        age = time.time() - int(state.get("puus_ts") or 0)
        if "__puus" in pan._cookie_map(source) and 0 <= age < pan._PUUS_TTL:
            return source
        identity = (account, hashlib.sha256(source.encode()).digest())
        # The native app owns the saved state. Its standard refresh can be used
        # without invoking the helper that persists the new session to that file.
        with _session_lock:
            hit = _refreshed_session
            if hit and hit[0] == identity and hit[1] > time.monotonic():
                fresh = hit[2]
            else:
                fresh = pan._refresh_puus(source)
                _refreshed_session = (identity, time.monotonic() + pan._PUUS_TTL, fresh)
        if _account(pan) != account:
            raise QuarkBridgeError("夸克账号已切换，请重新打开资源")
        return fresh
    except QuarkBridgeError:
        raise
    except Exception:
        raise QuarkBridgeError("夸克登录状态不可用，请在飞牛扩展里检查或重新登录") from None


def playback_cookie(expected_scope: str | None = None) -> str:
    """Get the active session for same-process HTTPS delivery, never persist it."""
    pan = _pan_module()
    account = _account(pan)
    _ensure_account(account, expected_scope)
    return _cookie(pan, account)


def _cached(cache, key):
    with _cache_lock:
        entry = cache.pop(key, None)
        if entry is None or entry[0] <= time.monotonic():
            return None
        cache[key] = entry
        return entry[1]


def _retain(cache, key, value, ttl):
    with _cache_lock:
        cache[key] = (time.monotonic() + ttl, value)
        cache.move_to_end(key)
        if len(cache) > _MAX_CACHE_ENTRIES:
            cache.popitem(last=False)


def _client(pan, cookie):
    client = pan._client(cookie)
    client.timeout = _REQUEST_TIMEOUT
    return client


def _token(pan, client, share_id, passcode):
    try:
        json = pan._api(client, "POST", f"{pan.SHARE}/share/sharepage/token?pr=ucpro&fr=pc",
                        _retry=1, json={"pwd_id": share_id, "passcode": passcode,
                                        "support_visit_limit_private_share": True})
        stoken = (json.get("data") or {}).get("stoken")
        if isinstance(stoken, str) and stoken:
            return stoken
    except Exception:
        pass
    raise QuarkBridgeError("夸克分享不可访问，请检查有效期、提取码和登录状态")


def _directory(pan, client, share_id, stoken, parent, deadline):
    for page in range(1, _MAX_PAGES + 1):
        if time.monotonic() >= deadline:
            raise QuarkBridgeError("夸克分享浏览超时，请稍后重试")
        try:
            json = pan._api(client, "GET", f"{pan.SHARE}/share/sharepage/detail", _retry=1,
                            params={"pr": "ucpro", "fr": "pc", "pwd_id": share_id,
                                    "stoken": stoken, "pdir_fid": parent, "force": 0,
                                    "_page": page, "_size": _PAGE_SIZE, "_fetch_total": 1,
                                    "_sort": "file_type:asc,file_name:asc", "ver": 2})
            rows = (json.get("data") or {}).get("list")
            if not isinstance(rows, list) or len(rows) > _PAGE_SIZE:
                raise ValueError
        except Exception:
            raise QuarkBridgeError("夸克没有返回可读取的分享目录") from None
        yield from (row for row in rows if isinstance(row, dict))
        if len(rows) < _PAGE_SIZE:
            return
        total = (json.get("metadata") or {}).get("_total")
        if isinstance(total, int) and page * _PAGE_SIZE >= total:
            return
    raise QuarkBridgeError("夸克分享目录太大，超出桥接的浏览范围")


def _fid(value) -> str:
    return value if isinstance(value, str) and _ID.fullmatch(value) else ""


def _dimension(value) -> int:
    if type(value) is int or isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 5:
        pixels = int(value)
        if 16 <= pixels <= 16384:
            return pixels
    return 0


def _video(pan, row, parent) -> _Video | None:
    name = row.get("file_name")
    fid = _fid(row.get("fid"))
    if row.get("dir") or not fid or not isinstance(name, str) or not name.lower().endswith(pan.VIDEO_EXT):
        return None
    try:
        size = max(0, int(row.get("size") or 0))
    except (ValueError, TypeError, OverflowError):
        size = 0
    return _Video(fid, name, size, parent, _dimension(row.get("video_width")),
                  _dimension(row.get("video_height")))


def _listing(session) -> tuple[_Video, ...]:
    pan, account, share_id, passcode, cache_key = session
    cached = _cached(_directories, cache_key)
    if cached is not None:
        return cached
    if not _listing_slots.acquire(blocking=False):
        raise QuarkBridgeError("正在读取其他夸克分享，请稍后重试")
    try:
        cookie = _cookie(pan, account)
        folders = deque([("0", 0)])
        seen = {"0"}
        videos: dict[str, _Video] = {}
        deadline = time.monotonic() + _LIST_BUDGET
        with _client(pan, cookie) as client:
            stoken = _token(pan, client, share_id, passcode)
            while folders:
                parent, depth = folders.popleft()
                for row in _directory(pan, client, share_id, stoken, parent, deadline):
                    video = _video(pan, row, parent)
                    if video:
                        videos.setdefault(video.fid, video)
                        if len(videos) > _MAX_VIDEOS:
                            raise QuarkBridgeError("夸克分享中的视频数量超出浏览范围")
                    child = _fid(row.get("fid")) if row.get("dir") else ""
                    if child and child not in seen:
                        if len(seen) >= _MAX_DIRECTORIES or depth >= _MAX_DEPTH:
                            raise QuarkBridgeError("夸克分享目录过深或过多，超出浏览范围")
                        seen.add(child)
                        folders.append((child, depth + 1))
        result = tuple(sorted(videos.values(), key=lambda item: (item.name, item.fid)))
        _retain(_directories, cache_key, result, _LIST_TTL)
        return result
    except QuarkBridgeError:
        raise
    except Exception:
        raise QuarkBridgeError("读取夸克分享失败，请稍后重试") from None
    finally:
        _listing_slots.release()


def list_videos(share_url: str) -> list[dict[str, str | int]]:
    """Return file metadata only; this call never transfers any cloud files."""
    rows = []
    for item in _listing(_session(share_url)):
        row = {"fid": item.fid, "name": item.name, "size": item.size}
        if item.width and item.height:
            row.update(width=item.width, height=item.height)
        rows.append(row)
    return rows


def _begin_transfer(session, key, source_fid, cookie) -> _Transfer:
    pan, account, share_id, passcode, _ = session
    listed = next((video for video in _listing(session) if video.fid == source_fid), None)
    if listed is None:
        raise QuarkBridgeError("没有找到所选择的视频，请重新打开资源")
    folder = _fid(pan._ensure_save_dir(cookie))
    if not folder or folder == "0":
        raise QuarkBridgeError("无法确认 fnmedia转存 目录，未转存文件")
    if _account(pan) != account:
        raise QuarkBridgeError("夸克账号已切换，请重新打开资源")
    deadline = time.monotonic() + _LIST_BUDGET
    with _client(pan, cookie) as client:
        stoken = _token(pan, client, share_id, passcode)
        rows = _directory(pan, client, share_id, stoken, listed.parent, deadline)
        row = next((row for row in rows if row.get("fid") == source_fid), None)
        current = _video(pan, row or {}, listed.parent)
        token = (row or {}).get("share_fid_token")
        if current != listed or not isinstance(token, str) or not token:
            raise QuarkBridgeError("所选视频已变更，请重新打开资源")
        # A network error may occur after the provider accepted this write.
        # Suppress immediate repeat submissions until the outcome is clear.
        _retain(_transfers, key, _Transfer("uncertain"), _RETRY_TTL)
        result = pan._api(client, "POST", f"{pan.SHARE}/share/sharepage/save?pr=ucpro&fr=pc",
                          _retry=1, json={"fid_list": [source_fid], "fid_token_list": [token],
                                          "to_pdir_fid": folder, "pwd_id": share_id,
                                          "stoken": stoken, "pdir_fid": listed.parent,
                                          "pdir_save_all": False, "exclude_fids": [], "scene": "link"})
    task_id = _fid((result.get("data") or {}).get("task_id"))
    if not task_id:
        raise QuarkBridgeError("夸克没有返回转存任务标识，请在 fnmedia转存 中检查")
    transfer = _Transfer("pending", task_id, folder)
    _retain(_transfers, key, transfer, _PENDING_TTL)
    return transfer


def _poll_transfer(pan, key, source_fid, cookie, pending: _Transfer) -> str:
    deadline = time.monotonic() + _POLL_BUDGET
    for attempt in range(_POLL_ATTEMPTS):
        if time.monotonic() >= deadline:
            break
        payload = pan._pc("GET", f"{pan.BASE}/task?pr=ucpro&fr=pc", cookie, _tries=1,
                          params={"task_id": pending.value, "retry_index": attempt})
        task = payload.get("data") or {}
        status = str(task.get("status", ""))
        if status == "2":
            saved = task.get("save_as") or {}
            fids = saved.get("save_as_top_fids")
            actual = _fid(fids[0]) if isinstance(fids, list) and len(fids) == 1 else ""
            dest = saved.get("to_pdir_fid")
            if not actual or actual in ("0", source_fid) or (dest and dest != pending.folder):
                _retain(_transfers, key, _Transfer("uncertain"), _PENDING_TTL)
                raise QuarkBridgeError("无法确认这次转存对应的唯一文件，请在 fnmedia转存 中检查")
            _retain(_transfers, key, _Transfer("complete", actual), _STAGED_TTL)
            # cache_register rewrites the native account state. A separate
            # process must not register these staged files in the native ledger.
            return actual
        if status in ("3", "4"):
            _retain(_transfers, key, _Transfer("failed"), _RETRY_TTL)
            raise QuarkBridgeError("夸克转存任务失败或暂停，请稍后重试")
        if attempt + 1 < _POLL_ATTEMPTS:
            time.sleep(0.6)
    raise QuarkBridgeError("夸克转存仍在处理，再次播放将继续查询同一任务")


def stage_video(share_url: str, source_fid: str, expected_scope: str | None = None) -> str:
    """Transfer the selected file only and return its exact new Quark FID."""
    if not _fid(source_fid) or source_fid == "0":
        raise QuarkBridgeError("所选视频标识无效")
    session = _session(share_url)
    pan, account, _, _, cache_key = session
    _ensure_account(account, expected_scope)
    key = cache_key, source_fid
    if not _transfer_slot.acquire(timeout=0.5):
        raise QuarkBridgeError("夸克正在处理另一个播放请求，请稍后重试")
    try:
        transfer = _cached(_transfers, key)
        if transfer and transfer.status == "complete":
            return transfer.value
        if transfer and transfer.status in ("uncertain", "failed"):
            raise QuarkBridgeError("上次转存尚未确认，请在 fnmedia转存 中检查后再试")
        cookie = _cookie(pan, account)
        if transfer is None:
            transfer = _begin_transfer(session, key, source_fid, cookie)
        return _poll_transfer(pan, key, source_fid, cookie, transfer)
    except QuarkBridgeError:
        raise
    except Exception:
        raise QuarkBridgeError("夸克转存暂时无法确认，请稍后重试") from None
    finally:
        _transfer_slot.release()
