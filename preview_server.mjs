import http from "node:http";
import { execFile } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const PORT = Number(process.env.PORT || 3000);
const HOST = "0.0.0.0";

function runPython(args, timeoutMs = 20000) {
  return new Promise((resolve) => {
    const started = performance.now();
    execFile(
      "python3",
      args,
      { cwd: __dirname, timeout: timeoutMs, maxBuffer: 2 * 1024 * 1024 },
      (err, stdout, stderr) => {
        const durationMs = Math.round(performance.now() - started);
        resolve({
          ok: !err,
          code: err && typeof err.code === "number" ? err.code : err ? 1 : 0,
          durationMs,
          stdout: String(stdout || ""),
          stderr: String(stderr || ""),
        });
      }
    );
  });
}

function getProjectStats() {
  const files = [
    { name: "main.py", role: "GUI, StreamAudioEngine, CoverCacheManager, My Wave, Player" },
    { name: "perf_core.py", role: "StreamURLCache, AudioFileCache, PriorityTrackResolver, PCMStreamBuffer, DebouncedStateWriter" },
    { name: "google_sync.py", role: "Google OAuth PKCE + Drive appDataFolder sync (non-blocking sign-out)" },
    { name: "tray_manager.py", role: "System tray integration with state-change deduplication" },
    { name: "tests/test_aether_core.py", role: "Unit tests for OAuth, sync, perf_core, tray & main.py AST" },
  ];
  return files.map((f) => {
    const fp = path.join(__dirname, f.name);
    let lines = 0;
    let bytes = 0;
    try {
      const content = fs.readFileSync(fp, "utf-8");
      lines = content.split("\n").length;
      bytes = Buffer.byteLength(content, "utf-8");
    } catch {
      // ignore
    }
    return { ...f, lines, bytes };
  });
}

function parseUnittestOutput(raw) {
  const cases = [];
  const lineRe = /^(\S+)\s+\(([^)]+)\)\s+\.\.\.\s+(ok|FAIL|ERROR|skipped.*)$/;
  for (const line of raw.split(/\r?\n/)) {
    const m = line.trim().match(lineRe);
    if (m) {
      cases.push({
        name: m[1],
        suite: m[2].split(".").slice(-2, -1)[0] || m[2],
        status: m[3].startsWith("ok") ? "ok" : m[3],
      });
    }
  }
  const summaryMatch = raw.match(/Ran\s+(\d+)\s+tests?\s+in\s+([\d.]+)s/);
  return {
    cases,
    total: summaryMatch ? Number(summaryMatch[1]) : cases.length,
    reportedSec: summaryMatch ? Number(summaryMatch[2]) : null,
  };
}

