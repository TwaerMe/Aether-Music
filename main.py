import colorsys
import io
import json
import os
import random
import subprocess
import threading
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
import imageio_ffmpeg
import numpy as np
import requests
import sounddevice as sd
import yt_dlp
from mutagen import File as MutagenFile
from mutagen.id3 import APIC
from PIL import Image, ImageDraw

# ==========================================
# ПАЛИТРА В ДУХЕ SPOTIFY / ЯНДЕКС МУЗЫКИ (БЕЗ НЕОНА)
# ==========================================
THEME = {
    "bg_root": "#0D0F12",        # Основной матовый фон контента
    "bg_sidebar": "#12151B",     # Левая панель навигации
    "bg_bar": "#14181F",         # Нижняя панель плеера
    "bg_card": "#191E26",        # Карточки и строки
    "bg_hover": "#222833",       # Наведение
    "border": "#232934",         # Тонкие разделители
    "text_main": "#F2F4F8",      # Основной мягкий белый
    "text_muted": "#8E96A4",     # Вторичный серый
    "text_dim": "#58606E",       # Приглушенный текст
    "wave_gold": "#C8A464",      # Спокойный песочно-золотой (акцент Волны)
    "wave_soft": "#28241C",      # Мягкая подложка акцента
    "like_red": "#C96A6A",       # Приглушенный коралловый для лайков
}

ctk.set_appearance_mode("dark")

APP_DATA_DIR = os.path.join(os.path.expanduser("~"), ".aether_player")
STATE_FILE = os.path.join(APP_DATA_DIR, "user_state.json")
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "Aether")


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
    r1 = int(min(size) * 0.30)
    r2 = int(min(size) * 0.10)
    draw.ellipse((cx - r1, cy - r1, cx + r1, cy + r1), outline=accent, width=2)
    draw.ellipse((cx - r2, cy - r2, cx + r2, cy + r2), outline=THEME["text_muted"], width=1)
    return make_rounded_cover(img, size=size, radius=radius)


def extract_muted_accent(img: Image.Image) -> tuple[str, str]:
    """Извлекает цвет обложки и приглушает насыщенность (никакого неона)."""
    try:
        small = img.convert("RGB").resize((28, 28))
        pixels = list(small.getdata())
        r_sum = g_sum = b_sum = count = 0
        for r, g, b in pixels:
            if 30 < (r + g + b) // 3 < 225:
                r_sum += r
                g_sum += g
                b_sum += b
                count += 1
        if count == 0:
            return THEME["wave_gold"], THEME["wave_soft"]

        r, g, b = (r_sum / count) / 255.0, (g_sum / count) / 255.0, (b_sum / count) / 255.0
        h, l, s = colorsys.rgb_to_hls(r, g, b)

        s_muted = min(max(s, 0.20), 0.38)
        l_main = min(max(l, 0.55), 0.64)
        r1, g1, b1 = colorsys.hls_to_rgb(h, l_main, s_muted)
        accent_hex = f"#{int(r1*255):02x}{int(g1*255):02x}{int(b1*255):02x}"

        r2, g2, b2 = colorsys.hls_to_rgb(h, 0.17, min(s_muted, 0.24))
        soft_hex = f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"
        return accent_hex, soft_hex
    except Exception:
        return THEME["wave_gold"], THEME["wave_soft"]


# ==========================================
# МОДЕЛЬ ТРЕКА И ПОТОКОВЫЙ АУДИОДВИЖОК
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
        source_type: str = "YT",  # "YT" | "SC" | "LOCAL"
    ):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.filepath = filepath
        self.webpage_url = webpage_url
        self.thumbnail_url = thumbnail_url
        self.source_type = source_type
        self.stream_url: str | None = None
        self.cover_pil: Image.Image | None = None

    @property
    def uid(self) -> str:
        return self.webpage_url or self.filepath or f"{self.artist}:{self.title}"

    @property
    def is_online(self) -> bool:
        return self.source_type in ("YT", "SC")

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
        return cls(
            title=data.get("title", "Без названия"),
            artist=data.get("artist", "Неизвестный исполнитель"),
            duration=float(data.get("duration", 0.0)),
            filepath=data.get("filepath"),
            webpage_url=data.get("webpage_url"),
            thumbnail_url=data.get("thumbnail_url"),
            source_type=data.get("source_type", "YT"),
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
                            cover_pil = Image.open(io.BytesIO(tag.data))
                            break
                    if cover_pil is None and hasattr(audio, "pictures") and audio.pictures:
                        cover_pil = Image.open(io.BytesIO(audio.pictures[0].data))
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


class StreamAudioEngine:
    SUPPORTED_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")

    def __init__(self, on_track_end_callback=None):
        self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.on_track_end = on_track_end_callback

        self.volume: float = 0.75
        self.is_playing: bool = False
        self.is_paused: bool = False

        self._proc: subprocess.Popen | None = None
        self._stop_event = threading.Event()
        self._session_id: int = 0

        self._current_source: str | None = None
        self._is_http: bool = False
        self._base_offset: float = 0.0
        self._frames_played: int = 0
        self.sample_rate = 44100

    def play_source(self, source: str, start_sec: float = 0.0):
        self.stop()
        self._session_id += 1
        sid = self._session_id

        self._current_source = source
        self._is_http = source.startswith("http://") or source.startswith("https://")
        self._base_offset = max(0.0, start_sec)
        self._frames_played = 0
        self.is_playing = True
        self.is_paused = False
        self._stop_event.clear()

        threading.Thread(
            target=self._playback_worker,
            args=(sid, source, self._base_offset),
            daemon=True,
        ).start()

    def _playback_worker(self, sid: int, source: str, start_sec: float):
        cmd = [self.ffmpeg_exe]
        if self._is_http:
            cmd.extend(["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5"])
        if start_sec > 0.1:
            cmd.extend(["-ss", f"{start_sec:.2f}"])
        cmd.extend([
            "-i", source,
            "-vn",
            "-f", "s16le",
            "-acodec", "pcm_s16le",
            "-ar", str(self.sample_rate),
            "-ac", "2",
            "-loglevel", "quiet",
            "pipe:1",
        ])

        creationflags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            self._proc = proc
        except Exception:
            self.is_playing = False
            return

        finished_naturally = False
        chunk_bytes = 4096

        try:
            with sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=2,
                dtype="int16",
                blocksize=1024,
            ) as stream:
                while not self._stop_event.is_set() and sid == self._session_id:
                    if self.is_paused:
                        time.sleep(0.04)
                        continue

                    raw = proc.stdout.read(chunk_bytes)
                    if not raw:
                        if not self._stop_event.is_set() and sid == self._session_id:
                            finished_naturally = True
                        break

                    if self.volume < 0.99:
                        samples = np.frombuffer(raw, dtype=np.int16)
                        scaled = (samples.astype(np.float32) * self.volume).astype(np.int16)
                        raw = scaled.tobytes()

                    stream.write(raw)
                    self._frames_played += len(raw) // 4
        except Exception:
            pass
        finally:
            try:
                proc.terminate()
            except Exception:
                pass

        if finished_naturally and sid == self._session_id:
            self.is_playing = False
            if self.on_track_end:
                self.on_track_end()

    def stop(self):
        self._session_id += 1
        self._stop_event.set()
        self.is_playing = False
        self.is_paused = False
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def toggle_pause(self):
        if self.is_playing:
            self.is_paused = not self.is_paused

    def seek(self, seconds: float):
        if self._current_source and self.is_playing:
            was_paused = self.is_paused
            self.play_source(self._current_source, start_sec=seconds)
            self.is_paused = was_paused

    def set_volume(self, value: float):
        self.volume = max(0.0, min(1.0, value))

    def get_position(self) -> float:
        if not self.is_playing:
            return 0.0
        return self._base_offset + (self._frames_played / self.sample_rate)


