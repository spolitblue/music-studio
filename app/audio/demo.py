"""内置示范工程 —— 打开软件立刻有东西可听、可改。

不是随便糊几个音：这里写的是一段能听的 8 小节循环，
和弦走向 Am–F–C–G（华语流行/日系最常见的进行之一），
鼓组、贝斯、铺底、旋律各司其职，可以直接拿去改。

所有音符都由代码生成，因此这个工程同时也是**渲染管线的回归测试样本**。
"""

from __future__ import annotations

from app.audio import drums
from app.audio.project import (
    ADSR, Effect, FilterParams, MasterParams, Note, Project, SynthParams, Track,
)


def _n(step: int, pitch: int, length: int = 1, vel: float = 0.85) -> Note:
    return Note(step=step, pitch=pitch, length=length, velocity=vel)


# --------------------------------------------------------------------------
# 和声骨架
# --------------------------------------------------------------------------

#: (和弦名, 根音 MIDI, 三和弦音高)
PROGRESSION: tuple[tuple[str, int, tuple[int, int, int]], ...] = (
    ("Am", 45, (57, 60, 64)),   # A2  / A3 C4 E4
    ("F",  41, (53, 57, 60)),   # F2  / F3 A3 C4
    ("C",  48, (52, 55, 60)),   # C3  / E3 G3 C4
    ("G",  43, (55, 59, 62)),   # G2  / G3 B3 D4
)

#: A 自然小调音阶（用于旋律）
A_MINOR = (57, 59, 60, 62, 64, 65, 67, 69, 71, 72, 74, 76)

#: 旋律：(bar, step_in_bar, 音高, 时值)
MELODY: tuple[tuple[int, int, int, int], ...] = (
    # 前 4 小节 —— 主题陈述
    (0, 0, 69, 2), (0, 2, 71, 2), (0, 4, 72, 4), (0, 8, 71, 2), (0, 10, 69, 2), (0, 12, 67, 4),
    (1, 0, 65, 2), (1, 2, 67, 2), (1, 4, 69, 6), (1, 12, 72, 4),
    (2, 0, 71, 2), (2, 2, 72, 2), (2, 4, 74, 4), (2, 8, 72, 2), (2, 10, 71, 2), (2, 12, 69, 4),
    (3, 0, 67, 4), (3, 4, 65, 2), (3, 6, 64, 2), (3, 8, 62, 8),
    # 后 4 小节 —— 变奏，音区抬高
    (4, 0, 72, 2), (4, 2, 74, 2), (4, 4, 76, 4), (4, 8, 74, 2), (4, 10, 72, 2), (4, 12, 71, 4),
    (5, 0, 69, 2), (5, 2, 71, 2), (5, 4, 72, 6), (5, 12, 74, 4),
    (6, 0, 76, 2), (6, 2, 74, 2), (6, 4, 72, 4), (6, 8, 71, 2), (6, 10, 69, 2), (6, 12, 67, 2),
    (7, 0, 65, 2), (7, 2, 67, 2), (7, 4, 69, 8), (7, 14, 64, 2),
)


# --------------------------------------------------------------------------
# 各轨构建
# --------------------------------------------------------------------------


