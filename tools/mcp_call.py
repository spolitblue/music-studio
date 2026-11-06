"""最小 MCP 客户端 —— 从命令行调一次制乐 MCP 服务器，顺便当它的端到端验证。

用途::

    .venv\\Scripts\\python tools\\mcp_call.py --list
    .venv\\Scripts\\python tools\\mcp_call.py analyze_reference --arg path=参考.mp3
    .venv\\Scripts\\python tools\\mcp_call.py replicate --arg reference=参考.mp3 \\
        --arg target_seconds=200 --arg seeds=3 --timeout 900
    .venv\\Scripts\\python tools\\mcp_call.py live_status

为什么自己做而不引 SDK：MCP 的 stdio 传输就是"一行一条 JSON-RPC 2.0"，
握手三步（initialize → notifications/initialized → tools/call）。
为了这一步去装一个 Node/Python SDK，对一个自包含的工程来说是净负担 ——
代价是这里必须真的按协议写对（所以它同时就是服务器那侧的可执行规格）。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable

# 自己也要能在这台机器的 GBK 控制台上打印中文（否则一条工具描述就能把 CLI 打崩）
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


class McpClient:
    """起一个 MCP 服务器进程，说 JSON-RPC，把通知顺手打印出来。"""

    def __init__(self, *, port: int | None = None, echo_notify: bool = True) -> None:
        cmd = [PY, "-u", str(ROOT / "tools" / "music_mcp.py")]
        if port:
            cmd += ["--port", str(port)]
        self.proc = subprocess.Popen(cmd, cwd=str(ROOT), stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                     text=True, encoding="utf-8", errors="replace",
                                     bufsize=1)
        self.echo_notify = echo_notify
        self._id = 0
        self._pending: dict[int, dict] = {}
        self._lock = threading.Lock()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()

    # -- 读 --------------------------------------------------------------

    def _read_stdout(self) -> None:
        for line in self.proc.stdout:                       # type: ignore[union-attr]
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if "id" in msg:
                with self._lock:
                    self._pending[msg["id"]] = msg
            elif self.echo_notify and msg.get("method") == "notifications/message":
                data = msg.get("params", {}).get("data", "")
                print(f"   [服务器] {data}", flush=True)

    def _read_stderr(self) -> None:
        for line in self.proc.stderr:                       # type: ignore[union-attr]
            line = line.strip()
            if line:
                print(f"   [stderr] {line}", file=sys.stderr, flush=True)

    # -- 写 --------------------------------------------------------------

    def _send(self, obj: dict) -> None:
        self.proc.stdin.write(json.dumps(obj, ensure_ascii=False) + "\n")   # type: ignore
        self.proc.stdin.flush()                                             # type: ignore

    def notify(self, method: str, params: dict | None = None) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params or {}})

    def call(self, method: str, params: dict | None = None, *, timeout: float = 60.0):
        self._id += 1
        mid = self._id
        self._send({"jsonrpc": "2.0", "id": mid, "method": method, "params": params or {}})
        t0 = time.time()
        while time.time() - t0 < timeout:
            with self._lock:
                if mid in self._pending:
                    return self._pending.pop(mid)
            time.sleep(0.02)
        raise TimeoutError(f"{method} 超过 {timeout:.0f}s 没回")

    def initialize(self) -> dict:
        out = self.call("initialize", {"protocolVersion": "2024-11-05",
                                       "capabilities": {},
                                       "clientInfo": {"name": "mcp_call", "version": "1.0"}})
        self.notify("notifications/initialized")
        return out

    def close(self) -> None:
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            self.proc.kill()


def _args_to_dict(pairs: list[str]) -> dict:
    out: dict = {}
    for item in pairs or []:
        if "=" not in item:
            raise SystemExit(f"--arg 要写成 key=value，收到「{item}」")
        key, raw = item.split("=", 1)
        try:
            out[key] = json.loads(raw)
        except json.JSONDecodeError:
            out[key] = raw
    return out


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="调用制乐 MCP 服务器")
    ap.add_argument("tool", nargs="?", default="live_status", help="要调的工具名")
    ap.add_argument("--arg", action="append", default=[], help="工具参数 key=value（可重复）")
    ap.add_argument("--list", action="store_true", help="只列工具")
    ap.add_argument("--port", type=int, default=None, help="控制台端口")
    ap.add_argument("--timeout", type=float, default=120.0)
    ap.add_argument("--quiet", action="store_true", help="不打印服务器推送的通知")
    args = ap.parse_args(argv)

    client = McpClient(port=args.port, echo_notify=not args.quiet)
    try:
        init = client.initialize()
        info = init.get("result", {}).get("serverInfo", {})
        print(f"已连上 MCP 服务器：{info.get('name')} {info.get('version')}")
        tools = client.call("tools/list").get("result", {}).get("tools", [])
        if args.list:
            for t in tools:
                print(f"\n· {t['name']}\n    {t['description']}")
                props = t.get("inputSchema", {}).get("properties", {})
                if props:
                    print("    参数：" + "、".join(f"{k}" for k in props))
            return 0

        payload = _args_to_dict(args.arg)
        print(f"调用 {args.tool}（{json.dumps(payload, ensure_ascii=False)}）…", flush=True)
        resp = client.call("tools/call", {"name": args.tool, "arguments": payload},
                           timeout=args.timeout)
        result = resp.get("result", {})
        for block in result.get("content", []):
            print(block.get("text", ""))
        if result.get("isError"):
            return 1
        if args.tool in ("replicate",):
            # 长任务：等它跑完，同时把服务器推送的进度印出来
            while True:
                time.sleep(3.0)
                job = json.loads(client.call("job_status").get("result", {})
                                 .get("content", [{}])[0].get("text", "{}"))
                if job.get("state") != "running":
                    print("\n任务结束：" + json.dumps(job.get("result") or job.get("error"),
                                                     ensure_ascii=False, indent=2)[:2000])
                    break
        return 0
    finally:
        client.close()


if __name__ == "__main__":
    raise SystemExit(main())
