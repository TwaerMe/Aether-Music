import colorsys
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import ctypes
import hashlib
import io
import json
import os
import random
import re
import subprocess
import tempfile
import threading
import time
import tkinter as tk
from tkinter import filedialog
import webbrowser

import customtkinter as ctk
import imageio_ffmpeg
import numpy as np
import requests
from requests.adapters import HTTPAdapter
import sounddevice as sd
import yt_dlp
from mutagen import File as MutagenFile
from mutagen.id3 import APIC
from PIL import Image, ImageDraw, ImageTk

from google_sync import (
    GoogleAuthError,
    GoogleCloudSyncClient,
    GoogleOAuthConfig,
    GoogleOAuthLoopbackFlow,
    merge_user_states,
)
from tray_manager import TrayManager, create_app_icon_image

# ==========================================
# ПАЛИТРА (МИНИМАЛИЗМ В ДУХЕ SPOTIFY / ЯНДЕКС МУЗЫКИ, БЕЗ НЕОНА)
# ==========================================
THEME = {
    "bg_root": "#0D0F12",        # Основной матовый фон
    "bg_sidebar": "#111419",     # Левая панель навигации
    "bg_bar": "#14181F",         # Нижняя панель плеера
    "bg_card": "#181D25",        # Карточки и активные строки
    "bg_card_alt": "#1D232D",    # Вторичные поверхности
    "bg_hover": "#232A36",       # Наведение
    "border": "#232934",         # Тонкие разделители
    "text_main": "#F2F4F8",      # Основной мягкий белый
    "text_muted": "#9099A8",     # Вторичный серый
    "text_dim": "#5A6372",       # Приглушенный текст
    "wave_gold": "#C8A464",      # Спокойный песочно-золотой акцент
    "wave_soft": "#28241C",      # Мягкая подложка акцента
    "like_red": "#CC6B6B",       # Приглушенный коралловый для лайков
    "ok_green": "#6BA882",       # Спокойный зеленый для статусов
    "err_red": "#C96A6A",        # Ошибки
}

EQ_PRESETS: dict[str, tuple[str, str]] = {
    "flat": ("EQ: Ровный", ""),
    "bass": ("EQ: Бас", "bass=g=5.5:f=105:w=0.65,treble=g=1.2:f=7500:w=0.7"),
    "vocal": ("EQ: Вокал", "equalizer=f=220:t=q:w=1.0:g=-1.5,equalizer=f=2800:t=q:w=1.2:g=3.5,treble=g=1.5:f=8500:w=0.7"),
    "night": ("EQ: Ночной", "bass=g=2.0:f=95:w=0.7,treble=g=-2.5:f=7000:w=0.7,acompressor=threshold=-20dB:ratio=3:attack=15:release=180"),
    "drive": ("EQ: Драйв", "bass=g=4.0:f=90:w=0.65,equalizer=f=1200:t=q:w=1.0:g=-1.0,treble=g=3.0:f=9000:w=0.7"),
}
EQ_ORDER = ["flat", "bass", "vocal", "night", "drive"]

ctk.set_appearance_mode("dark")

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".aether_player")
COVER_CACHE_DIR = os.path.join(APP_DATA_DIR, "covers")
STATE_FILE = os.path.join(APP_DATA_DIR, "user_state.json")
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "Aether")

# Надежные опции yt-dlp без конфликта ios + skip:hls
YDL_FAST_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,
    "nocheckcertificate": True,
    "no_check_formats": True,
    "format": "bestaudio[ext=m4a][protocol^=http]/bestaudio[acodec=opus][protocol^=http]/bestaudio/best",
    "noplaylist": True,
    "socket_timeout": 12,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
            "skip": ["translated_subs"],
        }
    },
}

YDL_FALLBACK_EXTRACT_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,
    "nocheckcertificate": True,
    "format": "bestaudio/best",
    "noplaylist": True,
    "socket_timeout": 15,
}

YDL_FAST_SEARCH_OPTS = {
    "quiet": True,
    "no_warnings": True,
    "ignoreerrors": True,
    "extract_flat": True,
    "skip_download": True,
    "nocheckcertificate": True,
    "socket_timeout": 12,
    "extractor_args": {
        "youtube": {
            "player_client": ["android", "web"],
        }
    },
}


# ==========================================
# ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ
# ==========================================
def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def clean_artist_name(artist: str) -> str:
    if not artist:
        return ""
    cleaned = re.sub(r"\s*-\s*Topic$", "", artist, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s*(Official|VEVO|Music)$", "", cleaned, flags=re.IGNORECASE)
    return cleaned.strip()


def normalize_track_signature(title: str, artist: str = "") -> str:
    """Нормализует название для защиты от дублей одной песни с разных каналов."""
    text = f"{artist} {title}".lower()
    text = re.sub(
        r"[\(\[\{].*?(official|video|audio|lyrics|lyric|hd|4k|mv|клип|премьера|музыка|live|visualizer).*?[\)\]\}]",
        " ",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"[^a-zа-яё0-9]+", " ", text)
    tokens = [t for t in text.split() if len(t) > 1]
    return " ".join(tokens[:6])


def extract_video_id(url: str | None) -> str | None:
    if not url:
        return None
    m = re.search(r"(?:v=|youtu\.be/|/shorts/|/embed/)([0-9A-Za-z_-]{11})", url)
    return m.group(1) if m else None


def make_circle_avatar(img: Image.Image, size=(34, 34)) -> Image.Image:
    return make_rounded_cover(img, size=size, radius=size[0] // 2)


def extract_stream_url_from_info(info: dict | None) -> str | None:
    """Надежно достает прямую аудиоссылку из ответа yt-dlp (включая requested_formats/formats)."""
    if not info:
        return None

    direct = info.get("url")
    if direct:
        return direct

    req_formats = info.get("requested_formats") or []
    for fmt in req_formats:
        if fmt and fmt.get("url") and fmt.get("acodec") not in (None, "none"):
            return fmt["url"]

    formats = info.get("formats") or []
    audio_candidates = []
    for fmt in formats:
        if not fmt or not fmt.get("url"):
            continue
        if fmt.get("acodec") in (None, "none"):
            continue
        is_audio_only = fmt.get("vcodec") in (None, "none")
        proto = str(fmt.get("protocol") or "")
        is_http = proto.startswith("http")
        abr = float(fmt.get("abr") or fmt.get("tbr") or 0.0)
        ext_bonus = 20 if fmt.get("ext") in ("m4a", "webm", "opus") else 0
        score = (1000 if is_audio_only else 0) + (200 if is_http else 0) + ext_bonus + abr
        audio_candidates.append((score, fmt["url"]))

    if audio_candidates:
        audio_candidates.sort(key=lambda x: x[0], reverse=True)
        return audio_candidates[0][1]

    return None


def make_rounded_cover(img: Image.Image, size=(56, 56), radius=10) -> Image.Image:
    img = img.convert("RGBA")
    w, h = img.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    img = img.crop((left, top, left + side, top + side))
    img = img.resize(size, Image.Resampling.LANCZOS)

    mask = Image.new("L", size, 0)
    draw = ImageDraw.Draw(mask)
    draw.rounded_rectangle((0, 0, size[0], size[1]), radius=radius, fill=255)
    img.putalpha(mask)
    return img


def create_placeholder_cover(size=(56, 56), radius=10, accent="#C8A464") -> Image.Image:
    img = Image.new("RGBA", size, THEME["bg_card"])
    draw = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    r1 = max(6, int(min(size) * 0.30))
    r2 = max(2, int(min(size) * 0.10))
    draw.ellipse((cx - r1, cy - r1, cx + r1, cy + r1), outline=accent, width=2)
    draw.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), outline=THEME["text_muted"], width=1)
    return make_rounded_cover(img, size=size, radius=radius)


def extract_muted_accent(img: Image.Image) -> tuple[str, str]:
    """Извлекает цвет обложки и приглушает насыщенность (без кислотного неона)."""
    try:
        small = img.convert("RGB").resize((28, 28))
        pixels = list(small.getdata())
        r_sum = g_sum = b_sum = count = 0
        for r, g, b in pixels:
            lum = (r + g + b) // 3
            if 28 < lum < 225:
                r_sum += r
                g_sum += g
                b_sum += b
                count += 1
        if count == 0:
            return THEME["wave_gold"], THEME["wave_soft"]

        r, g, b = (r_sum / count) / 255.0, (g_sum / count) / 255.0, (b_sum / count) / 255.0
        h, l, s = colorsys.rgb_to_hls(r, g, b)

        s_muted = min(max(s, 0.22), 0.40)
        l_main = min(max(l, 0.56), 0.65)
        r1, g1, b1 = colorsys.hls_to_rgb(h, l_main, s_muted)
        accent_hex = f"#{int(r1 * 255):02x}{int(g1 * 255):02x}{int(b1 * 255):02x}"

        r2, g2, b2 = colorsys.hls_to_rgb(h, 0.17, min(s_muted, 0.25))
        soft_hex = f"#{int(r2 * 255):02x}{int(g2 * 255):02x}{int(b2 * 255):02x}"
        return accent_hex, soft_hex
    except Exception:
        return THEME["wave_gold"], THEME["wave_soft"]


def hex_adjust_brightness(hex_color: str, factor: float = 1.12) -> str:
    try:
        hc = hex_color.lstrip("#")
        r = min(255, max(0, int(int(hc[0:2], 16) * factor)))
        g = min(255, max(0, int(int(hc[2:4], 16) * factor)))
        b = min(255, max(0, int(int(hc[4:6], 16) * factor)))
        return f"#{r:02x}{g:02x}{b:02x}"
    except Exception:
        return hex_color


def set_windows_app_id():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Aether.Music.Player.2")
        except Exception:
            pass


# ==========================================
# LRU-КЭШ ИЗОБРАЖЕНИЙ + ПУЛ HTTP-СЕССИЙ
# ==========================================
class CoverCacheManager:
    def __init__(self, max_mem_items: int = 160):
        self.max_mem_items = max_mem_items
        self._mem_cache: OrderedDict[str, ctk.CTkImage] = OrderedDict()
        self._inflight: dict[str, list] = {}
        self._lock = threading.Lock()
        self._pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="cover")

        self._session = requests.Session()
        adapter = HTTPAdapter(pool_connections=8, pool_maxsize=8, max_retries=1)
        self._session.mount("http://", adapter)
        self._session.mount("https://", adapter)
        self._session.headers.update({"User-Agent": "AetherMusic/2.0"})

        try:
            os.makedirs(COVER_CACHE_DIR, exist_ok=True)
        except Exception:
            pass

    def get_mem(self, key: str) -> ctk.CTkImage | None:
        img = self._mem_cache.get(key)
        if img is not None:
            self._mem_cache.move_to_end(key)
        return img

    def put_mem(self, key: str, img: ctk.CTkImage):
        self._mem_cache[key] = img
        self._mem_cache.move_to_end(key)
        while len(self._mem_cache) > self.max_mem_items:
            self._mem_cache.popitem(last=False)

    def _disk_path(self, url: str) -> str:
        digest = hashlib.sha1(url.encode("utf-8", errors="ignore")).hexdigest()
        return os.path.join(COVER_CACHE_DIR, f"{digest}.jpg")

    def fetch_pil_async(self, url: str, callback):
        """Скачивает (или берет с диска) PIL.Image через общий пул и вызывает callback(pil_img)."""
        with self._lock:
            if url in self._inflight:
                self._inflight[url].append(callback)
                return
            self._inflight[url] = [callback]

        def worker():
            pil_img = None
            dpath = self._disk_path(url)
            try:
                if os.path.exists(dpath):
                    with Image.open(dpath) as im:
                        pil_img = im.convert("RGB")
                else:
                    resp = self._session.get(url, timeout=6)
                    if resp.status_code == 200 and resp.content:
                        with Image.open(io.BytesIO(resp.content)) as im:
                            pil_img = im.convert("RGB")
                        try:
                            tmp = dpath + ".tmp"
                            pil_img.save(tmp, format="JPEG", quality=85)
                            os.replace(tmp, dpath)
                        except Exception:
                            pass
            except Exception:
                pil_img = None

            with self._lock:
                cbs = self._inflight.pop(url, [])

            if pil_img is not None:
                for cb in cbs:
                    try:
                        cb(pil_img)
                    except Exception:
                        pass

        self._pool.submit(worker)

    def shutdown(self):
        try:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._session.close()
        except Exception:
            pass


# ==========================================
# МОДЕЛЬ ТРЕКА
# ==========================================
class TrackItem:
    def __init__(
        self,
        title: str,
        artist: str = "Неизвестный исполнитель",
        duration: float = 0.0,
        filepath: str | None = None,
        webpage_url: str | None = None,
        thumbnail_url: str | None = None,
        source_type: str = "YT",  # "YT" | "LOCAL"
        reason: str | None = None,
    ):
        self.title = title
        self.artist = clean_artist_name(artist) or "Неизвестный исполнитель"
        self.duration = float(duration or 0.0)
        self.filepath = filepath
        self.webpage_url = webpage_url
        self.thumbnail_url = thumbnail_url
        self.source_type = source_type
        self.reason = reason

        self.stream_url: str | None = None
        self.stream_resolved_at: float = 0.0
        self.cover_pil: Image.Image | None = None

        # Потокобезопасный резолв без гонок
        self._resolve_lock = threading.Lock()
        self._resolve_event: threading.Event | None = None

    @property
    def uid(self) -> str:
        vid = extract_video_id(self.webpage_url)
        if vid:
            return f"yt:{vid}"
        return self.webpage_url or self.filepath or f"{self.artist}:{self.title}"

    @property
    def signature(self) -> str:
        return normalize_track_signature(self.title, self.artist)

    @property
    def video_id(self) -> str | None:
        return extract_video_id(self.webpage_url)

    @property
    def is_online(self) -> bool:
        return self.source_type == "YT"

    @property
    def has_fresh_stream(self) -> bool:
        return bool(self.stream_url) and (time.time() - self.stream_resolved_at) < 5400

    def invalidate_stream(self):
        self.stream_url = None
        self.stream_resolved_at = 0.0

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "artist": self.artist,
            "duration": self.duration,
            "filepath": self.filepath,
            "webpage_url": self.webpage_url,
            "thumbnail_url": self.thumbnail_url,
            "source_type": self.source_type,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "TrackItem":
        src = data.get("source_type", "YT")
        if src == "SC":
            src = "YT"
        return cls(
            title=data.get("title", "Без названия"),
            artist=data.get("artist", "Неизвестный исполнитель"),
            duration=float(data.get("duration", 0.0)),
            filepath=data.get("filepath"),
            webpage_url=data.get("webpage_url"),
            thumbnail_url=data.get("thumbnail_url"),
            source_type=src,
        )

    @classmethod
    def from_local_file(cls, filepath: str) -> "TrackItem":
        filename = os.path.basename(filepath)
        title = os.path.splitext(filename)[0]
        artist = "Локальный файл"
        duration = 0.0
        cover_pil = None

        try:
            audio = MutagenFile(filepath)
            if audio is not None:
                if hasattr(audio, "info") and hasattr(audio.info, "length"):
                    duration = float(audio.info.length)
                if audio.tags:
                    if "TIT2" in audio.tags:
                        title = str(audio.tags["TIT2"].text[0])
                    elif "title" in audio.tags:
                        title = str(audio.tags["title"][0])
                    if "TPE1" in audio.tags:
                        artist = str(audio.tags["TPE1"].text[0])
                    elif "artist" in audio.tags:
                        artist = str(audio.tags["artist"][0])
                    for tag in audio.tags.values():
                        if isinstance(tag, APIC):
                            with Image.open(io.BytesIO(tag.data)) as im:
                                cover_pil = im.convert("RGB")
                            break
                    if cover_pil is None and hasattr(audio, "pictures") and audio.pictures:
                        with Image.open(io.BytesIO(audio.pictures[0].data)) as im:
                            cover_pil = im.convert("RGB")
        except Exception:
            pass

        item = cls(
            title=title,
            artist=artist,
            duration=duration,
            filepath=filepath,
            source_type="LOCAL",
        )
        item.cover_pil = cover_pil
        return item