# ==========================================
# ГЛАВНОЕ ОКНО (SPOTIFY / ЯНДЕКС МУЗЫКА LAYOUT)
# ==========================================
class AetherStreamingApp(ctk.CTk):
    MOODS = ["Любое", "Спокойное", "Бодрое", "Грустное", "Фокус"]
    GENRES = ["Микс", "Инди", "Электроника", "Рок", "Фонк", "Lo-Fi"]

    MOOD_QUERIES = {
        "Любое": "atmospheric music",
        "Спокойное": "chill calm indie ambient",
        "Бодрое": "energetic upbeat electronic indie",
        "Грустное": "sad melancholic indie slow",
        "Фокус": "deep focus instrumental beats",
    }

    GENRE_QUERIES = {
        "Микс": "alternative indie electronic",
        "Инди": "indie rock dream pop",
        "Электроника": "electronic synthwave downtempo",
        "Рок": "alternative post punk rock",
        "Фонк": "phonk drift wave",
        "Lo-Fi": "lofi hip hop chillhop",
    }

    def __init__(self):
        super().__init__()

        self.title("Aether Music")
        self.geometry("1120x700")
        self.minsize(960, 620)
        self.configure(fg_color=THEME["bg_root"])

        self.engine = StreamAudioEngine(
            on_track_end_callback=lambda: self.after(0, self._on_track_finished)
        )

        # Состояние пользователя и рекомендации
        self.liked_tracks: list[TrackItem] = []
        self.disliked_uids: set[str] = set()
        self.session_played_uids: set[str] = set()
        self.local_tracks: list[TrackItem] = []
        self.search_results: list[TrackItem] = []
        self.wave_queue: list[TrackItem] = []

        self.active_queue: list[TrackItem] = []
        self.current_index: int = -1
        self.current_track: TrackItem | None = None
        self.is_wave_session: bool = False

        # Настройки навигации и волны
        self.current_page = "wave"      # "wave" | "search" | "library"
        self.library_subtab = "liked"   # "liked" | "local"
        self.search_source = "YT"       # "YT" | "SC"
        self.wave_mood = "Любое"
        self.wave_genre = "Микс"
        self.wave_fetching = False

        # Плеер
        self.play_gen = 0
        self.is_seeking = False
        self.repeat_mode = False
        self.shuffle_mode = False
        self.accent_color = THEME["wave_gold"]
        self.accent_soft = THEME["wave_soft"]

        # Кэш обложек
        self.thumb_cache: dict[str, ctk.CTkImage] = {}
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

        self._load_user_state()
        self._load_downloads_folder()
        self._build_layout()
        self._bind_shortcuts()
        self._render_page()
        self._prefill_wave_if_empty()
        self._tick_loop()

    # ==========================================
    # СОХРАНЕНИЕ И ЗАГРУЗКА ДАННЫХ ПОЛЬЗОВАТЕЛЯ
    # ==========================================
    def _load_user_state(self):
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.liked_tracks = [
                    TrackItem.from_dict(item) for item in data.get("liked_tracks", [])
                ]
                self.disliked_uids = set(data.get("disliked_uids", []))
                self.search_source = data.get("search_source", "YT")
        except Exception:
            pass

    def _save_user_state(self):
        try:
            os.makedirs(APP_DATA_DIR, exist_ok=True)
            data = {
                "liked_tracks": [t.to_dict() for t in self.liked_tracks],
                "disliked_uids": list(self.disliked_uids)[-300:],
                "search_source": self.search_source,
            }
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _load_downloads_folder(self):
        if os.path.isdir(DOWNLOAD_DIR):
            for fname in sorted(os.listdir(DOWNLOAD_DIR)):
                if fname.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                    self.local_tracks.append(
                        TrackItem.from_local_file(os.path.join(DOWNLOAD_DIR, fname))
                    )

    # ==========================================
    # КАРКАС ОКНА (3 ЗОНЫ: SIDEBAR / CONTENT / BOTTOM BAR)
    # ==========================================
    def _build_layout(self):
        self.grid_columnconfigure(0, weight=0, minsize=230)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=0, minsize=88)

        # ---------- 1. ЛЕВАЯ ПАНЕЛЬ НАВИГАЦИИ ----------
        self.sidebar = ctk.CTkFrame(
            self, fg_color=THEME["bg_sidebar"], corner_radius=0, width=230
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(4, weight=1)
        self.sidebar.grid_columnconfigure(0, weight=1)

        # Логотип
        logo_box = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        logo_box.grid(row=0, column=0, sticky="ew", padx=22, pady=(24, 22))
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

        # Кнопки разделов
        self.nav_buttons: dict[str, ctk.CTkButton] = {}
        nav_items = [
            ("wave", "🌊   Моя волна"),
            ("search", "🔍   Поиск"),
            ("library", "📚   Коллекция"),
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

        # Нижний блок сайдбара: выбор источника (YouTube / SoundCloud)
        source_box = ctk.CTkFrame(
            self.sidebar,
            fg_color=THEME["bg_card"],
            corner_radius=12,
            border_width=1,
            border_color=THEME["border"],
        )
        source_box.grid(row=5, column=0, sticky="ew", padx=14, pady=18)
        source_box.grid_columnconfigure((0, 1), weight=1)

        ctk.CTkLabel(
            source_box,
            text="Источник потока",
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_dim"],
        ).grid(row=0, column=0, columnspan=2, sticky="w", padx=12, pady=(8, 4))

        self.src_yt_btn = ctk.CTkButton(
            source_box,
            text="YouTube",
            height=28,
            corner_radius=7,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._set_search_source("YT"),
        )
        self.src_yt_btn.grid(row=1, column=0, sticky="ew", padx=(8, 3), pady=(0, 8))

        self.src_sc_btn = ctk.CTkButton(
            source_box,
            text="SoundCloud",
            height=28,
            corner_radius=7,
            font=ctk.CTkFont(size=11, weight="bold"),
            command=lambda: self._set_search_source("SC"),
        )
        self.src_sc_btn.grid(row=1, column=1, sticky="ew", padx=(3, 8), pady=(0, 8))
        self._sync_source_buttons()

        # ---------- 2. ЦЕНТРАЛЬНАЯ ОБЛАСТЬ КОНТЕНТА ----------
        self.content_area = ctk.CTkFrame(self, fg_color=THEME["bg_root"], corner_radius=0)
        self.content_area.grid(row=0, column=1, sticky="nsew")
        self.content_area.grid_columnconfigure(0, weight=1)
        self.content_area.grid_rowconfigure(0, weight=1)

        # ---------- 3. НИЖНЯЯ ПАНЕЛЬ ПЛЕЕРА (НА ВСЮ ШИРИНУ) ----------
        self.bottom_bar = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_bar"],
            corner_radius=0,
            height=88,
            border_width=1,
            border_color=THEME["border"],
        )
        self.bottom_bar.grid(row=1, column=0, columnspan=2, sticky="nsew")
        self.bottom_bar.grid_propagate(False)
        self.bottom_bar.grid_columnconfigure(0, weight=1, uniform="bar")
        self.bottom_bar.grid_columnconfigure(1, weight=2, uniform="bar")
        self.bottom_bar.grid_columnconfigure(2, weight=1, uniform="bar")
        self.bottom_bar.grid_rowconfigure(0, weight=1)

        # Левая секция бара: обложка + инфо + лайк/дизлайк
        bar_left = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_left.grid(row=0, column=0, sticky="w", padx=18)

        self.bar_cover_label = ctk.CTkLabel(
            bar_left, text="", image=self.bar_default_cover
        )
        self.bar_cover_label.pack(side="left", padx=(0, 12))

        meta_box = ctk.CTkFrame(bar_left, fg_color="transparent")
        meta_box.pack(side="left", padx=(0, 10))

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

        self.like_btn = ctk.CTkButton(
            bar_left,
            text="♡",
            width=32,
            height=32,
            corner_radius=16,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=16),
            command=self._toggle_like_current,
        )
        self.like_btn.pack(side="left", padx=2)

        self.dislike_btn = ctk.CTkButton(
            bar_left,
            text="⊘",
            width=32,
            height=32,
            corner_radius=16,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_dim"],
            font=ctk.CTkFont(size=14),
            command=self._dislike_and_skip_current,
        )
        self.dislike_btn.pack(side="left", padx=2)

        # Центральная секция бара: кнопки + прогресс
        bar_center = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_center.grid(row=0, column=1, sticky="ew", padx=12)
        bar_center.grid_columnconfigure(0, weight=1)

        btns_row = ctk.CTkFrame(bar_center, fg_color="transparent")
        btns_row.grid(row=0, column=0, pady=(8, 2))

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

        # Правая секция бара: скачать MP3 + источник + громкость
        bar_right = ctk.CTkFrame(self.bottom_bar, fg_color="transparent")
        bar_right.grid(row=0, column=2, sticky="e", padx=20)

        self.bar_download_btn = ctk.CTkButton(
            bar_right,
            text="↓ MP3",
            width=58,
            height=26,
            corner_radius=7,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=11),
            command=self._on_download_current,
        )
        self.bar_download_btn.pack(side="left", padx=(0, 8))

        self.bar_source_badge = ctk.CTkLabel(
            bar_right,
            text="READY",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_muted"],
            fg_color=THEME["bg_card"],
            corner_radius=6,
            padx=8,
            pady=2,
        )
        self.bar_source_badge.pack(side="left", padx=(0, 12))

        ctk.CTkLabel(
            bar_right, text="🔊", font=ctk.CTkFont(size=12), text_color=THEME["text_muted"]
        ).pack(side="left", padx=(0, 4))

        self.volume_slider = ctk.CTkSlider(
            bar_right,
            from_=0,
            to=1,
            width=84,
            height=12,
            fg_color=THEME["bg_card"],
            progress_color=THEME["text_muted"],
            button_color=THEME["text_main"],
            button_hover_color=self.accent_color,
            command=self._on_volume_change,
        )
        self.volume_slider.set(self.engine.volume)
        self.volume_slider.pack(side="left")

        self._sync_nav_buttons()

    # ==========================================
    # НАВИГАЦИЯ И ИСТОЧНИКИ
    # ==========================================
    def _switch_page(self, page: str):
        self.current_page = page
        self._sync_nav_buttons()
        self._render_page()

    def _sync_nav_buttons(self):
        for pid, btn in self.nav_buttons.items():
            if pid == self.current_page:
                btn.configure(fg_color=THEME["bg_card"], text_color=THEME["text_main"])
            else:
                btn.configure(fg_color="transparent", text_color=THEME["text_muted"])

    def _set_search_source(self, src: str):
        self.search_source = src
        self._sync_source_buttons()
        self._save_user_state()
        if self.current_page == "wave":
            self._refresh_wave()

    def _sync_source_buttons(self):
        if self.search_source == "YT":
            self.src_yt_btn.configure(fg_color=THEME["bg_hover"], text_color=THEME["text_main"])
            self.src_sc_btn.configure(fg_color="transparent", text_color=THEME["text_muted"])
        else:
            self.src_sc_btn.configure(fg_color=THEME["bg_hover"], text_color=THEME["text_main"])
            self.src_yt_btn.configure(fg_color="transparent", text_color=THEME["text_muted"])

    def _render_page(self):
        for w in self.content_area.winfo_children():
            w.destroy()

        if self.current_page == "wave":
            self._render_wave_page()
        elif self.current_page == "search":
            self._render_search_page()
        elif self.current_page == "library":
            self._render_library_page()

    # ==========================================
    # СТРАНИЦА 1: «МОЯ ВОЛНА» (КАК В ЯНДЕКС МУЗЫКЕ)
    # ==========================================
    def _render_wave_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        # Hero-карточка Моей волны
        hero = ctk.CTkFrame(
            container,
            fg_color=THEME["bg_sidebar"],
            corner_radius=20,
            border_width=1,
            border_color=THEME["border"],
        )
        hero.grid(row=0, column=0, sticky="ew", pady=(0, 18))
        hero.grid_columnconfigure(0, weight=1)

        hero_top = ctk.CTkFrame(hero, fg_color="transparent")
        hero_top.grid(row=0, column=0, sticky="ew", padx=26, pady=(22, 14))
        hero_top.grid_columnconfigure(0, weight=1)

        title_col = ctk.CTkFrame(hero_top, fg_color="transparent")
        title_col.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_col,
            text="Моя волна",
            font=ctk.CTkFont(family="Segoe UI", size=28, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(anchor="w")

        sub_info = (
            f"На основе {len(self.liked_tracks)} любимых треков  •  {self.wave_mood} • {self.wave_genre}"
            if self.liked_tracks
            else f"Бесконечный поток под настроение  •  {self.wave_mood} • {self.wave_genre}"
        )
        ctk.CTkLabel(
            title_col,
            text=sub_info,
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=THEME["text_muted"],
        ).pack(anchor="w", pady=(2, 0))

        # Кнопки запуска волны
        wave_actions = ctk.CTkFrame(hero_top, fg_color="transparent")
        wave_actions.grid(row=0, column=1, sticky="e")

        is_wave_playing = self.is_wave_session and self.engine.is_playing and not self.engine.is_paused
        play_wave_text = "⏸  Пауза" if is_wave_playing else "▶  Слушать волну"

        self.wave_main_btn = ctk.CTkButton(
            wave_actions,
            text=play_wave_text,
            height=44,
            corner_radius=22,
            fg_color=self.accent_color,
            hover_color="#D9B87A",
            text_color="#0D0F12",
            font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
            padx=22,
            command=self._start_or_toggle_wave,
        )
        self.wave_main_btn.pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            wave_actions,
            text="↻",
            width=44,
            height=44,
            corner_radius=22,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=16),
            command=self._refresh_wave,
        ).pack(side="left")

        # Настройка волны (пилюли настроения и жанра)
        tuning_box = ctk.CTkFrame(hero, fg_color="transparent")
        tuning_box.grid(row=1, column=0, sticky="ew", padx=26, pady=(0, 20))

        mood_row = ctk.CTkFrame(tuning_box, fg_color="transparent")
        mood_row.pack(fill="x", pady=(0, 8))
        ctk.CTkLabel(
            mood_row, text="Настроение:", font=ctk.CTkFont(size=12), text_color=THEME["text_dim"], width=86, anchor="w"
        ).pack(side="left")

        for m in self.MOODS:
            active = m == self.wave_mood
            ctk.CTkButton(
                mood_row,
                text=m,
                height=28,
                corner_radius=14,
                fg_color=self.accent_soft if active else THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=self.accent_color if active else THEME["text_muted"],
                border_width=1 if active else 0,
                border_color=self.accent_color,
                font=ctk.CTkFont(size=12, weight="bold" if active else "normal"),
                padx=14,
                command=lambda val=m: self._set_wave_mood(val),
            ).pack(side="left", padx=4)

        genre_row = ctk.CTkFrame(tuning_box, fg_color="transparent")
        genre_row.pack(fill="x")
        ctk.CTkLabel(
            genre_row, text="Стиль:", font=ctk.CTkFont(size=12), text_color=THEME["text_dim"], width=86, anchor="w"
        ).pack(side="left")

        for g in self.GENRES:
            active = g == self.wave_genre
            ctk.CTkButton(
                genre_row,
                text=g,
                height=28,
                corner_radius=14,
                fg_color=self.accent_soft if active else THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=self.accent_color if active else THEME["text_muted"],
                border_width=1 if active else 0,
                border_color=self.accent_color,
                font=ctk.CTkFont(size=12, weight="bold" if active else "normal"),
                padx=14,
                command=lambda val=g: self._set_wave_genre(val),
            ).pack(side="left", padx=4)

        # Заголовок очереди волны
        q_header = ctk.CTkFrame(container, fg_color="transparent")
        q_header.grid(row=1, column=0, sticky="ew", pady=(0, 8))
        ctk.CTkLabel(
            q_header,
            text="Далее в потоке",
            font=ctk.CTkFont(family="Segoe UI", size=16, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        # Список треков волны
        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        if not self.wave_queue:
            msg = "Подбираем треки для вашей волны..." if self.wave_fetching else "Нажмите «Слушать волну», чтобы запустить поток"
            ctk.CTkLabel(scroll, text=msg, text_color=THEME["text_muted"], font=ctk.CTkFont(size=13)).grid(
                row=0, column=0, pady=40
            )
            return

        for idx, track in enumerate(self.wave_queue):
            self._build_track_row(scroll, idx, track, self.wave_queue, is_wave=True)

    # ==========================================
    # СТРАНИЦА 2: ПОИСК
    # ==========================================
    def _render_search_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        # Верхняя строка поиска (как в Spotify)
        top_search = ctk.CTkFrame(container, fg_color="transparent")
        top_search.grid(row=0, column=0, sticky="ew", pady=(0, 16))
        top_search.grid_columnconfigure(0, weight=1)

        self.search_var = tk.StringVar(value=getattr(self, "_last_search_text", ""))
        self.search_entry = ctk.CTkEntry(
            top_search,
            textvariable=self.search_var,
            placeholder_text="Что хотите послушать? Исполнитель, трек или настроение...",
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

        self.search_submit_btn = ctk.CTkButton(
            top_search,
            text="Найти",
            height=44,
            width=100,
            corner_radius=22,
            fg_color=self.accent_color,
            hover_color="#D9B87A",
            text_color="#0D0F12",
            font=ctk.CTkFont(size=13, weight="bold"),
            command=self._execute_search,
        )
        self.search_submit_btn.grid(row=0, column=1)

        # Быстрые жанровые карточки, если поиск еще пуст
        if not self.search_results:
            cards_frame = ctk.CTkFrame(container, fg_color="transparent")
            cards_frame.grid(row=1, column=0, sticky="ew", pady=(6, 14))
            cards_frame.grid_columnconfigure((0, 1, 2, 3), weight=1)

            presets = [
                ("Ночной драйв", "night drive synthwave mix", "#242A36"),
                ("Инди и альтернатива", "indie rock aesthetic playlist", "#2A2426"),
                ("Спокойный фокус", "deep ambient chill beats", "#1F2926"),
                ("Фонк и электроника", "phonk house mix", "#292520"),
            ]
            for i, (title, q, bg) in enumerate(presets):
                card = ctk.CTkButton(
                    cards_frame,
                    text=title,
                    height=74,
                    corner_radius=14,
                    fg_color=bg,
                    hover_color=THEME["bg_hover"],
                    text_color=THEME["text_main"],
                    font=ctk.CTkFont(family="Segoe UI", size=14, weight="bold"),
                    command=lambda query=q: self._quick_search(query),
                )
                card.grid(row=0, column=i, sticky="ew", padx=5)

        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        if getattr(self, "_is_searching", False):
            ctk.CTkLabel(
                scroll,
                text=f"Ищем в {self.search_source}...",
                text_color=THEME["text_muted"],
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=0, pady=40)
            return

        if not self.search_results:
            ctk.CTkLabel(
                scroll,
                text="Введите запрос сверху или выберите подборку",
                text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=13),
            ).grid(row=0, column=0, pady=40)
            return

        for idx, track in enumerate(self.search_results):
            self._build_track_row(scroll, idx, track, self.search_results, is_wave=False)

    # ==========================================
    # СТРАНИЦА 3: КОЛЛЕКЦИЯ (ЛЮБИМЫЕ И ЛОКАЛЬНЫЕ)
    # ==========================================
    def _render_library_page(self):
        container = ctk.CTkFrame(self.content_area, fg_color="transparent")
        container.grid(row=0, column=0, sticky="nsew", padx=28, pady=24)
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(2, weight=1)

        header = ctk.CTkFrame(container, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        ctk.CTkLabel(
            header,
            text="Моя коллекция",
            font=ctk.CTkFont(family="Segoe UI", size=24, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        # Подвкладки: Любимые / Локальные и скачанные
        tabs_row = ctk.CTkFrame(container, fg_color="transparent")
        tabs_row.grid(row=1, column=0, sticky="ew", pady=(0, 14))

        ctk.CTkButton(
            tabs_row,
            text=f"♥  Мне нравится ({len(self.liked_tracks)})",
            height=34,
            corner_radius=17,
            fg_color=THEME["bg_card"] if self.library_subtab == "liked" else "transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"] if self.library_subtab == "liked" else THEME["text_muted"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_library_subtab("liked"),
        ).pack(side="left", padx=(0, 8))

        ctk.CTkButton(
            tabs_row,
            text=f"📁  Файлы и скачанное ({len(self.local_tracks)})",
            height=34,
            corner_radius=17,
            fg_color=THEME["bg_card"] if self.library_subtab == "local" else "transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"] if self.library_subtab == "local" else THEME["text_muted"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_library_subtab("local"),
        ).pack(side="left", padx=(0, 8))

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

        scroll = ctk.CTkScrollableFrame(
            container,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        scroll.grid(row=2, column=0, sticky="nsew")
        scroll.grid_columnconfigure(0, weight=1)

        items = self.liked_tracks if self.library_subtab == "liked" else self.local_tracks
        if not items:
            hint = (
                "Ставьте ♥ понравившимся трекам — они появятся здесь и улучшат Мою волну"
                if self.library_subtab == "liked"
                else "Добавьте папку с музыкой или скачайте треки в MP3"
            )
            ctk.CTkLabel(scroll, text=hint, text_color=THEME["text_dim"], font=ctk.CTkFont(size=13)).grid(
                row=0, column=0, pady=50
            )
            return

        for idx, track in enumerate(items):
            self._build_track_row(scroll, idx, track, items, is_wave=False)

    def _switch_library_subtab(self, sub: str):
        self.library_subtab = sub
        self._render_page()

    # ==========================================
    # СТРОКА ТРЕКА В СПИСКЕ (SPOTIFY / ЯНДЕКС СТИЛЬ)
    # ==========================================
    def _build_track_row(
        self,
        parent: ctk.CTkScrollableFrame,
        idx: int,
        track: TrackItem,
        queue_ref: list[TrackItem],
        is_wave: bool = False,
    ):
        is_current = self.current_track is not None and self.current_track.uid == track.uid
        row_bg = THEME["bg_card"] if is_current else "transparent"
        title_color = self.accent_color if is_current else THEME["text_main"]

        row = ctk.CTkFrame(parent, fg_color=row_bg, corner_radius=10, height=56)
        row.grid(row=idx, column=0, sticky="ew", pady=2, padx=2)
        row.grid_columnconfigure(2, weight=1)
        row.grid_propagate(False)

        # Номер или индикатор воспроизведения
        num_txt = "▶" if is_current else str(idx + 1)
        ctk.CTkLabel(
            row,
            text=num_txt,
            width=28,
            font=ctk.CTkFont(size=12, weight="bold" if is_current else "normal"),
            text_color=self.accent_color if is_current else THEME["text_dim"],
        ).grid(row=0, column=0, padx=(10, 6))

        # Миниатюра обложки
        cover_lbl = ctk.CTkLabel(row, text="", image=self.row_default_cover)
        cover_lbl.grid(row=0, column=1, padx=(0, 12), pady=8)
        self._load_thumbnail_async(track, cover_lbl, size=(40, 40), radius=8)

        # Название и артист (кликабельная область)
        info_btn = ctk.CTkButton(
            row,
            text=f"{track.title}\n{track.artist}",
            anchor="w",
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=title_color,
            font=ctk.CTkFont(family="Segoe UI", size=12, weight="bold" if is_current else "normal"),
            command=lambda q=queue_ref, i=idx, w=is_wave: self._play_from_list(q, i, w),
        )
        info_btn.grid(row=0, column=2, sticky="nsew", pady=4)

        # Длительность
        dur_str = format_time(track.duration) if track.duration > 0 else "--:--"
        ctk.CTkLabel(
            row,
            text=dur_str,
            width=46,
            font=ctk.CTkFont(size=11),
            text_color=THEME["text_muted"],
        ).grid(row=0, column=3, padx=8)

        # Кнопка лайка в строке
        liked = self._is_liked(track)
        ctk.CTkButton(
            row,
            text="♥" if liked else "♡",
            width=32,
            height=32,
            corner_radius=16,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["like_red"] if liked else THEME["text_muted"],
            font=ctk.CTkFont(size=14),
            command=lambda t=track: self._toggle_like_track(t),
        ).grid(row=0, column=4, padx=(0, 10))

    # ==========================================
    # ЛОГИКА «МОЕЙ ВОЛНЫ»
    # ==========================================
    def _set_wave_mood(self, mood: str):
        self.wave_mood = mood
        self._refresh_wave()

    def _set_wave_genre(self, genre: str):
        self.wave_genre = genre
        self._refresh_wave()

    def _prefill_wave_if_empty(self):
        if not self.wave_queue and not self.wave_fetching:
            self._fetch_wave_batch(autoplay=False)

    def _refresh_wave(self):
        self.wave_queue.clear()
        if self.current_page == "wave":
            self._render_page()
        self._fetch_wave_batch(autoplay=self.is_wave_session)

    def _start_or_toggle_wave(self):
        if self.is_wave_session and self.engine.is_playing:
            self._on_play_pause()
            if self.current_page == "wave":
                self._render_page()
            return

        if self.wave_queue:
            self._play_from_list(self.wave_queue, 0, is_wave=True)
        else:
            self._fetch_wave_batch(autoplay=True)

    def _build_wave_search_query(self) -> str:
        mood_q = self.MOOD_QUERIES.get(self.wave_mood, "")
        genre_q = self.GENRE_QUERIES.get(self.wave_genre, "")

        # Если есть любимые треки — подмешиваем случайного артиста из коллекции
        if self.liked_tracks and random.random() < 0.65:
            seed = random.choice(self.liked_tracks)
            clean_artist = seed.artist.replace(" - Topic", "").strip()
            if self.wave_mood == "Любое" and self.wave_genre == "Микс":
                return f"{clean_artist} similar tracks mix"
            return f"{clean_artist} {mood_q}"

        return f"{mood_q} {genre_q} playlist".strip()

    def _fetch_wave_batch(self, autoplay: bool = False):
        if self.wave_fetching:
            return
        self.wave_fetching = True
        query = self._build_wave_search_query()
        prefix = "scsearch15" if self.search_source == "SC" else "ytsearch15"

        def worker():
            fetched: list[TrackItem] = []
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": "in_playlist",
                    "skip_download": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"{prefix}:{query}", download=False)
                    for entry in info.get("entries", []) or []:
                        dur = float(entry.get("duration") or 0)
                        # Отсекаем 2-часовые подкасты, оставляем нормальные треки (45 сек – 10 мин)
                        if dur > 0 and (dur < 45 or dur > 600):
                            continue
                        item = self._entry_to_track(entry, self.search_source)
                        if not item:
                            continue
                        if item.uid in self.disliked_uids or item.uid in self.session_played_uids:
                            continue
                        if any(existing.uid == item.uid for existing in self.wave_queue):
                            continue
                        fetched.append(item)
            except Exception:
                pass

            random.shuffle(fetched)

            def done():
                self.wave_fetching = False
                self.wave_queue.extend(fetched[:10])
                if self.current_page == "wave":
                    self._render_page()
                if autoplay and self.wave_queue:
                    self._play_from_list(self.wave_queue, 0, is_wave=True)

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ==========================================
    # ОНЛАЙН-ПОИСК И СКАЧИВАНИЕ MP3
    # ==========================================
    def _quick_search(self, query: str):
        self._last_search_text = query
        self.search_var.set(query)
        self._execute_search()

    def _execute_search(self):
        query = self.search_var.get().strip()
        if not query:
            return
        self._last_search_text = query
        self._is_searching = True
        self._render_page()

        prefix = "scsearch18" if self.search_source == "SC" else "ytsearch18"

        def worker():
            results: list[TrackItem] = []
            try:
                ydl_opts = {
                    "quiet": True,
                    "no_warnings": True,
                    "extract_flat": "in_playlist",
                    "skip_download": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(f"{prefix}:{query}", download=False)
                    for entry in info.get("entries", []) or []:
                        item = self._entry_to_track(entry, self.search_source)
                        if item:
                            results.append(item)
            except Exception:
                pass

            def done():
                self._is_searching = False
                self.search_results = results
                if self.current_page == "search":
                    self._render_page()

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _entry_to_track(self, entry: dict, src: str) -> TrackItem | None:
        vid = entry.get("id")
        url = entry.get("url") or entry.get("webpage_url")
        if not url and vid and src == "YT":
            url = f"https://www.youtube.com/watch?v={vid}"
        if not url:
            return None
        if src == "YT" and not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        thumbs = entry.get("thumbnails") or []
        thumb_url = thumbs[-1]["url"] if thumbs else (
            f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg" if (vid and src == "YT") else None
        )
        return TrackItem(
            title=entry.get("title") or "Без названия",
            artist=entry.get("uploader") or entry.get("channel") or src,
            duration=float(entry.get("duration") or 0),
            webpage_url=url,
            thumbnail_url=thumb_url,
            source_type=src,
        )

    def _on_download_current(self):
        track = self.current_track
        if not track or not track.is_online or not track.webpage_url:
            return
        self.bar_download_btn.configure(state="disabled", text="...")

        def worker():
            saved_path = None
            try:
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
                    "ffmpeg_location": self.engine.ffmpeg_exe,
                    "postprocessors": [
                        {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
                    ],
                    "quiet": True,
                    "no_warnings": True,
                }
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(track.webpage_url, download=True)
                    base = os.path.splitext(ydl.prepare_filename(info))[0] + ".mp3"
                    if os.path.exists(base):
                        saved_path = base
            except Exception:
                pass

            def done():
                if saved_path:
                    self.bar_download_btn.configure(state="normal", text="✓ MP3")
                    item = TrackItem.from_local_file(saved_path)
                    if not any(t.filepath == saved_path for t in self.local_tracks):
                        self.local_tracks.insert(0, item)
                else:
                    self.bar_download_btn.configure(state="normal", text="Ошибка")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ==========================================
    # ВОСПРОИЗВЕДЕНИЕ, ЛАЙКИ И ПЕРЕКЛЮЧЕНИЕ
    # ==========================================
    def _play_from_list(self, queue_ref: list[TrackItem], index: int, is_wave: bool = False):
        self.active_queue = queue_ref
        self.is_wave_session = is_wave
        self._play_index(index)

    def _play_index(self, index: int):
        if not (0 <= index < len(self.active_queue)):
            return

        self.play_gen += 1
        gen = self.play_gen
        self.current_index = index
        track = self.active_queue[index]
        self.current_track = track
        self.session_played_uids.add(track.uid)

        # Если включена «Моя волна» и осталось мало треков — подгружаем следующую пачку
        if self.is_wave_session and (len(self.wave_queue) - index) <= 3:
            self._fetch_wave_batch(autoplay=False)

        self._update_bottom_bar(track, loading=track.is_online)
        self._render_page()

        if not track.is_online and track.filepath:
            self.engine.play_source(track.filepath, start_sec=0.0)
            self._sync_play_button()
            return

        self.engine.stop()
        self._sync_play_button()

        def resolve_worker():
            stream_url = track.stream_url
            duration = track.duration
            cover_pil = track.cover_pil

            try:
                if not stream_url and track.webpage_url:
                    ydl_opts = {
                        "format": "bestaudio/best",
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": True,
                    }
                    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                        info = ydl.extract_info(track.webpage_url, download=False)
                        stream_url = info.get("url")
                        if info.get("duration"):
                            duration = float(info["duration"])
                        if not track.thumbnail_url and info.get("thumbnail"):
                            track.thumbnail_url = info["thumbnail"]

                if cover_pil is None and track.thumbnail_url:
                    resp = requests.get(track.thumbnail_url, timeout=6)
                    if resp.status_code == 200:
                        cover_pil = Image.open(io.BytesIO(resp.content))
            except Exception:
                pass

            def on_ready():
                if gen != self.play_gen:
                    return
                track.stream_url = stream_url
                track.duration = duration
                if cover_pil:
                    track.cover_pil = cover_pil

                if stream_url:
                    self._update_bottom_bar(track, loading=False)
                    self.engine.play_source(stream_url, start_sec=0.0)
                    self._sync_play_button()
                    if self.current_page == "wave":
                        self._render_page()
                else:
                    self.bar_source_badge.configure(text="ERR")
                    # Если трек в Волне недоступен — автоматически переходим к следующему
                    if self.is_wave_session:
                        self.after(600, self._on_next)

            self.after(0, on_ready)

        threading.Thread(target=resolve_worker, daemon=True).start()

    def _update_bottom_bar(self, track: TrackItem, loading: bool = False):
        short_title = track.title if len(track.title) <= 30 else track.title[:28] + "…"
        short_artist = track.artist if len(track.artist) <= 32 else track.artist[:30] + "…"
        self.bar_title.configure(text=short_title)
        self.bar_artist.configure(text=short_artist)
        self.total_label.configure(text=format_time(track.duration))
        self.progress_slider.configure(to=max(1.0, track.duration))
        self.progress_slider.set(0)

        liked = self._is_liked(track)
        self.like_btn.configure(
            text="♥" if liked else "♡",
            text_color=THEME["like_red"] if liked else THEME["text_muted"],
        )

        if track.is_online:
            self.bar_source_badge.configure(text="..." if loading else track.source_type)
            self.bar_download_btn.configure(state="normal", text="↓ MP3")
            self.bar_download_btn.pack(side="left", padx=(0, 8))
        else:
            self.bar_source_badge.configure(text="LOCAL")
            self.bar_download_btn.pack_forget()

        if track.cover_pil:
            rounded = make_rounded_cover(track.cover_pil, size=(56, 56), radius=10)
            ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=(56, 56))
            self.thumb_cache[f"bar:{track.uid}"] = ctk_img
            self.bar_cover_label.configure(image=ctk_img)

            accent, soft = extract_muted_accent(track.cover_pil)
            self._apply_accent(accent, soft)
        else:
            self.bar_cover_label.configure(image=self.bar_default_cover)
            self._apply_accent(THEME["wave_gold"], THEME["wave_soft"])

    def _apply_accent(self, accent: str, soft: str):
        self.accent_color = accent
        self.accent_soft = soft
        self.progress_slider.configure(progress_color=accent, button_hover_color=accent)
        self.bar_source_badge.configure(fg_color=soft, text_color=accent)
        if self.repeat_mode:
            self.repeat_btn.configure(text_color=accent)
        if self.shuffle_mode:
            self.shuffle_btn.configure(text_color=accent)

    def _is_liked(self, track: TrackItem) -> bool:
        return any(t.uid == track.uid for t in self.liked_tracks)

    def _toggle_like_current(self):
        if self.current_track:
            self._toggle_like_track(self.current_track)

    def _toggle_like_track(self, track: TrackItem):
        if self._is_liked(track):
            self.liked_tracks = [t for t in self.liked_tracks if t.uid != track.uid]
        else:
            self.liked_tracks.insert(0, track)
            self.disliked_uids.discard(track.uid)
        self._save_user_state()

        if self.current_track and self.current_track.uid == track.uid:
            liked = self._is_liked(track)
            self.like_btn.configure(
                text="♥" if liked else "♡",
                text_color=THEME["like_red"] if liked else THEME["text_muted"],
            )
        self._render_page()

    def _dislike_and_skip_current(self):
        if not self.current_track:
            return
        uid = self.current_track.uid
        self.disliked_uids.add(uid)
        self.liked_tracks = [t for t in self.liked_tracks if t.uid != uid]
        self._save_user_state()
        self._on_next()

    def _load_thumbnail_async(self, track: TrackItem, label: ctk.CTkLabel, size=(40, 40), radius=8):
        cache_key = f"{size[0]}:{track.uid}"
        if cache_key in self.thumb_cache:
            label.configure(image=self.thumb_cache[cache_key])
            return

        if track.cover_pil:
            rounded = make_rounded_cover(track.cover_pil, size=size, radius=radius)
            ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=size)
            self.thumb_cache[cache_key] = ctk_img
            label.configure(image=ctk_img)
            return

        if not track.thumbnail_url:
            return

        def worker():
            try:
                resp = requests.get(track.thumbnail_url, timeout=5)
                if resp.status_code == 200:
                    pil_img = Image.open(io.BytesIO(resp.content))
                    track.cover_pil = pil_img
                    rounded = make_rounded_cover(pil_img, size=size, radius=radius)

                    def apply():
                        ctk_img = ctk.CTkImage(light_image=rounded, dark_image=rounded, size=size)
                        self.thumb_cache[cache_key] = ctk_img
                        try:
                            if label.winfo_exists():
                                label.configure(image=ctk_img)
                        except tk.TclError:
                            pass

                    self.after(0, apply)
            except Exception:
                pass

        threading.Thread(target=worker, daemon=True).start()

    # ==========================================
    # УПРАВЛЕНИЕ ПЛЕЕРОМ И ЛОКАЛЬНЫМИ ФАЙЛАМИ
    # ==========================================
    def _sync_play_button(self):
        if self.engine.is_playing and not self.engine.is_paused:
            self.play_btn.configure(text="⏸")
        else:
            self.play_btn.configure(text="▶")

    def _on_play_pause(self):
        if not self.engine.is_playing:
            if self.active_queue and self.current_index >= 0:
                self._play_index(self.current_index)
            else:
                self._start_or_toggle_wave()
            return
        self.engine.toggle_pause()
        self._sync_play_button()
        if self.current_page == "wave":
            self._render_page()

    def _on_next(self):
        if not self.active_queue:
            return
        if self.shuffle_mode and not self.is_wave_session and len(self.active_queue) > 1:
            choices = [i for i in range(len(self.active_queue)) if i != self.current_index]
            next_idx = random.choice(choices)
        else:
            next_idx = (self.current_index + 1) % len(self.active_queue)
        self._play_index(next_idx)

    def _on_prev(self):
        if not self.active_queue:
            return
        if self.engine.get_position() > 4.0:
            self.engine.seek(0.0)
        else:
            prev_idx = (self.current_index - 1) % len(self.active_queue)
            self._play_index(prev_idx)

    def _on_track_finished(self):
        if self.repeat_mode and self.current_index >= 0:
            self._play_index(self.current_index)
        else:
            self._on_next()

    def _toggle_repeat(self):
        self.repeat_mode = not self.repeat_mode
        self.repeat_btn.configure(
            text_color=self.accent_color if self.repeat_mode else THEME["text_muted"]
        )

    def _toggle_shuffle(self):
        self.shuffle_mode = not self.shuffle_mode
        self.shuffle_btn.configure(
            text_color=self.accent_color if self.shuffle_mode else THEME["text_muted"]
        )

    def _on_volume_change(self, val):
        self.engine.set_volume(float(val))

    def _on_seek_start(self, _e):
        self.is_seeking = True

    def _on_slider_drag(self, val):
        if self.is_seeking:
            self.elapsed_label.configure(text=format_time(float(val)))

    def _on_seek_end(self, _e):
        if self.is_seeking:
            self.engine.seek(float(self.progress_slider.get()))
            self.is_seeking = False

    def _on_add_local_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку с музыкой")
        if not folder:
            return
        for root, _, files in os.walk(folder):
            for f in sorted(files):
                if f.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                    fp = os.path.join(root, f)
                    if not any(t.filepath == fp for t in self.local_tracks):
                        self.local_tracks.append(TrackItem.from_local_file(fp))
        self._render_page()

    def _on_add_local_files(self):
        files = filedialog.askopenfilenames(
            title="Выберите аудиофайлы",
            filetypes=[("Audio Files", "*.mp3 *.flac *.wav *.ogg *.m4a")],
        )
        for fp in files:
            if not any(t.filepath == fp for t in self.local_tracks):
                self.local_tracks.append(TrackItem.from_local_file(fp))
        self._render_page()

    def _bind_shortcuts(self):
        self.bind("<space>", self._on_space_key)
        self.bind("<Left>", lambda _e: self.engine.seek(max(0.0, self.engine.get_position() - 5)))
        self.bind("<Right>", lambda _e: self.engine.seek(self.engine.get_position() + 5))

    def _on_space_key(self, _e):
        if isinstance(self.focus_get(), tk.Entry):
            return
        self._on_play_pause()

    def _tick_loop(self):
        if self.engine.is_playing and not self.is_seeking:
            pos = self.engine.get_position()
            if self.current_track and self.current_track.duration > 0:
                pos = min(pos, self.current_track.duration)
            self.progress_slider.set(pos)
            self.elapsed_label.configure(text=format_time(pos))

        self.after(200, self._tick_loop)


if __name__ == "__main__":
    app = AetherStreamingApp()
    app.mainloop()
