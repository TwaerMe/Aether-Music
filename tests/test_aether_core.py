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

        # New tray & Google sync handlers must exist
        for required_fn in (
            "_on_window_close_request",
            "_restore_from_tray",
            "_quit_app",
            "_open_google_auth_dialog",
            "_sync_google_cloud_async",
            "_sign_out_google",
            "_refresh_sidebar_account_box",
        ):
            self.assertIn(required_fn, func_names)


if __name__ == "__main__":
    unittest.main()