# ==========================================
# УЛУЧШЕННЫЙ ПОТОКОВЫЙ АУДИОДВИЖОК
# ==========================================
class StreamAudioEngine:
    SUPPORTED_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a", ".opus", ".aac")
    HTTP_PAUSE_IDLE_LIMIT = 8.0  # После 8 сек паузы закрываем HTTP-сокет и переподключаемся при resume

    def __init__(self, on_track_end_callback=None, on_stream_error_callback=None):
        self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.on_track_end = on_track_end_callback
        self.on_stream_error = on_stream_error_callback

        self.volume: float = 0.72
        self.muted: bool = False
        self.eq_preset: str = "flat"
        self.normalize_audio: bool = False

        self.is_playing: bool = False
        self.is_paused: bool = False
        self.sample_rate: int = 44100

        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._session_id: int = 0

        self._current_source: str | None = None
        self._expected_duration: float = 0.0
        self._base_offset: float = 0.0
        self._frames_played: int = 0
        self._paused_at_pos: float = 0.0
        self._paused_detached: bool = False

        # 5 полос для спокойного минималистичного индикатора уровня
        self.spectrum_bars: list[float] = [0.0, 0.0, 0.0, 0.0, 0.0]

    def _build_af_string(self) -> str:
        parts = []
        _, eq_filter = EQ_PRESETS.get(self.eq_preset, ("", ""))
        if eq_filter:
            parts.append(eq_filter)
        if self.normalize_audio:
            parts.append("dynaudnorm=f=150:g=15:p=0.92")
        return ",".join(parts)

    def _kill_proc_safe(self, proc: subprocess.Popen | None):
        if not proc:
            return
        try:
            if proc.stdout:
                proc.stdout.close()
        except Exception:
            pass
        try:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=0.8)
                except subprocess.TimeoutExpired:
                    proc.kill()
        except Exception:
            pass

    def play_source(self, source: str, start_sec: float = 0.0, expected_duration: float = 0.0):
        with self._lock:
            self._session_id += 1
            sid = self._session_id
            old_proc = self._proc
            self._proc = None
            self._stop_event.set()

        self._kill_proc_safe(old_proc)

        is_http = source.startswith("http://") or source.startswith("https://")
        af_str = self._build_af_string()

        with self._lock:
            if sid != self._session_id:
                return
            self._current_source = source
            self._expected_duration = max(0.0, float(expected_duration or 0.0))
            self._base_offset = max(0.0, float(start_sec or 0.0))
            self._frames_played = 0
            self._paused_at_pos = self._base_offset
            self._paused_detached = False
            self.is_playing = True
            self.is_paused = False
            self._stop_event = threading.Event()
            stop_ev = self._stop_event

        threading.Thread(
            target=self._playback_worker,
            args=(sid, source, is_http, self._base_offset, af_str, stop_ev),
            daemon=True,
            name=f"audio-{sid}",
        ).start()

    def _spawn_ffmpeg(self, source: str, is_http: bool, start_sec: float, af_str: str) -> subprocess.Popen:
        cmd = [self.ffmpeg_exe]
        cmd.extend([
            "-analyzeduration", "250000",
            "-probesize", "32768",
            "-fflags", "+nobuffer",
            "-flags", "low_delay",
        ])
        if is_http:
            cmd.extend([
                "-reconnect", "1",
                "-reconnect_streamed", "1",
                "-reconnect_at_eof", "0",
                "-reconnect_delay_max", "3",
            ])
        if start_sec > 0.15:
            cmd.extend(["-ss", f"{start_sec:.2f}"])

        cmd.extend(["-i", source, "-vn"])
        if af_str:
            cmd.extend(["-af", af_str])

        cmd.extend([
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", "2",
            "-loglevel", "error",
            "pipe:1",
        ])

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        return subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            creationflags=creationflags,
        )

    def _update_spectrum(self, samples_f32: np.ndarray):
        try:
            mono = samples_f32[0::2]
            if len(mono) < 256:
                return
            windowed = mono[:512] * np.hanning(min(512, len(mono)))
            spec = np.abs(np.fft.rfft(windowed))
            if len(spec) < 32:
                return
            # 5 логарифмических полос (суббас, бас, середина, верхняя середина, верха)
            slices = [(1, 4), (4, 10), (10, 26), (26, 65), (65, min(180, len(spec)))]
            new_bars = []
            for i, (a, b) in enumerate(slices):
                val = float(np.mean(spec[a:b])) / 140000.0
                val = min(1.0, max(0.0, val ** 0.65))
                prev = self.spectrum_bars[i]
                smoothed = prev * 0.55 + val * 0.45
                new_bars.append(smoothed)
            self.spectrum_bars = new_bars
        except Exception:
            pass

    def _playback_worker(
        self,
        sid: int,
        source: str,
        is_http: bool,
        start_sec: float,
        af_str: str,
        stop_ev: threading.Event,
    ):
        reconnect_attempts = 0
        max_reconnects = 2 if is_http else 0
        cur_start = start_sec
        finished_naturally = False
        had_fatal_error = False
        fade_in_samples_left = int(self.sample_rate * 0.025) * 2  # ~25мс мягкий старт без щелчка

        chunk_bytes = 8192  # ~46мс стерео int16 — плавно, без треска при скролле UI

        try:
            with sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=2,
                dtype="int16",
                blocksize=2048,
            ) as stream:
                while not stop_ev.is_set() and sid == self._session_id:
                    try:
                        proc = self._spawn_ffmpeg(source, is_http, cur_start, af_str)
                    except Exception:
                        had_fatal_error = True
                        break

                    with self._lock:
                        if sid != self._session_id or stop_ev.is_set():
                            self._kill_proc_safe(proc)
                            return
                        self._proc = proc

                    pause_started_at: float | None = None
                    detached_on_pause = False
                    got_any_audio = False

                    while not stop_ev.is_set() and sid == self._session_id:
                        if self.is_paused:
                            self.spectrum_bars = [b * 0.7 for b in self.spectrum_bars]
                            if pause_started_at is None:
                                pause_started_at = time.time()
                                self._paused_at_pos = self.get_position()

                            # Если онлайн-поток на паузе > 8 сек — закрываем ffmpeg,
                            # чтобы YouTube не разорвал сокет с ложным EOF
                            if is_http and not detached_on_pause and (time.time() - pause_started_at) >= self.HTTP_PAUSE_IDLE_LIMIT:
                                detached_on_pause = True
                                self._paused_detached = True
                                self._kill_proc_safe(proc)
                                with self._lock:
                                    if self._proc is proc:
                                        self._proc = None
                                break

                            time.sleep(0.03)
                            continue

                        pause_started_at = None
                        raw = proc.stdout.read(chunk_bytes) if proc.stdout else b""
                        if not raw:
                            break

                        got_any_audio = True
                        # Выравниваем по 4 байта (1 стерео сэмпл int16)
                        rem = len(raw) % 4
                        if rem:
                            raw = raw[:-rem]
                        if not raw:
                            continue

                        samples = np.frombuffer(raw, dtype=np.int16).astype(np.float32)

                        # Логарифмическая регулировка громкости
                        gain = 0.0 if self.muted else (self.volume ** 2.2)
                        if gain != 1.0:
                            samples *= gain

                        # Короткий fade-in без щелчка
                        if fade_in_samples_left > 0:
                            n = min(len(samples), fade_in_samples_left)
                            ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                            samples[:n] *= ramp
                            fade_in_samples_left -= n

                        self._update_spectrum(samples)
                        np.clip(samples, -32768.0, 32767.0, out=samples)
                        out_bytes = samples.astype(np.int16).tobytes()

                        stream.write(out_bytes)
                        self._frames_played += len(out_bytes) // 4

                    # Вышли из внутреннего цикла чтения
                    if stop_ev.is_set() or sid != self._session_id:
                        self._kill_proc_safe(proc)
                        return

                    # Если вышли из-за долгой паузы на HTTP — спокойно ждем снятия паузы и переподключаемся
                    if detached_on_pause:
                        while self.is_paused and not stop_ev.is_set() and sid == self._session_id:
                            self.spectrum_bars = [b * 0.7 for b in self.spectrum_bars]
                            time.sleep(0.04)
                        if stop_ev.is_set() or sid != self._session_id:
                            return
                        cur_start = self._paused_at_pos
                        self._base_offset = cur_start
                        self._frames_played = 0
                        self._paused_detached = False
                        fade_in_samples_left = int(self.sample_rate * 0.025) * 2
                        continue

                    rc = proc.poll()
                    if rc is None:
                        try:
                            rc = proc.wait(timeout=0.5)
                        except subprocess.TimeoutExpired:
                            rc = -1
                    self._kill_proc_safe(proc)

                    pos = self.get_position()
                    premature_eof = (
                        is_http
                        and self._expected_duration > 15.0
                        and pos < (self._expected_duration - 4.0)
                        and (rc != 0 or not got_any_audio)
                    )

                    if premature_eof and reconnect_attempts < max_reconnects:
                        reconnect_attempts += 1
                        cur_start = pos
                        self._base_offset = cur_start
                        self._frames_played = 0
                        fade_in_samples_left = int(self.sample_rate * 0.025) * 2
                        time.sleep(0.4)
                        continue

                    if got_any_audio or rc == 0:
                        finished_naturally = True
                    else:
                        had_fatal_error = True
                    break

        except Exception:
            had_fatal_error = True
        finally:
            self.spectrum_bars = [0.0, 0.0, 0.0, 0.0, 0.0]

        if sid != self._session_id or stop_ev.is_set():
            return

        self.is_playing = False
        self.is_paused = False
        if finished_naturally and self.on_track_end:
            self.on_track_end()
        elif had_fatal_error and self.on_stream_error:
            self.on_stream_error()

    def stop(self):
        with self._lock:
            self._session_id += 1
            self._stop_event.set()
            old_proc = self._proc
            self._proc = None
            self.is_playing = False
            self.is_paused = False
            self._paused_detached = False
            self.spectrum_bars = [0.0, 0.0, 0.0, 0.0, 0.0]
        self._kill_proc_safe(old_proc)

    def toggle_pause(self):
        if not self.is_playing:
            return
        if not self.is_paused:
            self._paused_at_pos = self.get_position()
            self.is_paused = True
        else:
            self.is_paused = False

    def seek(self, seconds: float):
        if self._current_source and self.is_playing:
            was_paused = self.is_paused
            target = max(0.0, float(seconds))
            if self._expected_duration > 0:
                target = min(target, max(0.0, self._expected_duration - 0.5))
            self.play_source(self._current_source, start_sec=target, expected_duration=self._expected_duration)
            if was_paused:
                self._paused_at_pos = target
                self.is_paused = True

    def set_volume(self, value: float):
        self.volume = max(0.0, min(1.0, float(value)))
        if self.volume > 0.0 and self.muted:
            self.muted = False

    def toggle_mute(self) -> bool:
        self.muted = not self.muted
        return self.muted

    def set_eq_preset(self, preset_key: str):
        if preset_key not in EQ_PRESETS:
            return
        self.eq_preset = preset_key
        if self.is_playing and self._current_source:
            self.seek(self.get_position())

    def set_normalize(self, enabled: bool):
        self.normalize_audio = bool(enabled)
        if self.is_playing and self._current_source:
            self.seek(self.get_position())

    def get_position(self) -> float:
        if not self.is_playing:
            return 0.0
        if self.is_paused and self._paused_detached:
            return self._paused_at_pos
        return self._base_offset + (self._frames_played / self.sample_rate)


