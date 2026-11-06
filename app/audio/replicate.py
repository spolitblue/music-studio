"""复刻 —— 从一段参考音频里提出"这首曲子长什么样"，再照着它重造一首可编辑的曲子。

为什么叫复刻而不是"克隆"
------------------------
音频里没有音符可以还原（那要先做音源分离，超出本工程"纯代码合成"的边界），
但**气质是可以量的**：速度、调式、织体密度、摇摆、明暗、鼓占多少。
把它们量出来，再交给编曲助手按这套参数重新生成 —— 结果不是原曲的复制，
而是"同一路数的另一首"，而且每一轨都是可继续编辑的音符。这才是编曲里
"复刻"这个词真正有用的意思。

与 :mod:`app.audio.transcribe` 的分工
-------------------------------------
* 扒谱（transcribe）：**把声音变成音符**，逐事件。
* 复刻（replicate）：**把声音变成参数**，只统计。
  它复用扒谱的起音与调式估计，所以两者对同一段音频的判断天然一致。

分析产物是纯数据（:class:`ReferenceProfile`），页面把它映射成
编曲助手的 ``GenParams`` —— 音频层不该反过来依赖界面层。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from app.audio import dsp, transcribe as tr

SR = dsp.SR

#: 大小调里最常用的几套和弦走向。复刻时按指纹从里面挑一套，
#: 让"复刻出来的曲子"有走向可循，而不是只有调式没有和声。
PROGRESSIONS: dict[str, tuple[tuple[int, ...], ...]] = {
    "major": ((1, 5, 6, 4), (1, 4, 5, 4), (6, 4, 1, 5), (1, 6, 4, 5)),
    "minor": ((1, 6, 3, 7), (1, 7, 6, 7), (6, 4, 1, 5), (1, 4, 6, 5)),
}


@dataclass
class ReferenceProfile:
    """参考音频的指纹。界面上显示的每一项都能在下面找到出处。"""

    bpm: float = 120.0
    key_pc: int = 0
    mode: str = "major"
    #: 起音密度（每秒多少个）与归一化后的织体密度 0~1
    onset_rate: float = 0.0
    density: float = 0.5
    #: 摇摆：八分音符后半拍比前半拍强多少（0 = 平直，0.3 左右是明显摇摆）
    swing: float = 0.0
    #: 全曲 RMS（dB）与平均谱质心（Hz）—— 响度与明暗
    energy_db: float = -60.0
    brightness_hz: float = 0.0
    #: 打击乐事件占全部事件的比例
    drum_ratio: float = 0.0
    seconds: float = 0.0
    bars: int = 8
    confidence: float = 0.0
    log: list[str] = field(default_factory=list)

    @property
    def key_name(self) -> str:
        names = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
        return f"{names[int(self.key_pc) % 12]} {'大调' if self.mode == 'major' else '小调'}"

    @property
    def brightness_label(self) -> str:
        b = self.brightness_hz
        if b < 1200.0:
            return "偏暗"
        if b < 2600.0:
            return "中性"
        if b < 4500.0:
            return "偏亮"
        return "很亮"

    @property
    def swing_label(self) -> str:
        if self.swing < 0.06:
            return "平直"
        if self.swing < 0.18:
            return "轻微摇摆"
        return "明显摇摆"

    def summary(self) -> str:
        return (f"{self.bpm:.0f} BPM · {self.key_name} · 织体 {self.density * 100:.0f}% · "
                f"{self.swing_label} · {self.brightness_label} · "
                f"鼓占 {self.drum_ratio * 100:.0f}%")

    def metrics(self) -> list[tuple[str, str]]:
        """给界面用的 (指标名, 读数) 列表 —— 让"复刻依据"是看得见的。"""
        return [
            ("速度", f"{self.bpm:.1f} BPM"),
            ("调式", self.key_name),
            ("织体密度", f"{self.onset_rate:.1f} 事件/秒（{self.density * 100:.0f}%）"),
            ("摇摆", f"{self.swing_label}（{self.swing:.2f}）"),
            ("响度", f"{self.energy_db:+.1f} dBFS"),
            ("明暗", f"{self.brightness_hz:.0f} Hz · {self.brightness_label}"),
            ("鼓占比", f"{self.drum_ratio * 100:.0f}%"),
            ("长度", f"{self.seconds:.1f}s ≈ {self.bars} 小节"),
        ]

    def pick_progression(self, seed: int = 0) -> tuple[int, ...]:
        """按调式与种子挑一套和弦走向（同一个种子结果稳定，可复现）。"""
        table = PROGRESSIONS.get(self.mode, PROGRESSIONS["major"])
        return table[int(seed) % len(table)]


def analyze_reference(audio: np.ndarray, sr: int = SR, *,
                      bpm: float | None = None,
                      transcription: tr.TranscribeResult | None = None
                      ) -> ReferenceProfile:
    """量一段参考音频的指纹。``transcription`` 可传入已有的扒谱结果，避免重复分析。"""
    res = transcription if transcription is not None else tr.transcribe(audio, sr, bpm=bpm)
    mono = tr.to_mono(audio)
    if mono.size == 0:
        return ReferenceProfile(log=["音频是空的"])

    profile = ReferenceProfile(
        bpm=float(res.bpm),
        key_pc=int(res.key_pc),
        mode=str(res.mode),
        seconds=float(mono.size / sr),
        confidence=float(res.confidence),
        log=list(res.log),
    )

    peak = float(np.max(np.abs(mono)))
    if peak > 1e-6:
        mono = mono / peak
    profile.energy_db = dsp.gain_to_db(dsp.rms(mono))

    mag = tr._stft_mag(mono)
    if mag.size:
        # 明暗用各帧质心的中位数：少数几帧的尖峰（一记镲）不该定义整首曲子的明暗
        step = max(1, mag.shape[0] // 240)          # 最多算 240 帧，长曲子也很快
        cents = [tr.spectral_centroid(mag[i], sr) for i in range(0, mag.shape[0], step)]
        profile.brightness_hz = float(np.median([c for c in cents if c > 0]) if cents else 0.0)

    events = res.total
    profile.onset_rate = float(events / max(0.5, profile.seconds))
    profile.density = float(np.clip(profile.onset_rate / 5.0, 0.0, 1.0))
    profile.drum_ratio = float(len(res.drums) / events) if events else 0.0
    profile.swing = _estimate_swing(res, sr)
    beat_seconds = 60.0 / max(1e-6, profile.bpm)
    profile.bars = int(np.clip(round(profile.seconds / (4.0 * beat_seconds)), 2, 16))

    profile.log.append(
        f"指纹：{profile.summary()}")
    return profile


def _estimate_swing(res: tr.TranscribeResult, sr: int = SR, *,
                    steps_per_beat: int = 4) -> float:
    """摇摆量：**反拍（奇数 16 分）相对网格晚了多少拍**。

    为什么不能用"后半拍比前半拍弱多少"（第一版）：正拍上落着底鼓与军鼓，
    反拍只有踩镲 —— 那个比值衡量的其实是"重音在正拍"，于是一段规规矩矩的
    直拍鼓型被判成"明显摇摆"（实测 0.35 满值）。摇摆的定义是**时值偏移**，
    不是力度分布。

    量化会把偏移抹掉，所以这里用 :attr:`TranscribeResult.onset_seconds`
    （量化前的起音时刻）：反拍的平均残余 − 正拍的平均残余，单位是拍。
    直拍时两边都接近 0；摇摆时反拍系统性偏后。起音检测本身有约 12ms 抖动
    （≈0.02 拍 @120BPM），所以小于 0.06 一律当平直。
    """
    onsets = getattr(res, "onset_seconds", None) or []
    if len(onsets) < 4 or res.bpm <= 1.0:
        return 0.0
    step_seconds = 60.0 / res.bpm / steps_per_beat
    even: list[float] = []
    odd: list[float] = []
    for t in onsets:
        idx = int(round(t / step_seconds))
        residual = (t / step_seconds - idx) / steps_per_beat      # 单位：拍
        (odd if idx % 2 else even).append(residual)
    if not even or not odd:
        return 0.0
    swing = float(np.mean(odd) - np.mean(even))
    return float(np.clip(swing, 0.0, 0.35))


__all__ = ["ReferenceProfile", "analyze_reference", "PROGRESSIONS", "SR"]
