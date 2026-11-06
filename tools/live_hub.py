"""实时控制台 —— 把"我正在干什么"变成一条可以边跑边看的网页。

为什么要它
----------
音乐这条流水线单次要跑几分钟（扒谱、抽采样、逐轨渲染、混音、导出），
而这段时间里外面什么都看不见：只有一个转圈的终端，跑完了才知道结果。
这个模块把每个阶段的进度、读数、波形、频谱实时推给浏览器，
于是"听不出好在哪、也不知道卡在哪"这两件事同时被解决。

三件事，一个模块
----------------
1. **事件总线**：``hub.log() / hub.stage() / hub.progress() / hub.metric() / hub.wave()``
   谁都能往里推事件，不需要关心有几个人在看。
2. **HTTP + SSE 服务**：127.0.0.1 上的小服务器，``/`` 是控制台页面，
   ``/events`` 是 Server-Sent Events 流，``/api/state`` 是快照，``/audio?f=`` 放试听。
3. **产物试听**：渲染出来的 WAV 直接在网页里播，不用去翻文件夹。

只绑 127.0.0.1，不做任何鉴权 —— 它是本机自己看的窗口，不是服务。
（仍然把文件读取限制在 ``_out`` 之下：一个能读任意路径的 HTTP 端点，
 哪怕只在本机，也是白送的漏洞。）
"""

from __future__ import annotations

import json
import math
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

#: 允许通过 /audio 读出去的目录（相对仓库根）。文件服务器只认这底下。
_ALLOWED_ROOT = Path(__file__).resolve().parents[1] / "_out"

DEFAULT_PORT = 8770


# ==========================================================================
# 事件中枢
# ==========================================================================


