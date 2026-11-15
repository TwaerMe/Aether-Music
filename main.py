import io
import os
import time
import tkinter as tk
from tkinter import filedialog

import customtkinter as ctk
import pygame
from mutagen import File as MutagenFile
from mutagen.id3 import APIC
from PIL import Image, ImageDraw

# Спокойная минималистичная палитра без неона (Nord / Slate)
PALETTE = {
    "bg_main": "#121316",        # Глубокий матовый угольный фон
    "bg_sidebar": "#181A1F",     # Графитовый для боковой панели
    "bg_card": "#1F2229",        # Карточка / выделенный трек
    "bg_hover": "#262A33",       # Наведение
    "text_primary": "#ECEFF4",   # Мягкий белый
    "text_secondary": "#8B92A0", # Приглушенный серо-стальной
    "accent": "#6E859E",         # Спокойный пыльно-синий акцент
    "accent_hover": "#819AB5",   # Светлее при наведении
    "border": "#282C34",         # Тонкие границы
}

ctk.set_appearance_mode("dark")


# ==========================================
# АУДИОДВИЖОК И МЕТАДАННЫЕ
# ==========================================
class TrackInfo:
    def __init__(self, filepath: str):
        self.filepath = filepath
        self.filename = os.path.basename(filepath)
        self.title = os.path.splitext(self.filename)[0]
        self.artist = "Неизвестный исполнитель"
        self.album = "Без альбома"
        self.duration = 0.0
        self.cover_image = None
        self._extract_metadata()

    def _extract_metadata(self):
        try:
            audio = MutagenFile(self.filepath)
            if audio is None:
                return

            if hasattr(audio, "info") and hasattr(audio.info, "length"):
                self.duration = float(audio.info.length)

            if audio.tags:
                if "TIT2" in audio.tags:
                    self.title = str(audio.tags["TIT2"].text[0])
                elif "title" in audio.tags:
                    self.title = str(audio.tags["title"][0])

                if "TPE1" in audio.tags:
                    self.artist = str(audio.tags["TPE1"].text[0])
                elif "artist" in audio.tags:
                    self.artist = str(audio.tags["artist"][0])

                if "TALB" in audio.tags:
                    self.album = str(audio.tags["TALB"].text[0])
                elif "album" in audio.tags:
                    self.album = str(audio.tags["album"][0])

                for tag in audio.tags.values():
                    if isinstance(tag, APIC):
                        self.cover_image = Image.open(io.BytesIO(tag.data))
                        break

                if self.cover_image is None and hasattr(audio, "pictures") and audio.pictures:
                    self.cover_image = Image.open(io.BytesIO(audio.pictures[0].data))
        except Exception:
            pass


