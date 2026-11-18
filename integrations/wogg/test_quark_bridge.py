"""The bridge only reads until staging and only stages the chosen source file."""
import unittest
from unittest.mock import patch
import time

import quark_bridge as bridge


class _Client:
    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False


def _file(fid, name, size, token="private-file-token"):
    return {"fid": fid, "file_name": name, "size": size, "share_fid_token": token}


class _Pan:
    SHARE = "https://share.invalid"
    BASE = "https://account.invalid"
    VIDEO_EXT = (".mkv", ".mp4")
    _PUUS_TTL = 4800

    def __init__(self):
        self.tokens = []
        self.directories = []
        self.submissions = []
        self.stage_folder_calls = 0
        self.polls = []
        self.registration = []
        self.uid = "test-account"
        self.logged_in = True
        self.puus_ts = int(time.time())
        self.refreshes = 0
        self.failure = None
        self.completed_fids = ["saved-selected"]
        self.status = 2
        self.contents = {
            "0": [{"fid": "s1", "dir": True, "file_name": "Season 1"},
                  _file("other", "S01E02.mkv", 123),
                  _file("nonvideo", "intro.txt", 1)],
            "s1": [_file("chosen", "S01E02.mkv", 123)],
        }

    def account_info_safe(self):
        return {"uid": self.uid, "saved_ts": 111, "logged_in": self.logged_in}

    def _load(self):
        return {"uid": self.uid, "saved_ts": 111, "puus_ts": self.puus_ts,
                "cookie": "private-login-cookie" if self.logged_in else ""}

    def _cookie_map(self, cookie):
        assert cookie == "private-login-cookie"
        return {"__puus": "present"}

    def _refresh_puus(self, cookie):
        assert cookie == "private-login-cookie"
        self.refreshes += 1
        return "private-refreshed-cookie"

    def _save(self, state):
        raise AssertionError("standalone bridge must not persist native state")

    def _client(self, cookie):
        assert cookie == "private-login-cookie"
        return _Client()

    def _api(self, client, method, url, _retry, **kw):
        assert client.timeout == bridge._REQUEST_TIMEOUT and _retry == 1
        if "/token" in url:
            assert method == "POST"
            self.tokens.append(kw["json"])
            return {"data": {"stoken": "private-share-token"}}
        if "/detail" in url:
            assert method == "GET"
            params = kw["params"]
            parent = params["pdir_fid"]
            start = (params["_page"] - 1) * params["_size"]
            self.directories.append((parent, params["_page"]))
            return {"data": {"list": self.contents[parent][start:start + params["_size"]]}}
        if "/save" in url:
            assert method == "POST"
            self.submissions.append(kw["json"])
            if self.failure:
                raise self.failure
            return {"data": {"task_id": "test-task"}}
        raise AssertionError(url)

    def _ensure_save_dir(self, cookie):
        self.stage_folder_calls += 1
        return "fnmedia-folder"

    def _pc(self, method, url, cookie, _tries, params):
        assert method == "GET" and url.endswith("/task?pr=ucpro&fr=pc")
        assert cookie == "private-login-cookie" and _tries == 1
        self.polls.append(params)
        return {"data": {"status": self.status,
                         "save_as": {"save_as_top_fids": self.completed_fids,
                                     "to_pdir_fid": "fnmedia-folder"}}}

    def cache_register(self, fids, metadata):
        self.registration.append((fids, metadata))