class LiveHub:
    """线程安全的事件中枢 + SSE 服务器。全进程一个就够（见文件底部的 ``hub()``）。"""

    def __init__(self, *, port: int = DEFAULT_PORT, title: str = "制乐 · 实时控制台",
                 history: int = 400) -> None:
        self.port = int(port)
        self.title = title
        self._lock = threading.RLock()
        self._clients: set[queue.Queue] = set()
        self._history: list[dict] = []
        self._history_max = int(history)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._t0 = time.time()
        self._stages: dict[str, dict] = {}
        self._metrics: dict[str, str] = {}
        self._status = "空闲"
        self._note = ""
        self._files: list[dict] = []
        self._wave: dict | None = None
        self._spec: list[float] | None = None
        self.url: str | None = None

    # -- 生命周期 --------------------------------------------------------

    def start(self) -> str:
        """起服务器（幂等）。返回控制台 URL。"""
        with self._lock:
            if self.url:
                return self.url
        for candidate in range(self.port, self.port + 12):
            try:
                server = ThreadingHTTPServer(("127.0.0.1", candidate), _make_handler(self))
            except OSError:
                continue
            server.daemon_threads = True
            self._server = server
            self.port = candidate
            self.url = f"http://127.0.0.1:{candidate}/"
            self._thread = threading.Thread(target=server.serve_forever,
                                            kwargs={"poll_interval": 0.2},
                                            name="live-hub", daemon=True)
            self._thread.start()
            self.log(f"实时控制台已启动：{self.url}", level="ok")
            return self.url
        raise OSError(f"{self.port}~{self.port + 11} 都被占用，起不了控制台")

    def stop(self) -> None:
        with self._lock:
            server, self._server = self._server, None
            self.url = None
        if server is not None:
            server.shutdown()
            server.server_close()

    @property
    def running(self) -> bool:
        return self._server is not None

    # -- 推事件 ----------------------------------------------------------

    def _emit(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        payload = json.dumps(event, ensure_ascii=False, default=str)
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_max:
                del self._history[:len(self._history) - self._history_max]
            dead = []
            for client in self._clients:
                try:
                    client.put_nowait(payload)
                except queue.Full:
                    dead.append(client)
            for client in dead:
                self._clients.discard(client)

    def log(self, text: str, *, level: str = "info", **extra) -> None:
        """一行日志。``level`` ∈ info / ok / warn / err / step。"""
        self._emit({"kind": "log", "text": str(text), "level": level, **extra})

    def status(self, text: str, *, running: bool = True, note: str = "") -> None:
        with self._lock:
            self._status = str(text)
            self._note = str(note)
        self._emit({"kind": "status", "text": str(text), "running": bool(running),
                    "note": str(note)})

    def metric(self, key: str, value) -> None:
        with self._lock:
            self._metrics[str(key)] = str(value)
        self._emit({"kind": "metric", "key": str(key), "value": str(value)})

    def metrics(self, **pairs) -> None:
        for key, value in pairs.items():
            self.metric(key.replace("_", " "), value)

    def stage(self, sid: str, label: str, *, frac: float = 0.0, detail: str = "",
              done: bool = False) -> None:
        """登记/更新一个阶段。同名 id 会原地更新，不会重复长出来。"""
        frac = max(0.0, min(1.0, float(frac)))
        with self._lock:
            self._stages[sid] = {"id": sid, "label": str(label), "frac": frac,
                                 "detail": str(detail), "done": bool(done)}
        self._emit({"kind": "stage", "id": sid, "label": str(label), "frac": frac,
                    "detail": str(detail), "done": bool(done)})

    def progress(self, sid: str, frac: float, *, detail: str = "") -> None:
        """只更新进度，标签沿用上次的。"""
        with self._lock:
            cur = self._stages.get(sid, {"label": sid})
        self.stage(sid, cur.get("label", sid), frac=frac, detail=detail)

    def wave(self, audio, sr: int = 44100, *, buckets: int = 720,
             label: str = "") -> None:
        """把一段音频压成 (min,max) 包络推给前端画波形。"""
        try:
            import numpy as np
        except Exception:                                       # pragma: no cover
            return
        data = np.asarray(audio, dtype="float32")
        if data.ndim == 1:
            data = np.stack([data, data])
        if data.size == 0:
            return
        mono = data.mean(axis=0)
        n = max(1, int(buckets))
        step = max(1, mono.size // n)
        usable = (mono.size // step) * step
        frames = mono[:usable].reshape(-1, step)
        lo, hi = frames.min(axis=1), frames.max(axis=1)
        env = [[float(a), float(b)] for a, b in zip(lo, hi)]
        self._wave = {"env": env, "label": str(label)}
        self._emit({"kind": "wave", "env": env, "label": str(label)})

    def spectrum(self, audio, sr: int = 44100, *, points: int = 160,
                 label: str = "") -> None:
        """推一条频谱曲线（dB，归一化到 0~1）给前端画柱子。"""
        try:
            import numpy as np
        except Exception:                                       # pragma: no cover
            return
        data = np.asarray(audio, dtype="float32")
        mono = data.mean(axis=0) if data.ndim == 2 else data
        if mono.size < 4096:
            return
        win = np.hanning(mono.size)
        mag = np.abs(np.fft.rfft(mono * win))
        freqs = np.fft.rfftfreq(mono.size, 1.0 / sr)
        # 对数频率分箱：听感上等距，而不是线性频率上等距
        edges = np.geomspace(40.0, min(16000.0, sr / 2.0), int(points) + 1)
        out: list[float] = []
        for i in range(int(points)):
            m = (freqs >= edges[i]) & (freqs < edges[i + 1])
            out.append(float(mag[m].mean()) if m.any() else 0.0)
        peak = max(out) or 1e-9
        curve = [round(float(20 * math.log10(max(v, peak * 1e-4) / peak)), 1) for v in out]
        self._spec = curve
        self._emit({"kind": "spec", "curve": curve, "label": str(label)})

    def file(self, path: str | Path, *, label: str = "", kind: str = "audio") -> None:
        """登记一个可试听的产物（同时给出 /audio 链接）。"""
        p = Path(path)
        try:
            rel = p.resolve().relative_to(_ALLOWED_ROOT)
        except ValueError:
            rel = None
        self.file_item({
            "name": p.name,
            "label": label or p.name,
            "kind": kind,
            "size": p.stat().st_size if p.exists() else 0,
            "url": f"/audio?f={rel.as_posix()}" if rel is not None else "",
        })

    def file_item(self, item: dict) -> None:
        """按已经算好的条目登记产物（跨进程转发走这条，路径不在本进程也无所谓）。"""
        with self._lock:
            self._files = [f for f in self._files if f["name"] != item.get("name")] + [item]
        self._emit({"kind": "file", "item": item})

    def remote_event(self, kind: str, payload: dict) -> None:
        """接收从**别的进程**转发来的一条事件（``POST /api/event``）。

        为什么要这条路：命令行跑在一次性的进程里，网页控制台是另一个常驻进程 ——
        进程内的事件总线跨不过去。所以让命令行把事件 POST 过来，面板照旧长在一块。

        注意这里**必须按 kind 分派到对应的方法**，不能直接 ``_emit``：
        状态（stages / metrics / wave / spec / files）是在那些方法里更新的，
        直接 _emit 的话事件推给了浏览器，但 ``/api/state`` 快照还是空的
        —— 刷新页面就全丢了（第一版就是这么错的）。
        """
        text = payload.pop("text", "")
        payload.pop("ts", None)
        if kind == "log":
            self.log(text, level=payload.pop("level", "info"))
        elif kind == "status":
            self.status(text, running=bool(payload.pop("running", True)),
                        note=payload.pop("note", ""))
        elif kind == "stage":
            self.stage(str(payload.pop("id", "?")), str(payload.pop("label", text)),
                       frac=payload.pop("frac", 0.0), detail=payload.pop("detail", ""),
                       done=payload.pop("done", False))
        elif kind == "metric":
            self.metric(payload.pop("key", "?"), payload.pop("value", ""))
        elif kind == "wave":
            env = payload.pop("env", [])
            with self._lock:
                self._wave = {"env": env, "label": payload.get("label", "")}
            self._emit({"kind": "wave", "env": env, "label": payload.get("label", "")})
        elif kind == "spec":
            curve = payload.pop("curve", [])
            with self._lock:
                self._spec = curve
            self._emit({"kind": "spec", "curve": curve,
                        "label": payload.get("label", "")})
        elif kind == "file":
            self.file_item(payload.pop("item", {}))
        else:
            self._emit({"kind": kind, "text": text, **payload})

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "kind": "hello",
                "title": self.title,
                "status": self._status,
                "note": self._note,
                "elapsed": time.time() - self._t0,
                "stages": list(self._stages.values()),
                "metrics": [{"key": k, "value": v} for k, v in self._metrics.items()],
                "files": list(self._files),
                "wave": self._wave,
                "spec": self._spec,
                "history": self._history[-120:],
            }

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=2000)
        with self._lock:
            self._clients.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(q)


# ==========================================================================
# HTTP / SSE
# ==========================================================================


def _make_handler(hub: LiveHub):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        server_version = "MusicStudioLive/1.0"

        # 默认的 stderr 日志会把每个请求都喷到终端，这里咽掉（事件本身走 hub.log）
        def log_message(self, fmt, *args):        # noqa: D102
            pass

        # -- 工具 ---------------------------------------------------------

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj) -> None:
            self._send(200, json.dumps(obj, ensure_ascii=False, default=str)
                       .encode("utf-8"), "application/json; charset=utf-8")

        # -- 路由 ---------------------------------------------------------

        def do_GET(self):                          # noqa: N802
            url = urlparse(self.path)
            path = url.path
            if path in ("/", "/index.html"):
                self._send(200, _PAGE.encode("utf-8"), "text/html; charset=utf-8")
            elif path == "/events":
                self._events()
            elif path == "/api/state":
                self._json(hub.snapshot())
            elif path == "/health":
                self._json({"ok": True, "clients": len(hub._clients)})
            elif path == "/audio":
                self._audio(parse_qs(url.query))
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        def do_POST(self):                         # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8") or "{}")
            except Exception:
                payload = {}
            if url.path == "/api/log":
                # 外部进程（比如 MCP 服务器）也能往控制台写一行
                hub.log(payload.get("text", ""), level=payload.get("level", "info"))
                self._json({"ok": True})
            elif url.path == "/api/event":
                # 跨进程事件入口：命令行跑在另一个进程里，事件照样能进这块面板
                kind = payload.pop("kind", "log")
                hub.remote_event(kind, payload)
                self._json({"ok": True})
            else:
                self._send(404, b"not found", "text/plain; charset=utf-8")

        # -- SSE ----------------------------------------------------------

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-transform")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q = hub.subscribe()
            try:
                self._write_event(json.dumps(hub.snapshot(), ensure_ascii=False,
                                             default=str))
                while True:
                    try:
                        payload = q.get(timeout=15.0)
                    except queue.Empty:
                        self.wfile.write(b": ping\n\n")     # 心跳，防中间层掐连接
                        self.wfile.flush()
                        continue
                    self._write_event(payload)
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                hub.unsubscribe(q)

        def _write_event(self, payload: str) -> None:
            self.wfile.write(b"data: " + payload.encode("utf-8") + b"\n\n")
            self.wfile.flush()

        # -- 音频 ---------------------------------------------------------

        def _audio(self, query: dict) -> None:
            name = (query.get("f") or [""])[0]
            target = (_ALLOWED_ROOT / name).resolve()
            try:
                target.relative_to(_ALLOWED_ROOT)          # 不许跳出 _out
            except ValueError:
                self._send(403, b"forbidden", "text/plain; charset=utf-8")
                return
            if not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            ctype = {"wav": "audio/wav", "mp3": "audio/mpeg", "png": "image/png",
                     "json": "application/json"}.get(target.suffix.lstrip(".").lower(),
                                                     "application/octet-stream")
            body = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Accept-Ranges", "none")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

    return Handler


