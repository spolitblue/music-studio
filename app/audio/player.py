"""实时播放 —— 基于 sounddevice / PortAudio。

播放线程与界面线程的边界
------------------------
PortAudio 的**回调跑在它自己的高优先级线程**上，绝不能在里面做任何耗时操作
（分配内存、加锁、改 UI）。所以这里的约定是：

* 回调只做一件事：从已经算好的 numpy 缓冲区里拷一段出来，原子地推进游标。
* 缓冲区在 ``load()`` 时一次性算好（离线渲染），播放期间**零计算、零分配**。
* 界面通过 :meth:`Player.snapshot` 轮询状态，而不是让回调反向通知 —— 避免跨线程 UI 调用。

这也意味着"改一个音符就要重渲染整首"。对 8 小节的曲子，渲染在秒级以内，
完全可以接受；换来的是播放绝对稳定、不会有实时合成常见的爆音与卡顿。
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass

import numpy as np

from app.audio import dsp

try:
    import sounddevice as sd

    HAVE_SOUNDDEVICE = True
except Exception:  # pragma: no cover
    sd = None
    HAVE_SOUNDDEVICE = False


@dataclass
class PlayerState:
    playing: bool = False
    position: float = 0.0      # 秒
    duration: float = 0.0
    peak_db: float = -120.0
    rms_db: float = -120.0
    looping: bool = False
    finished: bool = False


class Player:
    """离线渲染 + 流式回放。"""

    def __init__(self, samplerate: int = dsp.SR, blocksize: int = 1024):
        self.samplerate = int(samplerate)
        self.blocksize = int(blocksize)
        self._audio: np.ndarray | None = None      # (2, n) float32
        self._cursor = 0
        self._lock = threading.Lock()
        self._stream = None
        self._playing = False
        self._loop = False
        self._loop_range: tuple[float, float] | None = None
        self._peak = 0.0
        self._rms = 0.0
        self._finished = False
        self._started_at = 0.0
        self._start_cursor = 0
        self._device_error: str | None = None

    # -- 能力查询 --------------------------------------------------------

    @property
    def available(self) -> bool:
        return HAVE_SOUNDDEVICE

    @property
    def device_error(self) -> str | None:
        return self._device_error

    @staticmethod
    def list_devices() -> list[dict]:
        if not HAVE_SOUNDDEVICE:
            return []
        out = []
        try:
            for i, d in enumerate(sd.query_devices()):
                if d.get("max_output_channels", 0) > 0:
                    out.append({
                        "index": i,
                        "name": d["name"],
                        "channels": d["max_output_channels"],
                        "samplerate": d.get("default_samplerate", 44100),
                        "hostapi": sd.query_hostapis(d["hostapi"])["name"],
                    })
        except Exception:
            pass
        return out

    # -- 装载 ------------------------------------------------------------

    def load(self, stereo: np.ndarray, samplerate: int | None = None) -> None:
        """装载一段音频。会停掉当前播放。"""
        self.stop()
        data = np.asarray(stereo, dtype=np.float32)
        if data.ndim == 1:
            data = np.stack([data, data])
        if data.shape[0] == 1:
            data = np.repeat(data, 2, axis=0)
        if data.shape[0] > 2:
            data = data[:2]

        with self._lock:
            self._audio = np.ascontiguousarray(data)
            self._cursor = 0
            self._peak = 0.0
            self._rms = 0.0
            self._finished = False
        if samplerate:
            self.samplerate = int(samplerate)

    @property
    def duration(self) -> float:
        if self._audio is None:
            return 0.0
        return self._audio.shape[1] / float(self.samplerate)

    @property
    def loaded(self) -> bool:
        return self._audio is not None and self._audio.shape[1] > 0

    # -- 传输 ------------------------------------------------------------

    def play(self, at: float | None = None) -> bool:
        if not self.loaded or not HAVE_SOUNDDEVICE:
            return False
        if at is not None:
            self.seek(at)

        # 已经播到末尾 → 从头开始（用户按播放键的直觉）
        if self._cursor >= self._audio.shape[1] - 1 and not self._loop:
            self._cursor = 0

        if self._stream is not None:
            self._playing = True
            return True

        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate,
                channels=2,
                dtype="float32",
                blocksize=self.blocksize,
                callback=self._callback,
                finished_callback=self._on_finished,
            )
            self._stream.start()
        except Exception as exc:
            self._device_error = f"{type(exc).__name__}: {exc}"
            self._stream = None
            return False

        self._playing = True
        self._finished = False
        return True

    def pause(self) -> None:
        self._playing = False

    def stop(self) -> None:
        self._playing = False
        self._close_stream()
        with self._lock:
            self._cursor = 0
            self._peak = 0.0
            self._rms = 0.0
            self._finished = False

    def toggle(self) -> bool:
        if self._playing:
            self.pause()
            return False
        return self.play()

    def seek(self, seconds: float) -> None:
        if not self.loaded:
            return
        with self._lock:
            self._cursor = int(np.clip(seconds, 0.0, self.duration) * self.samplerate)

    def _close_stream(self) -> None:
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    # -- 循环 ------------------------------------------------------------

    def set_loop(self, enabled: bool, start: float | None = None, end: float | None = None) -> None:
        self._loop = bool(enabled)
        if start is not None and end is not None and end > start:
            self._loop_range = (float(start), float(end))
        else:
            self._loop_range = None

    @property
    def looping(self) -> bool:
        return self._loop

    # -- 音频回调（PortAudio 线程）---------------------------------------

    def _callback(self, outdata, frames, time_info, status):  # noqa: ANN001
        """必须极快：只做拷贝和游标推进。

        不取 ``self._lock`` 之外的任何锁，不做 numpy 运算以外的分配，
        更不碰 Qt —— 这是播放稳定的前提。
        """
        if self._audio is None or not self._playing:
            outdata.fill(0)
            return

        n = self._audio.shape[1]
        pos = self._cursor
        written = 0

        if self._loop:
            lo, hi = self._loop_bounds()
        else:
            lo, hi = 0, n

        while written < frames:
            remaining = hi - pos
            if remaining <= 0:
                if self._loop:
                    pos = lo
                    continue
                outdata[written:].fill(0)
                self._playing = False
                self._finished = True
                break
            take = min(frames - written, remaining)
            outdata[written:written + take] = self._audio[:, pos:pos + take].T
            pos += take
            written += take

        self._cursor = pos

        # 电平表：廉价近似即可，用于界面显示
        block = outdata[:written] if written > 0 else None
        if block is not None and written > 0:
            self._peak = float(np.max(np.abs(block)))
            self._rms = float(np.sqrt(np.mean(block ** 2)))

        if status:  # underflow/overflow 不应该让播放中断
            pass

    def _loop_bounds(self) -> tuple[int, int]:
        n = self._audio.shape[1] if self._audio is not None else 0
        if self._loop_range is None:
            return 0, n
        lo = int(max(0, self._loop_range[0]) * self.samplerate)
        hi = int(min(self.duration, self._loop_range[1]) * self.samplerate)
        if hi <= lo:
            return 0, n
        return lo, hi

    def _on_finished(self) -> None:
        pass

    # -- 状态快照（界面线程调用）----------------------------------------

    def snapshot(self) -> PlayerState:
        pos = self._cursor / float(self.samplerate)
        return PlayerState(
            playing=self._playing,
            position=min(pos, self.duration),
            duration=self.duration,
            peak_db=dsp.gain_to_db(max(1e-9, self._peak)),
            rms_db=dsp.gain_to_db(max(1e-9, self._rms)),
            looping=self._loop,
            finished=self._finished,
        )

    @property
    def position(self) -> float:
        return self._cursor / float(self.samplerate)

    @property
    def playing(self) -> bool:
        return self._playing


# --------------------------------------------------------------------------
# 节拍器 / 单音试听（合成短音，不需要装载整首）
# --------------------------------------------------------------------------


#: 静音模式：不开真音频流，但保持全部调用语义。
#: 测试默认打开（见 tests/run_all.py）；想听试听效果时设 MUSIC_STUDIO_SILENT_AUDIO=0。
SILENT_AUDIO = os.environ.get("MUSIC_STUDIO_SILENT_AUDIO", "").strip() not in ("", "0", "false", "False")


class BlipPlayer:
    """单击试听：常驻一条输出流，把要响的音排队播出去。

    为什么不用 sd.play() 或每次新开流：那两者都要重新启动一条音频流，
    实测延迟在几十毫秒量级，而且频繁开关容易失败。而试听是**跟着鼠标点击**走的 ——
    点一下要立刻听到声音，否则边写边听这个流程就断了。

    常驻流 + 待播队列（后到的一发覆盖前一发）能把它压到几毫秒。
    """

    def __init__(self, samplerate: int = dsp.SR):
        self.samplerate = int(samplerate)
        self._stream = None
        self._lock = threading.Lock()
        self._queue: list[np.ndarray] = []      # 形如 (n, 2)，最新的在最前
        self._pos = 0
        self._failed = False

    # -- 播放 ------------------------------------------------------------

    def play_buffer(self, stereo: np.ndarray) -> bool:
        """立刻响一发。会打断上一发（符合点击试听的直觉）。"""
        if not HAVE_SOUNDDEVICE or self._failed:
            return False
        data = np.asarray(stereo, dtype=np.float32)
        if data.ndim == 1:
            data = np.stack([data, data])
        if data.shape[0] > 2:
            data = data[:2]
        if data.shape[1] == 0:
            return False
        # 首尾各加一点点淡入淡出：不加的话每次打断都会啪一声
        n = data.shape[1]
        f = min(64, n // 8)
        if f > 1:
            data = data.copy()
            data[:, :f] *= np.linspace(0.0, 1.0, f, dtype=np.float32)
            data[:, -f:] *= np.linspace(1.0, 0.0, f, dtype=np.float32)

        with self._lock:
            self._queue = [np.ascontiguousarray(data.T)]   # (n, 2)
            self._pos = 0
        return self._ensure_stream()

    def _ensure_stream(self) -> bool:
        if SILENT_AUDIO:
            # 假装开好了：返回 True 让调用方语义不变，但不碰 PortAudio，
            # 于是扬声器安静、CI 不会因为「没有声卡」而变红。
            return True
        if self._stream is not None:
            return True
        try:
            self._stream = sd.OutputStream(
                samplerate=self.samplerate, channels=2, dtype="float32",
                blocksize=256,              # 小块 = 低延迟
                callback=self._callback,
            )
            self._stream.start()
            return True
        except Exception:
            self._stream = None
            self._failed = True
            return False

    def _callback(self, outdata, frames, time_info, status):  # noqa: ANN001
        """PortAudio 线程：只做拷贝，不做任何耗时操作。"""
        written = 0
        with self._lock:
            while written < frames and self._queue:
                cur = self._queue[0]
                take = min(frames - written, cur.shape[0] - self._pos)
                if take <= 0:
                    self._queue.pop(0)
                    self._pos = 0
                    continue
                outdata[written:written + take] = cur[self._pos:self._pos + take]
                self._pos += take
                written += take
                if self._pos >= cur.shape[0]:
                    self._queue.pop(0)
                    self._pos = 0
        if written < frames:
            outdata[written:].fill(0)

    def close(self) -> None:
        s = self._stream
        self._stream = None
        if s is not None:
            try:
                s.stop()
                s.close()
            except Exception:
                pass

    # -- 语义化接口 ------------------------------------------------------

    def play_note(self, stereo: np.ndarray) -> bool:
        return self.play_buffer(stereo)

    def click(self, accent: bool = False) -> None:
        """节拍器咔哒声 —— 高音为重拍。"""
        n = int(0.04 * self.samplerate)
        t = np.arange(n, dtype=np.float64) / self.samplerate
        freq = 1568.0 if accent else 1046.5
        env = np.exp(-t * 90.0)
        y = (np.sin(2 * np.pi * freq * t) * env).astype(np.float32) * 0.35
        self.play_buffer(np.stack([y, y]))


# --------------------------------------------------------------------------
# 全局试听单例
# --------------------------------------------------------------------------

_audition_player: BlipPlayer | None = None


def audition() -> BlipPlayer | None:
    """取全局试听器。所有编辑器页面共用一条输出流。

    共用是刻意的：每条 OutputStream 都要占一个系统音频句柄，多个页面各开一条
    既浪费又容易在切换页面时忘记关闭。而且共用一条流意味着**延迟恒定**，
    不会出现"这个页面点着响、那个页面点着延迟"的割裂感。
    """
    global _audition_player
    if not HAVE_SOUNDDEVICE:
        return None
    if _audition_player is None:
        try:
            _audition_player = BlipPlayer()
        except Exception:
            return None
    return _audition_player


def shutdown_audition() -> None:
    global _audition_player
    if _audition_player is not None:
        try:
            _audition_player.close()
        except Exception:
            pass
        _audition_player = None


__all__ = [
    "Player", "PlayerState", "BlipPlayer", "HAVE_SOUNDDEVICE", "sd",
    "audition", "shutdown_audition",
]
