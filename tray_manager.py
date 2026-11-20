from __future__ import annotations

import threading
from typing import Callable

try:
    import pystray
except ImportError:  # pragma: no cover - optional in headless/test environments
    pystray = None

try:
    from PIL import Image, ImageDraw
except ImportError:  # pragma: no cover
    Image = None
    ImageDraw = None


def create_app_icon_image(size: int = 64, accent: str = "#C8A464"):
    """Рисует минималистичную иконку Aether Music (темная плитка + золотой винил/волна)."""
    if Image is None or ImageDraw is None:
        return None

    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    pad = max(2, size // 16)
    radius = max(8, size // 4)
    draw.rounded_rectangle(
        (pad, pad, size - pad, size - pad),
        radius=radius,
        fill="#14181F",
        outline="#2A3140",
        width=max(1, size // 32),
    )

    cx, cy = size // 2, size // 2
    outer_r = int(size * 0.29)
    mid_r = int(size * 0.17)
    inner_r = max(2, int(size * 0.06))
    stroke = max(2, size // 20)

    draw.ellipse(
        (cx - outer_r, cy - outer_r, cx + outer_r, cy + outer_r),
        outline=accent,
        width=stroke,
    )
    draw.ellipse(
        (cx - mid_r, cy - mid_r, cx + mid_r, cy + mid_r),
        outline="#9099A8",
        width=max(1, stroke // 2),
    )
    draw.ellipse(
        (cx - inner_r, cy - inner_r, cx + inner_r, cy + inner_r),
        fill=accent,
    )
    return img


class TrayManager:
    """Управляет иконкой приложения в системном трее Windows/macOS/Linux."""

    def __init__(
        self,
        on_open: Callable[[], None],
        on_play_pause: Callable[[], None],
        on_next: Callable[[], None],
        on_prev: Callable[[], None],
        on_toggle_like: Callable[[], None],
        on_quit: Callable[[], None],
        get_play_state: Callable[[], bool],
        get_liked_state: Callable[[], bool],
        accent: str = "#C8A464",
    ):
        self._on_open = on_open
        self._on_play_pause = on_play_pause
        self._on_next = on_next
        self._on_prev = on_prev
        self._on_toggle_like = on_toggle_like
        self._on_quit = on_quit
        self._get_play_state = get_play_state
        self._get_liked_state = get_liked_state
        self._accent = accent

        self._icon = None
        self._thread: threading.Thread | None = None
        self._running = False
        self._notified_once = False
        self._last_tray_sig: tuple[str, bool, bool] | None = None

    @property
    def is_running(self) -> bool:
        return self._running and self._icon is not None

    def start(self) -> bool:
        if pystray is None:
            return False
        if self._running:
            return True

        icon_img = create_app_icon_image(64, accent=self._accent)
        if icon_img is None:
            return False

        menu = pystray.Menu(
            pystray.MenuItem(
                "Открыть Aether Music",
                lambda _icon, _item: self._on_open(),
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _item: "⏸ Пауза" if self._get_play_state() else "▶ Воспроизвести",
                lambda _icon, _item: self._on_play_pause(),
            ),
            pystray.MenuItem(
                "⏭ Следующий трек",
                lambda _icon, _item: self._on_next(),
            ),
            pystray.MenuItem(
                "⏮ Предыдущий трек",
                lambda _icon, _item: self._on_prev(),
            ),
            pystray.MenuItem(
                lambda _item: "♥ Убрать из любимых" if self._get_liked_state() else "♡ В любимые",
                lambda _icon, _item: self._on_toggle_like(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Выйти полностью",
                lambda _icon, _item: self._on_quit(),
            ),
        )

        try:
            self._icon = pystray.Icon("AetherMusic", icon_img, "Aether Music", menu)
            self._running = True
            self._thread = threading.Thread(target=self._run_loop, daemon=True, name="system-tray")
            self._thread.start()
            return True
        except Exception:
            self._icon = None
            self._running = False
            return False

    def _run_loop(self) -> None:
        try:
            if self._icon is not None:
                self._icon.run()
        except Exception:
            pass
        finally:
            self._running = False

    def update_state(self, title: str | None = None, artist: str | None = None) -> None:
        if not self.is_running or self._icon is None:
            return
        try:
            if title:
                label = f"{artist} — {title}" if artist else title
                tooltip = f"Aether Music • {label}"[:120]
            else:
                tooltip = "Aether Music"
            sig = (tooltip, bool(self._get_play_state()), bool(self._get_liked_state()))
            if sig == self._last_tray_sig:
                return
            self._last_tray_sig = sig
            self._icon.title = tooltip
            self._icon.update_menu()
        except Exception:
            pass

    def notify_minimized_once(self) -> None:
        if self._notified_once or not self.is_running or self._icon is None:
            return
        self._notified_once = True
        try:
            self._icon.notify(
                "Приложение свернуто в системный трей и продолжает работать.",
                "Aether Music",
            )
        except Exception:
            pass

    def stop(self) -> None:
        self._running = False
        icon = self._icon
        self._icon = None
        if icon is not None:
            try:
                icon.stop()
            except Exception:
                pass