class QuarkBridgeTests(unittest.TestCase):
    share = "https://pan.quark.cn/s/abcdef#/list/share?pwd=k9P2"

    def setUp(self):
        bridge._directories.clear()
        bridge._transfers.clear()
        bridge._refreshed_session = None
        self.pan = _Pan()
        importing = patch.object(bridge.importlib, "import_module", return_value=self.pan)
        self.addCleanup(importing.stop)
        self.importer = importing.start()

    def test_listing_is_read_only_cached_and_keeps_passcode(self):
        first = bridge.list_videos(self.share)
        self.assertEqual(first, [{"fid": "chosen", "name": "S01E02.mkv", "size": 123},
                                 {"fid": "other", "name": "S01E02.mkv", "size": 123}])
        first.clear()
        self.assertEqual(len(bridge.list_videos(self.share)), 2)
        self.assertEqual(self.pan.directories, [("0", 1), ("s1", 1)])
        self.assertEqual(self.pan.tokens[0]["passcode"], "k9P2")
        self.assertEqual(self.pan.submissions, [])
        self.assertEqual(self.pan.stage_folder_calls, 0)

    def test_nested_file_transfer_is_exact_and_idempotent(self):
        bridge.list_videos(self.share)
        self.assertEqual(bridge.stage_video(self.share, "chosen"), "saved-selected")
        self.assertEqual(bridge.stage_video(self.share, "chosen"), "saved-selected")
        self.assertEqual(len(self.pan.submissions), 1)
        write = self.pan.submissions[0]
        self.assertEqual(write["fid_list"], ["chosen"])
        self.assertEqual(write["pdir_fid"], "s1")
        self.assertEqual(write["fid_token_list"], ["private-file-token"])
        self.assertFalse(write["pdir_save_all"])
        self.assertFalse(self.pan.registration)

    def test_listing_exposes_numeric_dimensions_from_quark_only(self):
        self.pan.contents["s1"][0].update(video_width="3840", video_height=2160)
        self.pan.contents["0"][1].update(video_width=False, video_height=2160)
        chosen, other = bridge.list_videos(self.share)
        self.assertEqual((chosen["width"], chosen["height"]), (3840, 2160))
        self.assertNotIn("width", other)

    def test_refresh_uses_process_memory_without_persisting_native_state(self):
        self.assertEqual(bridge.playback_cookie(), "private-login-cookie")
        self.assertEqual(self.pan.refreshes, 0)
        self.pan.puus_ts = 0
        self.assertEqual(bridge.playback_cookie(), "private-refreshed-cookie")
        self.assertEqual(bridge.playback_cookie(), "private-refreshed-cookie")
        self.assertEqual(self.pan.refreshes, 1)
        self.pan.logged_in = False
        with self.assertRaises(bridge.QuarkBridgeError):
            bridge.playback_cookie()

    def test_poll_retry_reuses_the_same_pending_task(self):
        self.pan.status = 1
        with patch.object(bridge, "_POLL_ATTEMPTS", 1):
            with self.assertRaisesRegex(bridge.QuarkBridgeError, "仍在处理"):
                bridge.stage_video(self.share, "chosen")
            self.pan.status = 2
            self.assertEqual(bridge.stage_video(self.share, "chosen"), "saved-selected")
        self.assertEqual(len(self.pan.submissions), 1)
        self.assertEqual([poll["task_id"] for poll in self.pan.polls], ["test-task", "test-task"])

    def test_unknown_submit_outcome_redacts_secrets_and_avoids_retry_write(self):
        self.pan.failure = RuntimeError("private-login-cookie private-share-token")
        for _ in range(2):
            with self.assertRaises(bridge.QuarkBridgeError) as caught:
                bridge.stage_video(self.share, "chosen")
            self.assertNotIn("private-", str(caught.exception))
        self.assertEqual(len(self.pan.submissions), 1)

    def test_ambiguous_saved_fids_never_return_or_register_a_file(self):
        self.pan.completed_fids = ["unrelated", "saved-selected"]
        with self.assertRaisesRegex(bridge.QuarkBridgeError, "唯一文件"):
            bridge.stage_video(self.share, "chosen")
        self.assertFalse(self.pan.registration)

    def test_account_change_never_returns_the_old_accounts_cached_fid(self):
        self.assertEqual(bridge.stage_video(self.share, "chosen"), "saved-selected")
        self.pan.uid = "other-account"
        self.pan.completed_fids = ["new-accounts-fid"]
        self.assertEqual(bridge.stage_video(self.share, "chosen"), "new-accounts-fid")
        self.assertEqual(len(self.pan.submissions), 2)

    def test_worker_rejects_an_old_login_scope_before_transfer_or_playback(self):
        scope = bridge.account_scope()
        self.pan.uid = "other-account"
        with self.assertRaises(bridge.QuarkAccountChanged):
            bridge.stage_video(self.share, "chosen", scope)
        with self.assertRaises(bridge.QuarkAccountChanged):
            bridge.playback_cookie(scope)
        self.assertEqual(self.pan.stage_folder_calls, 0)
        self.assertFalse(self.pan.submissions)

    def test_orphan_fid_and_bad_host_do_not_start_transfer(self):
        for link in ("https://user@pan.quark.cn/s/abcdef",
                     "https://pan.quark.cn.attacker.invalid/s/abcdef",
                     "https://pan.quark.cn/s/abcdef?pwd=abcd&passcode=efgh"):
            with self.assertRaises(bridge.QuarkBridgeError):
                bridge.list_videos(link)
        self.importer.assert_not_called()
        with self.assertRaisesRegex(bridge.QuarkBridgeError, "没有找到"):
            bridge.stage_video(self.share, "missing")
        self.assertEqual(self.pan.stage_folder_calls, 0)
        self.assertFalse(self.pan.submissions)

    def test_lists_multiple_pages_but_stops_at_folder_depth_bound(self):
        with patch.object(bridge, "_PAGE_SIZE", 2):
            self.assertEqual(len(bridge.list_videos(self.share)), 2)
        self.assertIn(("0", 2), self.pan.directories)
        bridge._directories.clear()
        with patch.object(bridge, "_MAX_DEPTH", 0), self.assertRaisesRegex(
                bridge.QuarkBridgeError, "超出浏览范围"):
            bridge.list_videos(self.share)


if __name__ == "__main__":
    unittest.main()
