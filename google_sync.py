from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any, Callable

try:
    import requests as _requests
except ImportError:  # pragma: no cover - fallback for stdlib-only test environments
    _requests = None


GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USERINFO_URL = "https://www.googleapis.com/oauth2/v3/userinfo"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"
GOOGLE_DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
GOOGLE_DRIVE_UPLOAD_URL = "https://www.googleapis.com/upload/drive/v3/files"

GOOGLE_SCOPES = (
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/drive.appdata",
)

CLOUD_PROFILE_FILENAME = "aether_profile_v1.json"


class GoogleAuthError(Exception):
    """Ошибки авторизации или синхронизации с аккаунтом Google."""


def _atomic_write_json(path: str, payload: dict[str, Any], private: bool = False) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
    try:
        if private and os.name != "nt":
            try:
                os.fchmod(fd, 0o600)
            except Exception:
                pass
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


def _extract_track_uid(track_dict: dict[str, Any]) -> str:
    url = str(track_dict.get("webpage_url") or "")
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})", url)
    if m:
        return f"yt:{m.group(1)}"
    return (
        url
        or str(track_dict.get("filepath") or "")
        or f"{track_dict.get('artist', '')}:{track_dict.get('title', '')}"
    )


def build_pkce_pair() -> tuple[str, str]:
    """Генерирует пару (code_verifier, code_challenge) по RFC 7636 (S256)."""
    verifier = base64.urlsafe_b64encode(secrets.token_bytes(32)).rstrip(b"=").decode("ascii")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def build_auth_url(
    client_id: str,
    redirect_uri: str,
    code_challenge: str,
    state: str,
    scopes: tuple[str, ...] = GOOGLE_SCOPES,
) -> str:
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(scopes),
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": state,
        "access_type": "offline",
        "include_granted_scopes": "true",
        "prompt": "consent select_account",
    }
    return f"{GOOGLE_AUTH_URL}?{urllib.parse.urlencode(params)}"


