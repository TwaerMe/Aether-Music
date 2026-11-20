from __future__ import annotations

import hashlib
import heapq
import json
import os
import queue
import re
import tempfile
import threading
import time
import urllib.parse
from collections import OrderedDict
from typing import Any, Callable

try:
    import requests as _requests
    from requests.adapters import HTTPAdapter as _HTTPAdapter
except ImportError:  # pragma: no cover - fallback for stdlib-only test environments
    _requests = None
    _HTTPAdapter = None


_EXPIRE_RE = re.compile(r"[?&]expire=(\d+)")


def parse_stream_expiry(
    url: str | None,
    now: float | None = None,
    default_ttl: float = 5400.0,
    safety_margin: float = 480.0,
) -> float:
    """Извлекает реальное время жизни прямой аудиоссылки YouTube (параметр expire=)."""
    ts_now = time.time() if now is None else float(now)
    if not url:
        return ts_now
    m = _EXPIRE_RE.search(url)
    if m:
        try:
            exp = float(int(m.group(1)))
            if exp > ts_now + 60.0:
                return max(ts_now + 60.0, exp - safety_margin)
        except (TypeError, ValueError):
            pass
    return ts_now + default_ttl


def pick_optimal_thumbnail(thumbnails: list[dict[str, Any]] | None, video_id: str | None = None) -> str | None:
    """
    Выбирает оптимальную по размеру миниатюру (~180..480px) вместо огромных 1280x720,
    чтобы обложки загружались и декодировались в 5-10 раз быстрее.
    """
    if thumbnails:
        best_url: str | None = None
        best_score = float("inf")
        for item in thumbnails:
            if not isinstance(item, dict):
                continue
            url = item.get("url")
            if not url or not isinstance(url, str):
                continue
            # Избегаем webp если есть jpg аналогичного размера, но принимаем если других нет
            width = int(item.get("width") or 0)
            height = int(item.get("height") or 0)
            dim = max(width, height)
            if dim <= 0:
                # Оценка по имени файла YouTube
                if "mqdefault" in url:
                    dim = 320
                elif "hqdefault" in url:
                    dim = 480
                elif "sddefault" in url:
                    dim = 640
                elif "maxresdefault" in url:
                    dim = 1280
                else:
                    dim = 360
            # Целевой размер ~320px — идеально для 40x40 и 56x56 обложек и быстро качается
            penalty = abs(dim - 320)
            if dim < 120:
                penalty += 400
            if ".webp" in url.lower():
                penalty += 45
            if penalty < best_score:
                best_score = penalty
                best_url = url
        if best_url:
            return best_url

    if video_id:
        return f"https://i.ytimg.com/vi/{video_id}/mqdefault.jpg"
    return None


