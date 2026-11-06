"""采样器 —— 从一段音频里**抠出**真实音色，再用它们演奏新的音符。

为什么要有它
------------
本工程此前是"纯代码合成、零采样"：鼓是噪声与正弦拼的，音色是减法合成器给的。
结果是"结构复刻得很像，音色一听就不是那首曲子"——因为参考里的马林巴、雪橇铃、
软镲片，靠减法合成器是**造不出来**的。

采样是另一条路，而且这条路上没有版权上的模糊地带：样本来自用户自己手上的那段
音频，不是外部采样包。做法是——
1. 在参考音频上找起音（复用 :mod:`app.audio.transcribe` 的检测与粗分），
2. 按**鼓件类别**各挑一个"最干净"的实例抠出来当单件（kick / snare / hat…），
3. 再挑一个"最像乐音"的实例当音色样本（马林巴/铃/拨弦那种），
4. 用的时候按音高重采样播放（采样器最朴素的那种用法）。

抠样本的难点不在"剪一段"，在**挑哪一段**：参考里 90% 的起音都叠着别的乐器，
随便剪一个下来会连着别人的尾巴。所以选片标准是"这一下自己是干净的"——
用一个可解释的打分（起音后的能量 / 起音前的能量 + 尾巴的平坦度）来挑。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from app.audio import dsp, transcribe as tr, wavio

SR = dsp.SR


# ==========================================================================
# 数据结构
# ==========================================================================


@dataclass
class OneShot:
    """一个单件样本。``pitch`` 只有乐音样本才有（MIDI 浮点）。"""

    name: str
    samples: np.ndarray                       # (n,) float32 单声道
    sr: int = SR
    pitch: float | None = None
    peak: float = 1.0                         # 归一化之前的峰值（用来还原原始响度关系）
    meta: dict = field(default_factory=dict)

    @property
    def seconds(self) -> float:
        return self.samples.size / float(self.sr)

    def resampled(self, ratio: float) -> np.ndarray:
        """按比例重采样（线性插值）。ratio>1 变高变短，<1 变低变长。"""
        if abs(ratio - 1.0) < 1e-6 or self.samples.size < 2:
            return self.samples
        n_out = max(2, int(round(self.samples.size / ratio)))
        idx = np.arange(n_out, dtype=np.float64) * ratio
        idx = np.clip(idx, 0, self.samples.size - 1)
        i0 = np.floor(idx).astype(np.int64)
        i1 = np.minimum(i0 + 1, self.samples.size - 1)
        frac = (idx - i0).astype(np.float32)
        return (self.samples[i0] * (1.0 - frac) + self.samples[i1] * frac).astype(np.float32)


@dataclass
class SampleKit:
    """一套从参考音频里抠出来的音色。"""

    shots: dict[str, OneShot] = field(default_factory=dict)
    source: str = ""
    sr: int = SR
    log: list[str] = field(default_factory=list)

    def __contains__(self, key: str) -> bool:
        return key in self.shots

    def get(self, key: str) -> OneShot | None:
        return self.shots.get(key)

    @property
    def tone(self) -> OneShot | None:
        return self.shots.get("tone")

    def summary(self) -> str:
        parts = [f"{k}({v.seconds * 1000:.0f}ms)" for k, v in self.shots.items()]
        return f"{len(self.shots)} 个样本：" + "、".join(parts)


# ==========================================================================
# 抠样本
# ==========================================================================

#: 各类鼓件抠多长（秒）。尾巴留够：抠短了听感上是"啪"而不是"咚"。
CUT_SECONDS: dict[str, float] = {
    "kick": 0.40, "kick_808": 0.60, "snare": 0.28, "clap": 0.30, "rim": 0.14,
    "hat_closed": 0.12, "hat_pedal": 0.10, "hat_open": 0.40, "crash": 1.40,
    "ride": 1.00, "tom_low": 0.45, "tom_mid": 0.40, "tom_high": 0.35,
    "cowbell": 0.30, "perc": 0.30,
}

#: 每个起音前面留多少（秒）—— 真实起音往往比检测帧早一两个毫秒
PRE_ROLL = 0.004


def _cut(mono: np.ndarray, sr: int, t0: float, dur: float, *,
         fade_ms: float = 6.0) -> np.ndarray:
    """剪一段，两端加淡入淡出（剪口不淡就是一声"咔"）。"""
    a = max(0, int((t0 - PRE_ROLL) * sr))
    b = min(mono.size, a + int(dur * sr))
    if b - a < 32:
        return np.zeros(0, dtype=np.float32)
    seg = np.asarray(mono[a:b], dtype=np.float32).copy()
    n = max(1, int(fade_ms * 1e-3 * sr))
    n = min(n, seg.size // 2)
    if n > 1:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        seg[:n] *= ramp
        seg[-n:] *= ramp[::-1]
    return seg


def _isolation_score(mono: np.ndarray, sr: int, t0: float, dur: float
                     ) -> tuple[float, float]:
    """这一下"有多干净 + 有多响"，返回 ``(score, level)``。

    为什么不用响度挑：参考里最响的那一下往往叠着整个乐队（一记镲 + 弦乐 + 人声），
    剪下来当"底鼓"完全是灾难。干净比响更重要。

    打分必须是**有界**的 —— 第一版用 ``起音后能量/起音前能量``，遇到
    "起音前是静音"比值就爆到三百万（实测乐音样本因此选到了第 0.116 秒的引子），
    于是"分数最高的那一个"永远是最莫名的那一个。现在两项都做了上限：
    ``prom`` 截到 20，``focus`` 截到 3，分数落在 1~60，再用电平门挡掉近静音。
    """
    a = int(t0 * sr)
    before = mono[max(0, a - int(0.12 * sr)):a]
    seg = mono[a:a + int(dur * sr)]
    if seg.size < 16:
        return 0.0, 0.0
    e_before = float(np.sqrt(np.mean(before ** 2))) if before.size else 1e-9
    e_after = float(np.sqrt(np.mean(seg ** 2)))
    prom = min(e_after / max(e_before, 1e-9), 20.0)
    total = float(np.sum(seg ** 2)) + 1e-12
    third = max(1, seg.size // 3)
    head = float(np.sum(seg[:third] ** 2))
    focus = min((head / total) / 0.33, 3.0)       # 能量集中在前 1/3 → 是"一下"
    level = float(np.max(np.abs(seg)))
    return float(prom * focus), level


def extract_kit(audio: np.ndarray, sr: int = SR, *, max_seconds: float = 90.0,
                min_score: float = 1.5, min_level: float = 0.03,
                live=None) -> SampleKit:
    """从一段音频里抠出一套音色（鼓件单件 + 一个乐音样本）。

    ``live`` 传了 :class:`tools.live_hub.LiveHub` 就把过程推到实时控制台。

    两条门槛（``min_score`` / ``min_level``）是必须的：参考里绝大多数起音都叠着
    别的乐器，而且"安静处的一下"比值很好看却抠不出东西 —— 打分要是没上限、
    又没有电平门，选出来的永远是引子里的那一声。
    """
    mono = tr.to_mono(np.asarray(audio, dtype=np.float32))
    if mono.size > int(max_seconds * sr):
        mono = mono[:int(max_seconds * sr)]
    peak0 = float(np.max(np.abs(mono))) or 1.0
    mono = mono / peak0

    def log(text: str, level: str = "info") -> None:
        kit_log.append(text)
        if live is not None:
            try:
                live.log(text, level=level)
            except Exception:
                pass

    kit_log: list[str] = []
    kit = SampleKit(source="", sr=sr)
    log("开始采样：找起音 → 按类别挑最干净的一下 → 剪下来")

    mag = tr._stft_mag(mono)
    env = tr.onset_envelope(mag, sr)
    onsets = tr.detect_onsets(env, sr)
    log(f"检出 {len(onsets)} 个起音（前 {mono.size / sr:.0f}s）")

    bands = tr.band_signals(mono, sr)
    candidates: dict[str, list[tuple[float, float, float]]] = {}   # piece -> (score, t, level)
    pitched_hits: list[tuple[float, float, int]] = []              # (score, t, midi)
    rejected = 0

    for frame in onsets:
        t0 = frame * tr.HOP / sr
        feat = tr.drum_features(bands, mag, frame, mag.shape[0], sr)
        # 这里的门槛比扒谱松（0.35/160ms）：对"采样"来说，捞到一件能用的音色
        # 比分类精确更重要 —— 归错类的代价只是名字不好听，漏掉的代价是没得用
        percussive = feat.flatness >= 0.28 or feat.decay_ms < 260.0
        if percussive:
            piece = tr.classify_drum(feat)
            dur = CUT_SECONDS.get(piece, 0.30)
            score, level = _isolation_score(mono, sr, t0, dur)
            if score >= min_score and level >= min_level:
                candidates.setdefault(piece, []).append((score, t0, level))
            else:
                rejected += 1
        else:
            midi, conf = tr._best_pitched_segment(
                mono, int(t0 * sr), int((t0 + 0.5) * sr), sr)
            if midi is None or conf <= 0.4:
                continue
            score, level = _isolation_score(mono, sr, t0, 0.60)
            # 乐音样本要的是"有音高、够响、不在引子里"，纯干净度不是重点
            if level < 0.08 or score < 1.0 or t0 < 2.0:
                continue
            pitched_hits.append((conf * score, t0, midi))

    log(f"通过门槛的打击乐候选 {sum(len(v) for v in candidates.values())} 个"
        f"（被电平/干净度挡掉 {rejected} 个）· 乐音候选 {len(pitched_hits)} 个")

    # 每类挑一个：分数最高的那一个（分数已经把"干净"和"响"一起考虑了）
    for piece, items in sorted(candidates.items()):
        items.sort(key=lambda it: it[0], reverse=True)
        score, t0, level = items[0]
        dur = CUT_SECONDS.get(piece, 0.30)
        seg = _cut(mono, sr, t0, dur)
        if seg.size == 0:
            continue
        seg_peak = float(np.max(np.abs(seg))) or 1.0
        kit.shots[piece] = OneShot(name=piece, samples=seg / seg_peak, sr=sr,
                                   peak=seg_peak * peak0,
                                   meta={"score": round(score, 2), "at": round(t0, 3),
                                         "level": round(level, 3),
                                         "n_candidates": len(items)})
        log(f"  {piece:<11} 取自 {t0:6.2f}s · 干净度 {score:5.2f} · 电平 {level:.3f} · "
            f"{len(items)} 个候选 · {dur * 1000:.0f}ms")

    # 乐音样本：挑"最像乐音 × 最干净"的那一下
    if pitched_hits:
        pitched_hits.sort(key=lambda it: it[0], reverse=True)
        score, t0, midi = pitched_hits[0]
        seg = _cut(mono, sr, t0, 0.90, fade_ms=25.0)
        if seg.size:
            seg_peak = float(np.max(np.abs(seg))) or 1.0
            kit.shots["tone"] = OneShot(name="tone", samples=seg / seg_peak, sr=sr,
                                        pitch=float(midi), peak=seg_peak * peak0,
                                        meta={"score": round(score, 2), "at": round(t0, 3)})
            log(f"  tone        取自 {t0:6.2f}s · 基频 MIDI {midi} · 综合分 {score:.2f}")
    if not kit.shots:
        log("没抠出任何样本（这一段可能太空或太吵）", level="warn")
    kit.log = kit_log
    return kit


def save_kit(kit: SampleKit, out_dir: str | Path, *, live=None) -> Path:
    """把样本落成真实文件（人可以直接听），并写 kit.json。"""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    manifest = {"source": kit.source, "sr": kit.sr, "samples": {}}
    for name, shot in kit.shots.items():
        path = out / f"{name}.wav"
        wavio.save_wav(path, np.stack([shot.samples, shot.samples]), kit.sr,
                       bit_depth=24)
        manifest["samples"][name] = {
            "file": path.name, "seconds": round(shot.seconds, 3),
            "pitch": shot.pitch, "peak": round(shot.peak, 5), "meta": shot.meta,
        }
        if live is not None:
            try:
                live.file(path, label=f"样本 · {name}", kind="audio")
                live.wave(shot.samples, kit.sr, label=f"样本 {name} · {shot.seconds * 1000:.0f}ms")
            except Exception:
                pass
    (out / "kit.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2),
                                  encoding="utf-8")
    return out


def load_kit(path: str | Path) -> SampleKit:
    """从目录里读回一套音色。"""
    base = Path(path)
    manifest = json.loads((base / "kit.json").read_text(encoding="utf-8"))
    kit = SampleKit(source=manifest.get("source", ""), sr=manifest.get("sr", SR))
    for name, info in manifest["samples"].items():
        audio, sr = wavio.load_wav(base / info["file"])
        mono = np.asarray(audio, dtype=np.float32)
        mono = mono.mean(axis=0) if mono.ndim == 2 else mono
        kit.shots[name] = OneShot(name=name, samples=mono, sr=sr,
                                  pitch=info.get("pitch"), peak=info.get("peak", 1.0),
                                  meta=info.get("meta") or {})
    return kit


# ==========================================================================
# 用样本演奏
# ==========================================================================


def play_into(buffer: np.ndarray, shot: OneShot, t_seconds: float, *,
              gain: float = 1.0, pitch: float | None = None,
              root: float | None = None, sr: int = SR) -> None:
    """把一个样本叠进缓冲区（就地相加）。

    ``pitch``/``root`` 给乐音样本用：按半音差重采样（采样器最朴素的用法）。
    鼓件不传，就原样播放。
    """
    data = shot.samples
    if pitch is not None:
        r0 = root if root is not None else shot.pitch
        if r0 is not None:
            data = shot.resampled(2.0 ** ((float(pitch) - float(r0)) / 12.0))
    start = int(t_seconds * sr)
    if start >= buffer.size:
        return
    n = min(data.size, buffer.size - start)
    if n <= 0:
        return
    buffer[start:start + n] += data[:n] * float(gain)


def render_hits(events, kit: SampleKit, *, seconds: float, sr: int = SR,
                gain: float = 1.0) -> np.ndarray:
    """把一串 ``(时刻, 样本名, 力度)`` 渲染成单声道缓冲。"""
    buf = np.zeros(int(seconds * sr) + sr, dtype=np.float32)
    for t, name, vel in events:
        shot = kit.get(name)
        if shot is None:
            continue
        play_into(buf, shot, t, gain=gain * float(vel), sr=sr)
    return buf


# ==========================================================================
# Minecraft 音符盒音色
# ==========================================================================

#: 音符盒乐器 → 资产里的文件名。1.20+ 改过几个名字（xylophone→xylobone、
#: chime→icechime），所以这里存的是**游戏里的乐器名**，文件名的差异在加载时兜。
MC_INSTRUMENTS: dict[str, str] = {
    "basedrum": "bd", "snare": "snare", "hat": "hat", "bass": "bass",
    "harp": "harp", "bell": "bell", "flute": "flute", "chime": "icechime",
    "guitar": "guitar", "xylophone": "xylobone", "iron_xylophone": "iron_xylophone",
    "cow_bell": "cow_bell", "didgeridoo": "didgeridoo", "bit": "bit",
    "banjo": "banjo", "pling": "pling", "trumpet": "trumpet",
}
MC_ALIASES: dict[str, tuple[str, ...]] = {
    "xylophone": ("xylobone", "xylophone"),
    "chime": ("icechime", "chime"),
    "basedrum": ("bd", "basedrum", "bassattack"),
    "harp": ("harp", "harp2"),
}

#: 音符盒样本在资产索引里的路径前缀
MC_NOTE_PREFIX = "minecraft/sounds/note/"

#: 音符盒的基准音：游戏里 F#4 那一格播放的就是样本原速
MC_BASE_PITCH = 66.0

#: 打击乐三件（不参与移调 —— 底鼓/军鼓/踩镲在游戏里也不按音高用）
MC_PERCUSSION: tuple[str, ...] = ("basedrum", "snare", "hat")


def find_minecraft_assets() -> Path | None:
    """找 Minecraft 的 assets 目录（多启动器的常见位置都试一遍）。"""
    import os
    home = Path.home()
    cands = [
        home / ".minecraft" / "assets",
        Path(os.environ.get("APPDATA", "")) / ".minecraft" / "assets",
        home / "AppData" / "Roaming" / ".minecraft" / "assets",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Packages" / "Microsoft.4297127D64EC6_8wekyb3d8bbwe"
        / "LocalCache" / "Roaming" / ".minecraft" / "assets",
    ]
    for c in cands:
        if c and (c / "indexes").is_dir() and (c / "objects").is_dir():
            return c
    return None


def _mc_index(assets: Path) -> dict:
    """挑一个 note 样本最全的资产索引（版本越新越全）。"""
    import json
    best: tuple[int, dict] = (0, {})
    for f in sorted((assets / "indexes").glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        objs = data.get("objects") or {}
        notes = {k: v for k, v in objs.items() if MC_NOTE_PREFIX in k}
        if len(notes) > best[0]:
            best = (len(notes), notes)
    return best[1]


def _trim_shot(mono: np.ndarray, sr: int, *, keep: float = 1.6) -> np.ndarray:
    """去掉头尾静音、限长、两端淡入淡出。"""
    if mono.size == 0:
        return mono
    peak = float(np.max(np.abs(mono))) or 1.0
    loud = np.nonzero(np.abs(mono) > peak * 0.02)[0]
    if loud.size:
        mono = mono[max(0, loud[0] - int(0.002 * sr)): loud[-1] + int(0.02 * sr)]
    mono = mono[: int(keep * sr)]
    n = min(int(0.004 * sr), mono.size // 4)
    if n > 1:
        ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
        mono = mono.copy()
        mono[:n] *= ramp
        mono[-n:] *= ramp[::-1]
    pk = float(np.max(np.abs(mono))) or 1.0
    return (mono / pk).astype(np.float32)


def load_note_block_kit(assets_dir: str | Path | None = None, *,
                        include: tuple[str, ...] | None = None,
                        live=None) -> SampleKit:
    """把 Minecraft 的音符盒音色读成一套样本。

    为什么直接读游戏资产而不是自己合成：音符盒的音色就是这个游戏的一部分
    （木琴/铃/竖琴/贝斯/鼓都是**录好的样本**，游戏只是把它们按音高重采样），
    自己做一套"像它的"合成音色既不像也没必要。这里读的是本机 .minecraft
    里的原文件，不联网、不改游戏文件。
    """
    from app.audio import audiodecode
    assets = Path(assets_dir) if assets_dir else find_minecraft_assets()
    if assets is None or not (assets / "objects").is_dir():
        raise FileNotFoundError("找不到 Minecraft 的 assets 目录（可用 assets_dir 指定）")
    index = _mc_index(assets)
    if not index:
        raise FileNotFoundError(f"{assets} 的索引里没有音符盒样本")

    def log(text: str, level: str = "info") -> None:
        kit_log.append(text)
        if live is not None:
            try:
                live.log(text, level=level)
            except Exception:
                pass

    kit_log: list[str] = []
    kit = SampleKit(source=f"Minecraft · {assets}", sr=SR)
    wanted = list(include) if include else list(MC_INSTRUMENTS)
    for instrument in wanted:
        names = MC_ALIASES.get(instrument, (MC_INSTRUMENTS.get(instrument, instrument),))
        entry = None
        picked = ""
        for name in names:
            entry = index.get(f"{MC_NOTE_PREFIX}{name}.ogg")
            if entry is not None:
                picked = name
                break
        if entry is None:
            log(f"  {instrument:<16} 资产里没有，跳过", level="warn")
            continue
        obj = assets / "objects" / entry["hash"][:2] / entry["hash"]
        if not obj.exists():
            log(f"  {instrument:<16} 资产对象缺失（{entry['hash'][:10]}），跳过", level="warn")
            continue
        try:
            audio, _ = audiodecode.decode(obj, SR, channels=1)
        except Exception as exc:                       # noqa: BLE001
            log(f"  {instrument:<16} 解码失败：{exc}", level="warn")
            continue
        mono = np.asarray(audio, dtype=np.float32)
        mono = mono.mean(axis=0) if mono.ndim == 2 else mono
        mono = _trim_shot(mono, SR)
        if mono.size < 64:
            continue
        # 基频：**用实测值当根音**，测不出来才退回 F#4。
        #
        # 为什么不是一律钉在 F#4（游戏里的基准音）：游戏对**所有乐器**用同一个
        # note→速率映射，于是同一格里贝斯比其他乐器低两个八度 —— 那是样本本身
        # 的音区差。可我们的贝斯音符在 MIDI 36~47，若按 F#4 当根音就要把 352ms
        # 的样本拉长 30 个半音（速率 0.18），直接变成 16Hz 的嗡嗡声。
        # 用实测基频当根音，则"每个样本按自己的真实音高演奏"：音程准、
        # 各乐器留在自己的音区里（贝斯 42 / 竖琴 66 / 铃 78 本来就是这样）。
        # 打击乐不参与移调。
        midi = None
        measured = None
        if instrument not in MC_PERCUSSION:
            try:
                guess, conf = tr._autocorr_pitch(mono[: min(mono.size, int(0.25 * SR))], SR)
                if guess is not None and conf > 0.3:
                    measured = float(guess)
            except Exception:
                measured = None
            midi = measured if measured is not None else MC_BASE_PITCH
        kit.shots[instrument] = OneShot(
            name=instrument, samples=mono, sr=SR, pitch=midi,
            meta={"file": f"{picked}.ogg", "seconds": round(mono.size / SR, 3),
                  "pitch": midi, "measured_pitch": measured})
        log(f"  {instrument:<16} {picked + '.ogg':<20} {mono.size / SR * 1000:6.0f}ms"
            + (f" · 根音 MIDI {midi:.0f}（实测 {measured}）" if midi
               else " · 打击乐，不参与移调"))
    if not kit.shots:
        raise RuntimeError("一个音符盒样本都没读出来")
    kit.log = kit_log
    return kit


__all__ = ["OneShot", "SampleKit", "extract_kit", "save_kit", "load_kit",
           "load_note_block_kit", "find_minecraft_assets", "MC_INSTRUMENTS",
           "play_into", "render_hits", "CUT_SECONDS", "SR"]
