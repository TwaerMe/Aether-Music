import ast
import base64
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
import urllib.parse
import urllib.request

from google_sync import (
    CLOUD_PROFILE_FILENAME,
    GOOGLE_DRIVE_FILES_URL,
    GOOGLE_DRIVE_UPLOAD_URL,
    GOOGLE_TOKEN_URL,
    GOOGLE_USERINFO_URL,
    GoogleAccountSession,
    GoogleAuthError,
    GoogleCloudSyncClient,
    GoogleOAuthConfig,
    GoogleOAuthLoopbackFlow,
    _SimpleHttpResponse,
    build_auth_url,
    build_pkce_pair,
    merge_user_states,
)
from perf_core import (
    AudioFileCache,
    DebouncedStateWriter,
    PCMStreamBuffer,
    PriorityTrackResolver,
    StreamURLCache,
    parse_stream_expiry,
    pick_optimal_thumbnail,
)
from tray_manager import TrayManager


class FakeTransport:
    def __init__(self, handler):
        self.handler = handler
        self.calls = []
        self.closed = False

    def request(self, method, url, *, params=None, headers=None, data=None, timeout=12.0):
        self.calls.append((method, url, params, headers, data))
        return self.handler(method, url, params=params, headers=headers, data=data)

    def close(self):
        self.closed = True