def atomic_write_json(path: str, payload: dict[str, Any], compact: bool = True) -> None:
    """Атомарная запись JSON через временный файл и os.replace."""
    parent = os.path.dirname(os.path.abspath(path))
    os.makedirs(parent, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            if compact:
                json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
            else:
                json.dump(payload, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except Exception:
                pass


class DebouncedStateWriter:
    """
    Выносит запись состояния на диск в отдельный фоновый поток с дебаунсом,
    полностью убирая микрофризы UI-потока при лайках, скипах и смене треков.
    """

    def __init__(self, delay_sec: float = 0.45):
        self.delay_sec = max(0.05, float(delay_sec))
        self._lock = threading.Lock()
        self._pending: dict[str, dict[str, Any]] = {}
        self._timer: threading.Timer | None = None
        self._closed = False

    def schedule(self, targets: list[tuple[str, dict[str, Any]]]) -> None:
        if self._closed:
            return
        with self._lock:
            for path, data in targets:
                self._pending[path] = data
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(self.delay_sec, self._flush_timer)
            self._timer.daemon = True
            self._timer.start()

    def _flush_timer(self) -> None:
        self.flush_sync()

    def flush_sync(self) -> None:
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None
            batch = list(self._pending.items())
            self._pending.clear()

        for path, data in batch:
            try:
                atomic_write_json(path, data, compact=False)
            except Exception:
                pass

    def close(self) -> None:
        self._closed = True
        self.flush_sync()


class StreamURLCache:
    """
    Потокобезопасный LRU-кэш прямых stream_url по track.uid с сохранением на диск.
    Позволяет мгновенно запускать треки из Любимых/Истории/Поиска/Волны без повторного yt-dlp.
    """

    def __init__(self, cache_file: str | None = None, max_items: int = 500):
        self.cache_file = cache_file
        self.max_items = max(32, int(max_items))
        self._lock = threading.Lock()
        self._entries: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._dirty = False
        self._flush_timer: threading.Timer | None = None
        if cache_file:
            self._load()

    def _load(self) -> None:
        if not self.cache_file or not os.path.isfile(self.cache_file):
            return
        try:
            with open(self.cache_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict) or data.get("v") != 2:
                return
            now = time.time()
            items = data.get("entries")
            if not isinstance(items, dict):
                return
            for uid, entry in items.items():
                if not isinstance(entry, dict):
                    continue
                url = str(entry.get("url") or "")
                exp = float(entry.get("expires_at") or 0.0)
                hdrs = entry.get("headers")
                if url and exp > now + 30.0:
                    self._entries[str(uid)] = {
                        "url": url,
                        "expires_at": exp,
                        "duration": float(entry.get("duration") or 0.0),
                        "thumbnail_url": entry.get("thumbnail_url"),
                        "headers": dict(hdrs) if isinstance(hdrs, dict) else {},
                    }
            while len(self._entries) > self.max_items:
                self._entries.popitem(last=False)
        except Exception:
            pass

    def get(self, uid: str, now: float | None = None) -> dict[str, Any] | None:
        if not uid:
            return None
        ts_now = time.time() if now is None else float(now)
        with self._lock:
            entry = self._entries.get(uid)
            if entry is None:
                return None
            if float(entry.get("expires_at") or 0.0) <= ts_now:
                self._entries.pop(uid, None)
                return None
            self._entries.move_to_end(uid)
            res = dict(entry)
            res["headers"] = dict(entry.get("headers") or {})
            return res

    def put(
        self,
        uid: str,
        url: str,
        duration: float = 0.0,
        thumbnail_url: str | None = None,
        headers: dict[str, str] | None = None,
        now: float | None = None,
    ) -> float:
        if not uid or not url:
            return 0.0
        exp = parse_stream_expiry(url, now=now)
        clean_hdrs = {str(k): str(v) for k, v in (headers or {}).items() if k and v}
        with self._lock:
            self._entries[uid] = {
                "url": url,
                "expires_at": exp,
                "duration": float(duration or 0.0),
                "thumbnail_url": thumbnail_url,
                "headers": clean_hdrs,
            }
            self._entries.move_to_end(uid)
            while len(self._entries) > self.max_items:
                self._entries.popitem(last=False)
            self._dirty = True
            self._schedule_flush_locked()
        return exp

    def invalidate(self, uid: str) -> None:
        if not uid:
            return
        with self._lock:
            if uid in self._entries:
                self._entries.pop(uid, None)
                self._dirty = True
                self._schedule_flush_locked()

    def _schedule_flush_locked(self) -> None:
        if not self.cache_file:
            return
        if self._flush_timer is not None:
            self._flush_timer.cancel()
        self._flush_timer = threading.Timer(1.5, self.flush_sync)
        self._flush_timer.daemon = True
        self._flush_timer.start()

    def flush_sync(self) -> None:
        if not self.cache_file:
            return
        with self._lock:
            if self._flush_timer is not None:
                self._flush_timer.cancel()
                self._flush_timer = None
            if not self._dirty:
                return
            now = time.time()
            live = {
                k: v
                for k, v in self._entries.items()
                if float(v.get("expires_at") or 0.0) > now + 30.0
            }
            self._dirty = False
        try:
            atomic_write_json(self.cache_file, {"v": 2, "entries": live}, compact=True)
        except Exception:
            pass

    def close(self) -> None:
        self.flush_sync()


class AudioFileCache:
    """
    Умный локальный LRU-кэш аудиопотоков (до max_bytes).
    Тихо подгружает в фоне следующий трек в очереди и текущий трек,
    чтобы переключения треков и перемотка работали мгновенно (<30 мс) без сетевых лагов.
    """

    def __init__(
        self,
        cache_dir: str,
        max_bytes: int = 160 * 1024 * 1024,
        max_single_file_bytes: int = 24 * 1024 * 1024,
        urgent_active_fn: Callable[[], bool] | None = None,
    ):
        self.cache_dir = cache_dir
        self.max_bytes = max(10 * 1024 * 1024, int(max_bytes))
        self.max_single_file_bytes = max(2 * 1024 * 1024, int(max_single_file_bytes))
        self._urgent_active_fn = urgent_active_fn

        self._lock = threading.Lock()
        self._inflight: set[str] = set()
        self._closing = False
        self._task_q: queue.Queue[tuple[str, str, dict[str, str]] | None] = queue.Queue(maxsize=16)

        self._session = None
        if _requests is not None:
            self._session = _requests.Session()
            if _HTTPAdapter is not None:
                adapter = _HTTPAdapter(pool_connections=6, pool_maxsize=6, max_retries=1)
                self._session.mount("http://", adapter)
                self._session.mount("https://", adapter)
            self._session.headers.update({"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})

        try:
            os.makedirs(self.cache_dir, exist_ok=True)
            self._cleanup_stale_parts()
        except Exception:
            pass

        self._worker = threading.Thread(target=self._download_loop, daemon=True, name="audio-cache-dl")
        self._worker.start()

    def _cleanup_stale_parts(self) -> None:
        try:
            for name in os.listdir(self.cache_dir):
                if name.endswith(".part"):
                    try:
                        os.remove(os.path.join(self.cache_dir, name))
                    except Exception:
                        pass
        except Exception:
            pass

    def _path_for_uid(self, uid: str) -> str:
        digest = hashlib.sha1(uid.encode("utf-8", errors="ignore")).hexdigest()
        return os.path.join(self.cache_dir, f"{digest}.aud")

    def invalidate(self, uid: str) -> None:
        if not uid:
            return
        path = self._path_for_uid(uid)
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass

    def get_cached_path(self, uid: str) -> str | None:
        if not uid:
            return None
        path = self._path_for_uid(uid)
        try:
            st = os.stat(path)
            if st.st_size >= 32768:
                try:
                    os.utime(path, None)
                except Exception:
                    pass
                return path
        except OSError:
            return None
        return None

    def warm_async(self, uid: str, stream_url: str, headers: dict[str, str] | None = None) -> bool:
        """Ставит аудиофайл в фоновую предзагрузку на диск (не блокирует UI)."""
        if self._closing or not uid or not stream_url or self._session is None:
            return False
        if not (stream_url.startswith("http://") or stream_url.startswith("https://")):
            return False
        # Не пытаемся скачивать HLS/DASH плейлисты напрямую как единый файл
        low = stream_url.lower()
        if ".m3u8" in low or ".mpd" in low or "/manifest/" in low:
            return False
        if self.get_cached_path(uid) is not None:
            return False

        with self._lock:
            if uid in self._inflight:
                return False
            self._inflight.add(uid)

        try:
            req_hdrs = {str(k): str(v) for k, v in (headers or {}).items() if k and v}
            self._task_q.put_nowait((uid, stream_url, req_hdrs))
            return True
        except queue.Full:
            with self._lock:
                self._inflight.discard(uid)
            return False

    def _download_loop(self) -> None:
        while not self._closing:
            try:
                item = self._task_q.get(timeout=0.5)
            except queue.Empty:
                continue
            if item is None:
                break
            uid, stream_url, req_hdrs = item
            try:
                self._download_one(uid, stream_url, req_hdrs)
            except Exception:
                pass
            finally:
                with self._lock:
                    self._inflight.discard(uid)

    def _download_one(self, uid: str, stream_url: str, req_hdrs: dict[str, str] | None = None) -> bool:
        if self._closing or self._session is None:
            return False
        target = self._path_for_uid(uid)
        if os.path.isfile(target) and os.path.getsize(target) >= 32768:
            return True

        part = f"{target}.{os.getpid()}.{threading.get_ident()}.part"
        written = 0
        try:
            with self._session.get(
                stream_url,
                headers=req_hdrs or None,
                stream=True,
                timeout=(6.0, 12.0),
            ) as resp:
                if resp.status_code not in (200, 206):
                    return False
                ctype = str(resp.headers.get("Content-Type") or "").lower()
                if "mpegurl" in ctype or "text/" in ctype or "json" in ctype or "xml" in ctype:
                    return False
                clen = resp.headers.get("Content-Length")
                if clen and clen.isdigit() and int(clen) > self.max_single_file_bytes:
                    return False

                os.makedirs(self.cache_dir, exist_ok=True)
                with open(part, "wb") as f:
                    for chunk in resp.iter_content(chunk_size=65536):
                        if self._closing:
                            return False
                        if not chunk:
                            continue
                        if written == 0:
                            # Отсекаем HTML/XML ответы на всякий случай
                            head = chunk[:32].lstrip().lower()
                            if head.startswith((b"<html", b"<!doctype", b"<?xml", b"#extm3u")):
                                return False
                        written += len(chunk)
                        if written > self.max_single_file_bytes:
                            return False
                        f.write(chunk)
                        if self._urgent_active_fn is not None and self._urgent_active_fn():
                            time.sleep(0.04)

            if written >= 32768 and not self._closing:
                os.replace(part, target)
                self.enforce_size_limit()
                return True
        except Exception:
            pass
        finally:
            if os.path.exists(part):
                try:
                    os.remove(part)
                except Exception:
                    pass
        return False

    def enforce_size_limit(self) -> None:
        try:
            entries: list[tuple[float, int, str]] = []
            total = 0
            for name in os.listdir(self.cache_dir):
                if not name.endswith(".aud"):
                    continue
                fp = os.path.join(self.cache_dir, name)
                try:
                    st = os.stat(fp)
                    entries.append((st.st_mtime, st.st_size, fp))
                    total += st.st_size
                except OSError:
                    continue
            if total <= self.max_bytes:
                return
            entries.sort(key=lambda x: x[0])  # Старые первыми
            for _mtime, sz, fp in entries:
                if total <= int(self.max_bytes * 0.82):
                    break
                try:
                    os.remove(fp)
                    total -= sz
                except OSError:
                    pass
        except Exception:
            pass

    def close(self) -> None:
        self._closing = True
        try:
            self._task_q.put_nowait(None)
        except queue.Full:
            pass
        if self._session is not None:
            try:
                self._session.close()
            except Exception:
                pass


class PriorityTrackResolver:
    """
    Приоритетный потокобезопасный резолвер аудиопотоков с глобальной дедупликацией по uid.
    Гарантирует, что трек, на который кликнул пользователь (priority=0), никогда не стоит
    в очереди за фоновым префетчем (priority=2), а устаревшие фоновые задачи пропускаются.
    """

    PRIORITY_URGENT = 0
    PRIORITY_NEXT = 1
    PRIORITY_PREFETCH = 2

    def __init__(
        self,
        resolve_fn: Callable[[Any, bool], str | None],
        num_workers: int = 3,
        on_resolved_cb: Callable[[Any, str, int], None] | None = None,
    ):
        self._resolve_fn = resolve_fn
        self._on_resolved_cb = on_resolved_cb
        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._heap: list[tuple[int, int, int, Any, bool, Callable[[str | None], None] | None]] = []
        self._seq = 0
        self._prefetch_gen = 0
        self._urgent_inflight = 0
        self._inflight_by_uid: dict[str, threading.Event] = {}
        self._results_by_uid: dict[str, str | None] = {}
        self._closing = False

        self._workers: list[threading.Thread] = []
        for idx in range(max(1, int(num_workers))):
            t = threading.Thread(target=self._worker_loop, daemon=True, name=f"resolver-{idx}")
            t.start()
            self._workers.append(t)

    @property
    def has_urgent_work(self) -> bool:
        return self._urgent_inflight > 0

    def bump_prefetch_generation(self) -> int:
        with self._lock:
            self._prefetch_gen += 1
            return self._prefetch_gen

    def submit(
        self,
        track: Any,
        priority: int = PRIORITY_PREFETCH,
        force_refresh: bool = False,
        on_done: Callable[[str | None], None] | None = None,
        gen: int | None = None,
    ) -> None:
        if self._closing or track is None:
            return
        with self._cond:
            self._seq += 1
            use_gen = self._prefetch_gen if gen is None else gen
            heapq.heappush(self._heap, (int(priority), self._seq, use_gen, track, bool(force_refresh), on_done))
            self._cond.notify()

    def _run_deduped(self, track: Any, force_refresh: bool, priority: int) -> str | None:
        uid = getattr(track, "uid", "") or str(id(track))
        with self._lock:
            existing_ev = self._inflight_by_uid.get(uid)
            if existing_ev is not None and not force_refresh:
                owner = False
                ev = existing_ev
            else:
                ev = threading.Event()
                self._inflight_by_uid[uid] = ev
                owner = True
                if priority == self.PRIORITY_URGENT:
                    self._urgent_inflight += 1

        if not owner:
            ev.wait(timeout=18.0)
            with self._lock:
                url = self._results_by_uid.get(uid)
            if url and hasattr(track, "stream_url") and not track.stream_url:
                track.stream_url = url
                track.stream_resolved_at = time.time()
            return url

        url: str | None = None
        try:
            url = self._resolve_fn(track, force_refresh)
            if url and self._on_resolved_cb is not None:
                try:
                    self._on_resolved_cb(track, url, priority)
                except Exception:
                    pass
        except Exception:
            url = None
        finally:
            with self._lock:
                self._results_by_uid[uid] = url
                if len(self._results_by_uid) > 512:
                    # Удаляем старейшую запись
                    self._results_by_uid.pop(next(iter(self._results_by_uid)), None)
                if self._inflight_by_uid.get(uid) is ev:
                    self._inflight_by_uid.pop(uid, None)
                if priority == self.PRIORITY_URGENT and self._urgent_inflight > 0:
                    self._urgent_inflight -= 1
                ev.set()

        return url

    def _worker_loop(self) -> None:
        while True:
            with self._cond:
                while not self._heap and not self._closing:
                    self._cond.wait(timeout=0.4)
                if self._closing:
                    return
                priority, _seq, gen, track, force_refresh, on_done = heapq.heappop(self._heap)
                cur_gen = self._prefetch_gen

            # Пропускаем устаревшие низкоприоритетные префетчи
            if priority >= self.PRIORITY_PREFETCH and gen != cur_gen:
                continue

            # Если прямо сейчас идет срочный резолв клика пользователя — слегка притормаживаем фоновый префетч
            if priority >= self.PRIORITY_PREFETCH and self._urgent_inflight > 0:
                time.sleep(0.15)
                with self._lock:
                    if self._closing or gen != self._prefetch_gen:
                        continue

            url = self._run_deduped(track, force_refresh, priority)
            if on_done is not None and not self._closing:
                try:
                    on_done(url)
                except Exception:
                    pass

    def close(self) -> None:
        with self._cond:
            self._closing = True
            self._heap.clear()
            self._cond.notify_all()


class PCMStreamBuffer:
    """
    Ограниченный producer-consumer буфер для PCM-чанков между reader-потоком ffmpeg
    и audio-выводом sounddevice. Убирает щелчки и микрофризы звука при сетевых скачках и UI-скролле.
    """

    def __init__(self, max_chunks: int = 56):
        self._q: queue.Queue[bytes | None] = queue.Queue(maxsize=max(4, int(max_chunks)))
        self._closed = False
        self._eof = False

    def put_chunk(self, data: bytes, timeout: float = 0.25) -> bool:
        while not self._closed:
            try:
                self._q.put(data, timeout=timeout)
                return True
            except queue.Full:
                continue
        return False

    def mark_eof(self) -> None:
        self._eof = True
        while not self._closed:
            try:
                self._q.put(None, timeout=0.1)
                return
            except queue.Full:
                try:
                    self._q.get_nowait()
                except queue.Empty:
                    pass

    def get_chunk(self, timeout: float = 0.2) -> tuple[bool, bytes | None]:
        """Возвращает (has_item, chunk). Если has_item=True и chunk=None — достигнут EOF."""
        try:
            item = self._q.get(timeout=timeout)
            return True, item
        except queue.Empty:
            return False, None

    def close(self) -> None:
        self._closed = True
        while True:
            try:
                self._q.get_nowait()
            except queue.Empty:
                break
        try:
            self._q.put_nowait(None)
        except queue.Full:
            pass