class AudioEngine:
    SUPPORTED_EXTS = (".mp3", ".wav", ".ogg", ".flac")

    def __init__(self):
        pygame.mixer.init(frequency=44100, size=-16, channels=2, buffer=2048)
        self.playlist: list[TrackInfo] = []
        self.current_index: int = -1
        self.is_playing: bool = False
        self.is_paused: bool = False
        self.volume: float = 0.75
        self._start_offset: float = 0.0
        self._play_timestamp: float = 0.0
        self._paused_position: float = 0.0
        pygame.mixer.music.set_volume(self.volume)

    def load_folder(self, folder_path: str) -> list[TrackInfo]:
        tracks = []
        for root, _, files in os.walk(folder_path):
            for f in sorted(files):
                if f.lower().endswith(self.SUPPORTED_EXTS):
                    tracks.append(TrackInfo(os.path.join(root, f)))
        self.playlist = tracks
        return tracks

    def add_files(self, filepaths: list[str]) -> list[TrackInfo]:
        for fp in filepaths:
            if fp.lower().endswith(self.SUPPORTED_EXTS):
                self.playlist.append(TrackInfo(fp))
        return self.playlist

    @property
    def current_track(self) -> TrackInfo | None:
        if 0 <= self.current_index < len(self.playlist):
            return self.playlist[self.current_index]
        return None

    def play_index(self, index: int):
        if not (0 <= index < len(self.playlist)):
            return
        self.current_index = index
        track = self.playlist[index]
        pygame.mixer.music.load(track.filepath)
        pygame.mixer.music.play()
        self.is_playing = True
        self.is_paused = False
        self._start_offset = 0.0
        self._play_timestamp = time.time()
        self._paused_position = 0.0

    def toggle_play_pause(self):
        if self.current_index == -1 and self.playlist:
            self.play_index(0)
            return

        if self.is_playing and not self.is_paused:
            self._paused_position = self.get_position()
            pygame.mixer.music.pause()
            self.is_paused = True
        elif self.is_paused:
            pygame.mixer.music.unpause()
            self._start_offset = self._paused_position
            self._play_timestamp = time.time()
            self.is_paused = False

    def next_track(self):
        if not self.playlist:
            return
        next_idx = (self.current_index + 1) % len(self.playlist)
        self.play_index(next_idx)

    def prev_track(self):
        if not self.playlist:
            return
        if self.get_position() > 3.0:
            self.seek(0.0)
        else:
            prev_idx = (self.current_index - 1) % len(self.playlist)
            self.play_index(prev_idx)

    def seek(self, seconds: float):
        if not self.current_track:
            return
        seconds = max(0.0, min(seconds, self.current_track.duration))
        try:
            pygame.mixer.music.play(start=seconds)
            self._start_offset = seconds
            self._play_timestamp = time.time()
            self._paused_position = seconds
            if self.is_paused:
                pygame.mixer.music.pause()
            else:
                self.is_playing = True
        except Exception:
            pass

    def set_volume(self, value: float):
        self.volume = max(0.0, min(1.0, value))
        pygame.mixer.music.set_volume(self.volume)

    def get_position(self) -> float:
        if not self.is_playing or not self.current_track:
            return 0.0
        if self.is_paused:
            return self._paused_position
        pos = self._start_offset + (time.time() - self._play_timestamp)
        return min(pos, self.current_track.duration)

    def check_track_finished(self) -> bool:
        if self.is_playing and not self.is_paused:
            if not pygame.mixer.music.get_busy():
                if self.current_track and self.get_position() >= max(0.5, self.current_track.duration - 0.5):
                    return True
        return False


# ==========================================
# ИНТЕРФЕЙС ПРИЛОЖЕНИЯ
# ==========================================
def format_time(seconds: float) -> str:
    seconds = max(0, int(seconds))
    mins, secs = divmod(seconds, 60)
    return f"{mins:02d}:{secs:02d}"


def create_placeholder_cover(size=(240, 240)) -> Image.Image:
    img = Image.new("RGB", size, PALETTE["bg_card"])
    draw = ImageDraw.Draw(img)
    cx, cy = size[0] // 2, size[1] // 2
    draw.ellipse((cx - 56, cy - 56, cx + 56, cy + 56), outline=PALETTE["accent"], width=2)
    draw.ellipse((cx - 16, cy - 16, cx + 16, cy + 16), outline=PALETTE["text_secondary"], width=2)
    return img