class TestGoogleOAuthAndSync(unittest.TestCase):
    def test_pkce_and_auth_url(self):
        verifier, challenge = build_pkce_pair()
        expected_challenge = (
            base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest())
            .rstrip(b"=")
            .decode("ascii")
        )
        self.assertEqual(challenge, expected_challenge)

        url = build_auth_url(
            client_id="12345.apps.googleusercontent.com",
            redirect_uri="http://127.0.0.1:45678/callback",
            code_challenge=challenge,
            state="state-token-123",
        )
        parsed = urllib.parse.urlparse(url)
        qs = urllib.parse.parse_qs(parsed.query)
        self.assertEqual(qs["client_id"], ["12345.apps.googleusercontent.com"])
        self.assertEqual(qs["code_challenge_method"], ["S256"])
        self.assertEqual(qs["state"], ["state-token-123"])
        self.assertIn("https://www.googleapis.com/auth/drive.appdata", qs["scope"][0])

    def test_oauth_config_parsing_and_persistence(self):
        cfg = GoogleOAuthConfig.parse_json_payload(
            {
                "installed": {
                    "client_id": "9999-abc.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-secret",
                }
            }
        )
        self.assertTrue(cfg.is_valid)
        self.assertEqual(cfg.client_secret, "GOCSPX-secret")

        with self.assertRaises(GoogleAuthError):
            GoogleOAuthConfig.parse_json_payload({"installed": {"client_id": "invalid-id"}})

        with tempfile.TemporaryDirectory() as tmpdir:
            saved = cfg.save(tmpdir)
            self.assertTrue(os.path.isfile(saved))
            discovered = GoogleOAuthConfig.discover(tmpdir)
            self.assertIsNotNone(discovered)
            self.assertEqual(discovered.client_id, cfg.client_id)

    def test_loopback_oauth_flow_end_to_end(self):
        cfg = GoogleOAuthConfig("5555-demo.apps.googleusercontent.com", "GOCSPX-demo")

        def fake_http(method, url, params=None, headers=None, data=None):
            if url == GOOGLE_TOKEN_URL:
                self.assertEqual(data["grant_type"], "authorization_code")
                self.assertEqual(data["code"], "auth-code-xyz")
                return _SimpleHttpResponse(
                    200,
                    json.dumps(
                        {
                            "access_token": "acc-token-1",
                            "refresh_token": "ref-token-1",
                            "expires_in": 3600,
                        }
                    ).encode("utf-8"),
                )
            if url == GOOGLE_USERINFO_URL:
                self.assertEqual(headers["Authorization"], "Bearer acc-token-1")
                return _SimpleHttpResponse(
                    200,
                    json.dumps(
                        {
                            "sub": "google-sub-42",
                            "email": "listener@gmail.com",
                            "name": "Alex Music",
                            "picture": "https://example.com/pic.jpg",
                        }
                    ).encode("utf-8"),
                )
            raise AssertionError(f"Unexpected URL: {url}")

        transport = FakeTransport(fake_http)
        flow = GoogleOAuthLoopbackFlow(cfg, transport=transport)

        def simulate_browser(auth_url: str):
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(auth_url).query)
            cb_url = f"{qs['redirect_uri'][0]}?code=auth-code-xyz&state={qs['state'][0]}"

            def hit_cb():
                time.sleep(0.05)
                with urllib.request.urlopen(cb_url, timeout=3) as r:
                    body = r.read().decode("utf-8")
                    self.assertIn("Вход выполнен", body)

            threading.Thread(target=hit_cb, daemon=True).start()

        session = flow.run(timeout_sec=5.0, open_browser_fn=simulate_browser)
        self.assertEqual(session.sub, "google-sub-42")
        self.assertEqual(session.email, "listener@gmail.com")
        self.assertEqual(session.refresh_token, "ref-token-1")

    def test_cloud_sync_refresh_create_update_and_pull(self):
        cloud_store = {}

        def fake_http(method, url, params=None, headers=None, data=None):
            if url == GOOGLE_TOKEN_URL:
                return _SimpleHttpResponse(
                    200,
                    json.dumps({"access_token": "fresh-token", "expires_in": 3600}).encode("utf-8"),
                )
            if method == "GET" and url == GOOGLE_DRIVE_FILES_URL:
                files = [{"id": "file-123", "name": CLOUD_PROFILE_FILENAME}] if "file-123" in cloud_store else []
                return _SimpleHttpResponse(200, json.dumps({"files": files}).encode("utf-8"))
            if method == "POST" and url == GOOGLE_DRIVE_UPLOAD_URL:
                raw = data.decode("utf-8")
                # Extract the second JSON part from multipart body
                json_part = raw.split("Content-Type: application/json; charset=UTF-8\r\n\r\n")[2].split("\r\n--")[0]
                cloud_store["file-123"] = json.loads(json_part)
                return _SimpleHttpResponse(200, json.dumps({"id": "file-123"}).encode("utf-8"))
            if method == "PATCH" and url == f"{GOOGLE_DRIVE_UPLOAD_URL}/file-123":
                cloud_store["file-123"] = json.loads(data.decode("utf-8"))
                return _SimpleHttpResponse(200, json.dumps({"id": "file-123"}).encode("utf-8"))
            if method == "GET" and url == f"{GOOGLE_DRIVE_FILES_URL}/file-123":
                return _SimpleHttpResponse(200, json.dumps(cloud_store["file-123"]).encode("utf-8"))
            raise AssertionError(f"Unhandled request: {method} {url}")

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GoogleCloudSyncClient(tmpdir, transport=FakeTransport(fake_http))
            client.set_config(GoogleOAuthConfig("123.apps.googleusercontent.com", "sec"))
            # Expired access token to force refresh_token path
            client.set_session(
                GoogleAccountSession(
                    sub="sub-1",
                    email="u@gmail.com",
                    name="User",
                    picture="",
                    access_token="expired",
                    refresh_token="ref-1",
                    expires_at=time.time() - 10,
                )
            )
            self.assertIsNone(client.pull_profile())
            self.assertTrue(client.push_profile({"liked_tracks": [{"title": "Song A"}], "updated_at": 100.0}))
            self.assertEqual(client.session.cloud_file_id, "file-123")
            self.assertEqual(client.session.access_token, "fresh-token")

            self.assertTrue(client.push_profile({"liked_tracks": [{"title": "Song B"}], "updated_at": 200.0}))
            pulled = client.pull_profile()
            self.assertEqual(pulled["liked_tracks"][0]["title"], "Song B")

    def test_merge_user_states_likes_unlikes_and_recommendations(self):
        t1 = {"title": "Track 1", "artist": "Artist A", "webpage_url": "https://www.youtube.com/watch?v=AAAAAAAAAAA"}
        t2 = {"title": "Track 2", "artist": "Artist B", "webpage_url": "https://www.youtube.com/watch?v=BBBBBBBBBBB"}
        t3 = {"title": "Track 3", "artist": "Artist C", "webpage_url": "https://www.youtube.com/watch?v=CCCCCCCCCCC"}

        local_state = {
            "updated_at": 200.0,
            "liked_tracks": [t1, t3],
            "liked_timestamps": {"yt:AAAAAAAAAAA": 100.0, "yt:CCCCCCCCCCC": 190.0},
            "unliked_timestamps": {"yt:BBBBBBBBBBB": 195.0},  # Explicitly unliked on newer local device
            "history_tracks": [t3],
            "disliked_uids": ["yt:DDDDDDDDDDD"],
            "artist_scores": {"Artist A": 10.0, "Artist C": 6.0},
            "seed_videos": [{"vid": "CCCCCCCCCCC", "title": "Track 3", "artist": "Artist C"}],
            "custom_seeds": ["Synthwave"],
            "custom_seed_timestamps": {"synthwave": 180.0},
            "removed_seed_timestamps": {"phonk": 195.0},
            "volume": 0.65,
            "eq_preset": "bass",
            "normalize_audio": True,
            "wave_mood": "Ночной драйв",
        }
        cloud_state = {
            "updated_at": 150.0,
            "liked_tracks": [t1, t2],
            "liked_timestamps": {"yt:AAAAAAAAAAA": 100.0, "yt:BBBBBBBBBBB": 120.0},
            "unliked_timestamps": {},
            "history_tracks": [t2, t1],
            "disliked_uids": ["yt:CCCCCCCCCCC", "yt:EEEEEEEEEEE"],
            "artist_scores": {"Artist A": 4.0, "Artist B": 8.0},
            "seed_videos": [{"vid": "AAAAAAAAAAA", "title": "Track 1", "artist": "Artist A"}],
            "custom_seeds": ["Phonk", "Indie"],
            "custom_seed_timestamps": {"phonk": 110.0, "indie": 130.0},
            "removed_seed_timestamps": {},
            "volume": 0.80,
            "eq_preset": "flat",
            "normalize_audio": False,
            "wave_mood": "Любое",
        }

        merged = merge_user_states(local_state, cloud_state)
        liked_titles = [t["title"] for t in merged["liked_tracks"]]
        # t2 was explicitly unliked at 195 > 120 so it must stay removed; t3 & t1 stay liked
        self.assertEqual(liked_titles, ["Track 3", "Track 1"])
        # t3 was liked so it must be removed from disliked_uids
        self.assertNotIn("yt:CCCCCCCCCCC", merged["disliked_uids"])
        self.assertIn("yt:DDDDDDDDDDD", merged["disliked_uids"])
        self.assertIn("yt:EEEEEEEEEEE", merged["disliked_uids"])
        # Custom seed "Phonk" was removed at 195 > 110; "Synthwave" & "Indie" remain
        self.assertEqual(merged["custom_seeds"], ["Synthwave", "Indie"])
        # Artist scores merged across both devices
        self.assertIn("Artist B", merged["artist_scores"])
        self.assertGreater(merged["artist_scores"]["Artist A"], 7.0)
        # Newer player settings win
        self.assertEqual(merged["eq_preset"], "bass")
        self.assertEqual(merged["wave_mood"], "Ночной драйв")
        self.assertTrue(merged["normalize_audio"])