# ==========================================
# ГЛАВНОЕ ПРИЛОЖЕНИЕ AETHER MUSIC
# ==========================================
class AetherStreamingApp(ctk.CTk):
    MOODS = ["Любое", "Спокойное", "Бодрое", "Грустное", "Ночной драйв", "Фокус"]

    MOOD_MODIFIERS = {
        "Любое": "",
        "Спокойное": "chill calm acoustic",
        "Бодрое": "energetic upbeat",
        "Грустное": "sad melancholic slow",
        "Ночной драйв": "night drive atmospheric synth",
        "Фокус": "instrumental ambient focus beats",
    }

    NOISE_WORDS = (
        "podcast", "подкаст", "интервью", "interview", "обзор",
        "review", "full album", "1 hour", "2 hours", "часа музыки",
        "стрим", "stream", "реакция", "reaction", "audiobook", "аудиокнига",
        "gameplay", "прохождение", "trailer", "трейлер",
    )

    def __init__(self):
        super().__init__()
        set_windows_app_id()

        self.title("Aether Music")
        self.geometry("1180x740")
        self.minsize(980, 640)
        self.configure(fg_color=THEME["bg_root"])
        self._window_icon_ref = None
        try:
            icon_pil = create_app_icon_image(64, accent=THEME["wave_gold"])
            if icon_pil is not None:
                self._window_icon_ref = ImageTk.PhotoImage(icon_pil)
                self.iconphoto(True, self._window_icon_ref)
        except Exception:
            pass

        self.engine = StreamAudioEngine(
            on_track_end_callback=lambda: self.after(0, self._on_track_finished),
            on_stream_error_callback=lambda: self.after(0, self._on_stream_error),
        )

        self.cover_cache = CoverCacheManager(max_mem_items=160)
        self.prefetch_pool = ThreadPoolExecutor(max_workers=5, thread_name_prefix="prefetch")
        self.thread_local = threading.local()
        self._state_lock = threading.Lock()
        self._closing = False

        # Кэш поисковых запросов
        self.search_cache: OrderedDict[str, list[TrackItem]] = OrderedDict()

        # Профиль вкуса, история, очередь и аккаунт Google
        self.liked_tracks: list[TrackItem] = []
        self.liked_timestamps: dict[str, float] = {}
        self.unliked_timestamps: dict[str, float] = {}
        self.history_tracks: list[TrackItem] = []

        self.disliked_uids: set[str] = set()
        self.session_played_uids: set[str] = set()
        self.session_played_sigs: set[str] = set()
        self.artist_scores: dict[str, float] = {}
        self.seed_videos: list[dict] = []
        self.custom_seeds: list[str] = []
        self.custom_seed_timestamps: dict[str, float] = {}
        self.removed_seed_timestamps: dict[str, float] = {}
        self.state_updated_at: float = 0.0

        self.google_sync = GoogleCloudSyncClient(APP_DATA_DIR)
        self.google_sync_status: str = "Синхронизировано" if self.google_sync.is_signed_in else ""
        self.google_syncing: bool = False
        self._cloud_dirty: bool = False
        self._cloud_sync_timer: str | None = None
        self._active_oauth_flow: GoogleOAuthLoopbackFlow | None = None
        self._google_avatar_img: ctk.CTkImage | None = None
        self._google_avatar_url: str = ""

        self.local_tracks: list[TrackItem] = []
        self.search_results: list[TrackItem] = []
        self.wave_queue: list[TrackItem] = []
        self.up_next_queue: list[TrackItem] = []  # Ручная очередь "Играть следующим"

        self.active_queue: list[TrackItem] = []
        self.current_index: int = -1
        self.current_track: TrackItem | None = None
        self.is_wave_session: bool = False
        self._track_started_at: float = 0.0
        self._track_credited: bool = False
        self._consecutive_errors: int = 0

        # Состояние навигации и фильтров
        self.current_page: str = "wave"       # "wave" | "search" | "library" | "queue"
        self.library_subtab: str = "liked"    # "liked" | "history" | "local"
        self.library_filter_text: str = ""
        self.wave_mood: str = "Любое"
        self.wave_fetching: bool = False
        self._is_searching: bool = False
        self._last_search_text: str = ""

        # Реестр виджетов строк для точечного обновления БЕЗ сброса скролла
        self._row_registry: dict[str, list[dict]] = {}
        self._wave_scroll: ctk.CTkScrollableFrame | None = None
        self._wave_status_lbl: ctk.CTkLabel | None = None
        self._wave_main_btn: ctk.CTkButton | None = None
        self._wave_rendered_count: int = 0

        # Плеер
        self.play_gen: int = 0
        self.is_seeking: bool = False
        self.repeat_mode: bool = False
        self.shuffle_mode: bool = False
        self.accent_color: str = THEME["wave_gold"]
        self.accent_soft: str = THEME["wave_soft"]

        # Дефолтные обложки
        self.bar_default_cover = ctk.CTkImage(
            light_image=create_placeholder_cover((56, 56), 10),
            dark_image=create_placeholder_cover((56, 56), 10),
            size=(56, 56),
        )
        self.row_default_cover = ctk.CTkImage(
            light_image=create_placeholder_cover((40, 40), 8),
            dark_image=create_placeholder_cover((40, 40), 8),
            size=(40, 40),
        )

        # Системный трей (сворачивание вместо закрытия)
        self.tray = TrayManager(
            on_open=lambda: self.after(0, self._restore_from_tray),
            on_play_pause=lambda: self.after(0, self._on_play_pause),
            on_next=lambda: self.after(0, self._on_next),
            on_prev=lambda: self.after(0, self._on_prev),
            on_toggle_like=lambda: self.after(0, self._toggle_like_current),
            on_quit=lambda: self.after(0, self._quit_app),
            get_play_state=lambda: self.engine.is_playing and not self.engine.is_paused,
            get_liked_state=lambda: bool(self.current_track and self._is_liked(self.current_track)),
            accent=THEME["wave_gold"],
        )
        self.tray.start()

        self._load_user_state()
        self._build_layout()
        self._bind_shortcuts()
        self._render_page()

        # Асинхронная загрузка локальных файлов из ~/Music/Aether без фриза при старте
        self._load_downloads_folder_async()
        self._prefill_wave_if_empty()

        # Тихая синхронизация с Google при запуске, если пользователь уже вошел
        if self.google_sync.is_signed_in:
            self._sync_google_cloud_async(pull_first=True)

        # Глобальные медиа-клавиши Windows (Play/Pause, Next, Prev даже в свернутом виде)
        if os.name == "nt":
            threading.Thread(target=self._windows_media_keys_watcher, daemon=True, name="media-keys").start()

        self.protocol("WM_DELETE_WINDOW", self._on_window_close_request)
        self._tick_loop()

    # ==========================================
    # WINDOWS МЕДИА-КЛАВИШИ (РАБОТАЮТ В ФОНЕ / ИГРАХ)
    # ==========================================
    def _windows_media_keys_watcher(self):
        try:
            user32 = ctypes.windll.user32
            keys = {
                0xB3: self._on_play_pause,  # VK_MEDIA_PLAY_PAUSE
                0xB0: self._on_next,        # VK_MEDIA_NEXT_TRACK
                0xB1: self._on_prev,        # VK_MEDIA_PREV_TRACK
            }
            prev_state = {vk: False for vk in keys}
            while not self._closing:
                for vk, handler in keys.items():
                    pressed = bool(user32.GetAsyncKeyState(vk) & 0x8000)
                    if pressed and not prev_state[vk]:
                        self.after(0, handler)
                    prev_state[vk] = pressed
                time.sleep(0.06)
        except Exception:
            pass

    # ==========================================
    # ИНСТАНСЫ YT-DLP
    # ==========================================
    def _get_extract_ydl(self, fallback: bool = False) -> yt_dlp.YoutubeDL:
        attr = "fallback_ydl" if fallback else "extract_ydl"
        ydl = getattr(self.thread_local, attr, None)
        if ydl is None:
            opts = dict(YDL_FALLBACK_EXTRACT_OPTS if fallback else YDL_FAST_EXTRACT_OPTS)
            ydl = yt_dlp.YoutubeDL(opts)
            setattr(self.thread_local, attr, ydl)
        return ydl

    def _get_search_ydl(self, playlist_end: int = 15) -> yt_dlp.YoutubeDL:
        opts = dict(YDL_FAST_SEARCH_OPTS)
        opts["playlistend"] = playlist_end
        return yt_dlp.YoutubeDL(opts)

    # ==========================================
    # АТОМАРНОЕ СОХРАНЕНИЕ И ЗАГРУЗКА СОСТОЯНИЯ
    # ==========================================
    def _export_state_dict(self) -> dict:
        return {
            "schema_version": 2,
            "updated_at": self.state_updated_at or time.time(),
            "liked_tracks": [t.to_dict() for t in self.liked_tracks],
            "liked_timestamps": dict(self.liked_timestamps),
            "unliked_timestamps": dict(self.unliked_timestamps),
            "history_tracks": [t.to_dict() for t in self.history_tracks[:80]],
            "disliked_uids": list(self.disliked_uids)[-500:],
            "artist_scores": dict(self.artist_scores),
            "seed_videos": list(self.seed_videos[-45:]),
            "custom_seeds": list(self.custom_seeds[:12]),
            "custom_seed_timestamps": dict(self.custom_seed_timestamps),
            "removed_seed_timestamps": dict(self.removed_seed_timestamps),
            "volume": round(self.engine.volume, 3),
            "eq_preset": self.engine.eq_preset,
            "normalize_audio": self.engine.normalize_audio,
            "wave_mood": self.wave_mood,
        }

    def _apply_state_dict(self, data: dict, update_ui: bool = False):
        self.state_updated_at = float(data.get("updated_at") or time.time())
        self.liked_tracks = [TrackItem.from_dict(x) for x in data.get("liked_tracks", []) if isinstance(x, dict)]
        self.liked_timestamps = {
            str(k): float(v) for k, v in (data.get("liked_timestamps") or {}).items()
        }
        self.unliked_timestamps = {
            str(k): float(v) for k, v in (data.get("unliked_timestamps") or {}).items()
        }
        self.history_tracks = [TrackItem.from_dict(x) for x in data.get("history_tracks", []) if isinstance(x, dict)][:80]
        self.disliked_uids = set(str(x) for x in (data.get("disliked_uids") or []))
        self.artist_scores = {str(k): float(v) for k, v in (data.get("artist_scores") or {}).items()}
        self.seed_videos = list(data.get("seed_videos") or [])[-45:]
        self.custom_seeds = [str(s) for s in (data.get("custom_seeds") or [])[:12]]
        self.custom_seed_timestamps = {
            str(k): float(v) for k, v in (data.get("custom_seed_timestamps") or {}).items()
        }
        self.removed_seed_timestamps = {
            str(k): float(v) for k, v in (data.get("removed_seed_timestamps") or {}).items()
        }

        # Восстанавливаем настройки плеера
        self.engine.volume = max(0.0, min(1.0, float(data.get("volume", 0.72))))
        eq = str(data.get("eq_preset", "flat"))
        self.engine.eq_preset = eq if eq in EQ_PRESETS else "flat"
        self.engine.normalize_audio = bool(data.get("normalize_audio", False))
        mood = str(data.get("wave_mood", "Любое"))
        self.wave_mood = mood if mood in self.MOODS else "Любое"

        if not self.seed_videos and self.liked_tracks:
            for t in self.liked_tracks[:20]:
                if t.video_id:
                    self.seed_videos.append({"vid": t.video_id, "title": t.title, "artist": t.artist})
                self._adjust_artist_score(t.artist, 3.0, save=False)

        if update_ui:
            try:
                if hasattr(self, "volume_slider"):
                    self.volume_slider.set(self.engine.volume)
                if hasattr(self, "eq_btn"):
                    eq_label, _ = EQ_PRESETS.get(self.engine.eq_preset, ("EQ: Ровный", ""))
                    self.eq_btn.configure(
                        text=eq_label,
                        fg_color=self.accent_soft if self.engine.eq_preset != "flat" else THEME["bg_card"],
                        text_color=self.accent_color if self.engine.eq_preset != "flat" else THEME["text_muted"],
                    )
                if self.current_track and hasattr(self, "like_btn"):
                    liked = self._is_liked(self.current_track)
                    self.like_btn.configure(
                        text="♥" if liked else "♡",
                        text_color=THEME["like_red"] if liked else THEME["text_muted"],
                    )
                self._refresh_sidebar_taste_box()
                self._render_page()
                self._update_tray_state()
            except Exception:
                pass

    def _active_state_file(self) -> str:
        if self.google_sync.is_signed_in and self.google_sync.session:
            prof_dir = os.path.join(APP_DATA_DIR, "profiles")
            os.makedirs(prof_dir, exist_ok=True)
            safe_sub = re.sub(r"[^0-9A-Za-z_-]", "_", self.google_sync.session.sub)
            return os.path.join(prof_dir, f"{safe_sub}.json")
        return STATE_FILE

    def _load_user_state(self):
        try:
            target_file = self._active_state_file()
            if not os.path.exists(target_file):
                target_file = STATE_FILE
            if not os.path.exists(target_file):
                return
            with open(target_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._apply_state_dict(data, update_ui=False)
        except Exception:
            pass

    def _write_state_file_atomic(self, filepath: str, data: dict):
        parent = os.path.dirname(os.path.abspath(filepath))
        os.makedirs(parent, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(prefix="state_", suffix=".tmp", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except Exception:
                    pass

    def _save_user_state(self, sync_cloud: bool = True):
        self.state_updated_at = time.time()
        data = self._export_state_dict()
        with self._state_lock:
            try:
                self._write_state_file_atomic(STATE_FILE, data)
                if self.google_sync.is_signed_in:
                    self._write_state_file_atomic(self._active_state_file(), data)
            except Exception:
                pass

        if sync_cloud and self.google_sync.is_signed_in and not self._closing:
            self._cloud_dirty = True
            self._schedule_cloud_push()

    def _adjust_artist_score(self, artist: str, delta: float, save: bool = True):
        key = clean_artist_name(artist)
        if not key or key.lower() in ("неизвестный исполнитель", "локальный файл", "youtube"):
            return
        cur = self.artist_scores.get(key, 0.0)
        self.artist_scores[key] = max(-15.0, min(40.0, cur + delta))
        if save:
            self._save_user_state()

    def _record_positive_seed(self, track: TrackItem, weight: float = 2.0, save: bool = True):
        self._adjust_artist_score(track.artist, weight, save=False)
        vid = track.video_id
        if vid:
            self.seed_videos = [s for s in self.seed_videos if s.get("vid") != vid]
            self.seed_videos.append({"vid": vid, "title": track.title, "artist": track.artist})
            self.seed_videos = self.seed_videos[-45:]
        if save:
            self._save_user_state()
            self._refresh_sidebar_taste_box()

    def _record_history(self, track: TrackItem):
        self.history_tracks = [t for t in self.history_tracks if t.uid != track.uid]
        self.history_tracks.insert(0, track)
        self.history_tracks = self.history_tracks[:80]
        self._save_user_state()

    def _get_top_taste_artists(self, limit: int = 6) -> list[str]:
        ranked = [(a, s) for a, s in self.artist_scores.items() if s > 0.5]
        ranked.sort(key=lambda x: x[1], reverse=True)
        return [a for a, _ in ranked[:limit]]

    def _load_downloads_folder_async(self):
        if not os.path.isdir(DOWNLOAD_DIR):
            return

        def worker():
            found: list[TrackItem] = []
            try:
                for fname in sorted(os.listdir(DOWNLOAD_DIR)):
                    if fname.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                        found.append(TrackItem.from_local_file(os.path.join(DOWNLOAD_DIR, fname)))
            except Exception:
                pass

            if found and not self._closing:
                def apply():
                    existing = {t.filepath for t in self.local_tracks}
                    for item in found:
                        if item.filepath not in existing:
                            self.local_tracks.append(item)
                    if self.current_page == "library" and self.library_subtab == "local":
                        self._render_page()

                self.after(0, apply)

        threading.Thread(target=worker, daemon=True, name="scan-downloads").start()

    # ==========================================
    # СИСТЕМНЫЙ ТРЕЙ (СВОРАЧИВАНИЕ И ВОССТАНОВЛЕНИЕ)
    # ==========================================
    def _on_window_close_request(self):
        """При нажатии на крестик сворачиваем в системный трей, если трей активен."""
        if self.tray.is_running:
            self._save_user_state(sync_cloud=True)
            self.withdraw()
            self.tray.notify_minimized_once()
        else:
            self._quit_app()

    def _restore_from_tray(self):
        if self._closing:
            return
        try:
            self.deiconify()
            self.state("normal")
            self.attributes("-topmost", True)
            self.lift()
            self.focus_force()
            self.after(60, lambda: self.attributes("-topmost", False))
        except Exception:
            pass

    def _update_tray_state(self):
        if not hasattr(self, "tray"):
            return
        if self.current_track:
            self.tray.update_state(self.current_track.title, self.current_track.artist)
        else:
            self.tray.update_state(None, None)

    # ==========================================
    # АККАУНТ GOOGLE И ОБЛАЧНАЯ СИНХРОНИЗАЦИЯ
    # ==========================================
    def _schedule_cloud_push(self, delay_ms: int = 2500):
        if not self.google_sync.is_signed_in or self._closing:
            return
        if self._cloud_sync_timer is not None:
            try:
                self.after_cancel(self._cloud_sync_timer)
            except Exception:
                pass
            self._cloud_sync_timer = None
        self._cloud_sync_timer = self.after(delay_ms, self._flush_scheduled_cloud_push)

    def _flush_scheduled_cloud_push(self):
        self._cloud_sync_timer = None
        if self.google_sync.is_signed_in and not self._closing:
            self._sync_google_cloud_async(pull_first=False)

    def _sync_google_cloud_async(self, pull_first: bool = True, refresh_wave_after: bool = False):
        if not self.google_sync.is_signed_in or self.google_syncing or self._closing:
            return

        self.google_syncing = True
        self.google_sync_status = "Синхронизация…"
        self._refresh_sidebar_account_box()

        local_snapshot = self._export_state_dict()

        def worker():
            merged_state: dict | None = None
            err_msg: str | None = None
            try:
                payload_to_push = local_snapshot
                if pull_first:
                    cloud_state = self.google_sync.pull_profile()
                    if cloud_state:
                        merged_state = merge_user_states(local_snapshot, cloud_state)
                        payload_to_push = merged_state
                self.google_sync.push_profile(payload_to_push)
            except GoogleAuthError as exc:
                err_msg = str(exc)
            except Exception:
                err_msg = "Нет связи с Google"

            def done():
                self.google_syncing = False
                if err_msg:
                    self.google_sync_status = err_msg[:36]
                else:
                    self._cloud_dirty = False
                    self.google_sync_status = "Синхронизировано"
                    if merged_state is not None:
                        self._apply_state_dict(merged_state, update_ui=True)
                        self._save_user_state(sync_cloud=False)
                        if refresh_wave_after:
                            self._refresh_wave()
                self._refresh_sidebar_account_box()

            if not self._closing:
                self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="google-sync").start()

    def _load_google_avatar_async(self, url: str):
        if not url or url == self._google_avatar_url:
            return
        self._google_avatar_url = url

        def on_pil(pil_img: Image.Image):
            circ = make_circle_avatar(pil_img, size=(34, 34))

            def apply():
                if self._closing or not self.google_sync.is_signed_in:
                    return
                self._google_avatar_img = ctk.CTkImage(light_image=circ, dark_image=circ, size=(34, 34))
                self._refresh_sidebar_account_box()

            if not self._closing:
                self.after(0, apply)

        self.cover_cache.fetch_pil_async(url, on_pil)

    def _sign_out_google(self):
        if self._cloud_sync_timer is not None:
            try:
                self.after_cancel(self._cloud_sync_timer)
            except Exception:
                pass
            self._cloud_sync_timer = None
        self._save_user_state(sync_cloud=False)
        self.google_sync.sign_out()
        self.google_sync_status = ""
        self.google_syncing = False
        self._cloud_dirty = False
        self._google_avatar_img = None
        self._google_avatar_url = ""
        self._refresh_sidebar_account_box()

    def _open_google_auth_dialog(self, force_setup: bool = False):
        if hasattr(self, "_auth_dialog") and self._auth_dialog is not None:
            try:
                if self._auth_dialog.winfo_exists():
                    self._auth_dialog.lift()
                    self._auth_dialog.focus_force()
                    return
            except Exception:
                pass

        dialog = ctk.CTkToplevel(self)
        self._auth_dialog = dialog
        dialog.title("Вход через Google — Aether Music")
        dialog.geometry("470x410")
        dialog.resizable(False, False)
        dialog.configure(fg_color=THEME["bg_sidebar"])
        dialog.transient(self)
        dialog.grab_set()

        body = ctk.CTkFrame(dialog, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        def on_dialog_close():
            if self._active_oauth_flow is not None:
                self._active_oauth_flow.cancel()
                self._active_oauth_flow = None
            try:
                dialog.destroy()
            except Exception:
                pass

        dialog.protocol("WM_DELETE_WINDOW", on_dialog_close)

        status_var = tk.StringVar(value="")

        def render_waiting_view():
            for w in body.winfo_children():
                w.destroy()

            ctk.CTkLabel(
                body,
                text="Вход в аккаунт Google",
                font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
                text_color=THEME["text_main"],
            ).pack(anchor="w", pady=(6, 6))

            ctk.CTkLabel(
                body,
                text="Все ваши лайкнутые треки, история прослушивания и профиль рекомендаций «Моей волны» будут автоматически сохраняться в вашем Google аккаунте.",
                font=ctk.CTkFont(size=12),
                text_color=THEME["text_muted"],
                wraplength=420,
                justify="left",
            ).pack(anchor="w", pady=(0, 18))

            status_box = ctk.CTkFrame(
                body, fg_color=THEME["bg_card"], corner_radius=12, border_width=1, border_color=THEME["border"]
            )
            status_box.pack(fill="x", pady=(0, 16))

            status_lbl = ctk.CTkLabel(
                status_box,
                textvariable=status_var,
                font=ctk.CTkFont(size=12, weight="bold"),
                text_color=self.accent_color,
                wraplength=390,
                justify="left",
            )
            status_lbl.pack(anchor="w", padx=16, pady=16)

            btn_row = ctk.CTkFrame(body, fg_color="transparent")
            btn_row.pack(fill="x", pady=(8, 0))

            ctk.CTkButton(
                btn_row,
                text="Сменить OAuth-ключ",
                height=34,
                corner_radius=10,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_muted"],
                font=ctk.CTkFont(size=12),
                command=lambda: (
                    self._active_oauth_flow.cancel() if self._active_oauth_flow else None,
                    render_setup_view(),
                ),
            ).pack(side="left")

            ctk.CTkButton(
                btn_row,
                text="Отмена",
                width=96,
                height=34,
                corner_radius=10,
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=on_dialog_close,
            ).pack(side="right")

            start_browser_flow()

        def start_browser_flow():
            cfg = self.google_sync.config
            if cfg is None or not cfg.is_valid:
                render_setup_view()
                return

            status_var.set("Открываем браузер… Выберите аккаунт Google и подтвердите доступ.")
            flow = GoogleOAuthLoopbackFlow(cfg)
            self._active_oauth_flow = flow

            def worker():
                try:
                    session = flow.run(timeout_sec=180.0)
                    self.google_sync.set_session(session)

                    def on_ok():
                        self._active_oauth_flow = None
                        try:
                            if dialog.winfo_exists():
                                dialog.destroy()
                        except Exception:
                            pass
                        self._google_avatar_img = None
                        self._google_avatar_url = ""
                        self._refresh_sidebar_account_box()
                        self._sync_google_cloud_async(pull_first=True, refresh_wave_after=True)

                    if not self._closing:
                        self.after(0, on_ok)
                except GoogleAuthError as exc:
                    msg = str(exc)

                    def on_err():
                        self._active_oauth_flow = None
                        if dialog.winfo_exists():
                            status_var.set(f"Ошибка: {msg}")

                    if not self._closing:
                        self.after(0, on_err)
                except Exception as exc:
                    msg = str(exc)

                    def on_err():
                        self._active_oauth_flow = None
                        if dialog.winfo_exists():
                            status_var.set(f"Не удалось выполнить вход: {msg}")

                    if not self._closing:
                        self.after(0, on_err)

            threading.Thread(target=worker, daemon=True, name="google-oauth").start()

        def render_setup_view():
            for w in body.winfo_children():
                w.destroy()

            ctk.CTkLabel(
                body,
                text="Подключение Google аккаунта",
                font=ctk.CTkFont(family="Segoe UI", size=19, weight="bold"),
                text_color=THEME["text_main"],
            ).pack(anchor="w", pady=(0, 6))

            ctk.CTkLabel(
                body,
                text=(
                    "Для входа без сторонних серверов Aether Music сохраняет ваши лайки и рекомендации "
                    "в скрытом хранилище Google Drive (appDataFolder).\n"
                    "1. Создайте бесплатный OAuth Client ID (тип: Desktop app) в Google Cloud Console "
                    "и включите Google Drive API.\n"
                    "2. Загрузите скачанный JSON-файл или вставьте Client ID ниже."
                ),
                font=ctk.CTkFont(size=11),
                text_color=THEME["text_muted"],
                wraplength=422,
                justify="left",
            ).pack(anchor="w", pady=(0, 12))

            top_actions = ctk.CTkFrame(body, fg_color="transparent")
            top_actions.pack(fill="x", pady=(0, 12))

            def on_pick_json():
                fp = filedialog.askopenfilename(
                    parent=dialog,
                    title="Выберите Google client_secret.json",
                    filetypes=[("JSON Files", "*.json")],
                )
                if not fp:
                    return
                try:
                    cfg = GoogleOAuthConfig.load_from_file(fp)
                    self.google_sync.set_config(cfg)
                    status_var.set("")
                    render_waiting_view()
                except GoogleAuthError as exc:
                    status_var.set(str(exc))

            ctk.CTkButton(
                top_actions,
                text="Выбрать client_secret.json",
                height=34,
                corner_radius=10,
                fg_color=self.accent_color,
                hover_color=hex_adjust_brightness(self.accent_color, 1.1),
                text_color="#0D0F12",
                font=ctk.CTkFont(size=12, weight="bold"),
                command=on_pick_json,
            ).pack(side="left", padx=(0, 8))

            ctk.CTkButton(
                top_actions,
                text="Открыть Google Console ↗",
                height=34,
                corner_radius=10,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=lambda: webbrowser.open("https://console.cloud.google.com/apis/credentials"),
            ).pack(side="left")

            cur_cfg = self.google_sync.config
            cid_var = tk.StringVar(value=cur_cfg.client_id if cur_cfg else "")
            csec_var = tk.StringVar(value=cur_cfg.client_secret if cur_cfg else "")

            ctk.CTkLabel(body, text="Client ID", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_muted"]).pack(anchor="w")
            ctk.CTkEntry(
                body,
                textvariable=cid_var,
                placeholder_text="1234567890-xxxx.apps.googleusercontent.com",
                height=34,
                corner_radius=10,
                fg_color=THEME["bg_root"],
                border_color=THEME["border"],
                border_width=1,
                text_color=THEME["text_main"],
                placeholder_text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=12),
            ).pack(fill="x", pady=(3, 8))

            ctk.CTkLabel(body, text="Client Secret (если указан в Google Console)", font=ctk.CTkFont(size=11, weight="bold"), text_color=THEME["text_muted"]).pack(anchor="w")
            ctk.CTkEntry(
                body,
                textvariable=csec_var,
                placeholder_text="GOCSPX-...",
                show="•",
                height=34,
                corner_radius=10,
                fg_color=THEME["bg_root"],
                border_color=THEME["border"],
                border_width=1,
                text_color=THEME["text_main"],
                placeholder_text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=12),
            ).pack(fill="x", pady=(3, 8))

            ctk.CTkLabel(
                body,
                textvariable=status_var,
                font=ctk.CTkFont(size=11),
                text_color=THEME["err_red"],
                wraplength=420,
                justify="left",
            ).pack(anchor="w", pady=(2, 8))

            bottom_row = ctk.CTkFrame(body, fg_color="transparent")
            bottom_row.pack(fill="x", side="bottom")

            def on_save_manual():
                cfg = GoogleOAuthConfig(client_id=cid_var.get().strip(), client_secret=csec_var.get().strip())
                try:
                    self.google_sync.set_config(cfg)
                    status_var.set("")
                    render_waiting_view()
                except GoogleAuthError as exc:
                    status_var.set(str(exc))

            ctk.CTkButton(
                bottom_row,
                text="Сохранить и войти через Google",
                height=36,
                corner_radius=10,
                fg_color=THEME["bg_card_alt"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12, weight="bold"),
                command=on_save_manual,
            ).pack(side="left")

            ctk.CTkButton(
                bottom_row,
                text="Закрыть",
                width=84,
                height=36,
                corner_radius=10,
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_muted"],
                font=ctk.CTkFont(size=12),
                command=on_dialog_close,
            ).pack(side="right")

        self.google_sync.reload_config()
        if not force_setup and self.google_sync.config and self.google_sync.config.is_valid:
            render_waiting_view()
        else:
            render_setup_view()

    # ==========================================
    # КАРКАС ОКНА (SIDEBAR / CONTENT / BOTTOM BAR)
    # ==========================================
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0, minsize=244)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0, minsize=92)

        # ---------- 1. ЛЕВАЯ ПАНЕЛЬ ----------
        self.sidebar = ctk.CTkFrame(self, fg_color=THEME["bg_sidebar"], corner_radius=0, width=244)
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(5, weight=1)
        self.sidebar.grid_columnconfigure(0, weight=1)

        logo_box = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo_box.grid(row=0, column=0, sticky="ew", padx=22, pady=(24, 20))
        ctk.CTkLabel(
            logo_box,
            text="Aether",
            font=ctk.CTkFont(family="Segoe UI", size=22, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")
        ctk.CTkLabel(
            logo_box,
            text=" music",
            font=ctk.CTkFont(family="Segoe UI", size=22),
            text_color=THEME["text_muted"],
        ).pack(side="left")

        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("wave", "🌊   Моя волна"),
            ("search", "🔍   Поиск"),
            ("library", "📚   Коллекция"),
            ("queue", "☰   Очередь"),
        ]
        for idx, (page_id, label) in enumerate(nav_items, start=1):
            btn = ctk.CTkButton(
                self.sidebar,
                text=label,
                anchor="w",
                height=42,
                corner_radius=10,
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_muted"],
                font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                command=lambda p=page_id: self._switch_page(p),
            )
            btn.grid(row=idx, column=0, sticky="ew", padx=14, pady=3)
            self.nav_buttons[page_id] = btn

        # Карточка аккаунта Google
        self.account_box = ctk.CTkFrame(
            self.sidebar,
            fg_color=THEME["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=THEME["border"],
        )
        self.account_box.grid(row=6, column=0, sticky="ew", padx=14, pady=(8, 4))
        self.account_box.grid_columnconfigure(0, weight=1)
        self._refresh_sidebar_account_box()

        # Индикатор аудио-активности + профиль вкуса внизу сайдбара
        self.taste_summary_box = ctk.CTkFrame(
            self.sidebar,
            fg_color=THEME["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=THEME["border"],
        )
        self.taste_summary_box.grid(row=7, column=0, sticky="ew", padx=14, pady=(4, 16))
        self.taste_summary_box.grid_columnconfigure(0, weight=1)
        self._refresh_sidebar_taste_box()

        # ---------- 2. ЦЕНТРАЛЬНАЯ ОБЛАСТЬ ----------
        self.content_area = ctk.CTkFrame(self, fg_color=THEME["bg_root"], corner_radius=0)
        self.content_area.grid(row=0, column=1, sticky="nsew")
        self.content_area.grid_columnconfigure(0, weight=1)
        self.content_area.grid_rowconfigure(0, weight=1)

        # ---------- 3. НИЖНЯЯ ПАНЕЛЬ ПЛЕЕРА ----------
        self.bottom_bar = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_bar"],
            corner_radius=0,
            height=92,
            border_width=1,
            border_color=THEME["border"],
        )
        self.bottom_bar.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.bottom_bar.grid_propagate(False)
        self.bottom_bar.grid_columnconfigure(0, weight=5, uniform="bar")
        self.bottom_bar.grid_columnconfigure(1, weight=7, uniform="bar")
        self.bottom_bar.grid_columnconfigure(2, weight=5, uniform="bar")
        self.bottom_bar.grid_rowconfigure(0, weight=1)

        # Левый блок бара: обложка + инфо + лайк/дизлайк + 5-полосный мини-индикатор
        bar_left = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_left.grid(row=0, column=0, sticky="w", padx=16)

        self.bar_cover_label = ctk.CTkLabel(bar_left, text="", image=self.bar_default_cover)
        self.bar_cover_label.pack(side="left", padx=(0, 12))

        meta_box = ctk.CTkFrame(bar_left, fg_color="transparent")
        meta_box.pack(side="left", padx=(0, 8))

        self.bar_title = ctk.CTkLabel(
            meta_box,
            text="Ничего не играет",
            font=ctk.CTkFont(family="Segoe UI", size=13, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        )
        self.bar_title.pack(anchor="w")

        self.bar_artist = ctk.CTkLabel(
            meta_box,
            text="Запустите Мою волну",
            font=ctk.CTkFont(family="Segoe UI", size=11),
            text_color=THEME["text_muted"],
            anchor="w",
        )
        self.bar_artist.pack(anchor="w")

        # Минималистичный 5-полосный живой уровень звука (canvas 28x14, без неона)
        self.meter_canvas = tk.Canvas(
            bar_left,
            width=26,
            height=18,
            bg=THEME["bg_bar"],
            highlightthickness=0,
            bd=0,
        )
        self.meter_canvas.pack(side="left", padx=(4, 6))
        self.meter_rects = []
        for i in range(5):
            x0 = i * 5 + 1
            rect = self.meter_canvas.create_rectangle(
                x0, 15, x0 + 3, 18, fill=THEME["text_dim"], outline=""
            )
            self.meter_rects.append(rect)

        self.like_btn = ctk.CTkButton(
            bar_left,
            text="♡",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=16),
            command=self._toggle_like_current,
        )
        self.like_btn.pack(side="left", padx=1)

        self.dislike_btn = ctk.CTkButton(
            bar_left,
            text="⊘",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=14),
            command=self._dislike_and_skip_current,
        )
        self.dislike_btn.pack(side="left", padx=1)

        # Центральный блок: управление + ползунок прогресса
        bar_center = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_center.grid(row=0, column=1, sticky="ew", padx=10)
        bar_center.grid_columnconfigure(0, weight=1)

        btns_row = ctk.CTkFrame(bar_center, fg_color="transparent")
        btns_row.grid(row=0, column=0, pady=(10, 3))

        self.shuffle_btn = ctk.CTkButton(
            btns_row,
            text="⇄",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=14),
            command=self._toggle_shuffle,
        )
        self.shuffle_btn.pack(side="left", padx=6)

        self.prev_btn = ctk.CTkButton(
            btns_row,
            text="⏮",
            width=34,
            height=34,
            corner_radius=17,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=15),
            command=self._on_prev,
        )
        self.prev_btn.pack(side="left", padx=6)

        self.play_btn = ctk.CTkButton(
            btns_row,
            text="▶",
            width=40,
            height=40,
            corner_radius=20,
            fg_color=THEME["text_main"],
            hover_color="#DCE1EA",
            text_color="#0D0F12",
            font=ctk.CTkFont(size=15, weight="bold"),
            command=self._on_play_pause,
        )
        self.play_btn.pack(side="left", padx=10)

        self.next_btn = ctk.CTkButton(
            btns_row,
            text="⏭",
            width=34,
            height=34,
            corner_radius=17,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=15),
            command=self._on_next,
        )
        self.next_btn.pack(side="left", padx=6)

        self.repeat_btn = ctk.CTkButton(
            btns_row,
            text="↻",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=14),
            command=self._toggle_repeat,
        )
        self.repeat_btn.pack(side="left", padx=6)

        seek_row = ctk.CTkFrame(bar_center, fg_color="transparent")
        seek_row.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        seek_row.grid_columnconfigure(1, weight=1)

        self.elapsed_label = ctk.CTkLabel(
            seek_row, text="00:00", font=ctk.CTkFont(size=10), text_color=THEME["text_muted"], width=38
        )
        self.elapsed_label.grid(row=0, column=0, padx=(0, 6))

        self.progress_slider = ctk.CTkSlider(
            seek_row,
            from_=0,
            to=100,
            height=12,
            fg_color=THEME["bg_card"],
            progress_color=self.accent_color,
            button_color=THEME["text_main"],
            button_hover_color=self.accent_color,
            button_length=10,
            command=self._on_slider_drag,
        )
        self.progress_slider.set(0)
        self.progress_slider.grid(row=0, column=1, sticky="ew")
        self.progress_slider.bind("<ButtonPress-1>", self._on_seek_start)
        self.progress_slider.bind("<ButtonRelease-1>", self._on_seek_end)

        self.total_label = ctk.CTkLabel(
            seek_row, text="00:00", font=ctk.CTkFont(size=10), text_color=THEME["text_muted"], width=38
        )
        self.total_label.grid(row=0, column=2, padx=(6, 0))

        # Правый блок на grid (чтобы кнопки никогда не прыгали и не улетали в конец)
        bar_right = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_right.grid(row=0, column=2, sticky="e", padx=16)

        eq_label, _ = EQ_PRESETS.get(self.engine.eq_preset, ("EQ: Ровный", ""))
        self.eq_btn = ctk.CTkButton(
            bar_right,
            text=eq_label,
            width=82,
            height=26,
            corner_radius=7,
            fg_color=self.accent_soft if self.engine.eq_preset != "flat" else THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=self.accent_color if self.engine.eq_preset != "flat" else THEME["text_muted"],
            font=ctk.CTkFont(size=11),
            command=self._cycle_eq_preset,
        )
        self.eq_btn.grid(row=0, column=0, padx=(0, 6))

        self.bar_download_btn = ctk.CTkButton(
            bar_right,
            text="↓ MP3",
            width=56,
            height=26,
            corner_radius=7,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=11),
            command=self._on_download_current,
        )
        self.bar_download_btn.grid(row=0, column=1, padx=(0, 6))

        self.bar_source_badge = ctk.CTkLabel(
            bar_right,
            text="READY",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_muted"],
            fg_color=THEME["bg_card"],
            corner_radius=6,
            padx=7,
            pady=2,
        )
        self.bar_source_badge.grid(row=0, column=2, padx=(0, 8))

        self.mute_btn = ctk.CTkButton(
            bar_right,
            text="🔊",
            width=26,
            height=26,
            corner_radius=13,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=12),
            command=self._toggle_mute,
        )
        self.mute_btn.grid(row=0, column=3, padx=(0, 2))

        self.volume_slider = ctk.CTkSlider(
            bar_right,
            from_=0,
            to=1,
            width=78,
            height=12,
            fg_color=THEME["bg_card"],
            progress_color=THEME["text_muted"],
            button_color=THEME["text_main"],
            button_hover_color=self.accent_color,
            command=self._on_volume_change,
        )
        self.volume_slider.set(self.engine.volume)
        self.volume_slider.grid(row=0, column=4)

        self._sync_nav_buttons()

    def _refresh_sidebar_account_box(self):
        if not hasattr(self, "account_box"):
            return
        for w in self.account_box.winfo_children():
            w.destroy()

        sess = self.google_sync.session
        if sess is None:
            ctk.CTkLabel(
                self.account_box,
                text="Аккаунт Google",
                font=ctk.CTkFont(size=11, weight="bold"),
                text_color=THEME["text_main"],
            ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 2))

            ctk.CTkLabel(
                self.account_box,
                text="Сохраняйте лайки и волну в облаке",
                font=ctk.CTkFont(size=10),
                text_color=THEME["text_dim"],
                wraplength=192,
                justify="left",
                anchor="w",
            ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

            ctk.CTkButton(
                self.account_box,
                text="G   Войти через Google",
                height=30,
                corner_radius=8,
                fg_color=THEME["bg_card_alt"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                border_width=1,
                border_color=THEME["border"],
                font=ctk.CTkFont(size=11, weight="bold"),
                command=self._open_google_auth_dialog,
            ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
            return

        if sess.picture and sess.picture != self._google_avatar_url:
            self._load_google_avatar_async(sess.picture)

        top = ctk.CTkFrame(self.account_box, fg_color="transparent")
        top.grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        top.grid_columnconfigure(1, weight=1)

        if self._google_avatar_img is not None:
            avatar_lbl = ctk.CTkLabel(top, text="", image=self._google_avatar_img)
            avatar_lbl.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        else:
            initial = (sess.name[:1] or sess.email[:1] or "G").upper()
            ctk.CTkLabel(
                top,
                text=initial,
                width=32,
                height=32,
                corner_radius=16,
                fg_color=self.accent_soft,
                text_color=self.accent_color,
                font=ctk.CTkFont(size=13, weight="bold"),
            ).grid(row=0, column=0, rowspan=2, padx=(0, 8))

        short_name = sess.name if len(sess.name) <= 18 else sess.name[:16] + "…"
        short_email = sess.email if len(sess.email) <= 22 else sess.email[:20] + "…"
        ctk.CTkLabel(
            top,
            text=short_name,
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["text_main"],
            anchor="w",
        ).grid(row=0, column=1, sticky="w")
        ctk.CTkLabel(
            top,
            text=short_email,
            font=ctk.CTkFont(size=10),
            text_color=THEME["text_dim"],
            anchor="w",
        ).grid(row=1, column=1, sticky="w")

        status_text = self.google_sync_status or "Синхронизировано"
        status_color = (
            self.accent_color
            if self.google_syncing
            else (THEME["ok_green"] if status_text == "Синхронизировано" else THEME["err_red"])
        )
        ctk.CTkLabel(
            self.account_box,
            text=status_text,
            font=ctk.CTkFont(size=10),
            text_color=status_color,
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 6))

        actions = ctk.CTkFrame(self.account_box, fg_color="transparent")
        actions.grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))
        actions.grid_columnconfigure(0, weight=1)
        actions.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(
            actions,
            text="↻ Синхр.",
            height=24,
            corner_radius=7,
            fg_color=THEME["bg_root"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=10),
            state="disabled" if self.google_syncing else "normal",
            command=lambda: self._sync_google_cloud_async(pull_first=True),
        ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

        ctk.CTkButton(
            actions,
            text="Выйти",
            height=24,
            corner_radius=7,
            fg_color=THEME["bg_root"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=10),
            command=self._sign_out_google,
        ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _refresh_sidebar_taste_box(self):
        if not hasattr(self, "taste_summary_box"):
            return
        for w in self.taste_summary_box.winfo_children():
            w.destroy()

        ctk.CTkLabel(
            self.taste_summary_box,
            text="Профиль вкуса",
            font=ctk.CTkFont(size=11, weight="bold"),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=0, sticky="w", padx=12, pady=(10, 3))

        seeds = self.custom_seeds + [a for a in self._get_top_taste_artists(4) if a not in self.custom_seeds]
        if seeds:
            summary_txt = ", ".join(seeds[:4])
            if len(summary_txt) > 56:
                summary_txt = summary_txt[:53] + "…"
        else:
            summary_txt = "Добавьте любимых артистов или ставьте ♥"

        ctk.CTkLabel(
            self.taste_summary_box,
            text=summary_txt,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
            wraplength=192,
            justify="left",
            anchor="w",
        ).grid(row=1, column=0, sticky="w", padx=12, pady=(0, 8))

        # Компактный переключатель нормализации громкости
        norm_txt = "Нормализация: ВКЛ" if self.engine.normalize_audio else "Нормализация: ВЫКЛ"
        ctk.CTkButton(
            self.taste_summary_box,
            text=norm_txt,
            height=24,
            corner_radius=8,
            fg_color=self.accent_soft if self.engine.normalize_audio else THEME["bg_root"],
            hover_color=THEME["bg_hover"],
            text_color=self.accent_color if self.engine.normalize_audio else THEME["text_dim"],
            font=ctk.CTkFont(size=10, weight="bold" if self.engine.normalize_audio else "normal"),
            command=self._toggle_normalize,
        ).grid(row=2, column=0, sticky="ew", padx=10, pady=(0, 10))

    # ==========================================
    # НАВИГАЦИЯ И РЕАКТИВНЫЕ ОБНОВЛЕНИЯ СТРОК
    # ==========================================
    def _switch_page(self, page: str):
        self.current_page = page
        self._sync_nav_buttons()
        self._render_page()

    def _sync_nav_buttons(self):
        for pid, btn in self.nav_buttons.items():
            if pid == "queue":
                q_cnt = len(self.up_next_queue)
                btn.configure(text=f"☰   Очередь ({q_cnt})" if q_cnt else "☰   Очередь")
            if pid == self.current_page:
                btn.configure(fg_color=THEME["bg_card"], text_color=THEME["text_main"])
            else:
                btn.configure(fg_color="transparent", text_color=THEME["text_muted"])

    def _render_page(self):
        self._row_registry.clear()
        self._wave_scroll = None
        self._wave_status_lbl = None
        self._wave_main_btn = None
        self._wave_rendered_count = 0

        for w in self.content_area.winfo_children():
            w.destroy()

        if self.current_page == "wave":
            self._render_wave_page()
        elif self.current_page == "search":
            self._render_search_page()
        elif self.current_page == "library":
            self._render_library_page()
        elif self.current_page == "queue":
            self._render_queue_page()

    def _update_row_highlight_in_place(self, old_uid: str | None, new_uid: str | None):
        """Точечно обновляет только старую и новую активную строку — скролл и фокус не сбрасываются."""
        for uid, is_active in ((old_uid, False), (new_uid, True)):
            if not uid or uid not in self._row_registry:
                continue
            for ref in self._row_registry[uid]:
                try:
                    if not ref["row"].winfo_exists():
                        continue
                    ref["row"].configure(fg_color=THEME["bg_card"] if is_active else "transparent")
                    ref["num"].configure(
                        text="▶" if is_active else str(ref["idx"] + 1),
                        text_color=self.accent_color if is_active else THEME["text_dim"],
                        font=ctk.CTkFont(size=12, weight="bold" if is_active else "normal"),
                    )
                    ref["info"].configure(
                        text_color=self.accent_color if is_active else THEME["text_main"],
                        font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if is_active else "normal"),
                    )
                except tk.TclError:
                    pass

        self._sync_wave_hero_button()

    def _update_row_like_in_place(self, uid: str, liked: bool):
        for ref in self._row_registry.get(uid, []):
            try:
                if ref["like"].winfo_exists():
                    ref["like"].configure(
                        text="♥" if liked else "♡",
                        text_color=THEME["like_red"] if liked else THEME["text_muted"],
                    )
            except tk.TclError:
                pass

    def _sync_wave_hero_button(self):
        if self._wave_main_btn is None:
            return
        try:
            if self._wave_main_btn.winfo_exists():
                is_wave_playing = self.is_wave_session and self.engine.is_playing and not self.engine.is_paused
                self._wave_main_btn.configure(
                    text="⏸  Пауза" if is_wave_playing else "▶  Слушать волну",
                    fg_color=self.accent_color,
                    hover_color=hex_adjust_brightness(self.accent_color, 1.1),
                )
        except tk.TclError:
            pass

    def _update_wave_status_label(self):
        if self._wave_status_lbl is None:
            return
        try:
            if not self._wave_status_lbl.winfo_exists():
                return
            if self.wave_fetching:
                self._wave_status_lbl.configure(text="Подбираем под ваш вкус…", text_color=self.accent_color)
            else:
                self._wave_status_lbl.configure(text=f"{len(self.wave_queue)} треков в потоке", text_color=THEME["text_dim"])
        except tk.TclError:
            pass

    # ==========================================
    # СТРАНИЦА 1: «МОЯ ВОЛНА»
    # ==========================================
    def _render_wave_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=20)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        hero = ctk.CTkFrame(
            container,
            fg_color=THEME["bg_sidebar"],
            corner_radius=20,
            border_width=1,
            border_color=THEME["border"],
        )
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        hero.grid_columnconfigure(0, weight=1)

        hero_top = ctk.CTkFrame(hero, fg_color="transparent")
        hero_top.grid(row=0, column=0, sticky="ew", padx=24, pady=(18, 12))
        hero_top.grid_columnconfigure(0, weight=1)

        title_col = ctk.CTkFrame(hero_top, fg_color="transparent")
        title_col.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_col,
            text="Моя волна",
            font=ctk.CTkFont(family="Segoe UI", size=26, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(anchor="w")

        top_artists = self.custom_seeds + [a for a in self._get_top_taste_artists(3) if a not in self.custom_seeds]
        if top_artists:
            sub_info = f"Настроено под вас: {', '.join(top_artists[:3])}  •  {self.wave_mood}"
        else:
            sub_info = "Укажите любимых артистов или ставьте ♥ понравившимся трекам"

        ctk.CTkLabel(
            title_col,
            text=sub_info,
            font=ctk.CTkFont(family="Segoe UI", size=12),
            text_color=THEME["text_muted"],
        ).pack(anchor="w", pady=(2, 0))

        wave_actions = ctk.CTkFrame(hero_top, fg_color="transparent")
        wave_actions.grid(row=0, column=1, sticky="e")

        is_wave_playing = self.is_wave_session and self.engine.is_playing and not self.engine.is_paused
        self._wave_main_btn = ctk.CTkButton(
            wave_actions,
            text="⏸  Пауза" if is_wave_playing else "▶  Слушать волну",
            width=176,
            height=42,
            corner_radius=21,
            fg_color=self.accent_color,
            hover_color=hex_adjust_brightness(self.accent_color, 1.1),
            text_color="#0D0F12",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            command=self._start_or_toggle_wave,
        )
        self._wave_main_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            wave_actions,
            text="↻",
            width=42,
            height=42,
            corner_radius=21,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=16),
            command=self._refresh_wave,
        ).pack(side="left")

        tuning_box = ctk.CTkFrame(hero, fg_color="transparent")
        tuning_box.grid(row=1, column=0, sticky="ew", padx=24, pady=(0, 16))

        # Ряд 1: Настроение
        mood_row = ctk.CTkFrame(tuning_box, fg_color="transparent")
        mood_row.pack(fill="x", pady=(0, 9))
        ctk.CTkLabel(
            mood_row, text="Настроение:", font=ctk.CTkFont(size=12), text_color=THEME["text_dim"], width=94, anchor="w"
        ).pack(side="left")

        for m in self.MOODS:
            active = m == self.wave_mood
            btn_w = max(68, len(m) * 8 + 18)
            ctk.CTkButton(
                mood_row,
                text=m,
                width=btn_w,
                height=28,
                corner_radius=14,
                fg_color=self.accent_soft if active else THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=self.accent_color if active else THEME["text_muted"],
                border_width=1 if active else 0,
                border_color=self.accent_color,
                font=ctk.CTkFont(size=12, weight="bold" if active else "normal"),
                command=lambda val=m: self._set_wave_mood(val),
            ).pack(side="left", padx=3)

        # Ряд 2: Ориентиры вкуса
        seeds_row = ctk.CTkFrame(tuning_box, fg_color="transparent")
        seeds_row.pack(fill="x")
        ctk.CTkLabel(
            seeds_row, text="Мой вкус:", font=ctk.CTkFont(size=12), text_color=THEME["text_dim"], width=94, anchor="w"
        ).pack(side="left")

        self.seed_entry_var = tk.StringVar()
        seed_entry = ctk.CTkEntry(
            seeds_row,
            textvariable=self.seed_entry_var,
            placeholder_text="Добавить артиста или стиль (напр. Кишлак, Synthwave)...",
            width=355,
            height=30,
            corner_radius=15,
            fg_color=THEME["bg_root"],
            border_color=THEME["border"],
            border_width=1,
            text_color=THEME["text_main"],
            placeholder_text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=12),
        )
        seed_entry.pack(side="left", padx=(3, 6))
        seed_entry.bind("<Return>", lambda _e: self._add_custom_seed())

        ctk.CTkButton(
            seeds_row,
            text="+ Добавить",
            width=96,
            height=30,
            corner_radius=15,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=11, weight="bold"),
            command=self._add_custom_seed,
        ).pack(side="left")

        if self.custom_seeds or self._get_top_taste_artists(4):
            tags_row = ctk.CTkFrame(tuning_box, fg_color="transparent")
            tags_row.pack(fill="x", pady=(9, 0))
            ctk.CTkLabel(tags_row, text="", width=94).pack(side="left")

            for s in self.custom_seeds[:6]:
                chip_w = max(72, len(s) * 8 + 30)
                ctk.CTkButton(
                    tags_row,
                    text=f"{s}  ×",
                    width=chip_w,
                    height=24,
                    corner_radius=12,
                    fg_color=self.accent_soft,
                    hover_color=THEME["bg_hover"],
                    text_color=self.accent_color,
                    font=ctk.CTkFont(size=11),
                    command=lambda val=s: self._remove_custom_seed(val),
                ).pack(side="left", padx=3)

            auto_artists = [a for a in self._get_top_taste_artists(4) if a not in self.custom_seeds][:3]
            for a in auto_artists:
                chip_w = max(72, len(a) * 8 + 26)
                ctk.CTkButton(
                    tags_row,
                    text=f"♥ {a}",
                    width=chip_w,
                    height=24,
                    corner_radius=12,
                    fg_color=THEME["bg_card"],
                    hover_color=THEME["bg_hover"],
                    text_color=THEME["text_muted"],
                    font=ctk.CTkFont(size=11),
                    command=lambda val=a: self._promote_artist_seed(val),
                ).pack(side="left", padx=3)

        q_header = ctk.CTkFrame(container, fg_color="transparent")
        q_header.grid(row=1, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkLabel(
            q_header,
            text="Далее в потоке",
            font=ctk.CTkFont(family="Segoe UI", size=15, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        self._wave_status_lbl = ctk.CTkLabel(
            q_header,
            text="",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        )
        self._wave_status_lbl.pack(side="right")
        self._update_wave_status_label()

        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)
        self._wave_scroll = scroll

        if not self.wave_queue:
            msg = "Собираем персональную волну…" if self.wave_fetching else "Нажмите «Слушать волну», чтобы запустить поток"
            ctk.CTkLabel(scroll, text=msg, text_color=THEME["text_muted"], font=ctk.CTkFont(size=13)).grid(
                row=0, column=0, pady=40
            )
            return

        for idx, track in enumerate(self.wave_queue):
            self._build_track_row(scroll, idx, track, self.wave_queue, is_wave=True)
        self._wave_rendered_count = len(self.wave_queue)

    def _append_new_wave_rows(self):
        """Дорисовывает только новые треки в конец потока, не трогая существующие строки и скролл."""
        if self.current_page != "wave" or self._wave_scroll is None:
            return
        try:
            if not self._wave_scroll.winfo_exists():
                return
        except tk.TclError:
            return

        if self._wave_rendered_count == 0:
            # На экране была только заглушка — очистим только сам scroll, не трогая поля ввода сверху!
            for w in self._wave_scroll.winfo_children():
                w.destroy()

        for idx in range(self._wave_rendered_count, len(self.wave_queue)):
            self._build_track_row(self._wave_scroll, idx, self.wave_queue[idx], self.wave_queue, is_wave=True)
        self._wave_rendered_count = len(self.wave_queue)
        self._update_wave_status_label()

    def _add_custom_seed(self):
        val = self.seed_entry_var.get().strip()
        if not val:
            return
        self.seed_entry_var.set("")
        now = time.time()
        if not any(s.lower() == val.lower() for s in self.custom_seeds):
            self.custom_seeds.insert(0, val)
            self.custom_seeds = self.custom_seeds[:10]
        self.custom_seed_timestamps[val.lower()] = now
        self.removed_seed_timestamps.pop(val.lower(), None)
        self._adjust_artist_score(val, 4.0, save=True)
        self._refresh_sidebar_taste_box()
        self._refresh_wave()

    def _remove_custom_seed(self, val: str):
        self.custom_seeds = [s for s in self.custom_seeds if s != val]
        self.removed_seed_timestamps[val.lower()] = time.time()
        self._save_user_state()
        self._refresh_sidebar_taste_box()
        self._refresh_wave()

    def _promote_artist_seed(self, artist: str):
        if artist not in self.custom_seeds:
            self.custom_seeds.insert(0, artist)
            self.custom_seed_timestamps[artist.lower()] = time.time()
            self.removed_seed_timestamps.pop(artist.lower(), None)
            self._save_user_state()
            self._refresh_sidebar_taste_box()
            self._refresh_wave()

    # ==========================================
    # СТРАНИЦА 2: ПОИСК
    # ==========================================
    def _render_search_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        top_search = ctk.CTkFrame(container, fg_color="transparent")
        top_search.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        top_search.grid_columnconfigure(0, weight=1)

        self.search_var = tk.StringVar(value=self._last_search_text)
        self.search_entry = ctk.CTkEntry(
            top_search,
            textvariable=self.search_var,
            placeholder_text="Что хотите послушать? Исполнитель, трек или ссылка YouTube…",
            height=44,
            corner_radius=22,
            fg_color=THEME["bg_card"],
            border_color=THEME["border"],
            border_width=1,
            text_color=THEME["text_main"],
            placeholder_text_color=THEME["text_dim"],
            font=ctk.CTkFont(family="Segoe UI", size=13),
        )
        self.search_entry.grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.search_entry.bind("<Return>", lambda _e: self._execute_search())

        ctk.CTkButton(
            top_search,
            text="Найти",
            height=44,
            width=102,
            corner_radius=22,
            fg_color=self.accent_color,
            hover_color=hex_adjust_brightness(self.accent_color, 1.1),
            text_color="#0D0F12",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._execute_search,
        ).grid(row=0, column=1)

        if not self.search_results and not self._is_searching:
            cards_frame = ctk.CTkFrame(container, fg_color="transparent")
            cards_frame.grid(row=1, column=0, sticky="ew", pady=(4, 14))
            cards_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

            presets = [
                ("Ночной драйв", "night drive synthwave", "#222834"),
                ("Инди и альтернатива", "indie rock alternative", "#292326"),
                ("Спокойный фокус", "deep ambient chill beats", "#1E2825"),
                ("Фонк и электроника", "phonk house", "#28241F"),
            ]
            for i, (title, q, bg) in enumerate(presets):
                ctk.CTkButton(
                    cards_frame,
                    text=title,
                    height=72,
                    corner_radius=14,
                    fg_color=bg,
                    hover_color=THEME["bg_hover"],
                    text_color=THEME["text_main"],
                    font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                    command=lambda query=q: self._quick_search(query),
                ).grid(row=0, column=i, sticky="ew", padx=5)

        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        if self._is_searching:
            ctk.CTkLabel(scroll, text="Ищем треки…", text_color=THEME["text_muted"], font=ctk.CTkFont(size=13)).grid(
                row=0, column=0, pady=40
            )
            return

        if not self.search_results:
            ctk.CTkLabel(
                scroll,
                text="Введите название, артиста или вставьте прямую ссылку на YouTube-видео / плейлист",
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=0, pady=40)
            return

        for idx, track in enumerate(self.search_results):
            self._build_track_row(scroll, idx, track, self.search_results, is_wave=False)

    # ==========================================
    # СТРАНИЦА 3: КОЛЛЕКЦИЯ (ЛЮБИМЫЕ / ИСТОРИЯ / ФАЙЛЫ)
    # ==========================================
    def _render_library_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(3, weight=1)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        ctk.CTkLabel(
            header,
            text="Моя коллекция",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        # Быстрый фильтр по коллекции без сетевых запросов
        self.lib_filter_var = tk.StringVar(value=self.library_filter_text)
        filter_entry = ctk.CTkEntry(
            header,
            textvariable=self.lib_filter_var,
            placeholder_text="Фильтр по названию или артисту…",
            width=250,
            height=32,
            corner_radius=16,
            fg_color=THEME["bg_card"],
            border_color=THEME["border"],
            border_width=1,
            text_color=THEME["text_main"],
            placeholder_text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=12),
        )
        filter_entry.pack(side="right")
        filter_entry.bind("<KeyRelease>", self._on_library_filter_change)

        tabs_row = ctk.CTkFrame(container, fg_color="transparent")
        tabs_row.grid(row=1, column=0, sticky="ew", pady=(0, 12))

        subtabs = [
            ("liked", f"♥  Мне нравится ({len(self.liked_tracks)})"),
            ("history", f"🕘  Недавние ({len(self.history_tracks)})"),
            ("local", f"📁  Файлы ({len(self.local_tracks)})"),
        ]
        for tab_id, tab_label in subtabs:
            active = self.library_subtab == tab_id
            ctk.CTkButton(
                tabs_row,
                text=tab_label,
                height=34,
                corner_radius=17,
                fg_color=THEME["bg_card"] if active else "transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"] if active else THEME["text_muted"],
                font=ctk.CTkFont(size=12, weight="bold"),
                command=lambda s=tab_id: self._switch_library_subtab(s),
            ).pack(side="left", padx=(0, 6))

        if self.library_subtab == "local":
            ctk.CTkButton(
                tabs_row,
                text="+ Папка",
                height=32,
                corner_radius=10,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=self._on_add_local_folder,
            ).pack(side="right", padx=(6, 0))

            ctk.CTkButton(
                tabs_row,
                text="+ Файлы",
                height=32,
                corner_radius=10,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=self._on_add_local_files,
            ).pack(side="right")

        self._lib_scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        self._lib_scroll.grid(row=3, column=0, sticky="nsew")
        self._lib_scroll.grid_columnconfigure(0, weight=1)
        self._populate_library_scroll()

    def _on_library_filter_change(self, _e=None):
        self.library_filter_text = self.lib_filter_var.get().strip().lower()
        self._populate_library_scroll()

    def _populate_library_scroll(self):
        if not hasattr(self, "_lib_scroll") or not self._lib_scroll.winfo_exists():
            return
        self._row_registry.clear()
        for w in self._lib_scroll.winfo_children():
            w.destroy()

        if self.library_subtab == "liked":
            base_items = self.liked_tracks
            hint = "Ставьте ♥ понравившимся трекам — они появятся здесь и настроят Мою волну"
        elif self.library_subtab == "history":
            base_items = self.history_tracks
            hint = "Здесь появятся треки, которые вы слушали недавно"
        else:
            base_items = self.local_tracks
            hint = "Добавьте папку с музыкой или скачайте онлайн-треки в MP3"

        flt = self.library_filter_text
        items = (
            [t for t in base_items if flt in t.title.lower() or flt in t.artist.lower()]
            if flt
            else base_items
        )

        if not items:
            msg = "Ничего не найдено по фильтру" if (flt and base_items) else hint
            ctk.CTkLabel(self._lib_scroll, text=msg, text_color=THEME["text_dim"], font=ctk.CTkFont(size=13)).grid(
                row=0, column=0, pady=50
            )
            return

        for idx, track in enumerate(items):
            self._build_track_row(self._lib_scroll, idx, track, items, is_wave=False)

    def _switch_library_subtab(self, sub: str):
        self.library_subtab = sub
        self._render_page()

    # ==========================================
    # СТРАНИЦА 4: ОЧЕРЕДЬ ВОСПРОИЗВЕДЕНИЯ
    # ==========================================
    def _render_queue_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ctk.CTkLabel(
            header,
            text="Очередь воспроизведения",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        if self.up_next_queue:
            ctk.CTkButton(
                header,
                text="Очистить «Далее»",
                height=30,
                corner_radius=15,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_muted"],
                font=ctk.CTkFont(size=12),
                command=self._clear_up_next,
            ).pack(side="right")

        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=1, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        row_cursor = 0
        if self.current_track:
            ctk.CTkLabel(
                scroll, text="Сейчас играет", font=ctk.CTkFont(size=13, weight="bold"), text_color=THEME["text_muted"]
            ).grid(row=row_cursor, column=0, sticky="w", pady=(0, 6))
            row_cursor += 1
            self._build_track_row(scroll, row_cursor, self.current_track, [self.current_track], display_num=0, play_index_override=0)
            row_cursor += 1

        if self.up_next_queue:
            ctk.CTkLabel(
                scroll,
                text="Добавлено вручную (сыграет следующим)",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=self.accent_color,
            ).grid(row=row_cursor, column=0, sticky="w", pady=(14, 6))
            row_cursor += 1
            for i, tr in enumerate(self.up_next_queue):
                self._build_track_row(scroll, row_cursor, tr, self.up_next_queue, display_num=i, queue_list_idx=i)
                row_cursor += 1

        upcoming = self.active_queue[self.current_index + 1:] if (self.active_queue and self.current_index >= 0) else []
        if upcoming:
            ctk.CTkLabel(
                scroll,
                text="Далее из текущего списка",
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color=THEME["text_muted"],
            ).grid(row=row_cursor, column=0, sticky="w", pady=(14, 6))
            row_cursor += 1
            for offset, tr in enumerate(upcoming[:30]):
                real_idx = self.current_index + 1 + offset
                self._build_track_row(
                    scroll,
                    row_cursor,
                    tr,
                    self.active_queue,
                    is_wave=self.is_wave_session,
                    display_num=offset,
                    play_index_override=real_idx,
                )
                row_cursor += 1

        if row_cursor == 0:
            ctk.CTkLabel(
                scroll,
                text="Очередь пуста. Нажмите «+» у любого трека, чтобы поставить его следующим.",
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=0, pady=50)

    def _clear_up_next(self):
        self.up_next_queue.clear()
        self._sync_nav_buttons()
        if self.current_page == "queue":
            self._render_page()

    def _add_to_up_next(self, track: TrackItem):
        self.up_next_queue.append(track)
        if track.is_online and not track.has_fresh_stream:
            self._prefetch_batch([track])
        self._sync_nav_buttons()
        if self.current_page == "queue":
            self._render_page()

    # ==========================================
    # СТРОКА ТРЕКА (С КНОПКОЙ «ДАЛЕЕ», ЛАЙКОМ И РЕГИСТРАЦИЕЙ ДЛЯ ТОЧЕЧНЫХ ОБНОВЛЕНИЙ)
    # ==========================================
    def _build_track_row(
        self,
        parent: ctk.CTkScrollableFrame,
        grid_row: int,
        track: TrackItem,
        queue_ref: list[TrackItem],
        is_wave: bool = False,
        display_num: int | None = None,
        play_index_override: int | None = None,
        queue_list_idx: int | None = None,
    ):
        num_idx = grid_row if display_num is None else display_num
        play_idx = grid_row if play_index_override is None else play_index_override

        is_current = self.current_track is not None and self.current_track.uid == track.uid
        row_bg = THEME["bg_card"] if is_current else "transparent"
        title_color = self.accent_color if is_current else THEME["text_main"]

        row = ctk.CTkFrame(parent, fg_color=row_bg, corner_radius=10, height=56)
        row.grid(row=grid_row, column=0, sticky="ew", pady=2, padx=2)
        row.grid_columnconfigure(2, weight=1)
        row.grid_propagate(False)

        num_lbl = ctk.CTkLabel(
            row,
            text="▶" if is_current else str(num_idx + 1),
            width=28,
            font=ctk.CTkFont(size=12, weight="bold" if is_current else "normal"),
            text_color=self.accent_color if is_current else THEME["text_dim"],
        )
        num_lbl.grid(row=0, column=0, padx=(10, 6))

        cover_lbl = ctk.CTkLabel(row, text="", image=self.row_default_cover)
        cover_lbl.grid(row=0, column=1, padx=(0, 12), pady=8)
        self._load_thumbnail_async(track, cover_lbl, size=(40, 40), radius=8)

        subtitle = f"{track.artist}  •  {track.reason}" if (is_wave and track.reason) else track.artist

        def on_click_play():
            if queue_list_idx is not None:
                # Кликнули по треку из ручной очереди — вынимаем и сразу играем
                if 0 <= queue_list_idx < len(self.up_next_queue):
                    picked = self.up_next_queue.pop(queue_list_idx)
                    self._sync_nav_buttons()
                    self._play_single_injected(picked)
                    if self.current_page == "queue":
                        self._render_page()
            else:
                self._play_from_list(queue_ref, play_idx, is_wave)

        info_btn = ctk.CTkButton(
            row,
            text=f"{track.title}\n{subtitle}",
            anchor="w",
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=title_color,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if is_current else "normal"),
            command=on_click_play,
        )
        info_btn.grid(row=0, column=2, sticky="nsew", pady=4)

        dur_str = format_time(track.duration) if track.duration > 0 else "--:--"
        ctk.CTkLabel(
            row,
            text=dur_str,
            width=46,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=3, padx=6)

        # Кнопка «Поставить следующим» (или удалить из ручной очереди)
        if queue_list_idx is not None:
            ctk.CTkButton(
                row,
                text="×",
                width=28,
                height=28,
                corner_radius=14,
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=14),
                command=lambda qi=queue_list_idx: self._remove_from_up_next(qi),
            ).grid(row=0, column=4, padx=2)
        else:
            ctk.CTkButton(
                row,
                text="+",
                width=28,
                height=28,
                corner_radius=14,
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=14, weight="bold"),
                command=lambda t=track: self._add_to_up_next(t),
            ).grid(row=0, column=4, padx=2)

        liked = self._is_liked(track)
        like_btn = ctk.CTkButton(
            row,
            text="♥" if liked else "♡",
            width=30,
            height=30,
            corner_radius=15,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["like_red"] if liked else THEME["text_muted"],
            font=ctk.CTkFont(size=14),
            command=lambda t=track: self._toggle_like_track(t),
        )
        like_btn.grid(row=0, column=5, padx=(2, 10))

        self._row_registry.setdefault(track.uid, []).append({
            "row": row,
            "num": num_lbl,
            "info": info_btn,
            "like": like_btn,
            "idx": num_idx,
        })

    def _remove_from_up_next(self, idx: int):
        if 0 <= idx < len(self.up_next_queue):
            self.up_next_queue.pop(idx)
            self._sync_nav_buttons()
            if self.current_page == "queue":
                self._render_page()

    # ==========================================
    # АЛГОРИТМ «МОЕЙ ВОЛНЫ» С ДЕДУПЛИКАЦИЕЙ
    # ==========================================
    def _set_wave_mood(self, mood: str):
        self.wave_mood = mood
        self._save_user_state()
        self._refresh_wave()

    def _prefill_wave_if_empty(self):
        if not self.wave_queue and not self.wave_fetching:
            self._fetch_wave_batch(autoplay=False)

    def _refresh_wave(self):
        self.wave_queue.clear()
        self._wave_rendered_count = 0
        if self.current_page == "wave":
            self._render_page()
        self._fetch_wave_batch(autoplay=self.is_wave_session)

    def _start_or_toggle_wave(self):
        if self.is_wave_session and self.engine.is_playing:
            self._on_play_pause()
            return
        if self.wave_queue:
            self._play_from_list(self.wave_queue, 0, is_wave=True)
        else:
            self._fetch_wave_batch(autoplay=True)

    def _choose_wave_strategy(self) -> tuple[str, str]:
        mood_mod = self.MOOD_MODIFIERS.get(self.wave_mood, "")

        # 1. YouTube Radio (RD{vid}) по треку из лайков и истории
        if self.seed_videos and self.wave_mood == "Любое" and random.random() < 0.62:
            seed = random.choice(self.seed_videos[-22:])
            vid = seed.get("vid")
            artist = seed.get("artist") or "ваших треков"
            if vid:
                return f"https://www.youtube.com/watch?v={vid}&list=RD{vid}", f"Похоже на {artist}"

        # 2. Поиск по ручным ориентирам
        if self.custom_seeds and random.random() < 0.68:
            artist = random.choice(self.custom_seeds)
            q = f"{artist} {mood_mod}".strip()
            return f"ytsearch14:{q}", f"По ориентиру: {artist}"

        # 3. Топ-артисты профиля вкуса
        top_artists = self._get_top_taste_artists(limit=8)
        if top_artists:
            weights = [max(1.0, self.artist_scores.get(a, 1.0)) for a in top_artists]
            chosen = random.choices(top_artists, weights=weights, k=1)[0]
            q = f"{chosen} {mood_mod}".strip() if mood_mod else f"{chosen} music"
            return f"ytsearch14:{q}", f"На основе: {chosen}"

        # 4. Стартовая подборка
        base = mood_mod if mood_mod else "indie electronic alternative music"
        return f"ytsearch14:{base}", "Подборка волны"

    def _is_valid_music_candidate(self, entry: dict) -> bool:
        title = (entry.get("title") or "").lower()
        if not title or title in ("[deleted video]", "[private video]"):
            return False
        if any(bad in title for bad in self.NOISE_WORDS):
            return False
        dur = float(entry.get("duration") or 0)
        if dur > 0 and (dur < 45 or dur > 600):
            return False
        return True

    def _score_wave_candidate(self, track: TrackItem) -> float:
        score = random.uniform(0.0, 2.5)
        score += self.artist_scores.get(track.artist, 0.0) * 0.8
        if any(s.lower() in track.artist.lower() or s.lower() in track.title.lower() for s in self.custom_seeds):
            score += 3.0
        return score

    def _fetch_wave_batch(self, autoplay: bool = False):
        if self.wave_fetching:
            return
        self.wave_fetching = True
        self._update_wave_status_label()
        target_query, reason_label = self._choose_wave_strategy()

        def worker():
            candidates: list[TrackItem] = []
            existing_uids = {t.uid for t in self.wave_queue} | self.disliked_uids | self.session_played_uids
            existing_sigs = {t.signature for t in self.wave_queue if t.signature} | self.session_played_sigs

            try:
                with self._get_search_ydl(playlist_end=18) as ydl:
                    info = ydl.extract_info(target_query, download=False)
                    for entry in (info.get("entries") or []) if info else []:
                        if not entry or not self._is_valid_music_candidate(entry):
                            continue
                        item = self._entry_to_track(entry, reason=reason_label)
                        if not item:
                            continue
                        if item.uid in existing_uids:
                            continue
                        if item.signature and item.signature in existing_sigs:
                            continue
                        if self.artist_scores.get(item.artist, 0.0) <= -4.0:
                            continue
                        existing_uids.add(item.uid)
                        if item.signature:
                            existing_sigs.add(item.signature)
                        candidates.append(item)
            except Exception:
                pass

            candidates.sort(key=self._score_wave_candidate, reverse=True)
            ordered: list[TrackItem] = []
            last_artist = self.wave_queue[-1].artist if self.wave_queue else ""
            pool = candidates[:14]
            while pool and len(ordered) < 8:
                diff = [t for t in pool if t.artist != last_artist]
                chosen = (diff if diff else pool)[0]
                ordered.append(chosen)
                last_artist = chosen.artist
                pool.remove(chosen)

            def done():
                self.wave_fetching = False
                if ordered:
                    self.wave_queue.extend(ordered)
                    self._append_new_wave_rows()
                    self._prefetch_batch(ordered[:4])
                else:
                    self._update_wave_status_label()

                if autoplay and self.wave_queue:
                    self._play_from_list(self.wave_queue, 0, is_wave=True)

            if not self._closing:
                self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="wave-fetch").start()

    # ==========================================
    # ПОИСК (ПОДДЕРЖКА ПРЯМЫХ ССЫЛОК YOUTUBE И ПЛЕЙЛИСТОВ) + СКАЧИВАНИЕ MP3
    # ==========================================
    def _quick_search(self, query: str):
        self._last_search_text = query
        if hasattr(self, "search_var"):
            self.search_var.set(query)
        self._execute_search()

    def _execute_search(self):
        query = self.search_var.get().strip()
        if not query:
            return
        self._last_search_text = query
        cache_key = query.lower()

        if cache_key in self.search_cache:
            self.search_cache.move_to_end(cache_key)
            self.search_results = self.search_cache[cache_key]
            self._is_searching = False
            if self.current_page == "search":
                self._render_page()
            self._prefetch_batch(self.search_results[:5])
            return

        self._is_searching = True
        self._render_page()

        is_direct_url = query.startswith("http://") or query.startswith("https://")
        target = query if is_direct_url else f"ytsearch12:{query}"

        def worker():
            results: list[TrackItem] = []
            try:
                with self._get_search_ydl(playlist_end=25 if is_direct_url else 12) as ydl:
                    info = ydl.extract_info(target, download=False)
                    if info:
                        entries = info.get("entries")
                        if entries is not None:
                            for entry in entries:
                                if not entry:
                                    continue
                                item = self._entry_to_track(entry)
                                if item:
                                    results.append(item)
                        else:
                            item = self._entry_to_track(info)
                            if item:
                                results.append(item)
            except Exception:
                pass

            def done():
                self._is_searching = False
                self.search_results = results
                if results:
                    self.search_cache[cache_key] = results
                    while len(self.search_cache) > 30:
                        self.search_cache.popitem(last=False)
                if self.current_page == "search":
                    self._render_page()
                self._prefetch_batch(results[:5])

            if not self._closing:
                self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="search").start()

    def _entry_to_track(self, entry: dict, reason: str | None = None) -> TrackItem | None:
        vid = entry.get("id")
        url = entry.get("url") or entry.get("webpage_url")
        if not url and vid:
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url:
            return None
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        if not vid:
            vid = extract_video_id(url)

        thumbs = entry.get("thumbnails") or []
        thumb_url = thumbs[-1]["url"] if thumbs else (
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if vid else None
        )
        return TrackItem(
            title=entry.get("title") or "Без названия",
            artist=entry.get("uploader") or entry.get("channel") or "YouTube",
            duration=float(entry.get("duration") or 0),
            webpage_url=url,
            thumbnail_url=thumb_url,
            source_type="YT",
            reason=reason,
        )

    def _on_download_current(self):
        track = self.current_track
        if not track or not track.is_online or not track.webpage_url:
            return
        self.bar_download_btn.configure(state="disabled", text="…")

        def worker():
            saved_path = None
            try:
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).120s.%(ext)s"),
                    "ffmpeg_location": self.engine.ffmpeg_exe,
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                    ],
                    "quiet": True,
                    "no_warnings": True,
                    "nocheckcertificate": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(track.webpage_url, download=True)
                    if info:
                        base = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
                        if os.path.exists(base):
                            saved_path = base
            except Exception:
                pass

            def done():
                if saved_path:
                    self.bar_download_btn.configure(state="normal", text="✓ MP3", text_color=THEME["ok_green"])
                    item = TrackItem.from_local_file(saved_path)
                    if not any(t.filepath == saved_path for t in self.local_tracks):
                        self.local_tracks.insert(0, item)
                else:
                    self.bar_download_btn.configure(state="normal", text="Ошибка", text_color=THEME["err_red"])

            if not self._closing:
                self.after(0, done)

        threading.Thread(target=worker, daemon=True, name="download-mp3").start()

    # ==========================================
    # ПОТОКОБЕЗОПАСНЫЙ РЕЗОЛВ И ПРЕДЗАГРУЗКА
    # ==========================================
    def _resolve_track_sync(self, track: TrackItem, force_refresh: bool = False) -> str | None:
        if not force_refresh and track.has_fresh_stream:
            return track.stream_url
        if not track.webpage_url:
            return None

        # Координируем параллельные запросы к одному треку через threading.Event
        with track._resolve_lock:
            if not force_refresh and track.has_fresh_stream:
                return track.stream_url
            if track._resolve_event is not None:
                wait_ev = track._resolve_event
                owner = False
            else:
                wait_ev = threading.Event()
                track._resolve_event = wait_ev
                owner = True

        if not owner:
            wait_ev.wait(timeout=18.0)
            return track.stream_url

        try:
            url = None
            info = None
            for use_fallback in (False, True):
                try:
                    ydl = self._get_extract_ydl(fallback=use_fallback)
                    info = ydl.extract_info(track.webpage_url, download=False)
                    url = extract_stream_url_from_info(info)
                    if url:
                        break
                except Exception:
                    continue

            if url:
                track.stream_url = url
                track.stream_resolved_at = time.time()
            if info:
                if info.get("duration") and track.duration <= 0:
                    track.duration = float(info["duration"])
                if not track.thumbnail_url and info.get("thumbnail"):
                    track.thumbnail_url = info["thumbnail"]
        finally:
            with track._resolve_lock:
                wait_ev.set()
                track._resolve_event = None

        return track.stream_url

    def _prefetch_batch(self, tracks: list[TrackItem]):
        if self._closing:
            return
        for t in tracks:
            if t and t.is_online and not t.has_fresh_stream:
                self.prefetch_pool.submit(self._resolve_track_sync, t)

    def _prefetch_upcoming_in_queue(self, current_idx: int):
        upcoming = list(self.up_next_queue[:2])
        if self.active_queue:
            for offset in (1, 2, 3):
                if 0 <= current_idx + offset < len(self.active_queue):
                    upcoming.append(self.active_queue[current_idx + offset])
        self._prefetch_batch(upcoming)

    # ==========================================
    # ВОСПРОИЗВЕДЕНИЕ, ВКУС И ЗАЩИТА ОТ ЗАЦИКЛИВАНИЯ ОШИБОК
    # ==========================================
    def _evaluate_previous_track_signal(self):
        prev = self.current_track
        if not prev or self._track_credited or self._track_started_at <= 0:
            return
        listened = self.engine.get_position()
        if 1.0 < listened < 14.0 and prev.duration > 40.0:
            self._adjust_artist_score(prev.artist, -1.2, save=True)
            self._track_credited = True

    def _play_from_list(self, queue_ref: list[TrackItem], index: int, is_wave: bool = False):
        self.active_queue = queue_ref
        self.is_wave_session = is_wave
        self._consecutive_errors = 0
        if not is_wave and 0 <= index < len(queue_ref):
            self._record_positive_seed(queue_ref[index], weight=2.5)
        self._play_index(index)

    def _play_single_injected(self, track: TrackItem):
        """Воспроизводит трек из ручной очереди, не ломая текущую позицию в активном списке."""
        self._evaluate_previous_track_signal()
        old_uid = self.current_track.uid if self.current_track else None
        self.play_gen += 1
        gen = self.play_gen
        self.current_track = track
        self._track_started_at = time.time()
        self._track_credited = False
        self._start_track_playback(track, gen, old_uid)

    def _play_index(self, index: int):
        if not (0 <= index < len(self.active_queue)):
            return

        self._evaluate_previous_track_signal()
        old_uid = self.current_track.uid if self.current_track else None

        self.play_gen += 1
        gen = self.play_gen
        self.current_index = index
        track = self.active_queue[index]
        self.current_track = track
        self.session_played_uids.add(track.uid)
        if track.signature:
            self.session_played_sigs.add(track.signature)
        self._track_started_at = time.time()
        self._track_credited = False

        if self.is_wave_session and (len(self.wave_queue) - index) <= 4:
            self._fetch_wave_batch(autoplay=False)
        self._prefetch_upcoming_in_queue(index)

        self._start_track_playback(track, gen, old_uid)

    def _start_track_playback(self, track: TrackItem, gen: int, old_uid: str | None):
        need_resolve = track.is_online and not track.has_fresh_stream
        self._update_bottom_bar(track, loading=need_resolve)
        self._update_row_highlight_in_place(old_uid, track.uid)
        self._record_history(track)

        if not track.is_online and track.filepath:
            if not os.path.exists(track.filepath):
                self._handle_playback_failure("НЕТ ФАЙЛА")
                return
            self._consecutive_errors = 0
            self.engine.play_source(track.filepath, start_sec=0.0, expected_duration=track.duration)
            self._sync_play_button()
            return

        if track.has_fresh_stream and track.stream_url:
            self._consecutive_errors = 0
            self.engine.play_source(track.stream_url, start_sec=0.0, expected_duration=track.duration)
            self._sync_play_button()
            return

        self.engine.stop()
        self._sync_play_button()

        def resolve_worker():
            stream_url = self._resolve_track_sync(track)

            def on_ready():
                if gen != self.play_gen or self._closing:
                    return
                if stream_url:
                    self._consecutive_errors = 0
                    self._update_bottom_bar(track, loading=False)
                    self.engine.play_source(stream_url, start_sec=0.0, expected_duration=track.duration)
                    self._sync_play_button()
                    self._load_bar_cover_async(track)
                else:
                    self._handle_playback_failure("НЕТ СЕТИ")

            if not self._closing:
                self.after(0, on_ready)

        threading.Thread(target=resolve_worker, daemon=True, name="resolve-play").start()

    def _on_stream_error(self):
        # Если ссылка протухла прямо перед стартом — пробуем 1 раз перерезолвить
        track = self.current_track
        if track and track.is_online and self._consecutive_errors == 0:
            self._consecutive_errors += 1
            track.invalidate_stream()
            gen = self.play_gen
            self.bar_source_badge.configure(text="ПОВТОР…")

            def retry_worker():
                url = self._resolve_track_sync(track, force_refresh=True)

                def apply():
                    if gen != self.play_gen or self._closing:
                        return
                    if url:
                        self.bar_source_badge.configure(text="STREAM")
                        self.engine.play_source(url, start_sec=0.0, expected_duration=track.duration)
                        self._sync_play_button()
                    else:
                        self._handle_playback_failure("ОШИБКА")

                if not self._closing:
                    self.after(0, apply)

            threading.Thread(target=retry_worker, daemon=True, name="retry-stream").start()
            return

        self._handle_playback_failure("ОШИБКА")

    def _handle_playback_failure(self, badge_text: str):
        self._consecutive_errors += 1
        self.bar_source_badge.configure(text=badge_text, text_color=THEME["err_red"])
        self._sync_play_button()

        # Предохранитель: не крутим бесконечный цикл при обрыве интернета
        if self.is_wave_session and self._consecutive_errors < 3 and len(self.active_queue) > 1:
            self.after(600, self._on_next)

    # ==========================================
    # ОБЛОЖКИ, АКЦЕНТЫ И НИЖНЯЯ ПАНЕЛЬ
    # ==========================================
    def _load_bar_cover_async(self, track: TrackItem):
        if track.cover_pil is not None:
            rounded = make_rounded_cover(track.cover_pil, size=(56, 56), radius=10)
            ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=(56, 56))
            self.cover_cache.put_mem(f"bar:{track.uid}", ctk_img)
            self.bar_cover_label.configure(image=ctk_img)
            self.bar_cover_label._img_ref = ctk_img
            accent, soft = extract_muted_accent(track.cover_pil)
            self._apply_accent(accent, soft)
            return

        if not track.thumbnail_url:
            return

        def on_pil(pil_img: Image.Image):
            track.cover_pil = pil_img
            rounded = make_rounded_cover(pil_img, size=(56, 56), radius=10)
            accent, soft = extract_muted_accent(pil_img)

            def apply():
                if self._closing or not self.current_track or self.current_track.uid != track.uid:
                    return
                ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=(56, 56))
                self.cover_cache.put_mem(f"bar:{track.uid}", ctk_img)
                self.bar_cover_label.configure(image=ctk_img)
                self.bar_cover_label._img_ref = ctk_img
                self._apply_accent(accent, soft)

            if not self._closing:
                self.after(0, apply)

        self.cover_cache.fetch_pil_async(track.thumbnail_url, on_pil)

    def _load_thumbnail_async(self, track: TrackItem, label: ctk.CTkLabel, size=(40, 40), radius=8):
        cache_key = f"{size[0]}:{track.uid}"
        cached = self.cover_cache.get_mem(cache_key)
        if cached is not None:
            label.configure(image=cached)
            label._img_ref = cached
            return

        if track.cover_pil is not None:
            rounded = make_rounded_cover(track.cover_pil, size=size, radius=radius)
            ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=size)
            self.cover_cache.put_mem(cache_key, ctk_img)
            label.configure(image=ctk_img)
            label._img_ref = ctk_img
            return

        if not track.thumbnail_url:
            return

        def on_pil(pil_img: Image.Image):
            track.cover_pil = pil_img
            rounded = make_rounded_cover(pil_img, size=size, radius=radius)

            def apply():
                if self._closing:
                    return
                ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=size)
                self.cover_cache.put_mem(cache_key, ctk_img)
                try:
                    if label.winfo_exists():
                        label.configure(image=ctk_img)
                        label._img_ref = ctk_img
                except tk.TclError:
                    pass

            if not self._closing:
                self.after(0, apply)

        self.cover_cache.fetch_pil_async(track.thumbnail_url, on_pil)

    def _update_bottom_bar(self, track: TrackItem, loading: bool = False):
        short_title = track.title if len(track.title) <= 28 else track.title[:26] + "…"
        short_artist = track.artist if len(track.artist) <= 30 else track.artist[:28] + "…"
        self.bar_title.configure(text=short_title)
        self.bar_artist.configure(text=short_artist)
        self.total_label.configure(text=format_time(track.duration) if track.duration > 0 else "--:--")
        self.progress_slider.configure(to=max(1.0, track.duration if track.duration > 0 else 100.0))
        self.progress_slider.set(0)
        self.elapsed_label.configure(text="00:00")

        liked = self._is_liked(track)
        self.like_btn.configure(
            text="♥" if liked else "♡",
            text_color=THEME["like_red"] if liked else THEME["text_muted"],
        )

        if track.is_online:
            self.bar_source_badge.configure(
                text="СТАРТ…" if loading else "STREAM",
                text_color=self.accent_color,
                fg_color=self.accent_soft,
            )
            self.bar_download_btn.configure(state="normal", text="↓ MP3", text_color=THEME["text_main"])
            self.bar_download_btn.grid()
        else:
            self.bar_source_badge.configure(text="LOCAL", text_color=self.accent_color, fg_color=self.accent_soft)
            self.bar_download_btn.grid_remove()

        if track.cover_pil is not None:
            self._load_bar_cover_async(track)
        else:
            self.bar_cover_label.configure(image=self.bar_default_cover)
            self._apply_accent(THEME["wave_gold"], THEME["wave_soft"])
            if track.is_online:
                self._load_bar_cover_async(track)

        self._update_tray_state()

    def _apply_accent(self, accent: str, soft: str):
        self.accent_color = accent
        self.accent_soft = soft
        self.progress_slider.configure(progress_color=accent, button_hover_color=accent)
        self.volume_slider.configure(button_hover_color=accent)
        self.bar_source_badge.configure(fg_color=soft, text_color=accent)
        if self.repeat_mode:
            self.repeat_btn.configure(text_color=accent)
        if self.shuffle_mode:
            self.shuffle_btn.configure(text_color=accent)
        if self.engine.eq_preset != "flat":
            self.eq_btn.configure(fg_color=soft, text_color=accent)
        if self.current_track:
            self._update_row_highlight_in_place(None, self.current_track.uid)
        self._sync_wave_hero_button()

    # ==========================================
    # ЛАЙКИ, ДИЗЛАЙКИ, EQ И УПРАВЛЕНИЕ
    # ==========================================
    def _is_liked(self, track: TrackItem) -> bool:
        return any(t.uid == track.uid for t in self.liked_tracks)

    def _toggle_like_current(self):
        if self.current_track:
            self._toggle_like_track(self.current_track)

    def _toggle_like_track(self, track: TrackItem):
        now = time.time()
        if self._is_liked(track):
            self.liked_tracks = [t for t in self.liked_tracks if t.uid != track.uid]
            self.unliked_timestamps[track.uid] = now
            self._adjust_artist_score(track.artist, -2.5, save=True)
            liked = False
        else:
            self.liked_tracks.insert(0, track)
            self.liked_timestamps[track.uid] = now
            self.unliked_timestamps.pop(track.uid, None)
            self.disliked_uids.discard(track.uid)
            self._record_positive_seed(track, weight=4.0)
            liked = True

        if self.current_track and self.current_track.uid == track.uid:
            self.like_btn.configure(
                text="♥" if liked else "♡",
                text_color=THEME["like_red"] if liked else THEME["text_muted"],
            )

        self._refresh_sidebar_taste_box()
        self._update_tray_state()

        # Точечно обновляем сердечко без сброса скролла; перерисовываем только вкладку "Мне нравится"
        if self.current_page == "library" and self.library_subtab == "liked":
            self._populate_library_scroll()
        else:
            self._update_row_like_in_place(track.uid, liked)

    def _dislike_and_skip_current(self):
        if not self.current_track:
            return
        uid = self.current_track.uid
        self.disliked_uids.add(uid)
        if self._is_liked(self.current_track):
            self.unliked_timestamps[uid] = time.time()
        self.liked_tracks = [t for t in self.liked_tracks if t.uid != uid]
        self._adjust_artist_score(self.current_track.artist, -5.0, save=True)
        self._track_credited = True
        self._refresh_sidebar_taste_box()
        self._on_next()

    def _sync_play_button(self):
        playing = self.engine.is_playing and not self.engine.is_paused
        self.play_btn.configure(text="⏸" if playing else "▶")
        self._sync_wave_hero_button()
        self._update_tray_state()

    def _on_play_pause(self):
        if not self.engine.is_playing:
            if self.active_queue and self.current_index >= 0:
                self._play_index(self.current_index)
            else:
                self._start_or_toggle_wave()
            return
        self.engine.toggle_pause()
        self._sync_play_button()

    def _on_next(self):
        # Сначала играем треки из ручной очереди "Далее"
        if self.up_next_queue:
            nxt = self.up_next_queue.pop(0)
            self._sync_nav_buttons()
            self._play_single_injected(nxt)
            if self.current_page == "queue":
                self._render_page()
            return

        if not self.active_queue:
            return

        if self.shuffle_mode and not self.is_wave_session and len(self.active_queue) > 1:
            choices = [i for i in range(len(self.active_queue)) if i != self.current_index]
            next_idx = random.choice(choices)
        else:
            next_idx = (self.current_index + 1) % len(self.active_queue)
        self._play_index(next_idx)

    def _on_prev(self):
        if self.engine.is_playing and self.engine.get_position() > 4.0:
            self.engine.seek(0.0)
            return
        if not self.active_queue:
            return
        prev_idx = (self.current_index - 1) % len(self.active_queue)
        self._play_index(prev_idx)

    def _on_track_finished(self):
        if self.current_track and not self._track_credited:
            self._record_positive_seed(self.current_track, weight=2.0)
            self._track_credited = True

        if self.repeat_mode and self.current_track:
            self._play_single_injected(self.current_track)
        else:
            self._on_next()

    def _toggle_repeat(self):
        self.repeat_mode = not self.repeat_mode
        self.repeat_btn.configure(text_color=self.accent_color if self.repeat_mode else THEME["text_muted"])

    def _toggle_shuffle(self):
        self.shuffle_mode = not self.shuffle_mode
        self.shuffle_btn.configure(text_color=self.accent_color if self.shuffle_mode else THEME["text_muted"])

    def _cycle_eq_preset(self):
        idx = EQ_ORDER.index(self.engine.eq_preset) if self.engine.eq_preset in EQ_ORDER else 0
        nxt = EQ_ORDER[(idx + 1) % len(EQ_ORDER)]
        self.engine.set_eq_preset(nxt)
        label, _ = EQ_PRESETS[nxt]
        self.eq_btn.configure(
            text=label,
            fg_color=self.accent_soft if nxt != "flat" else THEME["bg_card"],
            text_color=self.accent_color if nxt != "flat" else THEME["text_muted"],
        )
        self._save_user_state()

    def _toggle_normalize(self):
        self.engine.set_normalize(not self.engine.normalize_audio)
        self._save_user_state()
        self._refresh_sidebar_taste_box()

    def _toggle_mute(self):
        muted = self.engine.toggle_mute()
        self.mute_btn.configure(text="🔇" if muted else "🔊")

    def _on_volume_change(self, val):
        self.engine.set_volume(float(val))
        self.mute_btn.configure(text="🔇" if self.engine.muted or self.engine.volume <= 0.001 else "🔊")

    def _on_seek_start(self, _e):
        self.is_seeking = True

    def _on_slider_drag(self, val):
        if self.is_seeking:
            self.elapsed_label.configure(text=format_time(float(val)))

    def _on_seek_end(self, _e):
        if self.is_seeking:
            self.engine.seek(float(self.progress_slider.get()))
            self.is_seeking = False

    # ==========================================
    # АСИНХРОННОЕ ДОБАВЛЕНИЕ ЛОКАЛЬНЫХ ФАЙЛОВ
    # ==========================================
    def _on_add_local_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку с музыкой")
        if not folder:
            return

        def worker():
            new_items: list[TrackItem] = []
            existing = {t.filepath for t in self.local_tracks}
            for root, _, files in os.walk(folder):
                for f in sorted(files):
                    if f.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                        fp = os.path.join(root, f)
                        if fp not in existing:
                            existing.add(fp)
                            new_items.append(TrackItem.from_local_file(fp))

            def apply():
                if new_items:
                    self.local_tracks.extend(new_items)
                    if self.current_page == "library":
                        self._render_page()

            if not self._closing:
                self.after(0, apply)

        threading.Thread(target=worker, daemon=True, name="scan-local-dir").start()

    def _on_add_local_files(self):
        files = filedialog.askopenfilenames(
            title="Выберите аудиофайлы",
            filetypes=[("Audio Files", "*.mp3 *.flac *.wav *.ogg *.m4a *.opus *.aac")],
        )
        if not files:
            return

        def worker():
            new_items: list[TrackItem] = []
            existing = {t.filepath for t in self.local_tracks}
            for fp in files:
                if fp not in existing:
                    existing.add(fp)
                    new_items.append(TrackItem.from_local_file(fp))

            def apply():
                if new_items:
                    self.local_tracks.extend(new_items)
                    if self.current_page == "library":
                        self._render_page()

            if not self._closing:
                self.after(0, apply)

        threading.Thread(target=worker, daemon=True, name="scan-local-files").start()

    # ==========================================
    # ГОРЯЧИЕ КЛАВИШИ (НЕ ЛОМАЮТ ВВОД ТЕКСТА)
    # ==========================================
    def _is_typing_in_input(self) -> bool:
        w = self.focus_get()
        return isinstance(w, (tk.Entry, tk.Text))

    def _bind_shortcuts(self):
        self.bind("<space>", self._hk_space)
        self.bind("<Left>", self._hk_seek_back)
        self.bind("<Right>", self._hk_seek_fwd)
        self.bind("<Up>", self._hk_vol_up)
        self.bind("<Down>", self._hk_vol_down)
        self.bind("<Control-f>", self._hk_focus_search)
        self.bind("<Control-Right>", lambda _e: self._on_next())
        self.bind("<Control-Left>", lambda _e: self._on_prev())
        self.bind("m", self._hk_mute)
        self.bind("M", self._hk_mute)
        self.bind("l", self._hk_like)
        self.bind("L", self._hk_like)
        self.bind("<Control-q>", lambda _e: self._quit_app())

    def _hk_space(self, _e):
        if not self._is_typing_in_input():
            self._on_play_pause()

    def _hk_seek_back(self, _e):
        if not self._is_typing_in_input():
            self.engine.seek(max(0.0, self.engine.get_position() - 5.0))

    def _hk_seek_fwd(self, _e):
        if not self._is_typing_in_input():
            self.engine.seek(self.engine.get_position() + 5.0)

    def _hk_vol_up(self, _e):
        if not self._is_typing_in_input():
            v = min(1.0, self.engine.volume + 0.05)
            self.engine.set_volume(v)
            self.volume_slider.set(v)
            self.mute_btn.configure(text="🔊")

    def _hk_vol_down(self, _e):
        if not self._is_typing_in_input():
            v = max(0.0, self.engine.volume - 0.05)
            self.engine.set_volume(v)
            self.volume_slider.set(v)
            self.mute_btn.configure(text="🔇" if v <= 0.001 else "🔊")

    def _hk_mute(self, _e):
        if not self._is_typing_in_input():
            self._toggle_mute()

    def _hk_like(self, _e):
        if not self._is_typing_in_input():
            self._toggle_like_current()

    def _hk_focus_search(self, _e):
        self._switch_page("search")
        if hasattr(self, "search_entry") and self.search_entry.winfo_exists():
            self.search_entry.focus_set()

    # ==========================================
    # ОСНОВНОЙ ЦИКЛ UI (ПРОГРЕСС + ЖИВОЙ ИНДИКАТОР)
    # ==========================================
    def _tick_loop(self):
        if self._closing:
            return

        is_visible = True
        try:
            is_visible = bool(self.winfo_viewable())
        except Exception:
            is_visible = True

        if self.engine.is_playing and not self.is_seeking:
            pos = self.engine.get_position()
            if self.current_track:
                if self.current_track.duration > 0:
                    pos = min(pos, self.current_track.duration)
                    if not self._track_credited and pos >= self.current_track.duration * 0.65:
                        self._record_positive_seed(self.current_track, weight=1.8)
                        self._track_credited = True
                elif is_visible and pos > self.progress_slider.cget("to"):
                    self.progress_slider.configure(to=pos + 30.0)
            if is_visible:
                self.progress_slider.set(pos)
                self.elapsed_label.configure(text=format_time(pos))

        # Обновляем 5-полосный минималистичный индикатор только когда окно видимо
        if is_visible:
            bars = self.engine.spectrum_bars
            playing_active = self.engine.is_playing and not self.engine.is_paused and not self.engine.muted
            bar_color = self.accent_color if playing_active else THEME["text_dim"]
            for i, rect in enumerate(self.meter_rects):
                lvl = bars[i] if playing_active else 0.0
                h = max(2, int(2 + lvl * 15))
                x0 = i * 5 + 1
                self.meter_canvas.coords(rect, x0, 18 - h, x0 + 3, 18)
                self.meter_canvas.itemconfig(rect, fill=bar_color)

        self.after(90 if is_visible else 350, self._tick_loop)

    def _quit_app(self):
        if self._closing:
            return
        self._closing = True

        if self._active_oauth_flow is not None:
            try:
                self._active_oauth_flow.cancel()
            except Exception:
                pass
            self._active_oauth_flow = None

        if self._cloud_sync_timer is not None:
            try:
                self.after_cancel(self._cloud_sync_timer)
            except Exception:
                pass
            self._cloud_sync_timer = None

        self._save_user_state(sync_cloud=False)

        if self.google_sync.is_signed_in and self._cloud_dirty:
            snapshot = self._export_state_dict()

            def final_push():
                try:
                    self.google_sync.push_profile(snapshot)
                except Exception:
                    pass

            push_thread = threading.Thread(target=final_push, daemon=True, name="google-final-push")
            push_thread.start()
            push_thread.join(timeout=2.5)

        try:
            self.tray.stop()
        except Exception:
            pass

        self.engine.stop()
        self.cover_cache.shutdown()
        self.google_sync.close()
        try:
            self.prefetch_pool.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        self.destroy()


if __name__ == "__main__":
    app = AetherStreamingApp()
    app.mainloop()