const BENCHMARK_SCRIPT = `
import json, os, tempfile, threading, time
from perf_core import (
    AudioFileCache,
    DebouncedStateWriter,
    PCMStreamBuffer,
    PriorityTrackResolver,
    StreamURLCache,
    pick_optimal_thumbnail,
)

results = []

# 1. StreamURLCache throughput
with tempfile.TemporaryDirectory() as td:
    cache = StreamURLCache(cache_file=os.path.join(td, "sc.json"), max_items=500)
    now = time.time()
    exp = int(now + 7200)
    t0 = time.perf_counter()
    for i in range(500):
        cache.put(f"yt:{i}", f"https://rr1.googlevideo.com/vp?expire={exp}&id={i}", duration=195.0, now=now)
    hits = 0
    for _ in range(10):
        for i in range(500):
            if cache.get(f"yt:{i}", now=now) is not None:
                hits += 1
    dt_ms = (time.perf_counter() - t0) * 1000.0
    cache.close()
    results.append({
        "id": "stream_cache",
        "title": "StreamURLCache (500 записей + 5000 LRU-чтений)",
        "metric": f"{dt_ms:.2f} мс",
        "detail": f"{hits}/5000 попаданий, ~{(dt_ms / 5000.0) * 1000.0:.2f} мкс/запрос",
        "passed": hits == 5000,
    })

# 2. PriorityTrackResolver preemption + dedup
class DummyTrack:
    def __init__(self, uid):
        self.uid = uid
        self.stream_url = None
        self.stream_resolved_at = 0.0

order = []
dedup_calls = 0
lock = threading.Lock()
gate = threading.Event()
started = threading.Event()

def fake_resolve(track, force_refresh):
    global dedup_calls
    if track.uid == "yt:hold":
        started.set()
        gate.wait(timeout=1.0)
        return "https://s/hold"
    if track.uid == "yt:dup":
        with lock:
            dedup_calls += 1
        time.sleep(0.03)
        return "https://s/dup"
    time.sleep(0.01)
    return f"https://s/{track.uid}"

res1 = PriorityTrackResolver(fake_resolve, num_workers=1)
ev_u, ev_l = threading.Event(), threading.Event()
res1.submit(DummyTrack("yt:hold"), priority=PriorityTrackResolver.PRIORITY_PREFETCH)
started.wait(timeout=1.0)
res1.submit(DummyTrack("yt:low"), priority=PriorityTrackResolver.PRIORITY_PREFETCH, on_done=lambda _: (order.append("prefetch"), ev_l.set()))
res1.submit(DummyTrack("yt:urgent"), priority=PriorityTrackResolver.PRIORITY_URGENT, on_done=lambda _: (order.append("urgent"), ev_u.set()))
t0 = time.perf_counter()
gate.set()
ev_u.wait(timeout=1.0)
urgent_ms = (time.perf_counter() - t0) * 1000.0
ev_l.wait(timeout=1.0)
res1.close()

res2 = PriorityTrackResolver(fake_resolve, num_workers=4)
evs = [threading.Event() for _ in range(4)]
for ev in evs:
    res2.submit(DummyTrack("yt:dup"), priority=PriorityTrackResolver.PRIORITY_URGENT, on_done=lambda _, e=ev: e.set())
for ev in evs:
    ev.wait(timeout=1.0)
res2.close()

results.append({
    "id": "priority_resolver",
    "title": "PriorityTrackResolver (приоритет клика + дедупликация)",
    "metric": f"{urgent_ms:.1f} мс",
    "detail": f"Порядок: {' → '.join(order)}; 4 параллельных запроса → {dedup_calls} вызов yt-dlp",
    "passed": order == ["urgent", "prefetch"] and dedup_calls == 1,
})

# 3. PCMStreamBuffer throughput (1000 x 8KB = ~8MB PCM)
buf = PCMStreamBuffer(max_chunks=56)
chunk = b"\\x00\\x01" * 4096
total_bytes = 0
t0 = time.perf_counter()
def producer():
    for _ in range(1000):
        buf.put_chunk(chunk)
    buf.mark_eof()
pt = threading.Thread(target=producer, daemon=True)
pt.start()
while True:
    ok, data = buf.get_chunk(timeout=1.0)
    if not ok or data is None:
        break
    total_bytes += len(data)
pt.join(timeout=1.0)
dt_sec = max(1e-6, time.perf_counter() - t0)
mb_s = (total_bytes / (1024 * 1024)) / dt_sec
results.append({
    "id": "pcm_buffer",
    "title": "PCMStreamBuffer (1000 чанков × 8 КБ)",
    "metric": f"{mb_s:.0f} МБ/с",
    "detail": f"Передано {total_bytes // (1024*1024)} МБ PCM за {dt_sec * 1000:.2f} мс без потерь",
    "passed": total_bytes == 1000 * len(chunk),
})

# 4. DebouncedStateWriter coalescing
with tempfile.TemporaryDirectory() as td:
    fp = os.path.join(td, "state.json")
    writer = DebouncedStateWriter(delay_sec=0.15)
    t0 = time.perf_counter()
    for i in range(50):
        writer.schedule([(fp, {"likes": i, "volume": 0.8})])
    sched_ms = (time.perf_counter() - t0) * 1000.0
    writer.flush_sync()
    with open(fp, "r", encoding="utf-8") as f:
        final = json.load(f)
    writer.close()
    results.append({
        "id": "state_writer",
        "title": "DebouncedStateWriter (50 событий → 1 запись)",
        "metric": f"{sched_ms:.2f} мс",
        "detail": f"Время на UI-потоке за 50 сохранений; итоговое значение likes={final.get('likes')}",
        "passed": final.get("likes") == 49,
    })

# 5. Thumbnail selection
thumbs = [
    {"url": "https://i.ytimg.com/vi/abc/default.jpg", "width": 120, "height": 90},
    {"url": "https://i.ytimg.com/vi/abc/mqdefault.webp", "width": 320, "height": 180},
    {"url": "https://i.ytimg.com/vi/abc/mqdefault.jpg", "width": 320, "height": 180},
    {"url": "https://i.ytimg.com/vi/abc/maxresdefault.jpg", "width": 1280, "height": 720},
]
picked = pick_optimal_thumbnail(thumbs, video_id="abc")
results.append({
    "id": "thumb_picker",
    "title": "pick_optimal_thumbnail (экономия трафика обложек)",
    "metric": "320px JPG",
    "detail": f"Выбрана {picked.split('/')[-1]} вместо maxresdefault.jpg (1280px)",
    "passed": picked.endswith("mqdefault.jpg"),
})

print(json.dumps({"results": results}, ensure_ascii=False))
`;