def _drum_track() -> Track:
    t = Track(
        name="鼓组",
        kind="drum",
        icon="drums",
        color="#F4900C",
        volume=0.9,
        pan=0.0,
        send_reverb=0.06,
    )
    t.kit = drums.default_voices()

    kick, snare, clap = 36, 38, 39
    hh, ho = 42, 46
    crash, ride, rim = 49, 51, 37
    tom_l, tom_h = 41, 45

    for bar in range(8):
        base = bar * 16
        last = bar == 7

        # 底鼓：四拍踩点 + 切分，第 8 小节加花
        for s in (0, 6, 10):
            t.add_note(_n(base + s, kick, 1, 1.0 if s == 0 else 0.9))
        if last:
            t.add_note(_n(base + 14, kick, 1, 0.8))
        else:
            t.add_note(_n(base + 8, kick, 1, 0.94))

        # 军鼓：反拍打点（2、4 拍）
        t.add_note(_n(base + 4, snare, 1, 0.9))
        t.add_note(_n(base + 12, snare, 1, 0.92))

        # 拍手只在副歌段（后 4 小节）叠在军鼓上
        if bar >= 4:
            t.add_note(_n(base + 12, clap, 1, 0.7))

        # 闭踩镲：八分音符摆
        for s in range(0, 16, 2):
            vel = 0.62 if s % 4 == 0 else 0.42
            t.add_note(_n(base + s, hh, 1, vel))

        # 开踩镲收句
        t.add_note(_n(base + 14, ho, 1, 0.5))

        # 边缘点缀
        if bar % 2 == 1:
            t.add_note(_n(base + 7, rim, 1, 0.5))
        if last:
            # 结尾过门
            t.add_note(_n(base + 8, tom_h, 1, 0.8))
            t.add_note(_n(base + 10, tom_h, 1, 0.75))
            t.add_note(_n(base + 12, tom_l, 1, 0.8))
            t.add_note(_n(base + 13, tom_l, 1, 0.7))
        if bar in (0, 4):
            t.add_note(_n(base, crash, 1, 0.62))
        if bar >= 4:
            t.add_note(_n(base + 2, ride, 1, 0.35))

    return t


def _bass_track() -> Track:
    sp = SynthParams(
        wave="saw",
        octave=-1,
        unison=2,
        detune_cents=6.0,
        spread=0.25,
        glide=0.02,
        amp=ADSR(0.004, 0.12, 0.72, 0.10, 2.2),
        filter=FilterParams(
            kind="lp", cutoff=520.0, resonance=2.4, env_amount=0.30,
            env=ADSR(0.003, 0.16, 0.0, 0.10, 2.6),
        ),
        volume=0.95,
    )
    t = Track(
        name="贝斯", kind="synth", icon="guitar", color="#77B255",
        synth=sp, volume=0.85, pan=0.0, send_reverb=0.02,
    )

    for bar in range(8):
        base = bar * 16
        root = PROGRESSION[bar % 4][1]
        # 八分音符律动，重音落在 0/6/8/10
        pattern = ((0, 3), (3, 2), (6, 2), (8, 4), (12, 2), (14, 2))
        for s, length in pattern:
            vel = 0.95 if s in (0, 8) else 0.72
            t.add_note(_n(base + s, root, length, vel))
        # 每两小节末尾加一个上行的经过音
        if bar % 2 == 1:
            t.add_note(_n(base + 14, root + 3, 2, 0.6))

    return t


def _pad_track() -> Track:
    sp = SynthParams(
        wave="organ",
        octave=0,
        unison=3,
        detune_cents=7.0,
        spread=0.85,
        amp=ADSR(0.35, 0.60, 0.62, 0.90, 1.6),
        filter=FilterParams(
            kind="lp", cutoff=2600.0, resonance=0.9, env_amount=0.0,
        ),
        volume=0.55,
    )
    t = Track(
        name="铺底", kind="synth", icon="violin", color="#9266CC",
        synth=sp, volume=0.62, pan=-0.12, send_reverb=0.42, send_delay=0.10,
    )
    t.effects = [Effect.filter("hp", 180.0, 0.7)]

    for bar in range(8):
        base = bar * 16
        _, _, triad = PROGRESSION[bar % 4]
        if bar < 4:
            # 前段：整小节长和弦
            for p in triad:
                t.add_note(_n(base, p, 15, 0.62))
        else:
            # 后段：切成两半，节奏更有推进感
            for p in triad:
                t.add_note(_n(base, p, 7, 0.66))
            for p in triad:
                t.add_note(_n(base + 8, p, 7, 0.58))
            t.add_note(_n(base + 6, triad[2] + 5, 2, 0.4))   # 加九音点缀
    return t