class TestTrayAndMainAstIntegrity(unittest.TestCase):
    def test_tray_manager_safe_without_backend(self):
        tray = TrayManager(
            on_open=lambda: None,
            on_play_pause=lambda: None,
            on_next=lambda: None,
            on_prev=lambda: None,
            on_toggle_like=lambda: None,
            on_quit=lambda: None,
            get_play_state=lambda: False,
            get_liked_state=lambda: False,
        )
        # Should never crash even if pystray/PIL are absent
        tray.start()
        tray.update_state("Song", "Artist")
        tray.notify_minimized_once()
        tray.stop()
        self.assertFalse(tray.is_running)

    def test_main_syntax_and_no_youtube_channel_connection_remnants(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        main_path = os.path.join(root, "main.py")
        with open(main_path, "r", encoding="utf-8") as f:
            src = f.read()

        # Valid Python 3.11 AST
        tree = ast.parse(src, filename="main.py")
        func_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }

        # Removed YouTube channel connection functions must not exist
        for forbidden_fn in (
            "normalize_channel_input",
            "_connect_youtube_channel",
            "_disconnect_youtube_channel",
            "_sync_youtube_channel",
            "_open_channel_tab",
        ):
            self.assertNotIn(forbidden_fn, func_names)

        # Removed channel attributes must not appear in main.py
        for forbidden_token in ("channel_url", "channel_handle", "channel_tracks", "channel_syncing"):
            self.assertNotIn(forbidden_token, src)

        # New tray, Google sync, playback & resize-optimization handlers must exist
        class_names = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        }
        self.assertIn("FastTrackScrollFrame", class_names)

        for required_fn in (
            "_on_window_close_request",
            "_restore_from_tray",
            "_quit_app",
            "_open_google_auth_dialog",
            "_sync_google_cloud_async",
            "_sign_out_google",
            "_refresh_sidebar_account_box",
            "_render_rows_chunked",
            "_schedule_keyboard_seek",
            "_seek_current_track",
            "_on_track_resolved_bg",
            "_on_root_configure",
            "_on_global_mousewheel",
            "extract_stream_info",
            "_sanitize_http_headers",
        ):
            self.assertIn(required_fn, func_names)

        # Broken yt-dlp / ffmpeg / PortAudio / slow CTkScrollableFrame regressions must not return
        for forbidden_regression in (
            "android_vr",
            "-probesize",
            "-analyzeduration",
            "-rw_timeout",
            "-http_persistent",
            'latency="low"',
            "CTkScrollableFrame(",
        ):
            self.assertNotIn(forbidden_regression, src)

        self.assertIn("from perf_core import", src)


