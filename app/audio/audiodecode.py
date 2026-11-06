"""任意音频 → ``(channels, n) float32``。

为什么需要它
------------
``wavio.load_wav`` 只认 WAV，而参考音频的现实来源多半是 mp3 / m4a。
本工程的音频层**故意不引入任何解码库**（纯代码合成、零采样依赖），
但 **PySide6 本身就带 QtMultimedia** —— 它在 Windows 上走 Media Foundation
（本机实测走的是随包附带的 FFmpeg 7.1.1），mp3 / aac / flac / ogg 都能解。
于是"能读 mp3"这件事不需要新增一个字节的依赖，也不要求用户去装 ffmpeg。

分派规则
--------
* ``.wav`` → :func:`app.audio.wavio.load_wav`（保留位深/浮点的精确语义）
* 其它   → :func:`decode`（QtMultimedia）

没装 QtMultimedia 或解码失败时**报错而不是静默返回空缓冲** ——
静默失败会让"扒谱结果是空的"变成一个查不出来的谜。
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from app.audio import dsp, wavio

SR = dsp.SR

#: 文件对话框用的后缀表（不含点）—— QtMultimedia 在本机的可用集合
AUDIO_SUFFIXES: tuple[str, ...] = (
    "wav", "mp3", "m4a", "aac", "flac", "ogg", "opus", "wma", "aiff", "aif",
)

#: ``QFileDialog`` 的过滤器文本（"音频文件 (*.wav *.mp3 …)"）
FILE_FILTER = "音频文件 (" + " ".join(f"*.{s}" for s in AUDIO_SUFFIXES) + ");;所有文件 (*)"


def _buffer_to_array(buf) -> np.ndarray:
    """``QAudioBuffer`` → (channels, frames) float32。

    PySide6 各版本对 ``constData()`` 的包装不一致（有的给 memoryview，
    有的给只读指针），所以两条路都试；都失败就明确抛错。
    """
    fmt = buf.format()
    ch = max(1, int(fmt.channelCount()))
    frames = int(buf.frameCount())
    for getter in ("constData", "data"):
        fn = getattr(buf, getter, None)
        if fn is None:
            continue
        try:
            arr = np.frombuffer(memoryview(fn()), dtype=np.float32, count=frames * ch)
            return arr.reshape(frames, ch).T.copy()
        except (TypeError, ValueError, BufferError):
            continue
    raise RuntimeError("拿不到 QAudioBuffer 的原始样本（constData/data 都不可用）")


def _qt_multimedia():
    """延迟导入 QtMultimedia 里解码要用的几个名字（没装时抛出可读的错误）。"""
    try:
        from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer, QUrl
        from PySide6.QtMultimedia import QAudioDecoder, QAudioFormat
    except ImportError as exc:                       # pragma: no cover - 环境缺件
        raise RuntimeError(f"这台机器上的 PySide6 没有 QtMultimedia（{exc}）") from exc
    return (QCoreApplication, QEventLoop, QTimer, QUrl, QAudioDecoder, QAudioFormat)


def decode(path: str | Path, sr: int = SR, *, channels: int = 2,
           timeout_s: float = 600.0) -> tuple[np.ndarray, int]:
    """用 QtMultimedia 解码任意音频，返回 ``((channels, n) float32, sr)``。

    ``sr`` 是**目标**采样率：解码器会自己重采样（输入 48k 也直接给 44.1k）。
    """
    src = Path(path)
    if not src.exists():
        raise FileNotFoundError(f"没有这个文件：{src}")
    (QCoreApplication, QEventLoop, QTimer, QUrl,
     QAudioDecoder, QAudioFormat) = _qt_multimedia()

    app = QCoreApplication.instance() or QCoreApplication(sys.argv[:1])

    fmt = QAudioFormat()
    fmt.setSampleRate(int(sr))
    fmt.setChannelCount(int(channels))
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)

    dec = QAudioDecoder()
    dec.setAudioFormat(fmt)
    dec.setSource(QUrl.fromLocalFile(str(src)))

    chunks: list[np.ndarray] = []
    state: dict[str, str] = {}

    def _on_buffer() -> None:
        buf = dec.read()
        if buf.isValid() and buf.frameCount():
            chunks.append(_buffer_to_array(buf))

    loop = QEventLoop()
    dec.bufferReady.connect(_on_buffer)
    dec.finished.connect(loop.quit)
    dec.error.connect(lambda err: (state.__setitem__("error", str(err)), loop.quit()))

    # 兜底：容器损坏时解码器可能既不 finished 也不报错，不能把调用方挂死
    guard = QTimer()
    guard.setSingleShot(True)
    guard.timeout.connect(lambda: (state.__setitem__("timeout", "1"), loop.quit()))
    guard.start(int(timeout_s * 1000))

    dec.start()
    loop.exec()
    guard.stop()

    if "error" in state:
        raise RuntimeError(f"解码失败：{state['error']}")
    if "timeout" in state:
        raise TimeoutError(f"解码超过 {timeout_s:.0f}s 仍未结束：{src.name}")
    if not chunks:
        raise RuntimeError(f"解码器没有产出任何样本（格式可能不受支持）：{src.name}")

    audio = np.concatenate(chunks, axis=1)
    del app, dec                    # 让 Qt 对象在解释器退出前有序释放
    return audio, int(sr)


def load_audio_any(path: str | Path, *, sr: int = SR,
                   channels: int = 2) -> tuple[np.ndarray, int]:
    """读任意音频：WAV 走自家读取器（保留原始采样率），其它走 QtMultimedia。"""
    src = Path(path)
    if src.suffix.lower() == ".wav":
        audio, file_sr = wavio.load_wav(src)
        return np.asarray(audio, dtype=np.float32), int(file_sr)
    return decode(src, sr, channels=channels)


__all__ = ["SR", "AUDIO_SUFFIXES", "FILE_FILTER", "decode", "load_audio_any"]
