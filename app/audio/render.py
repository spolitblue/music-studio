"""混音渲染管线 —— 从工程数据到一段立体声音频。

信号流
------
::

    每条轨道:
        音符 → 合成器/鼓机 → 轨道效果链 → 音量 → 声像 ─┬→ 干声总线 ──┐
                                                      ├→ 混响发送 ──┤
                                                      └→ 延迟发送 ──┤
                                                                    ↓
    总线: 混响返回 + 延迟返回 ──→ 母带音量 ──→ 限幅器 ──→ 淡入淡出 ──→ 输出

设计要点
--------
* **发送式混响/延迟**：轨道上挂的是"送多少"，混响本身只在总线上算一次。
  否则 8 条轨道各自卷积一遍混响 = 8 倍开销，且尾巴互不相干、糊成一团。
* **独奏优先于静音**：与所有 DAW 一致。
* **限幅带前视（lookahead）**：增益在峰值到来**之前**就压下去，所以不需要
  为了削峰而整体降电平，成品能保持响度。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from app.audio import dsp, effects as fx, synth, drums
from app.audio.project import Project, Track

SR = dsp.SR


# ==========================================================================
# 限幅器
# ==========================================================================


def limiter(
    stereo: np.ndarray,
    threshold_db: float = -1.0,
    release: float = 0.10,
    ceiling: float = 0.99,
    lookahead: float = 0.002,
    sr: int = SR,
) -> np.ndarray:
    """前视限幅器。

    1. 用滑动窗口最大值算出"未来一段时间内的峰值包络"；
    2. 据此得出所需增益，因此增益在峰值之前就已经开始下降（无过冲、无失真）；
    3. 对增益做移动平均当作释放平滑，最后再串一级软削波兜底。
    """
    if stereo.size == 0:
        return stereo

    n = stereo.shape[1]
    threshold = float(dsp.db_to_gain(threshold_db))
    la = max(1, int(lookahead * sr))
    win = min(la, max(1, n // 2))

    # 两声道联动（否则会破坏声像）
    peak = np.maximum(np.abs(stereo[0]), np.abs(stereo[1]))

    if win > 1 and n > win:
        from numpy.lib.stride_tricks import sliding_window_view
        # 向前看 win 个样本，取最大值 —— 这就是"预知未来"
        padded = np.concatenate([peak, np.zeros(win, dtype=peak.dtype)])
        env = sliding_window_view(padded, win).max(axis=1)[:n]
    else:
        env = peak

    # 所需增益（峰值低于阈值时不动作，保持 1.0）
    gain = np.minimum(1.0, threshold / np.maximum(env, 1e-9))

    # 释放平滑：增益回升要慢，否则会产生"抽气"感
    rel = max(1, int(release * sr))
    if rel > 1 and n > rel:
        kernel = np.ones(rel, dtype=np.float64) / rel
        # 只平滑"回升"方向：取 running minimum 后再平均
        smooth = np.convolve(gain, kernel, mode="same")
        gain = np.minimum(gain, smooth)

    out = stereo * gain

    # 兜底：限幅器毕竟只是近似，末端软削波保证绝不越界
    if dsp.peak(out) > ceiling:
        out = dsp.soft_clip(out / ceiling, 1.0) * ceiling

    return out.astype(np.float32)


# ==========================================================================
# 轨道渲染
# ==========================================================================


def _blank(seconds: float, sr: int = SR) -> np.ndarray:
    return np.zeros((2, max(2, int(seconds * sr))), dtype=np.float32)


def _place(buffer: np.ndarray, chunk: np.ndarray, start: int) -> None:
    """把一段音频叠加到 buffer 的指定位置（越界自动截断）。"""
    if chunk.size == 0 or start >= buffer.shape[1]:
        return
    if start < 0:
        cut = -start
        if cut >= chunk.shape[1]:
            return
        chunk = chunk[:, cut:]
        start = 0
    end = min(buffer.shape[1], start + chunk.shape[1])
    if end <= start:
        return
    buffer[:, start:end] += chunk[:, : end - start]


def render_track(track: Track, project: Project, sr: int = SR,
                 note_callback: Callable[[int, int], None] | None = None) -> np.ndarray:
    """渲染一条轨道（含其效果链），返回干声 (2, n)。"""
    total_seconds = project.duration_seconds() + 1.0
    buf = _blank(total_seconds, sr)

    spb = project.seconds_per_step
    notes = track.sorted_notes()

    if track.kind == "drum":
        track.ensure_kit()

    prev_pitch: int | None = None
    for i, note in enumerate(notes):
        start = int(round(project.step_time(note.step) * sr))
        dur = max(1, note.length) * spb

        if track.kind == "drum":
            piece = _pitch_to_piece(note.pitch)
            voice = track.kit.get(piece)
            if voice is None or not voice.enabled:
                continue
            chunk = drums.render_drum(piece, voice, note.velocity, sr)
        else:
            # 松开时长：音符的实际发声长度略短于格子长度，音符之间才有"呼吸"
            sounding = max(0.02, dur * 0.96)
            chunk = synth.render_synth_note(
                track.synth, note.pitch, note.velocity, sounding, sr,
                glide_from=prev_pitch if track.synth.glide > 0 else None,
            )
            prev_pitch = note.pitch

        _place(buf, chunk, start)
        if note_callback is not None:
            note_callback(i + 1, len(notes))

    # ---- 轨道效果链 ----------------------------------------------------
    if track.effects:
        buf = fx.apply_chain(buf, track.effects, project.bpm, project.steps_per_beat, sr)

    return buf


def _pitch_to_piece(pitch: int) -> str:
    from app.audio.project import pitch_to_drum

    return pitch_to_drum(pitch)


# ==========================================================================
# 工程渲染
# ==========================================================================


@dataclass
class RenderStats:
    seconds: float = 0.0
    tracks: int = 0
    notes: int = 0
    peak: float = 0.0
    peak_db: float = -120.0
    rms_db: float = -120.0
    limited: bool = False
    sample_rate: int = SR


def render_project(
    project: Project,
    sr: int = SR,
    progress: Callable[[float, str], None] | None = None,
    apply_master_fx: bool = True,
) -> tuple[np.ndarray, RenderStats]:
    """把整个工程渲染成 (2, n) 立体声。

    ``progress`` 会收到 (0..1 的进度, 说明文字)，供界面显示。
    """
    t0 = time.perf_counter()
    stats = RenderStats(sample_rate=sr)

    audible = [t for t in project.tracks if project.is_audible(t)]
    stats.tracks = len(audible)
    stats.notes = sum(len(t.notes) for t in audible)

    total_seconds = project.duration_seconds() + 1.0
    dry = _blank(total_seconds, sr)
    reverb_send = _blank(total_seconds, sr)
    delay_send = _blank(total_seconds, sr)

    if not audible:
        stats.seconds = time.perf_counter() - t0
        return dry, stats

    total_notes = max(1, stats.notes)
    done_notes = 0

    for ti, track in enumerate(audible):
        label = f"渲染「{track.name}」"
        if progress:
            progress(0.05 + 0.70 * ti / len(audible), label)

        def _tick(cur: int, total: int, _base=done_notes) -> None:
            if progress and total:
                frac = (ti + cur / total) / len(audible)
                progress(0.05 + 0.70 * frac, label)

        trk = render_track(track, project, sr, note_callback=_tick)
        done_notes += len(track.notes)

        # ---- 混音台：音量 + 声像 + 发送 --------------------------------
        vol = float(track.volume)
        gain_l, gain_r = dsp.equal_power_pan(track.pan)

        if track.send_reverb > 1e-4:
            reverb_send[0] += trk[0] * vol * track.send_reverb
            reverb_send[1] += trk[1] * vol * track.send_reverb
        if track.send_delay > 1e-4:
            delay_send[0] += trk[0] * vol * track.send_delay
            delay_send[1] += trk[1] * vol * track.send_delay

        dry[0] += trk[0] * vol * gain_l
        dry[1] += trk[1] * vol * gain_r

    # ---- 总线效果（只算一次）------------------------------------------
    mix = dry
    if apply_master_fx:
        if progress:
            progress(0.78, "总线混响")
        if dsp.peak(reverb_send) > 1e-5:
            wet = fx.apply_reverb(reverb_send, {**project.master.reverb.params, "mix": 1.0}, sr)
            mix = mix + wet
        if progress:
            progress(0.85, "总线延迟")
        if dsp.peak(delay_send) > 1e-5:
            wet = fx.apply_delay(delay_send, {**project.master.delay.params, "mix": 1.0},
                                 project.bpm, project.steps_per_beat, sr)
            mix = mix + wet

    # ---- 母带 ----------------------------------------------------------
    if progress:
        progress(0.92, "母带处理")

    # DC 阻断：合成类音源（尤其是软削波的底鼓、非对称的鼓件）会带一点直流，
    # 累加后既浪费动态余量，又会让限幅器误动作。所有专业母带链都有这一级。
    mix = np.stack([
        dsp.biquad_static(mix[0], "hp", 18.0, 0.707, sr),
        dsp.biquad_static(mix[1], "hp", 18.0, 0.707, sr),
    ])

    mix = mix * float(project.master.volume)

    pre_peak = dsp.peak(mix)
    if project.master.limiter_enabled and pre_peak > 1e-6:
        mix = limiter(
            mix,
            threshold_db=project.master.limiter_threshold,
            release=project.master.limiter_release,
            sr=sr,
        )
        stats.limited = dsp.peak(mix) < pre_peak - 1e-4 or pre_peak > dsp.db_to_gain(
            project.master.limiter_threshold)

    # 首尾淡入淡出 —— 避免播放开始/结束的爆音
    mix = dsp.fade(
        mix,
        fade_in=int(max(1.0, project.master.fade_in * sr)),
        fade_out=int(max(1.0, project.master.fade_out * sr)),
    )

    stats.peak = dsp.peak(mix)
    stats.peak_db = dsp.gain_to_db(stats.peak)
    stats.rms_db = dsp.gain_to_db(dsp.rms(mix))
    stats.seconds = time.perf_counter() - t0

    if progress:
        progress(1.0, "完成")
    return mix.astype(np.float32), stats


# ==========================================================================
# 快速预览（给波形图/试听用，跳过总线效果）
# ==========================================================================


def render_preview(project: Project, sr: int = SR) -> np.ndarray:
    """只渲染干声的快速预览，用于画波形、做缩略图。"""
    return render_project(project, sr, progress=None, apply_master_fx=False)[0]


def waveform_envelope(stereo: np.ndarray, buckets: int = 600) -> np.ndarray:
    """把音频压成 ``buckets`` 个 (min, max) 对，供绘制波形。

    返回形状 ``(buckets, 2)``：``[:,0]`` 是最小值，``[:,1]`` 是最大值。
    取每个桶的极值而非平均 —— 波形的信息都在峰值上。
    """
    if stereo.size == 0 or buckets <= 0:
        return np.zeros((max(1, buckets), 2), dtype=np.float32)

    mono = stereo.mean(axis=0) if stereo.ndim == 2 else stereo
    n = mono.size
    buckets = max(1, min(buckets, n))
    edges = np.linspace(0, n, buckets + 1).astype(np.int64)

    out = np.zeros((buckets, 2), dtype=np.float32)
    for i in range(buckets):
        a, b = edges[i], max(edges[i] + 1, edges[i + 1])
        seg = mono[a:b]
        if seg.size:
            out[i, 0] = float(seg.min())
            out[i, 1] = float(seg.max())
    return out


def meter(stereo: np.ndarray) -> tuple[float, float]:
    """返回 (峰值 dB, 有效值 dB)，供电平表使用。"""
    return dsp.gain_to_db(dsp.peak(stereo)), dsp.gain_to_db(dsp.rms(stereo))


__all__ = [
    "limiter", "render_track", "render_project", "render_preview",
    "waveform_envelope", "meter", "RenderStats", "SR",
]
