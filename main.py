import colorsys
import io
import math
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
from PIL import Image, ImageDraw, ImageFilter

# Спокойная обсидиановая палитра без неона
THEME = {
    "bg_root": "#0E1013",        # Основной глубокий матовый фон
    "bg_panel": "#15181E",       # Боковая панель и карточка плеера
    "bg_card": "#1C2028",        # Элементы списков и кнопки
    "bg_hover": "#242934",       # Состояние наведения
    "border": "#252A34",         # Тонкие разделители
    "text_main": "#F0F2F6",      # Мягкий белый
    "text_muted": "#8A92A0",     # Вторичный серо-стальной
    "text_dim": "#58606E",       # Приглушенный текст
    "accent": "#7D98B3",         # Базовый пыльно-синий акцент
    "accent_soft": "#242D38",    # Мягкая подложка акцента
}

ctk.set_appearance_mode("dark")

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Music", "Aether")


# ==========================================
# УТИЛИТЫ ДЛЯ ДИЗАЙНА И ОБЛОЖЕК
# ==========================================
def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds or 0))
    mins, secs = divmod(seconds, 60)
    hours, mins = divmod(mins, 60)
    if hours > 0:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins:02d}:{secs:02d}"


def make_rounded_cover(img: Image.Image, size=(240, 240), radius=18) -> Image.Image:
    """Центрирует обложку (убирает полосы 16:9) и делает плавные скругленные углы."""
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


def create_placeholder_cover(size=(240, 240), accent_hex="#7D98B3") -> Image.Image:
    img = Image.new("RGBA", size, THEME["bg_card"])
    draw = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    draw.ellipse((cx - 64, cy - 64, cx + 64, cy + 64), outline="#2C323E", width=2)
    draw.ellipse((cx - 42, cy - 42, cx + 42, cy + 42), outline=accent_hex, width=2)
    draw.ellipse((cx - 12, cy - 12, cx + 12, cy + 12), fill=THEME["bg_panel"], outline="#8A92A0", width=2)
    return make_rounded_cover(img, size=size, radius=18)


def extract_muted_accent(img: Image.Image) -> tuple[str, str]:
    """Извлекает цвет из обложки и приглушает его, чтобы не было неона."""
    try:
        small = img.convert("RGB").resize((32, 32))
        pixels = list(small.getdata())
        r_sum = g_sum = b_sum = count = 0
        for r, g, b in pixels:
            if 25 < (r + g + b) // 3 < 230:
                r_sum += r
                g_sum += g
                b_sum += b
                count += 1
        if count == 0:
            return THEME["accent"], THEME["accent_soft"]

        r, g, b = (r_sum / count) / 255.0, (g_sum / count) / 255.0, (b_sum / count) / 255.0
        h, l, s = colorsys.rgb_to_hls(r, g, b)

        # Жестко ограничиваем насыщенность (никакого кислотного неона)
        s_muted = min(max(s, 0.18), 0.36)
        l_main = min(max(l, 0.54), 0.65)
        r1, g1, b1 = colorsys.hls_to_rgb(h, l_main, s_muted)
        accent_hex = f"#{int(r1*255):02x}{int(g1*255):02x}{int(b1*255):02x}"

        # Мягкий темный оттенок для подложек
        r2, g2, b2 = colorsys.hls_to_rgb(h, 0.18, min(s_muted, 0.25))
        soft_hex = f"#{int(r2*255):02x}{int(g2*255):02x}{int(b2*255):02x}"
        return accent_hex, soft_hex
    except Exception:
        return THEME["accent"], THEME["accent_soft"]


# ==========================================
# МОДЕЛЬ ТРЕКА И АУДИОДВИЖОК (FFMPEG + SD)
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
        is_online: bool = False,
    ):
        self.title = title
        self.artist = artist
        self.duration = duration
        self.filepath = filepath
        self.webpage_url = webpage_url
        self.thumbnail_url = thumbnail_url
        self.is_online = is_online
        self.stream_url: str | None = None
        self.cover_pil: Image.Image | None = None

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
            is_online=False,
        )
        item.cover_pil = cover_pil
        return item