@dataclass
class GoogleOAuthConfig:
    client_id: str
    client_secret: str = ""

    @property
    def is_valid(self) -> bool:
        cid = self.client_id.strip()
        return bool(cid) and ".apps.googleusercontent.com" in cid

    def to_dict(self) -> dict[str, str]:
        return {
            "client_id": self.client_id.strip(),
            "client_secret": self.client_secret.strip(),
        }

    @classmethod
    def parse_json_payload(cls, data: Any) -> "GoogleOAuthConfig":
        if not isinstance(data, dict):
            raise GoogleAuthError("Некорректный JSON-файл конфигурации Google OAuth.")

        section = data.get("installed") or data.get("web") or data
        if not isinstance(section, dict):
            raise GoogleAuthError("Не удалось найти параметры OAuth-клиента в JSON.")

        client_id = str(section.get("client_id") or "").strip()
        client_secret = str(section.get("client_secret") or "").strip()
        cfg = cls(client_id=client_id, client_secret=client_secret)
        if not cfg.is_valid:
            raise GoogleAuthError(
                "Некорректный Client ID. Он должен заканчиваться на .apps.googleusercontent.com"
            )
        return cfg

    @classmethod
    def load_from_file(cls, filepath: str) -> "GoogleOAuthConfig":
        try:
            with open(filepath, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception as exc:
            raise GoogleAuthError(f"Не удалось прочитать файл OAuth: {exc}") from exc
        return cls.parse_json_payload(data)

    @classmethod
    def discover(cls, app_data_dir: str) -> "GoogleOAuthConfig | None":
        env_id = os.environ.get("AETHER_GOOGLE_CLIENT_ID", "").strip()
        env_secret = os.environ.get("AETHER_GOOGLE_CLIENT_SECRET", "").strip()
        if env_id:
            cfg = cls(client_id=env_id, client_secret=env_secret)
            if cfg.is_valid:
                return cfg

        candidates = [
            os.path.join(app_data_dir, "google_client_secret.json"),
        ]

        # Поддержка файла рядом с main.py или собранным EXE (PyInstaller)
        if getattr(sys, "frozen", False):
            exe_dir = os.path.dirname(sys.executable)
            candidates.append(os.path.join(exe_dir, "google_client_secret.json"))
            meipass = getattr(sys, "_MEIPASS", None)
            if meipass:
                candidates.append(os.path.join(meipass, "google_client_secret.json"))
        else:
            src_dir = os.path.dirname(os.path.abspath(__file__))
            candidates.append(os.path.join(src_dir, "google_client_secret.json"))

        for path in candidates:
            if os.path.isfile(path):
                try:
                    return cls.load_from_file(path)
                except GoogleAuthError:
                    continue
        return None

    def save(self, app_data_dir: str) -> str:
        if not self.is_valid:
            raise GoogleAuthError(
                "Некорректный Client ID. Он должен заканчиваться на .apps.googleusercontent.com"
            )
        target = os.path.join(app_data_dir, "google_client_secret.json")
        _atomic_write_json(target, {"installed": self.to_dict()}, private=True)
        return target


@dataclass
class GoogleAccountSession:
    sub: str
    email: str
    name: str
    picture: str
    access_token: str
    refresh_token: str
    expires_at: float
    cloud_file_id: str = ""

    @property
    def is_access_token_fresh(self) -> bool:
        return bool(self.access_token) and time.time() < (self.expires_at - 90.0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sub": self.sub,
            "email": self.email,
            "name": self.name,
            "picture": self.picture,
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "cloud_file_id": self.cloud_file_id,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GoogleAccountSession | None":
        if not isinstance(data, dict):
            return None
        sub = str(data.get("sub") or "").strip()
        email = str(data.get("email") or "").strip()
        refresh_token = str(data.get("refresh_token") or "").strip()
        access_token = str(data.get("access_token") or "").strip()
        if not sub or (not refresh_token and not access_token):
            return None
        return cls(
            sub=sub,
            email=email,
            name=str(data.get("name") or email or "Google Account"),
            picture=str(data.get("picture") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=float(data.get("expires_at") or 0.0),
            cloud_file_id=str(data.get("cloud_file_id") or ""),
        )

    @classmethod
    def load(cls, path: str) -> "GoogleAccountSession | None":
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as f:
                return cls.from_dict(json.load(f))
        except Exception:
            return None

    def save(self, path: str) -> None:
        _atomic_write_json(path, self.to_dict(), private=True)


class _SimpleHttpResponse:
    def __init__(self, status_code: int, content: bytes):
        self.status_code = status_code
        self.content = content

    @property
    def text(self) -> str:
        return self.content.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self.text)


class _DefaultHttpTransport:
    """HTTP-клиент на базе requests (или stdlib urllib в окружении без requests)."""

    def __init__(self):
        self._session = _requests.Session() if _requests is not None else None
        if self._session is not None:
            self._session.headers.update({"User-Agent": "AetherMusic/2.1"})

    def request(
        self,
        method: str,
        url: str,
        *,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        data: dict[str, Any] | bytes | None = None,
        timeout: float = 12.0,
    ) -> _SimpleHttpResponse:
        if self._session is not None:
            resp = self._session.request(
                method=method,
                url=url,
                params=params,
                headers=headers,
                data=data,
                timeout=timeout,
            )
            return _SimpleHttpResponse(resp.status_code, resp.content)

        full_url = url
        if params:
            sep = "&" if "?" in full_url else "?"
            full_url = f"{full_url}{sep}{urllib.parse.urlencode(params)}"

        req_headers = {"User-Agent": "AetherMusic/2.1"}
        if headers:
            req_headers.update(headers)

        body_bytes: bytes | None = None
        if isinstance(data, dict):
            body_bytes = urllib.parse.urlencode(data).encode("utf-8")
            req_headers.setdefault("Content-Type", "application/x-www-form-urlencoded")
        elif isinstance(data, bytes):
            body_bytes = data

        req = urllib.request.Request(full_url, data=body_bytes, headers=req_headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return _SimpleHttpResponse(resp.status, resp.read())
        except urllib.error.HTTPError as err:
            return _SimpleHttpResponse(err.code, err.read())

    def close(self) -> None:
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass


def _render_callback_html(success: bool, title: str, message: str) -> bytes:
    accent = "#6BA882" if success else "#C96A6A"
    symbol = "✓" if success else "✕"
    safe_title = html.escape(title)
    safe_msg = html.escape(message)
    page = f"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8" />
<title>Aether Music — Google</title>
<style>
  body {{
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: #0D0F12; color: #F2F4F8; font-family: 'Segoe UI', system-ui, sans-serif;
  }}
  .card {{
    background: #14181F; border: 1px solid #232934; border-radius: 18px;
    padding: 36px 40px; max-width: 420px; text-align: center;
    box-shadow: 0 20px 50px rgba(0,0,0,0.45);
  }}
  .badge {{
    width: 56px; height: 56px; margin: 0 auto 18px; border-radius: 28px;
    background: #181D25; border: 2px solid {accent}; color: {accent};
    display: flex; align-items: center; justify-content: center;
    font-size: 26px; font-weight: 700;
  }}
  h1 {{ margin: 0 0 10px; font-size: 20px; font-weight: 700; }}
  p {{ margin: 0; color: #9099A8; font-size: 14px; line-height: 1.5; }}
</style>
</head>
<body>
  <div class="card">
    <div class="badge">{symbol}</div>
    <h1>{safe_title}</h1>
    <p>{safe_msg}</p>
  </div>
</body>
</html>"""
    return page.encode("utf-8")


class GoogleOAuthLoopbackFlow:
    """Выполняет OAuth 2.0 (PKCE + локальный loopback callback) через системный браузер."""

    def __init__(self, config: GoogleOAuthConfig, transport: Any | None = None):
        self.config = config
        self.transport = transport or _DefaultHttpTransport()
        self._cancel_event = threading.Event()

    def cancel(self) -> None:
        self._cancel_event.set()

    def run(
        self,
        timeout_sec: float = 180.0,
        open_browser_fn: Callable[[str], Any] = webbrowser.open,
    ) -> GoogleAccountSession:
        if not self.config.is_valid:
            raise GoogleAuthError("Не настроен Google OAuth Client ID.")

        verifier, challenge = build_pkce_pair()
        expected_state = secrets.token_urlsafe(24)
        result: dict[str, str | None] = {"code": None, "error": None}
        got_callback = threading.Event()

        class CallbackHandler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlparse(self.path)
                if parsed.path != "/callback":
                    self.send_response(404)
                    self.end_headers()
                    return

                qs = urllib.parse.parse_qs(parsed.query)
                recv_state = (qs.get("state") or [""])[0]
                recv_error = (qs.get("error") or [""])[0]
                recv_code = (qs.get("code") or [""])[0]

                if recv_error:
                    result["error"] = recv_error
                    body = _render_callback_html(
                        False,
                        "Вход отменен",
                        "Доступ к аккаунту Google не был выдан. Вы можете закрыть эту вкладку.",
                    )
                elif not recv_code or recv_state != expected_state:
                    result["error"] = "invalid_state"
                    body = _render_callback_html(
                        False,
                        "Ошибка проверки входа",
                        "Не удалось подтвердить сессию OAuth. Попробуйте войти снова в Aether Music.",
                    )
                else:
                    result["code"] = recv_code
                    body = _render_callback_html(
                        True,
                        "Вход выполнен",
                        "Аккаунт Google подключен к Aether Music. Эту вкладку можно закрыть.",
                    )

                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                got_callback.set()

        server = HTTPServer(("127.0.0.1", 0), CallbackHandler)
        server.timeout = 0.25
        port = server.server_address[1]
        redirect_uri = f"http://127.0.0.1:{port}/callback"
        auth_url = build_auth_url(
            client_id=self.config.client_id,
            redirect_uri=redirect_uri,
            code_challenge=challenge,
            state=expected_state,
        )

        try:
            open_browser_fn(auth_url)
            deadline = time.time() + timeout_sec
            while not got_callback.is_set():
                if self._cancel_event.is_set():
                    raise GoogleAuthError("Вход отменен пользователем.")
                if time.time() >= deadline:
                    raise GoogleAuthError("Время ожидания входа в браузере истекло.")
                server.handle_request()
        finally:
            try:
                server.server_close()
            except Exception:
                pass

        if result["error"]:
            if result["error"] == "access_denied":
                raise GoogleAuthError("Доступ отклонен в окне Google.")
            raise GoogleAuthError(f"Ошибка авторизации Google: {result['error']}")

        code = result["code"]
        if not code:
            raise GoogleAuthError("Не получен код авторизации от Google.")

        token_payload: dict[str, str] = {
            "client_id": self.config.client_id,
            "code": code,
            "code_verifier": verifier,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if self.config.client_secret:
            token_payload["client_secret"] = self.config.client_secret

        token_resp = self.transport.request("POST", GOOGLE_TOKEN_URL, data=token_payload, timeout=14.0)
        if token_resp.status_code != 200:
            raise GoogleAuthError(f"Google не выдал токен ({token_resp.status_code}). Проверьте Client ID/Secret.")

        tokens = token_resp.json()
        access_token = str(tokens.get("access_token") or "")
        refresh_token = str(tokens.get("refresh_token") or "")
        expires_in = float(tokens.get("expires_in") or 3600.0)
        if not access_token:
            raise GoogleAuthError("Пустой access_token в ответе Google.")

        info_resp = self.transport.request(
            "GET",
            GOOGLE_USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=12.0,
        )
        if info_resp.status_code != 200:
            raise GoogleAuthError("Не удалось загрузить профиль Google.")

        profile = info_resp.json()
        sub = str(profile.get("sub") or "")
        if not sub:
            raise GoogleAuthError("В профиле Google отсутствует идентификатор пользователя.")

        return GoogleAccountSession(
            sub=sub,
            email=str(profile.get("email") or ""),
            name=str(profile.get("name") or profile.get("email") or "Google User"),
            picture=str(profile.get("picture") or ""),
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=time.time() + expires_in,
        )


class GoogleCloudSyncClient:
    """Хранит и синхронизирует профиль Aether Music в скрытой папке Google Drive appDataFolder."""

    def __init__(self, app_data_dir: str, transport: Any | None = None):
        self.app_data_dir = app_data_dir
        self.auth_file = os.path.join(app_data_dir, "google_auth.json")
        self.transport = transport or _DefaultHttpTransport()
        self._lock = threading.Lock()
        self.config: GoogleOAuthConfig | None = GoogleOAuthConfig.discover(app_data_dir)
        self.session: GoogleAccountSession | None = GoogleAccountSession.load(self.auth_file)

    @property
    def is_signed_in(self) -> bool:
        return self.session is not None

    def reload_config(self) -> GoogleOAuthConfig | None:
        self.config = GoogleOAuthConfig.discover(self.app_data_dir)
        return self.config

    def set_config(self, config: GoogleOAuthConfig) -> None:
        config.save(self.app_data_dir)
        self.config = config

    def set_session(self, session: GoogleAccountSession) -> None:
        with self._lock:
            self.session = session
            session.save(self.auth_file)

    def ensure_access_token(self) -> str:
        with self._lock:
            sess = self.session
            cfg = self.config

        if sess is None:
            raise GoogleAuthError("Аккаунт Google не подключен.")
        if sess.is_access_token_fresh:
            return sess.access_token
        if not sess.refresh_token:
            raise GoogleAuthError("Сессия Google устарела. Войдите снова.")
        if cfg is None or not cfg.is_valid:
            raise GoogleAuthError("Отсутствует конфигурация Google OAuth для обновления токена.")

        payload: dict[str, str] = {
            "client_id": cfg.client_id,
            "refresh_token": sess.refresh_token,
            "grant_type": "refresh_token",
        }
        if cfg.client_secret:
            payload["client_secret"] = cfg.client_secret

        resp = self.transport.request("POST", GOOGLE_TOKEN_URL, data=payload, timeout=12.0)
        if resp.status_code != 200:
            raise GoogleAuthError("Не удалось обновить токен Google. Пожалуйста, войдите снова.")

        data = resp.json()
        new_token = str(data.get("access_token") or "")
        if not new_token:
            raise GoogleAuthError("Google вернул пустой токен при обновлении.")

        expires_in = float(data.get("expires_in") or 3600.0)
        with self._lock:
            if self.session is not None:
                self.session.access_token = new_token
                self.session.expires_at = time.time() + expires_in
                if data.get("refresh_token"):
                    self.session.refresh_token = str(data["refresh_token"])
                self.session.save(self.auth_file)
        return new_token

    def _find_cloud_file_id(self, token: str) -> str | None:
        resp = self.transport.request(
            "GET",
            GOOGLE_DRIVE_FILES_URL,
            params={
                "spaces": "appDataFolder",
                "q": f"name='{CLOUD_PROFILE_FILENAME}' and trashed=false",
                "fields": "files(id,name,modifiedTime)",
                "pageSize": 1,
            },
            headers={"Authorization": f"Bearer {token}"},
            timeout=12.0,
        )
        if resp.status_code != 200:
            raise GoogleAuthError(f"Ошибка доступа к Google Drive appData ({resp.status_code}).")
        files = resp.json().get("files") or []
        if files and files[0].get("id"):
            fid = str(files[0]["id"])
            with self._lock:
                if self.session is not None and self.session.cloud_file_id != fid:
                    self.session.cloud_file_id = fid
                    self.session.save(self.auth_file)
            return fid
        return None

    def pull_profile(self) -> dict[str, Any] | None:
        token = self.ensure_access_token()
        with self._lock:
            file_id = self.session.cloud_file_id if self.session else ""

        if not file_id:
            file_id = self._find_cloud_file_id(token) or ""
        if not file_id:
            return None

        resp = self.transport.request(
            "GET",
            f"{GOOGLE_DRIVE_FILES_URL}/{file_id}",
            params={"alt": "media"},
            headers={"Authorization": f"Bearer {token}"},
            timeout=14.0,
        )
        if resp.status_code == 404:
            with self._lock:
                if self.session is not None:
                    self.session.cloud_file_id = ""
            file_id = self._find_cloud_file_id(token) or ""
            if not file_id:
                return None
            resp = self.transport.request(
                "GET",
                f"{GOOGLE_DRIVE_FILES_URL}/{file_id}",
                params={"alt": "media"},
                headers={"Authorization": f"Bearer {token}"},
                timeout=14.0,
            )

        if resp.status_code != 200:
            raise GoogleAuthError(f"Не удалось скачать профиль из Google ({resp.status_code}).")

        data = resp.json()
        return data if isinstance(data, dict) else None

    def push_profile(self, payload: dict[str, Any]) -> bool:
        token = self.ensure_access_token()
        content_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        content_bytes = content_json.encode("utf-8")

        with self._lock:
            file_id = self.session.cloud_file_id if self.session else ""
        if not file_id:
            file_id = self._find_cloud_file_id(token) or ""

        if file_id:
            resp = self.transport.request(
                "PATCH",
                f"{GOOGLE_DRIVE_UPLOAD_URL}/{file_id}",
                params={"uploadType": "media", "fields": "id,modifiedTime"},
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json; charset=UTF-8",
                },
                data=content_bytes,
                timeout=15.0,
            )
            if resp.status_code == 200:
                return True
            if resp.status_code != 404:
                raise GoogleAuthError(f"Ошибка обновления облачного профиля ({resp.status_code}).")
            # Файл был удален — пересоздаем ниже
            with self._lock:
                if self.session is not None:
                    self.session.cloud_file_id = ""

        boundary = "aether_drive_sync_boundary_7f9a2c"
        meta_json = json.dumps(
            {"name": CLOUD_PROFILE_FILENAME, "parents": ["appDataFolder"]},
            ensure_ascii=False,
        )
        multipart_body = (
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{meta_json}\r\n"
            f"--{boundary}\r\n"
            "Content-Type: application/json; charset=UTF-8\r\n\r\n"
            f"{content_json}\r\n"
            f"--{boundary}--\r\n"
        ).encode("utf-8")

        resp = self.transport.request(
            "POST",
            GOOGLE_DRIVE_UPLOAD_URL,
            params={"uploadType": "multipart", "fields": "id,modifiedTime"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": f"multipart/related; boundary={boundary}",
            },
            data=multipart_body,
            timeout=15.0,
        )
        if resp.status_code not in (200, 201):
            raise GoogleAuthError(f"Не удалось создать облачный профиль ({resp.status_code}).")

        created_id = str(resp.json().get("id") or "")
        if created_id:
            with self._lock:
                if self.session is not None:
                    self.session.cloud_file_id = created_id
                    self.session.save(self.auth_file)
        return True

    def sign_out(self) -> None:
        with self._lock:
            sess = self.session
            self.session = None

        try:
            if os.path.exists(self.auth_file):
                os.remove(self.auth_file)
        except Exception:
            pass

        if sess and (sess.refresh_token or sess.access_token):
            token = sess.refresh_token or sess.access_token

            def _revoke():
                try:
                    self.transport.request("POST", GOOGLE_REVOKE_URL, data={"token": token}, timeout=4.0)
                except Exception:
                    pass

            threading.Thread(target=_revoke, daemon=True, name="google-revoke").start()

    def close(self) -> None:
        self.transport.close()


def _merge_timestamp_maps(a: dict[str, Any] | None, b: dict[str, Any] | None, limit: int = 400) -> dict[str, float]:
    merged: dict[str, float] = {}
    for src in (a or {}, b or {}):
        if not isinstance(src, dict):
            continue
        for k, v in src.items():
            try:
                ts = float(v)
            except (TypeError, ValueError):
                continue
            key = str(k)
            if key not in merged or ts > merged[key]:
                merged[key] = ts
    if len(merged) > limit:
        top_items = sorted(merged.items(), key=lambda kv: kv[1], reverse=True)[:limit]
        merged = dict(top_items)
    return merged


def merge_user_states(local_state: dict[str, Any], cloud_state: dict[str, Any] | None) -> dict[str, Any]:
    """
    Детерминированно объединяет локальный профиль и облачный профиль Google Drive.
    Сохраняет лайки с обоих устройств, учитывает явные снятия лайков (unliked_timestamps),
    объединяет рекомендации (artist_scores, seed_videos, custom_seeds) и историю.
    """
    if not cloud_state or not isinstance(cloud_state, dict):
        return dict(local_state)

    local_ts = float(local_state.get("updated_at") or 0.0)
    cloud_ts = float(cloud_state.get("updated_at") or 0.0)
    newer, older = (local_state, cloud_state) if local_ts >= cloud_ts else (cloud_state, local_state)

    # 1. Лайкнутые треки с учетом timestamp лайка и снятия лайка
    liked_ts = _merge_timestamp_maps(local_state.get("liked_timestamps"), cloud_state.get("liked_timestamps"), limit=1500)
    unliked_ts = _merge_timestamp_maps(local_state.get("unliked_timestamps"), cloud_state.get("unliked_timestamps"), limit=500)

    track_by_uid: dict[str, dict[str, Any]] = {}
    order_rank: dict[str, int] = {}
    rank_cursor = 0
    for source_state in (newer, older):
        for item in source_state.get("liked_tracks") or []:
            if not isinstance(item, dict):
                continue
            uid = _extract_track_uid(item)
            if uid not in track_by_uid:
                track_by_uid[uid] = item
                order_rank[uid] = rank_cursor
                rank_cursor += 1
            if uid not in liked_ts:
                liked_ts[uid] = float(source_state.get("updated_at") or 1.0)

    merged_liked_uids: list[str] = []
    for uid in track_by_uid:
        l_ts = liked_ts.get(uid, 0.0)
        u_ts = unliked_ts.get(uid, 0.0)
        if u_ts > l_ts:
            continue
        merged_liked_uids.append(uid)

    merged_liked_uids.sort(key=lambda u: (-liked_ts.get(u, 0.0), order_rank.get(u, 0)))
    merged_liked_tracks = [track_by_uid[u] for u in merged_liked_uids]
    liked_uid_set = set(merged_liked_uids)

    # 2. История прослушивания (без дублей, сначала свежие)
    merged_history: list[dict[str, Any]] = []
    seen_hist: set[str] = set()
    for source_state in (newer, older):
        for item in source_state.get("history_tracks") or []:
            if not isinstance(item, dict):
                continue
            uid = _extract_track_uid(item)
            if uid not in seen_hist:
                seen_hist.add(uid)
                merged_history.append(item)
            if len(merged_history) >= 80:
                break
        if len(merged_history) >= 80:
            break

    # 3. Дизлайки (кроме треков, которые снова лайкнули)
    disliked_list: list[str] = []
    seen_disliked: set[str] = set()
    for source_state in (newer, older):
        for uid in source_state.get("disliked_uids") or []:
            s_uid = str(uid)
            if s_uid in liked_uid_set or s_uid in seen_disliked:
                continue
            seen_disliked.add(s_uid)
            disliked_list.append(s_uid)
    disliked_list = disliked_list[:500]

    # 4. Веса артистов (рекомендации)
    newer_scores = newer.get("artist_scores") or {}
    older_scores = older.get("artist_scores") or {}
    merged_scores: dict[str, float] = {}
    all_artists = set(newer_scores.keys()) | set(older_scores.keys())
    for artist in all_artists:
        in_new = artist in newer_scores
        in_old = artist in older_scores
        if in_new and in_old:
            nv = float(newer_scores[artist])
            ov = float(older_scores[artist])
            if (nv >= 0 and ov >= 0) or (nv <= 0 and ov <= 0):
                val = nv * 0.7 + ov * 0.3
            else:
                val = nv
        elif in_new:
            val = float(newer_scores[artist])
        else:
            val = float(older_scores[artist])
        merged_scores[str(artist)] = round(max(-15.0, min(40.0, val)), 3)

    # 5. Seed-видео для «Моей волны»
    merged_seeds: list[dict[str, Any]] = []
    seen_vids: set[str] = set()
    for source_state in (older, newer):
        for seed in source_state.get("seed_videos") or []:
            if not isinstance(seed, dict):
                continue
            vid = str(seed.get("vid") or "").strip()
            if not vid:
                continue
            if vid in seen_vids:
                merged_seeds = [s for s in merged_seeds if s.get("vid") != vid]
            seen_vids.add(vid)
            merged_seeds.append(seed)
    merged_seeds = merged_seeds[-45:]

    # 6. Ручные ориентиры вкуса с учетом удалений
    seed_add_ts = _merge_timestamp_maps(local_state.get("custom_seed_timestamps"), cloud_state.get("custom_seed_timestamps"), limit=100)
    seed_rem_ts = _merge_timestamp_maps(local_state.get("removed_seed_timestamps"), cloud_state.get("removed_seed_timestamps"), limit=100)
    merged_custom_seeds: list[str] = []
    seen_custom_lower: set[str] = set()
    for source_state in (newer, older):
        for raw_seed in source_state.get("custom_seeds") or []:
            s = str(raw_seed).strip()
            if not s:
                continue
            key = s.lower()
            if key in seen_custom_lower:
                continue
            add_t = seed_add_ts.get(key, float(source_state.get("updated_at") or 1.0))
            rem_t = seed_rem_ts.get(key, 0.0)
            if rem_t > add_t:
                continue
            seen_custom_lower.add(key)
            merged_custom_seeds.append(s)
            if len(merged_custom_seeds) >= 12:
                break
        if len(merged_custom_seeds) >= 12:
            break

    # 7. Настройки плеера берем из более свежей версии
    return {
        "schema_version": 2,
        "updated_at": max(local_ts, cloud_ts, time.time()),
        "liked_tracks": merged_liked_tracks,
        "liked_timestamps": liked_ts,
        "unliked_timestamps": unliked_ts,
        "history_tracks": merged_history,
        "disliked_uids": disliked_list,
        "artist_scores": merged_scores,
        "seed_videos": merged_seeds,
        "custom_seeds": merged_custom_seeds,
        "custom_seed_timestamps": seed_add_ts,
        "removed_seed_timestamps": seed_rem_ts,
        "volume": float(newer.get("volume", local_state.get("volume", 0.72))),
        "eq_preset": str(newer.get("eq_preset", local_state.get("eq_preset", "flat"))),
        "normalize_audio": bool(newer.get("normalize_audio", local_state.get("normalize_audio", False))),
        "wave_mood": str(newer.get("wave_mood", local_state.get("wave_mood", "Любое"))),
    }