class _DummyTrack:
    def __init__(self, uid: str):
        self.uid = uid
        self.stream_url = None
        self.stream_resolved_at = 0.0


class TestPerfCoreOptimizations(unittest.TestCase):
    def test_parse_stream_expiry(self):
        now = 1_700_000_000.0
        url_with_exp = f"https://rr1---sn.googlevideo.com/videoplayback?expire={int(now + 10000)}&id=1"
        exp = parse_stream_expiry(url_with_exp, now=now, safety_margin=500.0)
        self.assertAlmostEqual(exp, now + 9500.0, delta=1.0)

        url_no_exp = "https://example.com/audio.webm"
        self.assertAlmostEqual(parse_stream_expiry(url_no_exp, now=now, default_ttl=3600.0), now + 3600.0, delta=1.0)
        self.assertEqual(parse_stream_expiry(None, now=now), now)

    def test_pick_optimal_thumbnail(self):
        thumbs = [
            {"url": "https://i.ytimg.com/vi/abc/default.jpg", "width": 120, "height": 90},
            {"url": "https://i.ytimg.com/vi/abc/mqdefault.webp", "width": 320, "height": 180},
            {"url": "https://i.ytimg.com/vi/abc/mqdefault.jpg", "width": 320, "height": 180},
            {"url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg", "width": 1280, "height": 720},
        ]
        chosen = pick_optimal_thumbnail(thumbs, video_id="abc")
        self.assertEqual(chosen, "https://i.ytimg.com/vi/abc/mqdefault.jpg")
        self.assertEqual(pick_optimal_thumbnail([], video_id="XYZ12345678"), "https://i.ytimg.com/vi/XYZ12345678/mqdefault.jpg")

    def test_stream_url_cache_lru_expiry_and_persistence(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache_path = os.path.join(tmpdir, "stream_cache.json")
            now = time.time()
            future_exp = int(now + 7200)
            cache = StreamURLCache(cache_file=cache_path, max_items=32)

            cache.put(
                "yt:1",
                f"https://example.com/a?expire={future_exp}",
                duration=180.0,
                thumbnail_url="https://img/1.jpg",
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.youtube.com/"},
                now=now,
            )
            hit = cache.get("yt:1", now=now)
            self.assertIsNotNone(hit)
            self.assertEqual(hit["duration"], 180.0)
            self.assertEqual(hit["thumbnail_url"], "https://img/1.jpg")
            self.assertEqual(hit["headers"]["User-Agent"], "Mozilla/5.0")

            # Expired query returns None
            self.assertIsNone(cache.get("yt:1", now=now + 10000))

            # Re-add, flush, and reload from disk
            cache.put(
                "yt:2",
                f"https://example.com/b?expire={future_exp}",
                duration=210.0,
                headers={"User-Agent": "TestUA"},
                now=now,
            )
            cache.put("yt:3", f"https://example.com/c?expire={future_exp}", duration=120.0, now=now)
            cache.invalidate("yt:3")
            cache.close()

            reloaded = StreamURLCache(cache_file=cache_path, max_items=32)
            hit2 = reloaded.get("yt:2", now=now)
            self.assertIsNotNone(hit2)
            self.assertEqual(hit2["headers"], {"User-Agent": "TestUA"})
            self.assertIsNone(reloaded.get("yt:3", now=now))
            reloaded.close()

            # Legacy v1 cache files on disk must be ignored to drop stale URLs without headers
            legacy_path = os.path.join(tmpdir, "legacy_v1.json")
            with open(legacy_path, "w", encoding="utf-8") as f:
                json.dump(
                    {"v": 1, "items": {"yt:old": {"url": f"https://example.com/old?expire={future_exp}", "expires_at": future_exp}}},
                    f,
                )
            legacy_cache = StreamURLCache(cache_file=legacy_path, max_items=32)
            self.assertIsNone(legacy_cache.get("yt:old", now=now))
            legacy_cache.close()

    def test_audio_file_cache_hit_and_lru_eviction(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioFileCache(cache_dir=tmpdir, max_bytes=10 * 1024 * 1024)
            try:
                # Tiny file (< 32KB) must be ignored
                tiny_path = cache._path_for_uid("yt:tiny")
                with open(tiny_path, "wb") as f:
                    f.write(b"x" * 1024)
                self.assertIsNone(cache.get_cached_path("yt:tiny"))

                # Valid cached file (>= 32KB) returns path and invalidate() deletes it
                ok_path = cache._path_for_uid("yt:ok")
                with open(ok_path, "wb") as f:
                    f.write(b"a" * 32768)
                self.assertEqual(cache.get_cached_path("yt:ok"), ok_path)
                cache.invalidate("yt:ok")
                self.assertIsNone(cache.get_cached_path("yt:ok"))
                self.assertFalse(os.path.exists(ok_path))

                # HLS / DASH playlist URLs must never be queued for single-file download
                self.assertFalse(cache.warm_async("yt:hls", "https://example.com/live/index.m3u8"))
                self.assertFalse(cache.warm_async("yt:dash", "https://manifest.googlevideo.com/api/manifest/dash/id/1.mpd"))

                # Enforce size limit evicts oldest .aud files first
                cache.max_bytes = 70000
                p1 = cache._path_for_uid("yt:old1")
                p2 = cache._path_for_uid("yt:old2")
                p3 = cache._path_for_uid("yt:new3")
                now = time.time()
                for p, ts in ((p1, now - 300), (p2, now - 200), (p3, now + 50)):
                    with open(p, "wb") as f:
                        f.write(b"z" * 35000)
                    os.utime(p, (ts, ts))
                cache.enforce_size_limit()
                self.assertFalse(os.path.exists(p1))
                self.assertTrue(os.path.exists(p3))
            finally:
                cache.close()

    def test_priority_resolver_preempts_and_deduplicates(self):
        calls: list[str] = []
        calls_lock = threading.Lock()
        gate = threading.Event()
        started_first = threading.Event()

        def resolve_fn(track, force_refresh):
            with calls_lock:
                calls.append(track.uid)
            if track.uid == "yt:blocker":
                started_first.set()
                gate.wait(timeout=2.0)
            else:
                time.sleep(0.03)
            return f"https://stream/{track.uid}"

        # 1 worker makes priority ordering deterministic
        resolver = PriorityTrackResolver(resolve_fn=resolve_fn, num_workers=1)
        try:
            order: list[str] = []
            done_urgent = threading.Event()
            done_low = threading.Event()

            resolver.submit(_DummyTrack("yt:blocker"), priority=PriorityTrackResolver.PRIORITY_PREFETCH)
            self.assertTrue(started_first.wait(timeout=1.5))

            resolver.submit(
                _DummyTrack("yt:low"),
                priority=PriorityTrackResolver.PRIORITY_PREFETCH,
                on_done=lambda u: (order.append("low"), done_low.set()),
            )
            resolver.submit(
                _DummyTrack("yt:urgent"),
                priority=PriorityTrackResolver.PRIORITY_URGENT,
                on_done=lambda u: (order.append("urgent"), done_urgent.set()),
            )
            gate.set()

            self.assertTrue(done_urgent.wait(timeout=2.0))
            self.assertTrue(done_low.wait(timeout=2.0))
            self.assertEqual(order, ["urgent", "low"])
        finally:
            resolver.close()

        # Deduplication across 2 workers for two different Track objects with the same uid
        dedup_calls = 0
        dedup_lock = threading.Lock()

        def slow_resolve(track, force_refresh):
            nonlocal dedup_calls
            with dedup_lock:
                dedup_calls += 1
            time.sleep(0.08)
            return f"https://stream/{track.uid}"

        resolver2 = PriorityTrackResolver(resolve_fn=slow_resolve, num_workers=2)
        try:
            results: list[str | None] = []
            ev1 = threading.Event()
            ev2 = threading.Event()
            t_a = _DummyTrack("yt:same")
            t_b = _DummyTrack("yt:same")
            resolver2.submit(t_a, priority=PriorityTrackResolver.PRIORITY_URGENT, on_done=lambda u: (results.append(u), ev1.set()))
            resolver2.submit(t_b, priority=PriorityTrackResolver.PRIORITY_NEXT, on_done=lambda u: (results.append(u), ev2.set()))
            self.assertTrue(ev1.wait(timeout=2.0))
            self.assertTrue(ev2.wait(timeout=2.0))
            self.assertEqual(dedup_calls, 1)
            self.assertEqual(results, ["https://stream/yt:same", "https://stream/yt:same"])
        finally:
            resolver2.close()

    def test_debounced_state_writer_coalesces_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            fp = os.path.join(tmpdir, "state.json")
            writer = DebouncedStateWriter(delay_sec=0.2)
            try:
                writer.schedule([(fp, {"v": 1})])
                writer.schedule([(fp, {"v": 2})])
                writer.schedule([(fp, {"v": 3})])
                writer.flush_sync()
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.assertEqual(data, {"v": 3})
            finally:
                writer.close()

    def test_pcm_stream_buffer_fifo_eof_and_close(self):
        buf = PCMStreamBuffer(max_chunks=4)
        self.assertTrue(buf.put_chunk(b"1111"))
        self.assertTrue(buf.put_chunk(b"2222"))
        buf.mark_eof()

        self.assertEqual(buf.get_chunk(timeout=0.5), (True, b"1111"))
        self.assertEqual(buf.get_chunk(timeout=0.5), (True, b"2222"))
        self.assertEqual(buf.get_chunk(timeout=0.5), (True, None))

        buf.close()
        self.assertFalse(buf.put_chunk(b"3333"))

    def test_tray_update_state_deduplicates_redraws(self):
        tray = TrayManager(
            on_open=lambda: None,
            on_play_pause=lambda: None,
            on_next=lambda: None,
            on_prev=lambda: None,
            on_toggle_like=lambda: None,
            on_quit=lambda: None,
            get_play_state=lambda: True,
            get_liked_state=lambda: False,
        )

        class _FakeIcon:
            def __init__(self):
                self.title = ""
                self.updates = 0

            def update_menu(self):
                self.updates += 1

        fake = _FakeIcon()
        tray._icon = fake
        tray._running = True

        tray.update_state("Song A", "Artist A")
        tray.update_state("Song A", "Artist A")
        self.assertEqual(fake.updates, 1)

        tray.update_state("Song B", "Artist B")
        self.assertEqual(fake.updates, 2)

    def test_google_sign_out_revokes_token_non_blocking(self):
        revoke_started = threading.Event()
        release_revoke = threading.Event()

        def slow_revoke(method, url, params=None, headers=None, data=None):
            revoke_started.set()
            release_revoke.wait(timeout=2.0)
            return _SimpleHttpResponse(200, b"")

        with tempfile.TemporaryDirectory() as tmpdir:
            client = GoogleCloudSyncClient(tmpdir, transport=FakeTransport(slow_revoke))
            client.set_session(
                GoogleAccountSession(
                    sub="sub-9",
                    email="fast@gmail.com",
                    name="Fast",
                    picture="",
                    access_token="tok-1",
                    refresh_token="ref-1",
                    expires_at=time.time() + 3600,
                )
            )
            t0 = time.monotonic()
            client.sign_out()
            elapsed = time.monotonic() - t0
            # Must return immediately instead of blocking on network I/O
            self.assertLess(elapsed, 0.35)
            self.assertFalse(client.is_signed_in)
            self.assertTrue(revoke_started.wait(timeout=1.0))
            release_revoke.set()


if __name__ == "__main__":
    unittest.main()
