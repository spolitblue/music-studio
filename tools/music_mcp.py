"""制乐 MCP 服务器 —— 让任何 MCP 客户端（Claude / Cursor / 自制脚本）能驱动并**看着**这条流水线跑。

为什么是 MCP + 网页两条腿
------------------------
MCP 解决"谁能调用这套能力"：扒谱、复刻、采样、渲染都变成标准工具，
客户端不需要知道 numpy 与 PySide6 的存在。
网页控制台解决"人怎么看"：MCP 的 ``notifications/message`` 能推进客户端日志，
但看不到波形、频谱、进度条和产物试听 —— 那些只有浏览器里才舒服。

所以这里做的是**同一个事件总线长出两个出口**：
``tools.live_hub`` 里推一次，MCP 通知与 SSE 网页同时收到。

用法::

    .venv\\Scripts\\python tools\\music_mcp.py                 # MCP over stdio
    .venv\\Scripts\\python tools\\music_mcp.py --console-only   # 只起网页控制台
    .venv\\Scripts\\python tools\\music_mcp.py --selftest        # 自检（握手 + 列工具 + 调一次）

协议实现说明：stdio 传输是**按行分隔的 JSON-RPC 2.0**（不是 LSP 那套
``Content-Length`` 头）。stdout 只许走协议消息，任何打印都必须去 stderr。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# stdio 是协议通道，必须**无条件**是 UTF-8：中文 Windows 控制台默认 cp936，
# 那样写出去的 JSON 会把中文编成 GBK 字节，客户端按 UTF-8 解出来全是 U+FFFD
# （实测：工具描述整段变成问号）。这一行不能省，也不能指望外部设 PYTHONIOENCODING。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

from tools import music_ops                              # noqa: E402
from tools.live_hub import hub                           # noqa: E402

PROTOCOL_VERSION = "2024-11-05"
SERVER_INFO = {"name": "music-studio", "version": "1.0.0"}

#: 每次推 MCP 通知的最小间隔（秒）—— 渲染进度回调每秒能来几十次，
#: 原样转发会把客户端的日志面板刷爆，人反而什么都看不见。
NOTIFY_MIN_GAP = 0.4


def _tool(name: str, description: str, properties: dict, required: list[str]) -> dict:
    return {"name": name, "description": description,
            "inputSchema": {"type": "object", "properties": properties,
                            "required": required}}


TOOLS: list[dict] = [
    _tool("live_status",
          "看现在在干什么：控制台地址、当前状态、各阶段进度、最近日志。",
          {}, []),
    _tool("analyze_reference",
          "读一段参考音频（wav/mp3/m4a/flac），量出指纹：速度/调式/织体密度/摇摆/明暗/鼓占比。",
          {"path": {"type": "string", "description": "参考音频路径"},
           "bpm": {"type": "number", "description": "已知速度就直接给，省一次估计"}},
          ["path"]),
    _tool("replicate",
          "按参考音频的指纹重新生成一首曲子（后台跑）：扒谱 → 指纹 → 段落编排 → 音色映射 "
          "→ 渲染 → 导出 WAV/MIDI/工程 JSON，并把过程实时推到控制台。",
          {"reference": {"type": "string", "description": "参考音频路径"},
           "out_dir": {"type": "string", "description": "输出目录（默认 _out/mcp）"},
           "target_seconds": {"type": "number", "description": "目标时长（秒），自动决定段落数"},
           "sections": {"type": "integer", "description": "段落数（1~10）"},
           "bars": {"type": "integer", "description": "每段小节数（默认按指纹推）"},
           "seeds": {"type": "integer", "description": "生成几个候选（1~8）"},
           "seed": {"type": "integer", "description": "起始种子"},
           "headroom_db": {"type": "number",
                           "description": "母带目标峰值（dBFS），默认 -2；给 0 以上等于保持原样"},
           "mix": {"type": "string", "enum": ["gentle", "app"],
                   "description": "gentle（默认）= 采样 + 声部比例 + 频谱摆平；app = 软件原本的母带链"},
           "drums": {"type": "string", "enum": ["sampled", "native", "off"],
                     "description": "采样鼓（从参考抠）还是引擎鼓机"},
           "lead": {"type": "string", "enum": ["synth", "sampled"],
                    "description": "主音用合成器还是用参考里抠出来的音色"}},
          ["reference"]),
    _tool("sample_reference",
          "从参考音频里**采样**：找起音 → 按鼓件类别挑最干净的一下抠成单件样本 "
          "→ 再挑一个乐音样本，落成 WAV + kit.json，并推给控制台试听。",
          {"path": {"type": "string", "description": "参考音频路径"},
           "out_dir": {"type": "string", "description": "样本输出目录（默认 _out/mcp/samples）"}},
          ["path"]),
    _tool("job_status",
          "看后台任务（replicate/sample_reference）跑到哪了。", {}, []),
    _tool("list_outputs",
          "列产物（WAV/MIDI/工程 JSON），带大小与可试听的 URL。",
          {"out_dir": {"type": "string", "description": "目录（默认整个 _out）"}}, []),
    _tool("console_log",
          "取最近的控制台日志行（不给浏览器也能看过程）。",
          {"n": {"type": "integer", "description": "取多少行，默认 40"}}, []),
]


class McpServer:
    """最小可用的 MCP 服务器：initialize / tools/list / tools/call / ping。"""

    def __init__(self, live, *, notify: bool = True) -> None:
        self.live = live
        self.running = True
        self._job: dict = {"state": "idle"}
        self._job_lock = threading.Lock()
        self._out_lock = threading.Lock()
        self._last_notify = 0.0
        self._notify = notify

    # -- 输出 ------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj, ensure_ascii=False, default=str)
        with self._out_lock:
            sys.stdout.write(line + "\n")
            sys.stdout.flush()

    def _result(self, mid, result: dict) -> None:
        self._send({"jsonrpc": "2.0", "id": mid, "result": result})

    def _error(self, mid, code: int, message: str) -> None:
        self._send({"jsonrpc": "2.0", "id": mid,
                    "error": {"code": code, "message": message}})

    # -- 后台任务 ---------------------------------------------------------

    def _start_job(self, label: str, fn) -> dict:
        with self._job_lock:
            if self._job.get("state") == "running":
                return {"error": f"已有任务在跑：{self._job.get('label')}"}
            self._job = {"state": "running", "label": label, "started": time.time(),
                         "result": None, "error": None}

        def _run() -> None:
            self.live.log(f"后台任务开始：{label}", level="step")
            try:
                out = fn()
                with self._job_lock:
                    self._job.update({"state": "done", "result": out,
                                      "finished": time.time()})
                self.live.log(f"后台任务完成：{label}", level="ok")
            except Exception as exc:                     # noqa: BLE001
                with self._job_lock:
                    self._job.update({"state": "error", "error": f"{type(exc).__name__}: {exc}",
                                      "finished": time.time()})
                self.live.log(f"后台任务失败：{label} · {type(exc).__name__}: {exc}",
                              level="err")
                traceback.print_exc(file=sys.stderr)

        threading.Thread(target=_run, name="mcp-job", daemon=True).start()
        return {"job": self._job}

    # -- 工具实现 ---------------------------------------------------------

    def _tool_sample(self, args: dict) -> dict:
        path = args.get("path") or ""
        out_dir = args.get("out_dir") or str(ROOT / "_out" / "mcp" / "samples")
        info = music_ops.sample_reference(path, out_dir, live=self.live)
        return (f"采样完成：{info['summary']}\n目录：{info['dir']}\n"
                + json.dumps(info["samples"], ensure_ascii=False, indent=2))

    def _tool_live_status(self, _args: dict) -> dict:
        snap = self.live.snapshot()
        lines = [f"控制台：{self.live.url}",
                 f"状态：{snap['status']}　{snap.get('note', '')}",
                 f"已运行：{snap['elapsed']:.0f}s"]
        for st in snap["stages"]:
            mark = "✓" if st["done"] else f"{st['frac'] * 100:.0f}%"
            lines.append(f"  [{mark:>4}] {st['label']} {st.get('detail', '')}")
        if snap["metrics"]:
            lines.append("读数：" + " · ".join(f"{m['key']}={m['value']}"
                                              for m in snap["metrics"][:10]))
        job = self._job
        lines.append(f"后台任务：{job.get('state')} {job.get('label', '')}")
        lines.append("最近日志：")
        for ev in [h for h in snap["history"] if h.get("kind") == "log"][-10:]:
            lines.append("  " + str(ev.get("text", "")))
        return "\n".join(lines)

    def _tool_analyze(self, args: dict) -> dict:
        path = args.get("path") or ""
        audio = music_ops.load_reference(path, live=self.live)
        res, profile = music_ops.analyze(audio, bpm=args.get("bpm"), live=self.live)
        payload = {"bpm": profile.bpm, "key": profile.key_name, "mode": profile.mode,
                   "density": profile.density, "onset_rate": profile.onset_rate,
                   "swing": profile.swing, "energy_db": profile.energy_db,
                   "brightness_hz": profile.brightness_hz,
                   "drum_ratio": profile.drum_ratio, "seconds": profile.seconds,
                   "onsets": res.onsets, "pitched": len(res.pitched),
                   "drums": len(res.drums)}
        return "\n".join([profile.summary(), json.dumps(payload, ensure_ascii=False,
                                                        indent=2)])

    def _tool_replicate(self, args: dict) -> dict:
        reference = args.get("reference") or ""
        out_dir = args.get("out_dir") or str(ROOT / "_out" / "mcp")
        kwargs = {k: args[k] for k in ("target_seconds", "sections", "bars", "seeds",
                                       "seed", "headroom_db", "mix", "drums", "lead",
                                       "kit_dir") if k in args}
        label = f"复刻 {Path(reference).name}"

        def _work():
            return music_ops.run_replicate(reference, out_dir, live=self.live, **kwargs)

        info = self._start_job(label, _work)
        if "error" in info:
            return info["error"]
        return (f"已在后台开始：{label}\n控制台（可实时看进度/波形/试听）：{self.live.url}\n"
                f"输出目录：{out_dir}\n用 job_status 查进度。")

    def _tool_job_status(self, _args: dict) -> dict:
        job = dict(self._job)
        result = job.get("result")
        if result is not None:
            job["result"] = {
                "out_dir": result.out_dir,
                "candidates": [
                    {"seed": c["seed"], "wav": c["path"], "notes": c["notes"],
                     "crest_db": round(c["health"]["crest_db"], 2),
                     "progressions": c["progressions"]}
                    for c in result.candidates],
            }
        return json.dumps(job, ensure_ascii=False, indent=2, default=str)

    def _tool_list_outputs(self, args: dict) -> dict:
        base = Path(args.get("out_dir") or (ROOT / "_out"))
        if not base.is_absolute():
            base = ROOT / base
        rows = []
        for p in sorted(base.rglob("*")):
            if p.is_file() and p.suffix.lower() in (".wav", ".mid", ".json", ".md"):
                try:
                    rel = p.resolve().relative_to(ROOT / "_out")
                    url = f"{self.live.url}audio?f={rel.as_posix()}"
                except ValueError:
                    url = ""
                rows.append(f"{p.relative_to(ROOT)}  {p.stat().st_size / 1048576:.2f} MB  {url}")
        return "\n".join(rows[-60:]) or "（没有产物）"

    def _tool_console_log(self, args: dict) -> dict:
        n = int(args.get("n") or 40)
        snap = self.live.snapshot()
        lines = [str(h.get("text", "")) for h in snap["history"] if h.get("kind") == "log"]
        return "\n".join(lines[-n:])

    _IMPL = {
        "live_status": _tool_live_status,
        "analyze_reference": _tool_analyze,
        "sample_reference": _tool_sample,
        "replicate": _tool_replicate,
        "job_status": _tool_job_status,
        "list_outputs": _tool_list_outputs,
        "console_log": _tool_console_log,
    }

    # -- 协议 ------------------------------------------------------------

    def handle(self, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        if method == "initialize":
            self._result(mid, {
                "protocolVersion": params.get("protocolVersion") or PROTOCOL_VERSION,
                "capabilities": {"tools": {"listChanged": False}, "logging": {}},
                "serverInfo": SERVER_INFO,
                "instructions": "制乐音乐软件：扒谱 / 复刻 / 采样 / 渲染。"
                                f"实时控制台在 {self.live.url}",
            })
        elif method in ("notifications/initialized", "notifications/cancelled"):
            pass
        elif method == "ping":
            self._result(mid, {})
        elif method == "tools/list":
            self._result(mid, {"tools": TOOLS})
        elif method == "tools/call":
            name = params.get("name")
            args = params.get("arguments") or {}
            fn = self._IMPL.get(name)
            if fn is None:
                self._error(mid, -32602, f"没有这个工具：{name}")
                return
            try:
                out = fn(self, args)
                self._result(mid, {"content": [{"type": "text", "text": str(out)}],
                                   "isError": False})
            except Exception as exc:                     # noqa: BLE001
                self.live.log(f"工具 {name} 失败：{type(exc).__name__}: {exc}", level="err")
                self._result(mid, {"content": [{"type": "text",
                                                "text": f"{type(exc).__name__}: {exc}"}],
                                   "isError": True})
        elif method in ("resources/list", "prompts/list"):
            key = "resources" if method.startswith("resources") else "prompts"
            self._result(mid, {key: []})
        elif mid is not None:
            self._error(mid, -32601, f"Method not found: {method}")

    def serve(self) -> None:
        reader = threading.Thread(target=self._forward_events, name="mcp-notify",
                                  daemon=True)
        reader.start()
        self.live.log("MCP 服务器已就绪（stdio）", level="ok")
        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(msg, list):
                for one in msg:
                    self.handle(one)
            else:
                self.handle(msg)

    def _forward_events(self) -> None:
        """把控制台事件转成 MCP 日志通知（节流）。"""
        q = self.live.subscribe()
        try:
            while True:
                payload = q.get()
                try:
                    ev = json.loads(payload)
                except Exception:
                    continue
                kind = ev.get("kind")
                if kind == "log":
                    text, level = ev.get("text"), ev.get("level", "info")
                elif kind == "stage":
                    now = time.time()
                    if not ev.get("done") and now - self._last_notify < NOTIFY_MIN_GAP:
                        continue
                    self._last_notify = now
                    text = f"{ev.get('label')} {ev.get('frac', 0) * 100:.0f}% {ev.get('detail', '')}"
                    level = "info"
                elif kind in ("status", "file"):
                    text = json.dumps(ev, ensure_ascii=False)[:300]
                    level = "info"
                else:
                    continue
                self._send({"jsonrpc": "2.0", "method": "notifications/message",
                            "params": {"level": "error" if level == "err" else
                                                ("warning" if level == "warn" else "info"),
                                       "logger": "music-studio", "data": text}})
        except Exception:
            return


# ==========================================================================
# 自检 / 入口
# ==========================================================================


def selftest(live) -> int:
    """不上客户端也能验：握手 → 列工具 → 调 live_status → 看控制台端点。"""
    import urllib.request

    srv = McpServer(live)
    out: list[dict] = []

    class _Catcher(list):
        pass

    real_send = srv._send

    def _capture(obj):
        out.append(obj)
        real_send(obj)

    srv._send = _capture                                   # type: ignore[assignment]
    srv.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                           "clientInfo": {"name": "selftest", "version": "0"}}})
    srv.handle({"jsonrpc": "2.0", "method": "notifications/initialized"})
    srv.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                "params": {"name": "live_status", "arguments": {}}})
    srv.handle({"jsonrpc": "2.0", "id": 4, "method": "ping"})
    srv.handle({"jsonrpc": "2.0", "id": 5, "method": "不存在的办法"})

    ok = True

    def check(label, cond, detail=""):
        nonlocal ok
        ok = ok and bool(cond)
        print(f"  [{'OK  ' if cond else 'FAIL'}] {label}{(' — ' + detail) if detail else ''}")

    init = next((o for o in out if o.get("id") == 1), {})
    tools = next((o for o in out if o.get("id") == 2), {})
    status = next((o for o in out if o.get("id") == 3), {})
    pong = next((o for o in out if o.get("id") == 4), {})
    bad = next((o for o in out if o.get("id") == 5), {})

    check("initialize 回了协议版本与服务器名",
          init.get("result", {}).get("serverInfo", {}).get("name") == "music-studio",
          str(init.get("result", {}).get("protocolVersion")))
    names = [t["name"] for t in tools.get("result", {}).get("tools", [])]
    check("tools/list 列全了", len(names) >= 6, "、".join(names))
    check("每个工具都有 inputSchema",
          all("inputSchema" in t for t in tools.get("result", {}).get("tools", [])))
    text = status.get("result", {}).get("content", [{}])[0].get("text", "")
    check("tools/call 真的返回了控制台状态", "控制台" in text and "阶段" not in text[:4], text[:60])
    check("ping 回空对象", pong.get("result") == {})
    check("未知方法回 -32601", bad.get("error", {}).get("code") == -32601)
    check("control 台 URL 可访问",
          "ok" in urllib.request.urlopen(live.url + "health", timeout=3).read().decode())
    print("\n" + ("MCP 自检全部通过" if ok else "MCP 自检有失败项"))
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="制乐 MCP 服务器（含实时控制台）")
    ap.add_argument("--port", type=int, default=int(os.environ.get("MUSIC_LIVE_PORT", 8770)))
    ap.add_argument("--console-only", action="store_true",
                    help="只起网页控制台，不说 MCP（stdio 上不读不写）")
    ap.add_argument("--selftest", action="store_true", help="自检后退出")
    ap.add_argument("--no-notify", action="store_true", help="不往 MCP 转发进度通知")
    args = ap.parse_args(argv)

    live = hub(autostart=True, port=args.port, title="制乐 · 实时控制台")
    print(f"[music-mcp] 控制台：{live.url}", file=sys.stderr, flush=True)

    if args.selftest:
        return selftest(live)
    if args.console_only:
        live.log("控制台以 --console-only 模式启动（等待操作）", level="ok")
        try:
            while True:
                time.sleep(1.0)
        except KeyboardInterrupt:
            return 0
    McpServer(live, notify=not args.no_notify).serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
