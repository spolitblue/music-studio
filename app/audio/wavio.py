"""音频与 MIDI 文件读写。

WAV **手写读写器**，不用标准库 ``wave``。原因很实际：Python 的 ``wave`` 模块
完全不支持 IEEE 浮点格式（``WAVE_FORMAT_IEEE_FLOAT``），连读都会直接抛
``unknown format: 3``。而浮点 WAV 是音乐制作里最常用的中间格式（不量化、
不丢精度、可以直接再编辑），所以自己实现。

导出 16/24 位时加 **TPDF 抖动** —— 把量化误差从"与信号相关的失真"变成"与信号
无关的白噪声"，这是专业导出链的标准做法。

MIDI 导出直接手写标准 MIDI 文件（SMF Type 1）：每种乐器一个轨，
鼓轨按 General MIDI 第 10 通道输出，导进任何 DAW 都能直接编辑。
"""

from __future__ import annotations

import struct
from pathlib import Path

import numpy as np

from app.audio import dsp
from app.audio.project import Project

SR = dsp.SR

FMT_PCM = 1
FMT_FLOAT = 3
FMT_EXTENSIBLE = 0xFFFE


# ==========================================================================
# 抖动
# ==========================================================================


def tpdf_dither(x: np.ndarray, bit_depth: int, seed: int = 0x5EED) -> np.ndarray:
    """TPDF（三角概率密度）抖动，幅度为 1 LSB 峰峰值。

    两个独立均匀分布相减得到三角分布 —— 这让量化误差的一阶矩和二阶矩都与信号
    无关，是抖动里性价比最高的一种。
    """
    if bit_depth >= 32:
        return x
    lsb = 2.0 / (2 ** bit_depth)
    rng = np.random.default_rng(seed)
    d = (rng.random(x.shape, dtype=np.float64) - rng.random(x.shape, dtype=np.float64)) * (lsb * 0.5)
    return (x + d).astype(np.float32)


# ==========================================================================
# WAV 写
# ==========================================================================