def _lead_track() -> Track:
    sp = SynthParams(
        wave="square",
        octave=0,
        unison=1,
        amp=ADSR(0.010, 0.18, 0.55, 0.28, 2.0),
        filter=FilterParams(
            kind="lp", cutoff=3200.0, resonance=1.6, env_amount=0.34, keytrack=0.5,
            env=ADSR(0.006, 0.30, 0.20, 0.22, 2.4),
        ),
        volume=0.68,
    )
    t = Track(
        name="主旋律", kind="synth", icon="trumpet", color="#DD2E44",
        synth=sp, volume=0.7, pan=0.14, send_reverb=0.24, send_delay=0.26,
    )
    t.effects = [Effect.distortion(drive=1.5, tone=6000.0, mix=0.22, mode="tube")]

    for bar, s, pitch, length in MELODY:
        vel = 0.9 if s in (0, 4, 8) else 0.74
        t.add_note(_n(bar * 16 + s, pitch, length, vel))
    return t


def _arp_track() -> Track:
    """分解和弦 —— 给后 4 小节加一层细碎的高音，让编曲有层次递进。"""
    sp = SynthParams(
        wave="triangle",
        octave=1,
        unison=1,
        amp=ADSR(0.002, 0.10, 0.0, 0.16, 3.0),
        filter=FilterParams(kind="lp", cutoff=7000.0, resonance=1.0),
        volume=0.42,
    )
    t = Track(
        name="分解音", kind="synth", icon="note", color="#55ACEE",
        synth=sp, volume=0.5, pan=0.3, send_reverb=0.35, send_delay=0.30,
    )
    for bar in range(4, 8):
        base = bar * 16
        _, _, triad = PROGRESSION[bar % 4]
        # 十六分音符上行 + 下行
        shape = (0, 1, 2, 1, 0, 1, 2, 1)
        for i in range(16):
            if i % 2:
                continue
            p = triad[shape[(i // 2) % len(shape)]] + 12
            t.add_note(_n(base + i, p, 2, 0.5 if i % 4 else 0.62))
    return t


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def demo_project() -> Project:
    """返回内置示范工程。"""
    master = MasterParams(
        volume=0.92,
        reverb=Effect.reverb(size=0.72, damping=0.48, mix=1.0, predelay=0.022, width=1.0),
        delay=Effect.delay(time_beats=0.375, feedback=0.34, mix=1.0, ping_pong=True, tone=4200.0),
        limiter_threshold=-1.0,
        limiter_release=0.12,
        fade_out=1.6,
    )
    p = Project(
        name="示范曲 · Am–F–C–G",
        author="Music Studio",
        bpm=96.0,
        steps_per_beat=4,
        beats_per_bar=4,
        bars=8,
        swing=0.0,
        master=master,
    )
    for build in (_drum_track, _bass_track, _pad_track, _lead_track, _arp_track):
        p.add_track(build())
    return p


def empty_project(bars: int = 8, bpm: float = 120.0) -> Project:
    """新建工程：一轨鼓 + 一轨合成器，让用户有个起点。"""
    p = Project(
        name="未命名工程",
        bpm=bpm,
        bars=bars,
        master=MasterParams(
            volume=0.9,
            reverb=Effect.reverb(size=0.55, damping=0.5, mix=1.0),
            delay=Effect.delay(time_beats=0.5, feedback=0.3, mix=1.0),
        ),
    )
    kit = Track(name="鼓组", kind="drum", icon="drums", color="#F4900C", volume=0.9)
    kit.kit = drums.default_voices()
    p.add_track(kit)

    keys = Track(
        name="合成器", kind="synth", icon="piano", color="#3B88C3", volume=0.8,
        synth=SynthParams(
            wave="saw", amp=ADSR(0.005, 0.15, 0.65, 0.22, 2.0),
            filter=FilterParams(cutoff=4200.0, resonance=1.4, env_amount=0.25),
        ),
        send_reverb=0.2,
    )
    p.add_track(keys)
    return p


__all__ = ["demo_project", "empty_project", "PROGRESSION", "MELODY", "A_MINOR"]