const HTML_PAGE = `<!doctype html>
<html lang="ru">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Aether Music — Оптимизация и диагностика</title>
  <style>
    :root {
      --bg: #0b0d12;
      --panel: #121620;
      --panel-2: #171c28;
      --border: #232a3b;
      --text: #eef1f7;
      --muted: #94a0b8;
      --gold: #ebb964;
      --gold-soft: rgba(235, 185, 100, 0.12);
      --green: #3dd68c;
      --green-soft: rgba(61, 214, 140, 0.12);
      --red: #f26d6d;
      --red-soft: rgba(242, 109, 109, 0.12);
      --mono: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
      --sans: -apple-system, BlinkMacSystemFont, "Segoe UI", Inter, Roboto, sans-serif;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      background: var(--bg);
      color: var(--text);
      font-family: var(--sans);
      line-height: 1.5;
      -webkit-font-smoothing: antialiased;
    }
    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      padding: 28px 22px 56px;
    }
    .topbar {
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding-bottom: 22px;
      border-bottom: 1px solid var(--border);
      margin-bottom: 24px;
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 14px;
    }
    .logo {
      width: 44px;
      height: 44px;
      border-radius: 12px;
      background: linear-gradient(135deg, #f3ca7b, #c58b32);
      color: #0b0d12;
      font-weight: 800;
      font-size: 20px;
      display: grid;
      place-items: center;
      letter-spacing: -0.02em;
    }
    h1 {
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: -0.02em;
    }
    .subtitle {
      margin: 2px 0 0;
      color: var(--muted);
      font-size: 13.5px;
    }
    .actions {
      display: flex;
      gap: 10px;
      flex-wrap: wrap;
    }
    button {
      border: 1px solid var(--border);
      background: var(--panel-2);
      color: var(--text);
      padding: 9px 15px;
      border-radius: 9px;
      font-size: 13.5px;
      font-weight: 600;
      cursor: pointer;
      transition: background 0.15s ease, border-color 0.15s ease, transform 0.08s ease;
    }
    button:hover { background: #1e2536; border-color: #354059; }
    button:active { transform: translateY(1px); }
    button.primary {
      background: var(--gold);
      color: #111318;
      border-color: var(--gold);
    }
    button.primary:hover { background: #f3c87a; }
    button:disabled { opacity: 0.55; cursor: wait; }

    .notice {
      background: var(--panel);
      border: 1px solid var(--border);
      border-left: 3px solid var(--gold);
      border-radius: 10px;
      padding: 14px 16px;
      margin-bottom: 22px;
      font-size: 13.5px;
      color: var(--muted);
    }
    .notice strong { color: var(--text); }
    .notice code {
      font-family: var(--mono);
      background: #0c0f16;
      border: 1px solid var(--border);
      padding: 1px 6px;
      border-radius: 5px;
      color: var(--gold);
      font-size: 12.5px;
    }

    .grid {
      display: grid;
      grid-template-columns: repeat(12, 1fr);
      gap: 18px;
    }
    .col-7 { grid-column: span 7; }
    .col-5 { grid-column: span 5; }
    .col-6 { grid-column: span 6; }
    .col-12 { grid-column: span 12; }
    @media (max-width: 900px) {
      .col-7, .col-5, .col-6 { grid-column: span 12; }
    }

    .card {
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 12px;
      padding: 18px 20px;
    }
    .card-head {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin-bottom: 14px;
    }
    .card-title {
      margin: 0;
      font-size: 15px;
      font-weight: 700;
    }
    .badge {
      font-family: var(--mono);
      font-size: 12px;
      font-weight: 600;
      padding: 3px 9px;
      border-radius: 6px;
      background: var(--gold-soft);
      color: var(--gold);
    }
    .badge.ok { background: var(--green-soft); color: var(--green); }
    .badge.err { background: var(--red-soft); color: var(--red); }

    .opt-list {
      margin: 0;
      padding: 0;
      list-style: none;
      display: grid;
      gap: 10px;
    }
    .opt-item {
      padding: 10px 12px;
      border-radius: 9px;
      background: var(--panel-2);
      border: 1px solid var(--border);
    }
    .opt-item b {
      display: block;
      font-size: 13.5px;
      color: var(--text);
      margin-bottom: 2px;
    }
    .opt-item span {
      font-size: 12.5px;
      color: var(--muted);
    }

    .test-list {
      display: grid;
      gap: 6px;
      max-height: 410px;
      overflow-y: auto;
      padding-right: 4px;
    }
    .test-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 10px;
      padding: 8px 11px;
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 8px;
      font-size: 12.5px;
    }
    .test-name {
      font-family: var(--mono);
      color: var(--text);
      overflow: hidden;
      text-overflow: ellipsis;
      white-space: nowrap;
    }
    .test-suite {
      color: var(--muted);
      font-size: 11.5px;
      display: block;
    }
    .status-dot {
      font-family: var(--mono);
      font-weight: 700;
      font-size: 11.5px;
      padding: 2px 7px;
      border-radius: 5px;
      flex-shrink: 0;
    }
    .status-dot.ok { background: var(--green-soft); color: var(--green); }
    .status-dot.fail { background: var(--red-soft); color: var(--red); }

    .bench-list {
      display: grid;
      gap: 10px;
    }
    .bench-row {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 14px;
      padding: 11px 13px;
      background: var(--panel-2);
      border: 1px solid var(--border);
      border-radius: 9px;
    }
    .bench-metric {
      font-family: var(--mono);
      font-size: 15px;
      font-weight: 700;
      color: var(--gold);
      white-space: nowrap;
    }

    table {
      width: 100%;
      border-collapse: collapse;
      font-size: 13px;
    }
    th, td {
      text-align: left;
      padding: 9px 10px;
      border-bottom: 1px solid var(--border);
    }
    th {
      color: var(--muted);
      font-weight: 600;
      font-size: 12px;
      text-transform: uppercase;
      letter-spacing: 0.04em;
    }
    td.mono { font-family: var(--mono); color: var(--gold); }
    .muted { color: var(--muted); }
  </style>
</head>
<body>
  <div class="wrap">
    <header class="topbar">
      <div class="brand">
        <div class="logo">Æ</div>
        <div>
          <h1>Aether Music — Панель диагностики</h1>
          <p class="subtitle">Десктопный Python-плеер (CustomTkinter + ffmpeg + yt-dlp) · Оптимизация и живые тесты</p>
        </div>
      </div>
      <div class="actions">
        <button id="btnTests" class="primary">Запустить Python-тесты</button>
        <button id="btnBench">Запустить бенчмарк perf_core</button>
      </div>
    </header>

    <div class="notice">
      <strong>Почему здесь панель диагностики, а не окно плеера:</strong>
      Aether Music — нативное десктопное приложение на Python/Tkinter (собирается в <code>AetherPlayer.exe</code> через GitHub Actions).
      Этот сервер превью работает на порту <code>3000</code>, не затрагивает Python-код и позволяет прямо здесь прогонять реальные тесты и замеры производительности <code>perf_core.py</code>.
      Локальный запуск на ПК: <code>pip install -r requirements.txt &amp;&amp; python main.py</code>
    </div>

    <div class="grid">
      <section class="card col-7">
        <div class="card-head">
          <h2 class="card-title">Автотесты (unittest + py_compile)</h2>
          <span id="testBadge" class="badge">Загрузка…</span>
        </div>
        <div id="testList" class="test-list"></div>
      </section>

      <section class="card col-5">
        <div class="card-head">
          <h2 class="card-title">Живой бенчмарк perf_core.py</h2>
          <span id="benchBadge" class="badge">Загрузка…</span>
        </div>
        <div id="benchList" class="bench-list"></div>
      </section>

      <section class="card col-6">
        <div class="card-head">
          <h2 class="card-title">Стабильное воспроизведение и быстрый старт</h2>
          <span class="badge">Playback</span>
        </div>
        <ul class="opt-list">
          <li class="opt-item">
            <b>Штатная цепочка yt-dlp + передача HTTP-заголовков в ffmpeg</b>
            <span>Убраны ломающие флаги (android_vr, -probesize, -rw_timeout); в ffmpeg и кэш передаются рабочие заголовки + авто-фоллбэк safe_mode.</span>
          </li>
          <li class="opt-item">
            <b>StreamURLCache v2 + кэширование следующего трека (AudioFileCache)</b>
            <span>Старые битые v1-ссылки автоматически игнорируются; в фоне скачивается только следующий трек, не отнимая канал у текущего.</span>
          </li>
          <li class="opt-item">
            <b>Приоритетный резолвер с дедупликацией по uid</b>
            <span>Клик пользователя (PRIORITY_URGENT) всегда обгоняет фоновый префетч, а дубликаты одного трека резолвятся ровно 1 раз.</span>
          </li>
          <li class="opt-item">
            <b>Чистый PortAudio-поток + PCM-буфер опережения (~2.2 с)</b>
            <span>Убран проблемный low-latency/cross-thread вывод (совместимо с Bluetooth/WASAPI/MME), чтение из ffmpeg буферизуется отдельно.</span>
          </li>
        </ul>
      </section>

      <section class="card col-6">
        <div class="card-head">
          <h2 class="card-title">Плавный ресайз окна без лагов</h2>
          <span class="badge">UI Smoothness</span>
        </div>
        <ul class="opt-list">
          <li class="opt-item">
            <b>Нативный FastTrackScrollFrame вместо CTkScrollableFrame</b>
            <span>Коалесцированный пересчет ширины (18 мс) и ленивый scrollregion полностью убирают рывки при растягивании окна.</span>
          </li>
          <li class="opt-item">
            <b>Нативные строки треков (tk.Frame + tk.Label)</b>
            <span>0 CTkCanvas-виджетов внутри списка треков и нативные внешние контейнеры — расширение окна больше не вызывает сотни перерисовок.</span>
          </li>
          <li class="opt-item">
            <b>Пауза _tick_loop на время ресайза + порционный рендер</b>
            <span>При перетаскивании краёв окна таймер уступает перерисовке, а длинные списки, обложки и ffmpeg-reaper работают без фризов.</span>
          </li>
          <li class="opt-item">
            <b>Дебаунс сохранения, миниатюры 320px и пауза FFT в трее</b>
            <span>state.json пишется атомарно в фоне, загружаются компактные mqdefault.jpg, а выход из Google и трей не блокируют UI.</span>
          </li>
        </ul>
      </section>

      <section class="card col-12">
        <div class="card-head">
          <h2 class="card-title">Модули Python-проекта (полностью сохранены для PyInstaller)</h2>
          <span id="pyBadge" class="badge">Python</span>
        </div>
        <table>
          <thead>
            <tr>
              <th>Файл</th>
              <th>Назначение</th>
              <th>Строк</th>
              <th>Размер</th>
            </tr>
          </thead>
          <tbody id="filesBody"></tbody>
        </table>
      </section>
    </div>
  </div>

  <script>
    const btnTests = document.getElementById("btnTests");
    const btnBench = document.getElementById("btnBench");
    const testBadge = document.getElementById("testBadge");
    const testList = document.getElementById("testList");
    const benchBadge = document.getElementById("benchBadge");
    const benchList = document.getElementById("benchList");
    const filesBody = document.getElementById("filesBody");
    const pyBadge = document.getElementById("pyBadge");

    function esc(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
    }

    async function loadInfo() {
      const res = await fetch("/api/info");
      const data = await res.json();
      pyBadge.textContent = data.pythonVersion || "Python 3";
      filesBody.innerHTML = (data.files || []).map((f) => \`
        <tr>
          <td class="mono">\${esc(f.name)}</td>
          <td class="muted">\${esc(f.role)}</td>
          <td>\${f.lines}</td>
          <td class="muted">\${(f.bytes / 1024).toFixed(1)} КБ</td>
        </tr>
      \`).join("");
    }

    async function runTests() {
      btnTests.disabled = true;
      testBadge.className = "badge";
      testBadge.textContent = "Выполнение…";
      try {
        const res = await fetch("/api/run-tests", { method: "POST" });
        const data = await res.json();
        const ok = data.compileOk && data.testsOk;
        testBadge.className = "badge " + (ok ? "ok" : "err");
        testBadge.textContent = ok
          ? \`\${data.passed}/\${data.total} OK · \${data.durationMs} мс\`
          : \`Ошибка (\${data.passed}/\${data.total})\`;

        testList.innerHTML = (data.cases || []).map((c) => \`
          <div class="test-row">
            <div style="min-width:0">
              <div class="test-name">\${esc(c.name)}</div>
              <span class="test-suite">\${esc(c.suite)}</span>
            </div>
            <span class="status-dot \${c.status === "ok" ? "ok" : "fail"}">\${esc(c.status.toUpperCase())}</span>
          </div>
        \`).join("") || \`<div class="muted">\${esc(data.rawOutput || "Нет вывода")}</div>\`;
      } catch (e) {
        testBadge.className = "badge err";
        testBadge.textContent = "Ошибка запроса";
      } finally {
        btnTests.disabled = false;
      }
    }

    async function runBenchmark() {
      btnBench.disabled = true;
      benchBadge.className = "badge";
      benchBadge.textContent = "Замер…";
      try {
        const res = await fetch("/api/run-benchmark", { method: "POST" });
        const data = await res.json();
        const allOk = data.ok && (data.results || []).every((r) => r.passed);
        benchBadge.className = "badge " + (allOk ? "ok" : "err");
        benchBadge.textContent = allOk ? \`Готово · \${data.durationMs} мс\` : "Ошибка замера";
        benchList.innerHTML = (data.results || []).map((r) => \`
          <div class="bench-row">
            <div>
              <div style="font-size:13px;font-weight:600">\${esc(r.title)}</div>
              <div class="muted" style="font-size:12px">\${esc(r.detail)}</div>
            </div>
            <div class="bench-metric">\${esc(r.metric)}</div>
          </div>
        \`).join("");
      } catch {
        benchBadge.className = "badge err";
        benchBadge.textContent = "Ошибка запроса";
      } finally {
        btnBench.disabled = false;
      }
    }

    btnTests.addEventListener("click", runTests);
    btnBench.addEventListener("click", runBenchmark);

    loadInfo();
    runTests();
    runBenchmark();
  </script>
</body>
</html>`;

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url || "/", `http://${req.headers.host || "localhost"}`);

  if (req.method === "GET" && (url.pathname === "/" || url.pathname === "/index.html")) {
    res.writeHead(200, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" });
    res.end(HTML_PAGE);
    return;
  }

  if (req.method === "GET" && url.pathname === "/api/info") {
    const py = await runPython(["--version"], 5000);
    const payload = {
      pythonVersion: (py.stdout || py.stderr).trim(),
      files: getProjectStats(),
    };
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(JSON.stringify(payload));
    return;
  }

  if (req.method === "POST" && url.pathname === "/api/run-tests") {
    const compile = await runPython([
      "-m",
      "py_compile",
      "main.py",
      "perf_core.py",
      "google_sync.py",
      "tray_manager.py",
      "tests/test_aether_core.py",
    ]);
    const tests = await runPython(["-m", "unittest", "discover", "-s", "tests", "-v"]);
    const combined = `${tests.stdout}\n${tests.stderr}`.trim();
    const parsed = parseUnittestOutput(combined);
    const passed = parsed.cases.filter((c) => c.status === "ok").length;
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify({
        compileOk: compile.ok,
        testsOk: tests.ok,
        durationMs: compile.durationMs + tests.durationMs,
        passed,
        total: parsed.total,
        cases: parsed.cases,
        rawOutput: combined,
      })
    );
    return;
  }

  if (req.method === "POST" && url.pathname === "/api/run-benchmark") {
    const bench = await runPython(["-c", BENCHMARK_SCRIPT], 20000);
    let results = [];
    try {
      results = JSON.parse(bench.stdout).results || [];
    } catch {
      results = [];
    }
    res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
    res.end(
      JSON.stringify({
        ok: bench.ok && results.length > 0,
        durationMs: bench.durationMs,
        results,
        error: bench.ok ? null : bench.stderr,
      })
    );
    return;
  }

  res.writeHead(404, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({ error: "Not Found" }));
});

server.listen(PORT, HOST, () => {
  console.log(`Aether Music Diagnostics Preview listening on http://${HOST}:${PORT}`);
});