class MinimalPlayerApp(ctk.CTk):
    def __init__(self):
        super().__init__()

        self.title("Aether — Minimal Audio")
        self.geometry("920x600")
        self.minsize(780, 520)
        self.configure(fg_color=PALETTE["bg_main"])

        self.engine = AudioEngine()
        self.is_seeking = False
        self.repeat_mode = False
        self.track_buttons: list[ctk.CTkButton] = []
        self.current_cover_img = None

        self._build_ui()
        self._bind_shortcuts()
        self._update_loop()

    def _build_ui(self):
        self.grid_columnconfigure(0, weight=0, minsize=280)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        # Левая панель (Медиатека)
        self.sidebar = ctk.CTkFrame(
            self, fg_color=PALETTE["bg_sidebar"], corner_radius=0, width=290
        )
        self.sidebar.grid(row=0, column=0, sticky="nsew")
        self.sidebar.grid_propagate(False)
        self.sidebar.grid_rowconfigure(2, weight=1)
        self.sidebar.grid_columnconfigure(0, weight=1)

        header_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        header_frame.grid(row=0, column=0, sticky="ew", padx=20, pady=(24, 12))

        ctk.CTkLabel(
            header_frame,
            text="Медиатека",
            font=ctk.CTkFont(family="Segoe UI", size=18, weight="bold"),
            text_color=PALETTE["text_primary"],
        ).pack(side="left")

        btn_frame = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        btn_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 12))

        ctk.CTkButton(
            btn_frame,
            text="+ Папка",
            height=32,
            corner_radius=8,
            fg_color=PALETTE["bg_card"],
            hover_color=PALETTE["bg_hover"],
            text_color=PALETTE["text_primary"],
            font=ctk.CTkFont(size=12),
            command=self._on_add_folder,
        ).pack(side="left", fill="x", expand=True, padx=(0, 6))

        ctk.CTkButton(
            btn_frame,
            text="+ Файлы",
            height=32,
            corner_radius=8,
            fg_color=PALETTE["bg_card"],
            hover_color=PALETTE["bg_hover"],
            text_color=PALETTE["text_primary"],
            font=ctk.CTkFont(size=12),
            command=self._on_add_files,
        ).pack(side="left", fill="x", expand=True, padx=(6, 0))

        self.playlist_scroll = ctk.CTkScrollableFrame(
            self.sidebar,
            fg_color="transparent",
            scrollbar_button_color=PALETTE["bg_card"],
            scrollbar_button_hover_color=PALETTE["accent"],
        )
        self.playlist_scroll.grid(row=2, column=0, sticky="nsew", padx=12, pady=(0, 16))
        self.playlist_scroll.grid_columnconfigure(0, weight=1)

        self.empty_label = ctk.CTkLabel(
            self.playlist_scroll,
            text="Добавьте папку с музыкой\nили аудиофайлы (MP3, FLAC, WAV)",
            font=ctk.CTkFont(size=12),
            text_color=PALETTE["text_secondary"],
            justify="center",
        )
        self.empty_label.grid(row=0, column=0, pady=40)

        # Правая панель (Плеер)
        self.main_area = ctk.CTkFrame(self, fg_color=PALETTE["bg_main"], corner_radius=0)
        self.main_area.grid(row=0, column=1, sticky="nsew", padx=40, pady=32)
        self.main_area.grid_columnconfigure(0, weight=1)
        self.main_area.grid_rowconfigure(1, weight=1)

        top_bar = ctk.CTkFrame(self.main_area, fg_color="transparent")
        top_bar.grid(row=0, column=0, sticky="ew")

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filter_playlist())
        self.search_entry = ctk.CTkEntry(
            top_bar,
            textvariable=self.search_var,
            placeholder_text="Поиск по трекам...",
            height=34,
            width=240,
            corner_radius=8,
            fg_color=PALETTE["bg_sidebar"],
            border_color=PALETTE["border"],
            border_width=1,
            text_color=PALETTE["text_primary"],
            placeholder_text_color=PALETTE["text_secondary"],
            font=ctk.CTkFont(size=12),
        )
        self.search_entry.pack(side="right")

        center_frame = ctk.CTkFrame(self.main_area, fg_color="transparent")
        center_frame.grid(row=1, column=0, sticky="nsew")
        center_frame.grid_columnconfigure(0, weight=1)

        self.default_cover = ctk.CTkImage(
            light_image=create_placeholder_cover(),
            dark_image=create_placeholder_cover(),
            size=(230, 230),
        )

        self.cover_label = ctk.CTkLabel(
            center_frame,
            text="",
            image=self.default_cover,
            corner_radius=16,
        )
        self.cover_label.pack(pady=(20, 24))

        self.title_label = ctk.CTkLabel(
            center_frame,
            text="Ничего не воспроизводится",
            font=ctk.CTkFont(family="Segoe UI", size=20, weight="bold"),
            text_color=PALETTE["text_primary"],
            wraplength=460,
        )
        self.title_label.pack(pady=(0, 4))

        self.artist_label = ctk.CTkLabel(
            center_frame,
            text="Выберите трек в медиатеке слева",
            font=ctk.CTkFont(family="Segoe UI", size=13),
            text_color=PALETTE["text_secondary"],
        )
        self.artist_label.pack()

        controls_container = ctk.CTkFrame(self.main_area, fg_color="transparent")
        controls_container.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        controls_container.grid_columnconfigure(0, weight=1)

        time_frame = ctk.CTkFrame(controls_container, fg_color="transparent")
        time_frame.grid(row=0, column=0, sticky="ew", pady=(0, 4))

        self.elapsed_label = ctk.CTkLabel(
            time_frame,
            text="00:00",
            font=ctk.CTkFont(size=11),
            text_color=PALETTE["text_secondary"],
        )
        self.elapsed_label.pack(side="left")

        self.total_label = ctk.CTkLabel(
            time_frame,
            text="00:00",
            font=ctk.CTkFont(size=11),
            text_color=PALETTE["text_secondary"],
        )
        self.total_label.pack(side="right")

        self.progress_slider = ctk.CTkSlider(
            controls_container,
            from_=0,
            to=100,
            height=14,
            fg_color=PALETTE["bg_card"],
            progress_color=PALETTE["accent"],
            button_color=PALETTE["text_primary"],
            button_hover_color=PALETTE["accent_hover"],
            button_length=12,
            command=self._on_slider_move,
        )
        self.progress_slider.set(0)
        self.progress_slider.grid(row=1, column=0, sticky="ew", pady=(0, 20))
        self.progress_slider.bind("<ButtonPress-1>", self._on_seek_start)
        self.progress_slider.bind("<ButtonRelease-1>", self._on_seek_end)

        buttons_row = ctk.CTkFrame(controls_container, fg_color="transparent")
        buttons_row.grid(row=2, column=0, sticky="ew")
        buttons_row.grid_columnconfigure(1, weight=1)

        self.repeat_btn = ctk.CTkButton(
            buttons_row,
            text="↻",
            width=36,
            height=36,
            corner_radius=18,
            fg_color="transparent",
            hover_color=PALETTE["bg_card"],
            text_color=PALETTE["text_secondary"],
            font=ctk.CTkFont(size=16),
            command=self._toggle_repeat,
        )
        self.repeat_btn.grid(row=0, column=0, sticky="w")

        center_btns = ctk.CTkFrame(buttons_row, fg_color="transparent")
        center_btns.grid(row=0, column=1)

        self.prev_btn = ctk.CTkButton(
            center_btns,
            text="⏮",
            width=42,
            height=42,
            corner_radius=21,
            fg_color="transparent",
            hover_color=PALETTE["bg_card"],
            text_color=PALETTE["text_primary"],
            font=ctk.CTkFont(size=16),
            command=self._on_prev,
        )
        self.prev_btn.pack(side="left", padx=8)

        self.play_btn = ctk.CTkButton(
            center_btns,
            text="▶",
            width=54,
            height=54,
            corner_radius=27,
            fg_color=PALETTE["accent"],
            hover_color=PALETTE["accent_hover"],
            text_color="#121316",
            font=ctk.CTkFont(size=18, weight="bold"),
            command=self._on_play_pause,
        )
        self.play_btn.pack(side="left", padx=12)

        self.next_btn = ctk.CTkButton(
            center_btns,
            text="⏭",
            width=42,
            height=42,
            corner_radius=21,
            fg_color="transparent",
            hover_color=PALETTE["bg_card"],
            text_color=PALETTE["text_primary"],
            font=ctk.CTkFont(size=16),
            command=self._on_next,
        )
        self.next_btn.pack(side="left", padx=8)

        vol_frame = ctk.CTkFrame(buttons_row, fg_color="transparent")
        vol_frame.grid(row=0, column=2, sticky="e")

        ctk.CTkLabel(
            vol_frame,
            text="🔊",
            font=ctk.CTkFont(size=13),
            text_color=PALETTE["text_secondary"],
        ).pack(side="left", padx=(0, 6))

        self.volume_slider = ctk.CTkSlider(
            vol_frame,
            from_=0,
            to=1,
            width=90,
            height=12,
            fg_color=PALETTE["bg_card"],
            progress_color=PALETTE["text_secondary"],
            button_color=PALETTE["text_primary"],
            button_hover_color=PALETTE["accent"],
            command=self._on_volume_change,
        )
        self.volume_slider.set(self.engine.volume)
        self.volume_slider.pack(side="left")

    def _bind_shortcuts(self):
        self.bind("<space>", self._handle_space)
        self.bind("<Left>", lambda e: self.engine.seek(self.engine.get_position() - 5))
        self.bind("<Right>", lambda e: self.engine.seek(self.engine.get_position() + 5))

    def _handle_space(self, _event):
        if self.focus_get() != self.search_entry._entry:
            self._on_play_pause()

    def _on_add_folder(self):
        folder = filedialog.askdirectory(title="Выберите папку с музыкой")
        if folder:
            self.engine.load_folder(folder)
            self._render_playlist()
            if self.engine.playlist and self.engine.current_index == -1:
                self._play_track(0)

    def _on_add_files(self):
        files = filedialog.askopenfilenames(
            title="Выберите аудиофайлы",
            filetypes=[("Audio Files", "*.mp3 *.flac *.wav *.ogg")],
        )
        if files:
            was_empty = len(self.engine.playlist) == 0
            self.engine.add_files(list(files))
            self._render_playlist()
            if was_empty and self.engine.playlist:
                self._play_track(0)

    def _render_playlist(self, filter_text: str = ""):
        for widget in self.playlist_scroll.winfo_children():
            widget.destroy()
        self.track_buttons.clear()

        if not self.engine.playlist:
            self.empty_label = ctk.CTkLabel(
                self.playlist_scroll,
                text="Нет загруженных треков",
                font=ctk.CTkFont(size=12),
                text_color=PALETTE["text_secondary"],
            )
            self.empty_label.grid(row=0, column=0, pady=40)
            return

        q = filter_text.strip().lower()
        row_idx = 0
        for idx, track in enumerate(self.engine.playlist):
            if q and q not in track.title.lower() and q not in track.artist.lower():
                continue

            is_active = idx == self.engine.current_index
            bg_col = PALETTE["bg_card"] if is_active else "transparent"
            txt_col = PALETTE["accent_hover"] if is_active else PALETTE["text_primary"]

            display_text = f"{track.title}\n{track.artist}"
            btn = ctk.CTkButton(
                self.playlist_scroll,
                text=display_text,
                anchor="w",
                height=46,
                corner_radius=8,
                fg_color=bg_col,
                hover_color=PALETTE["bg_hover"],
                text_color=txt_col,
                font=ctk.CTkFont(size=12, weight="bold" if is_active else "normal"),
                command=lambda i=idx: self._play_track(i),
            )
            btn.grid(row=row_idx, column=0, sticky="ew", pady=2, padx=2)
            self.track_buttons.append(btn)
            row_idx += 1

    def _filter_playlist(self):
        self._render_playlist(self.search_var.get())

    def _play_track(self, index: int):
        self.engine.play_index(index)
        self._update_track_display()
        self._render_playlist(self.search_var.get())

    def _update_track_display(self):
        track = self.engine.current_track
        if not track:
            return

        self.title_label.configure(text=track.title)
        self.artist_label.configure(text=f"{track.artist}  •  {track.album}")
        self.total_label.configure(text=format_time(track.duration))
        self.progress_slider.configure(to=max(1.0, track.duration))

        if track.cover_image:
            cover_resized = track.cover_image.resize((230, 230), Image.Resampling.LANCZOS)
            self.current_cover_img = ctk.CTkImage(
                light_image=cover_resized, dark_image=cover_resized, size=(230, 230)
            )
            self.cover_label.configure(image=self.current_cover_img)
        else:
            self.cover_label.configure(image=self.default_cover)

        self._sync_play_button()

    def _sync_play_button(self):
        if self.engine.is_playing and not self.engine.is_paused:
            self.play_btn.configure(text="⏸")
        else:
            self.play_btn.configure(text="▶")

    def _on_play_pause(self):
        self.engine.toggle_play_pause()
        self._sync_play_button()
        if self.engine.current_track:
            self._update_track_display()

    def _on_next(self):
        self.engine.next_track()
        self._update_track_display()
        self._render_playlist(self.search_var.get())

    def _on_prev(self):
        self.engine.prev_track()
        self._update_track_display()
        self._render_playlist(self.search_var.get())

    def _toggle_repeat(self):
        self.repeat_mode = not self.repeat_mode
        color = PALETTE["accent_hover"] if self.repeat_mode else PALETTE["text_secondary"]
        self.repeat_btn.configure(text_color=color)

    def _on_volume_change(self, value):
        self.engine.set_volume(float(value))

    def _on_seek_start(self, _event):
        self.is_seeking = True

    def _on_slider_move(self, value):
        if self.is_seeking:
            self.elapsed_label.configure(text=format_time(float(value)))

    def _on_seek_end(self, _event):
        if self.is_seeking:
            self.engine.seek(float(self.progress_slider.get()))
            self.is_seeking = False

    def _update_loop(self):
        if self.engine.check_track_finished():
            if self.repeat_mode and self.engine.current_index != -1:
                self._play_track(self.engine.current_index)
            else:
                self._on_next()

        if self.engine.is_playing and not self.is_seeking:
            pos = self.engine.get_position()
            self.progress_slider.set(pos)
            self.elapsed_label.configure(text=format_time(pos))

        self.after(250, self._update_loop)


if __name__ == "__main__":
    app = MinimalPlayerApp()
    app.mainloop()