class StreamAudioEngine:
    """Потоковый движок на базе FFmpeg + SoundDevice (играет и файлы, и URL)."""

    SUPPORTED_EXTS = (".mp3", ".wav", ".ogg", ".flac", ".m4a")

    def __init__(self, on_track_end_callback=None):
        self.ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        self.on_track_end = on_track_end_callback

        self.volume: float = 0.75
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.current_level: float = 0.0  # Текущая громкость для визуализатора (0..1)

        self._proc: subprocess.Popen | None = None
        self._thread: threading.Thread | None = None
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

        self._thread = threading.Thread(
            target=self._playback_worker,
            args=(sid, source, self._base_offset),
            daemon=True,
        )
        self._thread.start()

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
        chunk_bytes = 4096  # 1024 stereo int16 frames

        try:
            with sd.RawOutputStream(
                samplerate=self.sample_rate,
                channels=2,
                dtype="int16",
                blocksize=1024,
            ) as stream:
                while not self._stop_event.is_set() and sid == self._session_id:
                    if self.is_paused:
                        self.current_level *= 0.8
                        time.sleep(0.04)
                        continue

                    raw = proc.stdout.read(chunk_bytes)
                    if not raw:
                        if not self._stop_event.is_set() and sid == self._session_id:
                            finished_naturally = True
                        break

                    # Регулировка громкости и расчет амплитуды для визуализатора
                    samples = np.frombuffer(raw, dtype=np.int16)
                    if len(samples) > 0:
                        rms = float(np.sqrt(np.mean(samples.astype(np.float32) ** 2))) / 16000.0
                        self.current_level = min(1.0, rms * 1.8)

                        if self.volume < 0.99:
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
            self.current_level = 0.0

        if finished_naturally and sid == self._session_id:
            self.is_playing = False
            if self.on_track_end:
                self.on_track_end()

    def stop(self):
        self._stop_event.set()
        self.is_playing = False
        self.is_paused = False
        self.current_level = 0.0
        if self._proc:
            try:
                self._proc.terminate()
            except Exception:
                pass
            self._proc = None

    def toggle_pause(self):
        if not self.is_playing:
            return
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
# ГЛАВНОЕ ОКНО ПРИЛОЖЕНИЯ
# ==========================================
class AetherPlayerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Aether — Minimal Audio")
        self.geometry("1020x660")
        self.minsize(880, 580)
        self.configure(fg_color=THEME["bg_root"])

        self.engine = StreamAudioEngine(
            on_track_end_callback=lambda: self.after(0, self._on_track_finished)
        )

        self.active_tab = "online"  # "online" | "local"
        self.online_results: list[TrackItem] = []
        self.local_tracks: list[TrackItem] = []
        self.active_queue: list[TrackItem] = []
        self.current_index: int = -1
        self.current_track: TrackItem | None = None

        self.play_gen: int = 0
        self.is_seeking: bool = False
        self.repeat_mode: bool = False
        self.shuffle_mode: bool = False

        self.accent_color = THEME["accent"]
        self.accent_soft = THEME["accent_soft"]
        self.default_cover = ctk.CTkImage(
            light_image=create_placeholder_cover(),
            dark_image=create_placeholder_cover(),
            size=(236, 236),
        )
        self.current_cover_ctk = None

        # Состояние столбцов визуализатора
        self.num_bars = 24
        self.bar_heights = [0.08] * self.num_bars
        self.bar_phases = [i * 0.45 for i in range(self.num_bars)]

        self._build_ui()
        self._bind_keys()
        self._load_initial_downloads()
        self._ui_tick()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=330)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # =================================================
        # ЛЕВАЯ КОЛОНКА: ПОИСК ОНЛАЙН И МЕДИАТЕКА
        # =================================================
        self.sidebar = ctk.CTkFrame(
            self,
            fg_color=THEME["bg_panel"],
            corner_radius=0,
            border_width=0,
            width=330,
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(3, weight=1)
        self.sidebar.grid_columnconfigure(0, weight=1)

        # Заголовок бренда
        brand_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand_frame.grid(row=0, column=0, sticky="ew", padx=22, pady=(22, 14))

        ctk.CTkLabel(
            brand_frame,
            text="AETHER",
            font=ctk.CTkFont(family="Segoe UI", size=17, weight="bold"),
            text_color=THEME["text_main"],
        ).pack(side="left")

        ctk.CTkLabel(
            brand_frame,
            text="  •  STREAM & LOCAL",
            font=ctk.CTkFont(family="Segoe UI", size=10, weight="bold"),
            text_color=THEME["text_dim"],
        ).pack(side="left", pady=(2, 0))

        # Переключатель вкладок (Онлайн / Медиатека)
        tab_container = ctk.CTkFrame(
            self.sidebar, fg_color=THEME["bg_root"], corner_radius=10, height=38
        )
        tab_container.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 14))
        tab_container.grid_columnconfigure((0, 1), weight=1)

        self.tab_online_btn = ctk.CTkButton(
            tab_container,
            text="Поиск онлайн",
            height=30,
            corner_radius=8,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=12, weight="bold"),
            command=lambda: self._switch_tab("online"),
        )
        self.tab_online_btn.grid(row=0, column=0, sticky="ew", padx=4, pady=4)

        self.tab_local_btn = ctk.CTkButton(
            tab_container,
            text="Медиатека",
            height=30,
            corner_radius=8,
            fg_color="transparent",
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=12),
            command=lambda: self._switch_tab("local"),
        )
        self.tab_local_btn.grid(row=0, column=1, sticky="ew", padx=4, pady=4)

        # Панель инструментов для активной вкладки
        self.tools_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        self.tools_frame.grid(row=2, column=0, sticky="ew", padx=20, pady=(0, 10))
        self.tools_frame.grid_columnconfigure(0, weight=1)

        # Список треков
        self.list_scroll = ctk.CTkScrollableFrame(
            self.sidebar,
            fg_color="transparent",
            scrollbar_button_color=THEME["bg_card"],
            scrollbar_button_hover_color=THEME["text_dim"],
        )
        self.list_scroll.grid(row=3, column=0, sticky="nsew", padx=12, pady=(0, 14))
        self.list_scroll.grid_columnconfigure(0, weight=1)

        self._render_sidebar_tools()
        self._render_track_list()

        # =================================================
        # ПРАВАЯ КОЛОНКА: СЦЕНА ПЛЕЕРА
        # =================================================
        self.stage = ctk.CTkFrame(self, fg_color=THEME["bg_root"], corner_radius=0)
        self.stage.grid(row=0, column=1, sticky="nsew", padx=32, pady=24)
        self.stage.grid_columnconfigure(0, weight=1)
        self.stage.grid_rowconfigure(0, weight=1)

        # Карточка "Сейчас играет"
        self.player_card = ctk.CTkFrame(
            self.stage,
            fg_color=THEME["bg_panel"],
            corner_radius=22,
            border_width=1,
            border_color=THEME["border"],
        )
        self.player_card.grid(row=0, column=0, sticky="nsew")
        self.player_card.grid_columnconfigure(0, weight=1)
        self.player_card.grid_rowconfigure(1, weight=1)

        # Верхняя строка карточки: бейдж источника и кнопка скачать
        card_top = ctk.CTkFrame(self.player_card, fg_color="transparent")
        card_top.grid(row=0, column=0, sticky="ew", padx=28, pady=(22, 0))

        self.source_badge = ctk.CTkLabel(
            card_top,
            text="READY",
            font=ctk.CTkFont(size=10, weight="bold"),
            text_color=THEME["text_muted"],
            fg_color=THEME["bg_card"],
            corner_radius=6,
            padx=10,
            pady=3,
        )
        self.source_badge.pack(side="left")

        self.download_btn = ctk.CTkButton(
            card_top,
            text="↓ Скачать MP3",
            height=28,
            corner_radius=8,
            fg_color=THEME["bg_card"],
            hover_color=THEME["bg_hover"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=11),
            command=self._on_download_current,
        )
        # Показываем кнопку скачивания только для онлайн-треков

        # Центр карточки: обложка, название, артист, визуализатор
        center_box = ctk.CTkFrame(self.player_card, fg_color="transparent")
        center_box.grid(row=1, column=0, sticky="nsew", padx=28)
        center_box.grid_columnconfigure(0, weight=1)

        self.cover_frame = ctk.CTkFrame(
            center_box,
            fg_color=THEME["bg_card"],
            corner_radius=20,
            border_width=1,
            border_color=THEME["border"],
        )
        self.cover_frame.pack(pady=(14, 18))

        self.cover_label = ctk.CTkLabel(
            self.cover_frame, text="", image=self.default_cover
        )
        self.cover_label.pack(padx=4, pady=4)

        self.title_label = ctk.CTkLabel(
            center_box,
            text="Выберите трек или найдите в поиске",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=THEME["text_main"],
            wraplength=500,
        )
        self.title_label.pack(pady=(0, 4))

        self.artist_label = ctk.CTkLabel(
            center_box,
            text="Онлайн-стриминг и локальная музыка в одном окне",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=THEME["text_muted"],
        )
        self.artist_label.pack(pady=(0, 14))

        # Минималистичный аудио-визуализатор
        self.viz_canvas = tk.Canvas(
            center_box,
            width=260,
            height=28,
            bg=THEME["bg_panel"],
            highlightthickness=0,
        )
        self.viz_canvas.pack(pady=(0, 6))

        # Нижняя панель управления внутри карточки
        controls_box = ctk.CTkFrame(self.player_card, fg_color="transparent")
        controls_box.grid(row=2, column=0, sticky="ew", padx=32, pady=(4, 26))
        controls_box.grid_columnconfigure(0, weight=1)

        time_row = ctk.CTkFrame(controls_box, fg_color="transparent")
        time_row.grid(row=0, column=0, sticky="ew", pady=(0, 2))

        self.elapsed_label = ctk.CTkLabel(
            time_row, text="00:00", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"]
        )
        self.elapsed_label.pack(side="left")

        self.total_label = ctk.CTkLabel(
            time_row, text="00:00", font=ctk.CTkFont(size=11), text_color=THEME["text_muted"]
        )
        self.total_label.pack(side="right")

        self.progress_slider = ctk.CTkSlider(
            controls_box,
            from_=0,
            to=100,
            height=14,
            fg_color=THEME["bg_card"],
            progress_color=self.accent_color,
            button_color=THEME["text_main"],
            button_hover_color=self.accent_color,
            button_length=12,
            command=self._on_slider_drag,
        )
        self.progress_slider.set(0)
        self.progress_slider.grid(row=1, column=0, sticky="ew", pady=(0, 18))
        self.progress_slider.bind("<ButtonPress-1>", self._on_seek_start)
        self.progress_slider.bind("<ButtonRelease-1>", self._on_seek_end)

        # Ряд кнопок управления
        transport = ctk.CTkFrame(controls_box, fg_color="transparent")
        transport.grid(row=2, column=0, sticky="ew")
        transport.grid_columnconfigure(1, weight=1)

        left_modes = ctk.CTkFrame(transport, fg_color="transparent")
        left_modes.grid(row=0, column=0, sticky="w")

        self.shuffle_btn = ctk.CTkButton(
            left_modes,
            text="⇄",
            width=34,
            height=34,
            corner_radius=17,
            fg_color="transparent",
            hover_color=THEME["bg_card"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=15),
            command=self._toggle_shuffle,
        )
        self.shuffle_btn.pack(side="left", padx=(0, 4))

        self.repeat_btn = ctk.CTkButton(
            left_modes,
            text="↻",
            width=34,
            height=34,
            corner_radius=17,
            fg_color="transparent",
            hover_color=THEME["bg_card"],
            text_color=THEME["text_muted"],
            font=ctk.CTkFont(size=15),
            command=self._toggle_repeat,
        )
        self.repeat_btn.pack(side="left")

        center_transport = ctk.CTkFrame(transport, fg_color="transparent")
        center_transport.grid(row=0, column=1)

        self.prev_btn = ctk.CTkButton(
            center_transport,
            text="⏮",
            width=42,
            height=42,
            corner_radius=21,
            fg_color="transparent",
            hover_color=THEME["bg_card"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=16),
            command=self._on_prev,
        )
        self.prev_btn.pack(side="left", padx=8)

        self.play_btn = ctk.CTkButton(
            center_transport,
            text="▶",
            width=54,
            height=54,
            corner_radius=27,
            fg_color=self.accent_color,
            hover_color="#94AEC8",
            text_color="#0E1013",
            font=ctk.CTkFont(size=18, weight="bold"),
            command=self._on_play_pause,
        )
        self.play_btn.pack(side="left", padx=10)

        self.next_btn = ctk.CTkButton(
            center_transport,
            text="⏭",
            width=42,
            height=42,
            corner_radius=21,
            fg_color="transparent",
            hover_color=THEME["bg_card"],
            text_color=THEME["text_main"],
            font=ctk.CTkFont(size=16),
            command=self._on_next,
        )
        self.next_btn.pack(side="left", padx=8)

        vol_box = ctk.CTkFrame(transport, fg_color="transparent")
        vol_box.grid(row=0, column=2, sticky="e")

        ctk.CTkLabel(
            vol_box, text="🔊", font=ctk.CTkFont(size=12), text_color=THEME["text_muted"]
        ).pack(side="left", padx=(0, 6))

        self.volume_slider = ctk.CTkSlider(
            vol_box,
            from_=0,
            to=1,
            width=86,
            height=12,
            fg_color=THEME["bg_card"],
            progress_color=THEME["text_muted"],
            button_color=THEME["text_main"],
            button_hover_color=self.accent_color,
            command=self._on_volume_change,
        )
        self.volume_slider.set(self.engine.volume)
        self.volume_slider.pack(side="left")

    # ==========================================
    # ВКЛАДКИ И СПИСКИ В САЙДБАРЕ
    # ==========================================
    def _switch_tab(self, tab_name: str):
        self.active_tab = tab_name
        if tab_name == "online":
            self.tab_online_btn.configure(
                fg_color=THEME["bg_card"], text_color=THEME["text_main"], font=ctk.CTkFont(size=12, weight="bold")
            )
            self.tab_local_btn.configure(
                fg_color="transparent", text_color=THEME["text_muted"], font=ctk.CTkFont(size=12)
            )
        else:
            self.tab_local_btn.configure(
                fg_color=THEME["bg_card"], text_color=THEME["text_main"], font=ctk.CTkFont(size=12, weight="bold")
            )
            self.tab_online_btn.configure(
                fg_color="transparent", text_color=THEME["text_muted"], font=ctk.CTkFont(size=12)
            )
        self._render_sidebar_tools()
        self._render_track_list()

    def _render_sidebar_tools(self):
        for w in self.tools_frame.winfo_children():
            w.destroy()

        if self.active_tab == "online":
            search_row = ctk.CTkFrame(self.tools_frame, fg_color="transparent")
            search_row.grid(row=0, column=0, sticky="ew")
            search_row.grid_columnconfigure(0, weight=1)

            self.online_query_var = tk.StringVar()
            self.online_entry = ctk.CTkEntry(
                search_row,
                textvariable=self.online_query_var,
                placeholder_text="Название трека или артист...",
                height=36,
                corner_radius=10,
                fg_color=THEME["bg_root"],
                border_color=THEME["border"],
                border_width=1,
                text_color=THEME["text_main"],
                placeholder_text_color=THEME["text_dim"],
                font=ctk.CTkFont(size=12),
            )
            self.online_entry.grid(row=0, column=0, sticky="ew", padx=(0, 6))
            self.online_entry.bind("<Return>", lambda _e: self._start_online_search())

            self.search_go_btn = ctk.CTkButton(
                search_row,
                text="Найти",
                width=64,
                height=36,
                corner_radius=10,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12, weight="bold"),
                command=self._start_online_search,
            )
            self.search_go_btn.grid(row=0, column=1)

            # Быстрые теги настроения
            chips_row = ctk.CTkFrame(self.tools_frame, fg_color="transparent")
            chips_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
            for tag in ["Lo-Fi", "Synthwave", "Ambient", "Phonk"]:
                ctk.CTkButton(
                    chips_row,
                    text=tag,
                    height=24,
                    width=58,
                    corner_radius=12,
                    fg_color=THEME["bg_card"],
                    hover_color=THEME["bg_hover"],
                    text_color=THEME["text_muted"],
                    font=ctk.CTkFont(size=11),
                    command=lambda t=tag: self._quick_search(t),
                ).pack(side="left", padx=(0, 5))
        else:
            btns = ctk.CTkFrame(self.tools_frame, fg_color="transparent")
            btns.grid(row=0, column=0, sticky="ew")
            btns.grid_columnconfigure((0, 1), weight=1)

            ctk.CTkButton(
                btns,
                text="+ Папка",
                height=34,
                corner_radius=9,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=self._on_add_folder,
            ).grid(row=0, column=0, sticky="ew", padx=(0, 4))

            ctk.CTkButton(
                btns,
                text="+ Файлы",
                height=34,
                corner_radius=9,
                fg_color=THEME["bg_card"],
                hover_color=THEME["bg_hover"],
                text_color=THEME["text_main"],
                font=ctk.CTkFont(size=12),
                command=self._on_add_files,
            ).grid(row=0, column=1, sticky="ew", padx=(4, 0))

    def _render_track_list(self, status_message: str | None = None):
        for w in self.list_scroll.winfo_children():
            w.destroy()

        if status_message:
            ctk.CTkLabel(
                self.list_scroll,
                text=status_message,
                font=ctk.CTkFont(size=12),
                text_color=THEME["text_muted"],
            ).grid(row=0, column=0, pady=40)
            return

        items = self.online_results if self.active_tab == "online" else self.local_tracks
        if not items:
            hint = (
                "Введите запрос сверху,\nчтобы слушать музыку онлайн"
                if self.active_tab == "online"
                else "Добавьте папку с музыкой\nили скачайте треки из поиска"
            )
            ctk.CTkLabel(
                self.list_scroll,
                text=hint,
                font=ctk.CTkFont(size=12),
                text_color=THEME["text_dim"],
                justify="center",
            ).grid(row=0, column=0, pady=45)
            return

        for idx, track in enumerate(items):
            is_active = self.current_track is track
            card_bg = THEME["bg_card"] if is_active else "transparent"
            title_col = self.accent_color if is_active else THEME["text_main"]

            row = ctk.CTkFrame(
                self.list_scroll,
                fg_color=card_bg,
                corner_radius=10,
                height=52,
            )
            row.grid(row=idx, column=0, sticky="ew", pady=2, padx=2)
            row.grid_columnconfigure(0, weight=1)
            row.grid_propagate(False)

            short_title = track.title if len(track.title) <= 32 else track.title[:30] + "…"
            dur_str = format_time(track.duration) if track.duration > 0 else ""
            sub_text = f"{track.artist[:24]}  •  {dur_str}" if dur_str else track.artist[:30]

            btn = ctk.CTkButton(
                row,
                text=f"{short_title}\n{sub_text}",
                anchor="w",
                fg_color="transparent",
                hover_color=THEME["bg_hover"],
                text_color=title_col,
                font=ctk.CTkFont(size=12, weight="bold" if is_active else "normal"),
                command=lambda i=idx, tab=self.active_tab: self._play_from_tab(tab, i),
            )
            btn.grid(row=0, column=0, sticky="nsew", padx=4, pady=2)

    # ==========================================
    # ОНЛАЙН-ПОИСК И ЗАГРУЗКА
    # ==========================================
    def _quick_search(self, tag: str):
        self.online_query_var.set(f"{tag} music mix")
        self._start_online_search()

    def _start_online_search(self):
        query = self.online_query_var.get().strip()
        if not query:
            return
        self.search_go_btn.configure(state="disabled", text="...")
        self._render_track_list("Поиск треков...")

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
                    info = ydl.extract_info(f"ytsearch15:{query}", download=False)
                    for entry in info.get("entries", []) or []:
                        vid = entry.get("id")
                        if not vid:
                            continue
                        url = entry.get("url") or f"https://www.youtube.com/watch?v={vid}"
                        if not url.startswith("http"):
                            url = f"https://www.youtube.com/watch?v={vid}"
                        thumbs = entry.get("thumbnails") or []
                        thumb_url = thumbs[-1]["url"] if thumbs else f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg"
                        results.append(
                            TrackItem(
                                title=entry.get("title") or "Без названия",
                                artist=entry.get("uploader") or entry.get("channel") or "YouTube",
                                duration=float(entry.get("duration") or 0),
                                webpage_url=url,
                                thumbnail_url=thumb_url,
                                is_online=True,
                            )
                        )
            except Exception:
                pass

            def done():
                self.search_go_btn.configure(state="normal", text="Найти")
                self.online_results = results
                if self.active_tab == "online":
                    if results:
                        self._render_track_list()
                    else:
                        self._render_track_list("Ничего не найдено")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    def _on_download_current(self):
        track = self.current_track
        if not track or not track.is_online or not track.webpage_url:
            return

        self.download_btn.configure(state="disabled", text="Загрузка...")

        def worker():
            saved_path = None
            try:
                os.makedirs(DOWNLOAD_DIR, exist_ok=True)
                ydl_opts = {
                    "format": "bestaudio/best",
                    "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title)s.%(ext)s"),
                    "ffmpeg_location": self.engine.ffmpeg_exe,
                    "postprocessors": [
                        {
                            "key": "FFmpegExtractAudio",
                            "preferredcodec": "mp3",
                            "preferredquality": "192",
                        }
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
                    self.download_btn.configure(state="normal", text="✓ Сохранено")
                    local_item = TrackItem.from_local_file(saved_path)
                    if not any(t.filepath == saved_path for t in self.local_tracks):
                        self.local_tracks.insert(0, local_item)
                        if self.active_tab == "local":
                            self._render_track_list()
                else:
                    self.download_btn.configure(state="normal", text="Ошибка загрузки")

            self.after(0, done)

        threading.Thread(target=worker, daemon=True).start()

    # ==========================================
    # ВОСПРОИЗВЕДЕНИЕ И УПРАВЛЕНИЕ
    # ==========================================
    def _play_from_tab(self, tab: str, index: int):
        self.active_queue = self.online_results if tab == "online" else self.local_tracks
        self._play_queue_index(index)

    def _play_queue_index(self, index: int):
        if not (0 <= index < len(self.active_queue)):
            return

        self.play_gen += 1
        gen = self.play_gen
        self.current_index = index
        track = self.active_queue[index]
        self.current_track = track

        self._render_track_list()
        self._update_now_playing_header(track, loading=track.is_online)

        if not track.is_online and track.filepath:
            self.engine.play_source(track.filepath, start_sec=0.0)
            self._sync_play_button()
            return

        # Онлайн-трек: получаем прямую ссылку на аудиопоток в фоне
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

            def start_stream():
                if gen != self.play_gen:
                    return
                track.stream_url = stream_url
                track.duration = duration
                if cover_pil:
                    track.cover_pil = cover_pil

                if stream_url:
                    self._update_now_playing_header(track, loading=False)
                    self.engine.play_source(stream_url, start_sec=0.0)
                    self._sync_play_button()
                else:
                    self.source_badge.configure(text="STREAM ERROR")

            self.after(0, start_stream)

        threading.Thread(target=resolve_worker, daemon=True).start()

    def _update_now_playing_header(self, track: TrackItem, loading: bool = False):
        self.title_label.configure(text=track.title)
        self.artist_label.configure(text=track.artist)
        self.total_label.configure(text=format_time(track.duration))
        self.progress_slider.configure(to=max(1.0, track.duration))
        self.progress_slider.set(0)

        if track.is_online:
            badge_txt = "LOADING STREAM..." if loading else "ONLINE STREAM"
            self.source_badge.configure(text=badge_txt)
            self.download_btn.configure(state="normal", text="↓ Скачать MP3")
            self.download_btn.pack(side="right")
        else:
            ext = os.path.splitext(track.filepath or "")[1].upper().lstrip(".") or "AUDIO"
            self.source_badge.configure(text=f"LOCAL {ext}")
            self.download_btn.pack_forget()

        if track.cover_pil:
            rounded = make_rounded_cover(track.cover_pil, size=(236, 236), radius=18)
            self.current_cover_ctk = ctk.CTkImage(
                light_image=rounded, dark_image=rounded, size=(236, 236)
            )
            self.cover_label.configure(image=self.current_cover_ctk)
            accent, soft = extract_muted_accent(track.cover_pil)
            self._apply_accent_color(accent, soft)
        else:
            placeholder = create_placeholder_cover(size=(236, 236), accent_hex=THEME["accent"])
            self.current_cover_ctk = ctk.CTkImage(
                light_image=placeholder, dark_image=placeholder, size=(236, 236)
            )
            self.cover_label.configure(image=self.current_cover_ctk)
            self._apply_accent_color(THEME["accent"], THEME["accent_soft"])

    def _apply_accent_color(self, accent: str, soft: str):
        self.accent_color = accent
        self.accent_soft = soft
        self.play_btn.configure(fg_color=accent)
        self.progress_slider.configure(progress_color=accent, button_hover_color=accent)
        self.source_badge.configure(fg_color=soft, text_color=accent)
        if self.repeat_mode:
            self.repeat_btn.configure(text_color=accent)
        if self.shuffle_mode:
            self.shuffle_btn.configure(text_color=accent)

    def _sync_play_button(self):
        if self.engine.is_playing and not self.engine.is_paused:
            self.play_btn.configure(text="⏸")
        else:
            self.play_btn.configure(text="▶")

    def _on_play_pause(self):
        if not self.engine.is_playing and self.active_queue:
            self._play_queue_index(max(0, self.current_index))
            return
        self.engine.toggle_pause()
        self._sync_play_button()

    def _on_next(self):
        if not self.active_queue:
            return
        if self.shuffle_mode and len(self.active_queue) > 1:
            choices = [i for i in range(len(self.active_queue)) if i != self.current_index]
            next_idx = random.choice(choices)
        else:
            next_idx = (self.current_index + 1) % len(self.active_queue)
        self._play_queue_index(next_idx)

    def _on_prev(self):
        if not self.active_queue:
            return
        if self.engine.get_position() > 4.0:
            self.engine.seek(0.0)
        else:
            prev_idx = (self.current_index - 1) % len(self.active_queue)
            self._play_queue_index(prev_idx)

    def _on_track_finished(self):
        if self.repeat_mode and self.current_index != -1:
            self._play_queue_index(self.current_index)
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

    def _on_add_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку с музыкой")
        if not folder:
            return
        added = []
        for root, _, files in os.walk(folder):
            for f in sorted(files):
                if f.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                    added.append(TrackItem.from_local_file(os.path.join(root, f)))
        self.local_tracks.extend(added)
        self._render_track_list()

    def _on_add_files(self):
        files = filedialog.askopenfilenames(
            title="Выберите аудиофайлы",
            filetypes=[("Audio Files", "*.mp3 *.flac *.wav *.ogg *.m4a")],
        )
        for fp in files:
            self.local_tracks.append(TrackItem.from_local_file(fp))
        self._render_track_list()

    def _load_initial_downloads(self):
        if os.path.isdir(DOWNLOAD_DIR):
            for f in sorted(os.listdir(DOWNLOAD_DIR)):
                if f.lower().endswith(StreamAudioEngine.SUPPORTED_EXTS):
                    self.local_tracks.append(
                        TrackItem.from_local_file(os.path.join(DOWNLOAD_DIR, f))
                    )

    def _bind_keys(self):
        self.bind("<space>", self._on_space_key)
        self.bind("<Left>", lambda _e: self.engine.seek(max(0.0, self.engine.get_position() - 5)))
        self.bind("<Right>", lambda _e: self.engine.seek(self.engine.get_position() + 5))

    def _on_space_key(self, _e):
        focused = self.focus_get()
        if isinstance(focused, tk.Entry):
            return
        self._on_play_pause()

    # ==========================================
    # ОБНОВЛЕНИЕ ПРОГРЕССА И ВИЗУАЛИЗАТОРА
    # ==========================================
    def _draw_visualizer(self):
        self.viz_canvas.delete("all")
        w, h = 260, 28
        bar_w = 4
        gap = (w - self.num_bars * bar_w) / (self.num_bars + 1)
        level = self.engine.current_level if (self.engine.is_playing and not self.engine.is_paused) else 0.0

        for i in range(self.num_bars):
            self.bar_phases[i] += 0.35 + (i % 4) * 0.08
            center_weight = 1.0 - abs(i - self.num_bars / 2) / (self.num_bars / 1.6)
            wave = 0.55 + 0.45 * math.sin(self.bar_phases[i])
            target = 0.10 + level * wave * max(0.35, center_weight)
            self.bar_heights[i] += (target - self.bar_heights[i]) * 0.35

            bh = max(3, min(h - 4, int(self.bar_heights[i] * (h - 4))))
            x0 = gap + i * (bar_w + gap)
            y0 = (h - bh) / 2
            x1 = x0 + bar_w
            y1 = y0 + bh
            col = self.accent_color if level > 0.02 else THEME["border"]
            self.viz_canvas.create_rectangle(x0, y0, x1, y1, fill=col, outline="")

    def _ui_tick(self):
        if self.engine.is_playing and not self.is_seeking:
            pos = self.engine.get_position()
            if self.current_track and self.current_track.duration > 0:
                pos = min(pos, self.current_track.duration)
            self.progress_slider.set(pos)
            self.elapsed_label.configure(text=format_time(pos))

        self._draw_visualizer()
        self.after(60, self._ui_tick)


if __name__ == "__main__":
    app = AetherPlayerApp()
    app.mainloop()