# ==========================================================================
# 页面（单文件，不依赖任何 CDN —— 本机离线也要能看）
# ==========================================================================

_PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>制乐 · 实时控制台</title>
<style>
  :root{--bg:#0b1016;--card:rgba(255,255,255,.055);--line:rgba(255,255,255,.10);
        --fg:#e8eef5;--dim:#8fa1b3;--teal:#4fd1c5;--blue:#5aa9ff;--amber:#f6c177;
        --red:#ff7b72;--green:#7ee787}
  *{box-sizing:border-box}
  body{margin:0;background:
        radial-gradient(1200px 700px at 12% -10%,#16324a 0%,transparent 60%),
        radial-gradient(900px 600px at 100% 0%,#2a1c3d 0%,transparent 55%),var(--bg);
       color:var(--fg);font:14px/1.55 "Segoe UI Variable","Microsoft YaHei",system-ui,sans-serif;
       min-height:100vh}
  header{display:flex;align-items:center;gap:14px;padding:16px 22px;
         border-bottom:1px solid var(--line);backdrop-filter:blur(14px);
         background:rgba(12,18,26,.55);position:sticky;top:0;z-index:5}
  h1{margin:0;font-size:16px;font-weight:650;letter-spacing:.2px}
  .pill{display:inline-flex;align-items:center;gap:7px;padding:4px 11px;border-radius:999px;
        background:var(--card);border:1px solid var(--line);font-size:12.5px;color:var(--dim)}
  .dot{width:8px;height:8px;border-radius:50%;background:var(--green);
       box-shadow:0 0 0 0 rgba(126,231,135,.6);animation:pulse 2s infinite}
  .dot.off{background:#5b6673;animation:none}
  @keyframes pulse{70%{box-shadow:0 0 0 9px rgba(126,231,135,0)}100%{box-shadow:0 0 0 0 rgba(126,231,135,0)}}
  main{display:grid;grid-template-columns:minmax(360px,1fr) minmax(420px,1.15fr);gap:16px;
       padding:18px 22px 40px}
  @media (max-width:900px){main{grid-template-columns:1fr}}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;padding:14px 16px;
        backdrop-filter:blur(18px);box-shadow:0 10px 30px rgba(0,0,0,.25)}
  .card h2{margin:0 0 10px;font-size:12px;font-weight:600;letter-spacing:.14em;
           text-transform:uppercase;color:var(--dim)}
  .stage{margin-bottom:11px}
  .stage .top{display:flex;justify-content:space-between;font-size:13px;margin-bottom:5px}
  .stage .top b{font-weight:550}
  .stage .detail{color:var(--dim);font-size:12px}
  .bar{height:7px;border-radius:99px;background:rgba(255,255,255,.09);overflow:hidden}
  .bar>i{display:block;height:100%;width:0;border-radius:99px;
         background:linear-gradient(90deg,var(--teal),var(--blue));transition:width .25s ease}
  .stage.done .bar>i{background:linear-gradient(90deg,#2fbf71,var(--green))}
  .metrics{display:grid;grid-template-columns:1fr 1fr;gap:9px}
  .metric{background:rgba(255,255,255,.045);border:1px solid var(--line);border-radius:11px;
          padding:8px 10px}
  .metric .k{font-size:11px;color:var(--dim);letter-spacing:.04em}
  .metric .v{font-size:14.5px;font-weight:600;font-variant-numeric:tabular-nums}
  #log{height:290px;overflow:auto;font:12.5px/1.6 ui-monospace,Consolas,"Cascadia Mono",monospace;
       white-space:pre-wrap;word-break:break-word}
  #log div{padding:1px 0;border-bottom:1px dashed rgba(255,255,255,.045)}
  #log .t{color:#61707f;margin-right:7px}
  #log .ok{color:var(--green)} #log .warn{color:var(--amber)} #log .err{color:var(--red)}
  #log .step{color:var(--teal);font-weight:600}
  canvas{width:100%;display:block;border-radius:10px;background:rgba(0,0,0,.28);
         border:1px solid var(--line)}
  #wave{height:108px}
  #spec{height:96px}
  .files{display:flex;flex-direction:column;gap:7px;max-height:210px;overflow:auto}
  .file{display:flex;align-items:center;gap:10px;background:rgba(255,255,255,.045);
        border:1px solid var(--line);border-radius:11px;padding:7px 10px;font-size:12.5px}
  .file .nm{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .file a{color:var(--blue);text-decoration:none}
  .file audio{height:30px}
  .grid2{display:grid;gap:16px}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1 id="title">制乐 · 实时控制台</h1>
  <span class="pill" id="status">连接中…</span>
  <span class="pill" id="elapsed">0:00</span>
  <span class="pill" id="note"></span>
</header>
<main>
  <div class="grid2">
    <section class="card"><h2>阶段</h2><div id="stages"></div></section>
    <section class="card"><h2>读数</h2><div class="metrics" id="metrics"></div></section>
    <section class="card"><h2>波形</h2><canvas id="wave" width="1200" height="216"></canvas>
      <div class="pill" id="wavelabel" style="margin-top:8px">等待渲染</div></section>
  </div>
  <div class="grid2">
    <section class="card"><h2>频谱</h2><canvas id="spec" width="1200" height="192"></canvas></section>
    <section class="card"><h2>实时日志</h2><div id="log"></div></section>
    <section class="card"><h2>产物（点开就能听）</h2><div class="files" id="files"></div></section>
  </div>
</main>
<script>
const $ = s => document.querySelector(s);
let t0 = null;

function fmtTime(s){s=Math.max(0,s|0);return (s/60|0)+":"+String(s%60).padStart(2,"0");}

function addLog(ts, text, level){
  const box = $("#log"), div = document.createElement("div");
  const d = new Date((ts||Date.now()/1000)*1000);
  const hh = String(d.getHours()).padStart(2,"0"), mm = String(d.getMinutes()).padStart(2,"0"),
        ss = String(d.getSeconds()).padStart(2,"0");
  div.innerHTML = '<span class="t">'+hh+":"+mm+":"+ss+'</span><span class="'+(level||"info")+'"></span>';
  div.lastChild.textContent = text;
  box.appendChild(div);
  while (box.children.length > 500) box.removeChild(box.firstChild);
  box.scrollTop = box.scrollHeight;
}

function renderStages(list){
  const box = $("#stages");
  const seen = new Set(list.map(s=>s.id));
  for (const el of [...box.children]) if (!seen.has(el.dataset.id)) el.remove();
  for (const st of list){
    let el = box.querySelector('[data-id="'+st.id+'"]');
    if (!el){ el = document.createElement("div"); el.className="stage";
      el.dataset.id = st.id;
      el.innerHTML = '<div class="top"><b></b><span class="pct"></span></div>'+
                     '<div class="bar"><i></i></div><div class="detail"></div>';
      box.appendChild(el); }
    el.querySelector("b").textContent = st.label;
    el.querySelector(".pct").textContent = st.done ? "✓" : Math.round(st.frac*100)+"%";
    el.querySelector("i").style.width = (st.done?100:st.frac*100)+"%";
    el.querySelector(".detail").textContent = st.detail || "";
    el.classList.toggle("done", !!st.done);
  }
}

function setMetric(k, v){
  let el = [...$("#metrics").children].find(e=>e.dataset.k===k);
  if (!el){ el = document.createElement("div"); el.className="metric"; el.dataset.k=k;
    el.innerHTML='<div class="k"></div><div class="v"></div>'; $("#metrics").appendChild(el); }
  el.querySelector(".k").textContent = k;
  el.querySelector(".v").textContent = v;
}

function drawWave(env){
  const c = $("#wave"), ctx = c.getContext("2d"), W = c.width, H = c.height;
  ctx.clearRect(0,0,W,H);
  ctx.strokeStyle="rgba(255,255,255,.10)"; ctx.beginPath();
  ctx.moveTo(0,H/2); ctx.lineTo(W,H/2); ctx.stroke();
  if (!env || !env.length) return;
  const g = ctx.createLinearGradient(0,0,0,H);
  g.addColorStop(0,"#5aa9ff"); g.addColorStop(.5,"#4fd1c5"); g.addColorStop(1,"#5aa9ff");
  ctx.strokeStyle = g; ctx.lineWidth = Math.max(1, W/env.length*.8); ctx.beginPath();
  const mid = H/2, half = H/2-6;
  for (let i=0;i<env.length;i++){
    const x = i*W/env.length;
    ctx.moveTo(x, mid - env[i][1]*half);
    ctx.lineTo(x, mid - env[i][0]*half);
  }
  ctx.stroke();
}

function drawSpec(curve){
  const c = $("#spec"), ctx = c.getContext("2d"), W = c.width, H = c.height;
  ctx.clearRect(0,0,W,H);
  if (!curve || !curve.length) return;
  const bw = W/curve.length;
  for (let i=0;i<curve.length;i++){
    const db = curve[i];                       // 0 = 峰，-80 = 底
    const h = Math.max(1, (1 + db/80) * (H-14));
    const g = ctx.createLinearGradient(0,H-h,0,H);
    g.addColorStop(0,"#7ee787"); g.addColorStop(.55,"#f6c177"); g.addColorStop(1,"#ff7b72");
    ctx.fillStyle = g;
    ctx.fillRect(i*bw+1, H-h, Math.max(1,bw-2), h);
  }
  ctx.fillStyle="rgba(255,255,255,.35)"; ctx.font="11px system-ui";
  ctx.fillText("40Hz", 4, H-3); ctx.fillText("16kHz", W-46, H-3);
}

function addFile(item){
  const box = $("#files");
  let el = [...box.children].find(e=>e.dataset.n===item.name);
  if (!el){ el = document.createElement("div"); el.className="file"; el.dataset.n=item.name;
    el.innerHTML='<span class="nm"></span><a target="_blank">打开</a><audio controls preload="none"></audio>';
    box.appendChild(el); }
  el.querySelector(".nm").textContent = item.label + " · " + (item.size/1048576).toFixed(1) + " MB";
  if (item.url){ el.querySelector("a").href = item.url; el.querySelector("audio").src = item.url; }
}

function apply(ev){
  if (ev.kind === "hello"){
    $("#title").textContent = ev.title || $("#title").textContent;
    t0 = Date.now()/1000 - (ev.elapsed||0);
    $("#status").textContent = ev.status || "空闲";
    $("#note").textContent = ev.note || "";
    $("#dot").classList.toggle("off", !ev.status || ev.status==="空闲");
    renderStages(ev.stages||[]);
    (ev.metrics||[]).forEach(m=>setMetric(m.key,m.value));
    (ev.files||[]).forEach(addFile);
    if (ev.wave) drawWave(ev.wave.env);
    if (ev.spec) drawSpec(ev.spec);
    $("#log").innerHTML = "";
    (ev.history||[]).forEach(h=>{ if (h.kind==="log") addLog(h.ts,h.text,h.level); });
  } else if (ev.kind === "log"){ addLog(ev.ts, ev.text, ev.level); }
  else if (ev.kind === "stage"){ renderStages([ev]); }
  else if (ev.kind === "metric"){ setMetric(ev.key, ev.value); }
  else if (ev.kind === "status"){
    $("#status").textContent = ev.text; $("#note").textContent = ev.note||"";
    $("#dot").classList.toggle("off", !ev.running);
  }
  else if (ev.kind === "wave"){ drawWave(ev.env); $("#wavelabel").textContent = ev.label||""; }
  else if (ev.kind === "spec"){ drawSpec(ev.curve); }
  else if (ev.kind === "file"){ addFile(ev.item); }
}

const es = new EventSource("/events");
es.onmessage = e => { try { apply(JSON.parse(e.data)); } catch(err){ console.error(err); } };
es.onerror = () => { $("#status").textContent = "连接断开，重连中…"; };
setInterval(()=>{ if (t0) $("#elapsed").textContent = fmtTime(Date.now()/1000 - t0); }, 500);
</script>
</body>
</html>
"""


# ==========================================================================
# 跨进程：把事件转发到常驻控制台
# ==========================================================================


class RemoteHub(LiveHub):
    """只转发、不起服务器的中枢。

    命令行/渲染进程设了 ``MUSIC_LIVE_URL`` 就用它：事件 POST 给常驻控制台进程，
    本地面板是那个进程里的一个。继承 :class:`LiveHub` 是为了复用
    "算包络 / 算频谱 / 维护本地快照"这些逻辑 —— 只把出口换掉。
    """

    #: 进度事件最小转发间隔（秒）：渲染回调每秒能来上百次，全 POST 出去没意义
    MIN_GAP = 0.15

    def __init__(self, base_url: str, *, title: str = "制乐 · 实时控制台") -> None:
        super().__init__(port=0, title=title)
        self.base_url = base_url.rstrip("/")
        self.url = base_url if base_url.endswith("/") else base_url + "/"
        self._last_post: dict[str, float] = {}
        self._failed = False
        self.sent = 0

    def start(self) -> str:
        self.log(f"命令行进程已接上控制台：{self.url}", level="ok")
        return self.url

    def snapshot(self) -> dict:
        """优先读**常驻控制台**的真实状态（本地那份只有本进程发过的事件）。"""
        import urllib.request
        try:
            raw = urllib.request.urlopen(self.base_url + "/api/state", timeout=3.0).read()
            snap = json.loads(raw.decode("utf-8"))
            snap["remote"] = True
            return snap
        except Exception:
            return super().snapshot()

    def _emit(self, event: dict) -> None:
        event = {"ts": time.time(), **event}
        with self._lock:
            self._history.append(event)
            if len(self._history) > self._history_max:
                del self._history[:len(self._history) - self._history_max]
        if self._failed:
            return
        kind = str(event.get("kind"))
        if kind in ("stage", "wave", "spectrum") and not event.get("done"):
            key = str(event.get("id") or kind)
            now = time.time()
            if now - self._last_post.get(key, 0.0) < self.MIN_GAP:
                return
            self._last_post[key] = now
        import urllib.error
        import urllib.request
        try:
            req = urllib.request.Request(
                self.base_url + "/api/event",
                data=json.dumps(event, ensure_ascii=False, default=str).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3.0).read()
            self.sent += 1
        except (urllib.error.URLError, OSError):
            self._failed = True                      # 控制台没了就安静降级，不拖垮渲染


# ==========================================================================
# 单例
# ==========================================================================

_HUB: LiveHub | None = None
_HUB_LOCK = threading.Lock()


def hub(*, autostart: bool = False, port: int | None = None,
        title: str | None = None) -> LiveHub:
    """取全局中枢。

    ``MUSIC_LIVE_URL`` 有值 → 走 :class:`RemoteHub`（把事件转给常驻控制台）；
    否则起自己的服务器（``autostart=True``）。
    """
    global _HUB
    with _HUB_LOCK:
        if _HUB is None:
            remote = os.environ.get("MUSIC_LIVE_URL", "").strip()
            if remote:
                _HUB = RemoteHub(remote, title=title or "制乐 · 实时控制台")
            else:
                _HUB = LiveHub(
                    port=int(port or os.environ.get("MUSIC_LIVE_PORT", DEFAULT_PORT)),
                    title=title or "制乐 · 实时控制台")
    if autostart and not isinstance(_HUB, RemoteHub):
        _HUB.start()
    return _HUB


__all__ = ["LiveHub", "RemoteHub", "hub", "DEFAULT_PORT"]