def _wav_header(channels: int, sr: int, bits: int, data_bytes: int, float_fmt: bool) -> bytes:
    fmt_tag = FMT_FLOAT if float_fmt else FMT_PCM
    block_align = channels * (bits // 8)
    byte_rate = sr * block_align

    fmt_chunk = struct.pack(
        "<HHIIHH", fmt_tag, channels, sr, byte_rate, block_align, bits
    )
    body = b"fmt " + struct.pack("<I", len(fmt_chunk)) + fmt_chunk

    # 非 PCM 格式必须带 fact 块，否则部分解码器会拒绝
    if float_fmt:
        frames = data_bytes // block_align if block_align else 0
        body += b"fact" + struct.pack("<II", 4, frames)

    body += b"data" + struct.pack("<I", data_bytes)

    riff_size = 4 + len(body) + data_bytes
    return b"RIFF" + struct.pack("<I", riff_size) + b"WAVE" + body


def save_wav(
    path: str | Path,
    stereo: np.ndarray,
    sr: int = SR,
    bit_depth: int = 16,
    dither: bool = True,
) -> Path:
    """把 (2, n) 或 (n,) 音频写成 WAV。

    ``bit_depth`` ∈ {16, 24, 32}。其中 **32 表示 32 位浮点**（不做量化、不加抖动）。
    """
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    data = np.asarray(stereo, dtype=np.float32)
    if data.ndim == 1:
        data = np.stack([data, data])
    channels = int(data.shape[0])
    interleaved = np.ascontiguousarray(data.T.reshape(-1))

    if bit_depth == 32:
        payload = np.clip(interleaved, -1.0, 1.0).astype("<f4").tobytes()
        header = _wav_header(channels, int(sr), 32, len(payload), float_fmt=True)
    elif bit_depth == 16:
        src = tpdf_dither(interleaved, 16) if dither else interleaved
        payload = np.round(np.clip(src, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        header = _wav_header(channels, int(sr), 16, len(payload), float_fmt=False)
    elif bit_depth == 24:
        src = tpdf_dither(interleaved, 24) if dither else interleaved
        as_int = np.round(np.clip(src, -1.0, 1.0) * 8388607.0).astype("<i4")
        # 取小端低 3 字节；先 view 成 uint8 再判别连续内存
        b = np.ascontiguousarray(as_int.view(np.uint8).reshape(-1, 4)[:, :3])
        payload = b.tobytes()
        header = _wav_header(channels, int(sr), 24, len(payload), float_fmt=False)
    else:
        raise ValueError(f"不支持的位深: {bit_depth}（可选 16 / 24 / 32）")

    with open(p, "wb") as f:
        f.write(header)
        f.write(payload)
    return p


# ==========================================================================
# WAV 读
# ==========================================================================


def _iter_chunks(raw: bytes):
    """遍历 RIFF 子块，产出 (id, payload)。"""
    pos = 12   # 跳过 "RIFF" + size + "WAVE"
    n = len(raw)
    while pos + 8 <= n:
        cid = raw[pos:pos + 4]
        (size,) = struct.unpack_from("<I", raw, pos + 4)
        start = pos + 8
        end = min(n, start + size)
        yield cid, raw[start:end]
        pos = start + size + (size & 1)   # 块按偶数字节对齐


def load_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """读取 WAV，返回 ``((channels, n) float32, 采样率)``。

    支持 8/16/24/32 位 PCM 与 32/64 位 IEEE 浮点，含 ``WAVE_FORMAT_EXTENSIBLE``。
    """
    raw = Path(path).read_bytes()
    if len(raw) < 12 or raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        raise ValueError(f"不是合法的 WAV 文件: {path}")

    fmt_tag = channels = sr = bits = 0
    data = b""

    for cid, payload in _iter_chunks(raw):
        if cid == b"fmt " and len(payload) >= 16:
            fmt_tag, channels, sr, _byte_rate, _align, bits = struct.unpack_from("<HHIIHH", payload, 0)
            if fmt_tag == FMT_EXTENSIBLE and len(payload) >= 40:
                # 真正的格式藏在 SubFormat GUID 的头两个字节
                (fmt_tag,) = struct.unpack_from("<H", payload, 24)
        elif cid == b"data":
            data = payload

    if not data or channels == 0:
        raise ValueError(f"WAV 缺少 fmt/data 块: {path}")

    if fmt_tag == FMT_FLOAT:
        if bits == 32:
            arr = np.frombuffer(data, dtype="<f4").astype(np.float32)
        elif bits == 64:
            arr = np.frombuffer(data, dtype="<f8").astype(np.float32)
        else:
            raise ValueError(f"不支持的浮点位深: {bits}")
    elif fmt_tag == FMT_PCM:
        if bits == 8:
            arr = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
        elif bits == 16:
            arr = np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        elif bits == 24:
            b = np.frombuffer(data, dtype=np.uint8)
            b = b[: (b.size // 3) * 3].reshape(-1, 3)
            # **符号扩展**：24 位是补码，最高字节的 bit7 是符号位。
            # 若直接补 0x00，−1（0xFFFFFF）会变成 +16777215 —— 整整差 2.0。
            padded = np.empty((b.shape[0], 4), dtype=np.uint8)
            padded[:, :3] = b
            padded[:, 3] = np.where(b[:, 2] & 0x80, 0xFF, 0x00)
            arr = padded.view("<i4").reshape(-1).astype(np.float32) / 8388608.0
        elif bits == 32:
            arr = np.frombuffer(data, dtype="<i4").astype(np.float32) / 2147483648.0
        else:
            raise ValueError(f"不支持的 PCM 位深: {bits}")
    else:
        raise ValueError(f"不支持的 WAV 格式标记: {fmt_tag}")

    arr = np.ascontiguousarray(arr, dtype=np.float32)
    if channels > 1:
        usable = (arr.size // channels) * channels
        arr = arr[:usable].reshape(-1, channels).T
    else:
        arr = arr.reshape(1, -1)
    return arr, int(sr)


# ==========================================================================
# MIDI
# ==========================================================================


def _vlq(value: int) -> bytes:
    """可变长度数量编码 —— MIDI 的 delta time 用的就是它。"""
    if value < 0:
        value = 0
    out = bytearray([value & 0x7F])
    value >>= 7
    while value:
        out.insert(0, (value & 0x7F) | 0x80)
        value >>= 7
    return bytes(out)


def _text_event(kind: int, text: str) -> bytes:
    data = text.encode("utf-8")[:127]
    return bytes([0xFF, kind]) + _vlq(len(data)) + data


#: 轨道类型 → GM 音色号（0 基），让导出的 MIDI 至少有个像样的默认音色
_GM_PROGRAM = {
    "saw": 81, "square": 80, "triangle": 80, "sine": 80, "organ": 16, "noise": 121,
}


def save_midi(path: str | Path, project: Project, ppq: int = 480) -> Path:
    """导出标准 MIDI 文件（SMF Type 1）。"""
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    ticks_per_step = ppq / max(1, project.steps_per_beat)
    tempo = int(round(60_000_000 / max(20.0, project.bpm)))   # 微秒/四分音符

    chunks: list[bytes] = []

    # ---- 轨 0：时间轴信息 ---------------------------------------------
    meta = bytearray()
    meta += _vlq(0) + _text_event(0x03, project.name)              # Track Name
    meta += _vlq(0) + bytes([0xFF, 0x51, 0x03]) + tempo.to_bytes(3, "big")   # Tempo
    meta += _vlq(0) + bytes([0xFF, 0x58, 0x04,
                             project.beats_per_bar, 2, 24, 8])     # Time Signature
    meta += _vlq(0) + bytes([0xFF, 0x2F, 0x00])                    # End of Track
    chunks.append(bytes(meta))

    # ---- 每条轨道一个 MTrk --------------------------------------------
    drum_channel = 9          # GM 约定：第 10 通道（0 基为 9）专供鼓
    melody_channel = 0
    used_melody = 0

    for track in project.tracks:
        if not track.notes:
            continue

        is_drum = track.kind == "drum"
        if is_drum:
            channel = drum_channel
        else:
            channel = melody_channel % 9          # 跳过 9（鼓通道）
            melody_channel += 1
            used_melody += 1

        events: list[tuple[int, bytes]] = []
        events.append((0, _text_event(0x03, track.name)))
        if not is_drum:
            prog = _GM_PROGRAM.get(track.synth.wave, 81)
            events.append((0, bytes([0xC0 | channel, prog])))

        for note in track.sorted_notes():
            start_tick = int(round(project.swung_step(note.step) * ticks_per_step))
            end_tick = int(round(project.swung_step(note.step + max(1, note.length)) * ticks_per_step))
            if end_tick <= start_tick:
                end_tick = start_tick + 1
            pitch = int(np.clip(note.pitch, 0, 127))
            vel = int(np.clip(round(note.velocity * 127), 1, 127))
            events.append((start_tick, bytes([0x90 | channel, pitch, vel])))
            events.append((end_tick, bytes([0x80 | channel, pitch, 0])))

        events.sort(key=lambda e: (e[0], e[1][0] & 0xF0 == 0x90))  # 同刻先关后开

        body = bytearray()
        last = 0
        for tick, payload in events:
            body += _vlq(tick - last) + payload
            last = tick
        body += _vlq(0) + bytes([0xFF, 0x2F, 0x00])
        chunks.append(bytes(body))

    # ---- 组装 ----------------------------------------------------------
    header = b"MThd" + struct.pack(">IHHH", 6, 1, len(chunks), ppq)
    out = bytearray(header)
    for c in chunks:
        out += b"MTrk" + struct.pack(">I", len(c)) + c

    p.write_bytes(bytes(out))
    return p


# ==========================================================================
# 便捷导出
# ==========================================================================


def export_all(
    out_dir: str | Path,
    stem: str,
    stereo: np.ndarray,
    project: Project,
    sr: int = SR,
    bit_depth: int = 16,
    want_midi: bool = True,
    want_project: bool = True,
) -> dict[str, Path]:
    """一次导出 WAV + MIDI + 工程 JSON。"""
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    written: dict[str, Path] = {}

    written["wav"] = save_wav(d / f"{stem}.wav", stereo, sr, bit_depth)
    if want_midi:
        written["midi"] = save_midi(d / f"{stem}.mid", project)
    if want_project:
        written["project"] = project.save(d / f"{stem}.musicproj.json")
    return written


__all__ = [
    "save_wav", "load_wav", "save_midi", "export_all", "tpdf_dither", "SR",
]
