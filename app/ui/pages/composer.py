"""编曲助手 —— 点几下出一段能听的音乐，写进工程后接着手改。

为什么是"规则 + 马尔可夫"而不是"随机铺音符"
------------------------------------------
纯随机的音符听上去就是噪音，原因不是概率不好，而是**缺约束**。听感上的
"不难听"来自三层约束，这里逐层落实：

1. **调式约束（最硬的一层）**：所有有音高的音符都从所选调式的音级表里取，
   绝不允许出现调外音。这一步做完，哪怕是乱排的音序也不会"跑调"。
2. **和声约束**：贝斯只走和弦音（根音为主、五度为辅），铺底只弹和弦音，
   旋律在**强拍上吸附到和弦音**。这一步做完，声部之间不会互相打架。
3. **进行约束（马尔可夫链）**：旋律的下一音由"当前音级 + 上一跳的方向和幅度"
   共同决定，级进权重远大于跳进，且跳进之后强制反向级进解决 —— 这是
   人写旋律时最本能的两条习惯，也是"纯随机"和"像人写的"之间最大的差别。

生成结果全部落在**新建轨道**上（名字以 "AI " 开头），用户的轨道一根不动。
重复点"生成"会**替换上一次生成的那一批**，而不是无限堆叠 —— 堆叠出来的
十份"AI 鼓组"既没法听也没法改，替换才符合"反复试听直到满意"的实际用法。
想彻底清掉，用「清空本次生成」。

"哪些轨道是这一批生成的"这个归属问题，由 ``Track.ai_generated`` 回答
（它跟着工程 JSON 一起往返），而不是由页面自己记一份 id 列表。理由是撤销 /
重做 / 打开工程都会把整棵轨道树从 JSON 重建，页面记的 id 会集体失效 ——
所以 `生成 → 生成 → Ctrl+Z → 再生成` 之后工程里始终只有一批 AI 轨道，
「清空本次生成」也不会因为认不出旧轨而留下永远清不掉的孤儿。

参数改动都会先进撤销栈（``controller.push_undo('AI 生成')``），所以生成、
清空、以及生成时顺手改掉的 bpm/小节数，都是可以 Ctrl+Z 撤回来的。
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from PySide6.QtCore import QPoint, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import (
    QColor, QFont, QFontMetricsF, QIntValidator, QKeySequence, QPainter, QPen,
    QShortcut,
)
from PySide6.QtWidgets import (
    QHBoxLayout, QLayout, QLineEdit, QScrollArea, QSizePolicy,
    QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import drums, render
from app.audio.project import (
    ADSR, Effect, FilterParams, Note, Project, SynthParams, Track,
)
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.controls import GlassSlider
from app.widgets.glass import (
    GlassButton, GlassCard, GlassChip, GlassImageButton, GlassPanel,
    GlassSegmented, GlassStyle, GlassSurface, make_label,
)

#: 自定义走向在界面上的标识（不是预设 key）
CUSTOM = "__custom__"

#: 十二个音名 —— 一律用 # 而不是 ♯：offline 渲染与缺字体的环境下 ♯ 会变豆腐块
PITCH_NAMES: tuple[str, ...] = (
    "C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B",
)


# ==========================================================================
# 调式
# ==========================================================================


@dataclass(frozen=True)
class ModeDef:
    """一个调式：音级相对主音的半音数。"""

    key: str
    label: str
    intervals: tuple[int, ...]
    hint: str = ""


#: 调式表。顺序即界面顺序 —— 最常用的排前面。
#: 只有 7 个音级才能叠出标准三和弦，五声/布鲁斯音阶是 5~6 个音级，
#: 叠出来的"三度"其实是四度叠置，听感偏现代，但**仍然全部在音阶内**，
#: 这是本模块不变的底线。
MODES: tuple[ModeDef, ...] = (
    ModeDef("major", "自然大调", (0, 2, 4, 5, 7, 9, 11), "流行/民谣的基本盘"),
    ModeDef("minor", "自然小调", (0, 2, 3, 5, 7, 8, 10), "忧郁，日系流行常用"),
    ModeDef("harmonic_minor", "和声小调", (0, 2, 3, 5, 7, 8, 11), "第七级升高，东方/古典味"),
    ModeDef("pentatonic", "五声音阶", (0, 3, 5, 7, 10), "怎么弹都不难听"),
    ModeDef("dorian", "多利亚", (0, 2, 3, 5, 7, 9, 10), "小调但六级升高，爵士/摇滚"),
    ModeDef("mixolydian", "混合利底亚", (0, 2, 4, 5, 7, 9, 10), "大调但七级降低，布鲁斯味"),
    ModeDef("blues", "布鲁斯音阶", (0, 3, 5, 6, 7, 10), "五声加蓝调音"),
)

MODES_BY_KEY: dict[str, ModeDef] = {m.key: m for m in MODES}


# ==========================================================================
# 和弦走向预设
# ==========================================================================


@dataclass(frozen=True)
class Progression:
    """一条和弦走向，用**级数**表示（1 起），这样换调式换主音都还成立。"""

    key: str
    label: str
    degrees: tuple[int, ...]
    hint: str = ""
    key_pc: int | None = None     # 建议主音（None = 不动当前选择）
    mode: str | None = None       # 建议调式
    suggest_bars: int | None = None

    def degree_text(self) -> str:
        return " ".join(str(d) for d in self.degrees)


PRESETS: tuple[Progression, ...] = (
    Progression("pop4536251", "4536251", (4, 5, 3, 6, 2, 5, 1),
                "华语流行万能和弦，最不容易出错", suggest_bars=8),
    Progression("canon", "卡农 15634145", (1, 5, 6, 3, 4, 1, 4, 5),
                "帕赫贝尔卡农，八小节完整循环", suggest_bars=8),
    Progression("amfcg", "Am–F–C–G", (1, 6, 3, 7),
                "日系/流行四和弦：小调 i–VI–III–VII",
                key_pc=9, mode="minor", suggest_bars=4),
    Progression("blues12", "十二小节布鲁斯", (1, 1, 1, 1, 4, 4, 1, 1, 5, 4, 1, 5),
                "标准 12 小节布鲁斯，I–IV–V",
                key_pc=9, mode="mixolydian", suggest_bars=12),
)

PRESET_BY_KEY: dict[str, Progression] = {p.key: p for p in PRESETS}


# ==========================================================================
# 和声内核（纯函数，脱离 Qt，便于测试）
# ==========================================================================


def pc_near(pc: int, target: int) -> int:
    """取"音高类等于 pc、且离 target 最近"的那个 MIDI 音。

    所有声部都靠它落到合适的音区：贝斯用 ~38（D2），铺底 ~55，旋律 ~67。
    比写死八度好在换主音时音区不会突然翻上去或掉下去。
    """
    pc = int(pc) % 12
    target = int(target)
    base = target - (target % 12) + pc
    return min((base - 12, base, base + 12), key=lambda p: abs(p - target))


def degree_pitch(intervals: tuple[int, ...], index: int, root_midi: int) -> int:
    """音级序号（可以为负、可以超过音阶长度）→ MIDI 音高。"""
    n = len(intervals)
    octave, idx = divmod(int(index), n)
    return int(root_midi) + 12 * octave + intervals[idx]


def chord_tones(intervals: tuple[int, ...], degree: int, root_midi: int) -> list[int]:
    """级数（1 起）→ 三和弦音高。

    按**音阶内**叠三度（+0/+2/+4 个音级），所以和弦音天然全在音阶里 ——
    不需要事后过滤，也就不会出现"过滤完和弦变二音"的怪事。
    """
    d = int(degree) - 1
    return [degree_pitch(intervals, d + 2 * k, root_midi) for k in range(3)]


#: (根音到三音, 根音到五音) 的半音数 → 和弦后缀
_QUALITY: dict[tuple[int, int], str] = {
    (4, 7): "",
    (3, 7): "m",
    (3, 6): "dim",
    (4, 8): "aug",
    (5, 7): "sus4",
    (2, 7): "sus2",
    (5, 10): "sus4",
    (7, 10): "sus4",
}


def chord_name(triad: list[int]) -> str:
    """三和弦音高 → 名字（Am / F / C / G …）。用于预览，不求多么严谨的记谱。"""
    if not triad:
        return "?"
    root = triad[0] % 12
    if len(triad) >= 3:
        rel = ((triad[1] - triad[0]) % 12, (triad[2] - triad[0]) % 12)
        quality = _QUALITY.get(rel, "")
    else:
        quality = ""
    return f"{PITCH_NAMES[root]}{quality}"


def scale_pitches(key_pc: int, mode: str, anchor: int = 60) -> list[int]:
    """主音附近一个八度的音阶音高（升序）。"""
    intervals = MODES_BY_KEY[mode].intervals
    root = pc_near(key_pc, anchor)
    return [root + iv for iv in intervals]


def scale_pc_set(key_pc: int, mode: str) -> set[int]:
    return {(int(key_pc) + iv) % 12 for iv in MODES_BY_KEY[mode].intervals}


def scale_names(key_pc: int, mode: str) -> list[str]:
    return [PITCH_NAMES[p % 12] for p in scale_pitches(key_pc, mode)]


@dataclass
class Harmony:
    """一次生成用到的和声骨架 —— 预览与生成共用，两边永远一致。"""

    key_pc: int
    mode: str
    bars: int
    degrees: tuple[int, ...]
    bar_degrees: list[int]
    bar_tones: list[list[int]]
    bar_names: list[str]
    cycle_names: list[str]
    scale_pitches: list[int]

    @property
    def mode_label(self) -> str:
        return MODES_BY_KEY[self.mode].label

    @property
    def key_name(self) -> str:
        return PITCH_NAMES[self.key_pc % 12]

    @property
    def scale_names(self) -> list[str]:
        return [PITCH_NAMES[p % 12] for p in self.scale_pitches]

    def progression_text(self) -> str:
        return " → ".join(self.cycle_names)


def build_harmony(params: "GenParams", bars: int) -> Harmony:
    """按参数推出每小节的和弦。

    一小节一个和弦，走向不够长就**按小节循环** —— 这是流行音乐最自然的处理
    （4 小节走向配 8 小节曲子就是走两遍），也让"454 个预设走向 × 任意小节数"
    这个组合空间不用再额外定义规则。
    """
    degrees = tuple(int(d) for d in params.degrees)
    if not degrees:
        raise ValueError("和弦走向是空的")
    intervals = MODES_BY_KEY[params.mode].intervals
    # 铺底的和弦锚在中音区：太低会跟贝斯糊在一起，太高又跟旋律抢位置
    anchor = pc_near(params.key_pc, 55)
    bar_degrees = [degrees[b % len(degrees)] for b in range(bars)]
    tones = [chord_tones(intervals, d, anchor) for d in bar_degrees]
    return Harmony(
        key_pc=int(params.key_pc) % 12,
        mode=params.mode,
        bars=bars,
        degrees=degrees,
        bar_degrees=bar_degrees,
        bar_tones=tones,
        bar_names=[chord_name(t) for t in tones],
        cycle_names=[chord_name(chord_tones(intervals, d, anchor)) for d in degrees],
        scale_pitches=scale_pitches(params.key_pc, params.mode),
    )


# ==========================================================================
# 生成参数与结果
# ==========================================================================


@dataclass
class GenParams:
    """一次生成的完整输入。"""

    key_pc: int = 9                 # A
    mode: str = "minor"
    degrees: tuple[int, ...] = (1, 6, 3, 7)
    bars: int = 8
    bpm: float = 96.0
    parts: dict[str, bool] = field(default_factory=lambda: {
        "drums": True, "bass": True, "pad": True, "lead": True,
    })
    density: float = 0.5
    seed: int | None = None         # None = 每次不同
    #: 取轨道配色时的起点。默认按工程里的轨道数，
    #: 但"同一份参数重生成"要把上一次生成的那批排除掉，颜色才会稳定
    palette_offset: int | None = None


#: 声部：key → (中文名, 轨道名, 图标)
PARTS: tuple[tuple[str, str, str, str], ...] = (
    ("drums", "鼓", "AI 鼓组", "drums"),
    ("bass", "贝斯", "AI 贝斯", "guitar"),
    ("pad", "和声铺底", "AI 和声", "violin"),
    ("lead", "旋律", "AI 旋律", "trumpet"),
)


@dataclass
class GenResult:
    """一次生成的产物 + 预览所需的全部信息。"""

    seed: int
    params: GenParams
    harmony: Harmony
    tracks: list[Track]

    @property
    def note_counts(self) -> dict[str, int]:
        return {t.name: len(t.notes) for t in self.tracks}

    @property
    def total_notes(self) -> int:
        return sum(len(t.notes) for t in self.tracks)


def _put(track: Track, used: set[tuple[int, int]], step: int, pitch: int,
         length: int, velocity: float) -> None:
    """加一个音符，同一格不重复加。

    ``used`` 是必须的：鼓型/贝斯线里好几条规则可能选中同一个格子
    （比如"切分底鼓"和"过门"都选 14），重复叠上去只会莫名其妙地变响。
    """
    step, pitch = int(step), int(pitch)
    if step < 0 or (step, pitch) in used:
        return
    used.add((step, pitch))
    track.add_note(Note(step=step, pitch=pitch, length=max(1, int(length)),
                        velocity=max(0.05, min(1.0, float(velocity)))))


# ==========================================================================
# 各声部生成
# ==========================================================================


def _scale_pos(spb: int, sixteenth: int) -> int:
    """把"第几个 16 分音符"换算成实际 step（工程的小节长度未必是 16）。"""
    return int(round(sixteenth * spb / 16.0))


def _build_drums(rng: random.Random, harmony: Harmony, spb: int,
                 bars: int, density: float, color: str) -> Track:
    """鼓组：底鼓落拍点、军鼓反拍、踩镲八分音符。

    这三条是流行/摇滚鼓型的**骨架**，任何密度下都不动；密度只改"加花"
    （切分底鼓、开镲、边击、过门）—— 所以调低密度得到的是"更干净的版本"，
    而不是"另一个鼓型"。
    """
    track = Track(name="AI 鼓组", kind="drum", icon="drums", color=color,
                  volume=0.9, send_reverb=0.06)
    track.kit = drums.default_voices()

    used: set[tuple[int, int]] = set()
    KICK, SNARE, CLAP = 36, 38, 39
    HAT_C, HAT_O, RIM = 42, 46, 37
    CRASH, RIDE = 49, 51
    TOM_L, TOM_M, TOM_H = 41, 43, 45

    for bar in range(bars):
        base = bar * spb
        fill_bar = (bar == bars - 1) or (density > 0.55 and bar % 4 == 3)

        # 底鼓：1、3 拍。少了它整段就"站不住"，所以无条件加
        _put(track, used, base + _scale_pos(spb, 0), KICK, 1, 1.0)
        _put(track, used, base + _scale_pos(spb, 8), KICK, 1, 0.94)

        # 切分底鼓：密度越高越爱"抢拍"，14 那一格兼作下一小节的推力
        for pos, prob in ((6, 0.18 + 0.5 * density),
                          (10, 0.20 + 0.45 * density),
                          (14, 0.10 + 0.4 * density)):
            if rng.random() < prob:
                _put(track, used, base + _scale_pos(spb, pos), KICK, 1,
                     0.84 if pos != 14 else 0.7)

        # 军鼓：2、4 拍反拍 —— 这是"反拍"的全部含义，永远在
        _put(track, used, base + _scale_pos(spb, 4), SNARE, 1, 0.9)
        _put(track, used, base + _scale_pos(spb, 12), SNARE, 1, 0.92)
        if rng.random() < 0.12 + 0.5 * density:
            # 拍手叠在 4 拍上：比军鼓软，只加宽不加重量
            _put(track, used, base + _scale_pos(spb, 12), CLAP, 1, 0.62)

        # 踩镲：八分音符摆；过门小节的后半让位给嗵鼓，否则会糊成一片
        for s in range(0, 16, 2):
            if fill_bar and s >= 12:
                continue
            step = base + _scale_pos(spb, s)
            if s == 14 and rng.random() < 0.28 + 0.4 * density:
                _put(track, used, step, HAT_O, 1, 0.5)
            else:
                _put(track, used, step, HAT_C, 1, 0.62 if s % 4 == 0 else 0.42)

        # 边击点缀：给偶数小节一点"手打"的碎劲
        if rng.random() < 0.18 + 0.3 * density:
            _put(track, used, base + _scale_pos(spb, rng.choice((7, 11, 15))),
                 RIM, 1, 0.45)

        if fill_bar:
            # 四连嗵鼓下行 —— 段落收尾的标志性动作
            for k, piece in enumerate((TOM_H, TOM_H, TOM_M, TOM_L)):
                _put(track, used, base + _scale_pos(spb, 12 + k), piece, 1,
                     0.78 - 0.05 * k)

        # 吊镲：曲首与后半段起点各来一下，听感上"段落翻页"
        if bar == 0 or (density > 0.5 and bar == bars // 2 and bars > 2):
            _put(track, used, base + _scale_pos(spb, 0), CRASH, 1, 0.6)
        if density > 0.62:
            _put(track, used, base + _scale_pos(spb, 2), RIDE, 1, 0.34)

    return track


def _build_bass(rng: random.Random, harmony: Harmony, spb: int,
                bars: int, density: float, color: str) -> Track:
    """贝斯：只走和弦音 —— 根音为主、五度为辅。

    贝斯是"和声的地基"，一旦它乱走，上面的铺底和旋律再对也听不出调性。
    所以这里把音高集合压到 {根音, 五度}（八度只是根音的复制），
    节奏才交给密度去变化。
    """
    synth = SynthParams(
        wave="saw",
        octave=0,                   # 根音已经落在 36~47（C2~B2），再降八度就成"嗡嗡声"了
        unison=2, detune_cents=6.0, spread=0.2, glide=0.02,
        amp=ADSR(0.004, 0.12, 0.72, 0.10, 2.2),
        filter=FilterParams(kind="lp", cutoff=560.0, resonance=2.2, env_amount=0.32,
                            env=ADSR(0.003, 0.16, 0.0, 0.10, 2.6)),
        volume=0.95,
    )
    track = Track(name="AI 贝斯", kind="synth", icon="guitar", color=color,
                  synth=synth, volume=0.85, send_reverb=0.02)

    used: set[tuple[int, int]] = set()
    half = max(1, spb // 2)
    eighth = max(1, spb // 8)

    for bar in range(bars):
        base = bar * spb
        tones = harmony.bar_tones[bar]
        root = pc_near(tones[0] % 12, 38)
        fifth = pc_near(tones[2] % 12, root + 5)
        octave = pc_near(tones[0] % 12, root + 12)

        # 骨架：1、3 拍各一个长音
        _put(track, used, base + 0, root, half, 0.96)
        _put(track, used, base + half, root, half, 0.9)

        # 变化：切分与经过音，密度越高越密
        plan = (
            (3, fifth, 0.12 + 0.45 * density, 0.62),
            (6, root, 0.18 + 0.5 * density, 0.72),
            (10, root, 0.18 + 0.45 * density, 0.7),
            (11, fifth, 0.06 + 0.35 * density, 0.6),
            # 14 那一格是"进入下一小节"的推力：每四小节末尾抬高八度，收句更明显
            (14, octave if bar % 4 == 3 else fifth, 0.12 + 0.4 * density, 0.62),
        )
        for pos, pitch, prob, vel in plan:
            if rng.random() < prob:
                _put(track, used, base + _scale_pos(spb, pos), pitch, eighth,
                     vel + rng.uniform(-0.04, 0.04))

    return track


def _voice_lead(previous: list[int] | None, pcs: list[int]) -> list[int]:
    """把和弦音摆到最靠近上一个和弦的位置。

    这是最朴素也最有效的一次"声部进行"：每个声部只走最近的八度，
    于是和弦切换时不会有声部突然跳一个八度那种刺耳感。

    超出音区时**整体平移八度**，绝不逐音钳位 —— 逐音钳位会改变音高类
    （A2=45 被钳到 48 就变成 C 了），把和弦弹成别的和弦。这个 bug 是测试里
    逐音核对和弦音时抓出来的。
    """
    if previous is None:
        out = [pc_near(pc, 58 + i * 3) for i, pc in enumerate(pcs)]
    else:
        out = [pc_near(pc, prev) for pc, prev in zip(pcs, previous)]
    out = sorted(out)
    while out and out[0] < _PAD_LO:
        out = [p + 12 for p in out]
    while out and out[-1] > _PAD_HI:
        out = [p - 12 for p in out]
    return sorted(out)


#: 铺底的音区（MIDI）。比贝斯高、比旋律低，三个声部互不遮挡
_PAD_LO, _PAD_HI = 50, 82


def _build_pad(rng: random.Random, harmony: Harmony, spb: int,
               bars: int, density: float, color: str) -> Track:
    """和声铺底：每小节一个和弦，只弹和弦音，带声部进行。

    密度高时把整小节的和弦切成两半，推进感立刻不一样 —— 这比"加更多音"
    更像编曲上的变化，也不会把和声弄脏。
    """
    synth = SynthParams(
        wave="organ", unison=3, detune_cents=7.0, spread=0.85,
        amp=ADSR(0.28, 0.60, 0.62, 0.90, 1.6),
        filter=FilterParams(kind="lp", cutoff=2400.0, resonance=0.9),
        volume=0.55,
    )
    track = Track(name="AI 和声", kind="synth", icon="violin", color=color,
                  synth=synth, volume=0.6, pan=-0.1,
                  send_reverb=0.4, send_delay=0.08)
    track.effects = [Effect.filter("hp", 170.0, 0.7)]

    used: set[tuple[int, int]] = set()
    previous: list[int] | None = None
    push = density > 0.55 and spb >= 8

    for bar in range(bars):
        base = bar * spb
        pcs = [p % 12 for p in harmony.bar_tones[bar]]
        voicing = _voice_lead(previous, pcs)
        previous = voicing
        if push:
            half = spb // 2
            for p in voicing:
                _put(track, used, base, p, max(1, half - 1), 0.6)
            for p in voicing:
                _put(track, used, base + half, p, max(1, spb - half - 1), 0.55)
        else:
            for p in voicing:
                _put(track, used, base, p, max(1, spb - 1), 0.6)

    return track


#: 旋律的马尔可夫转移权重：以"音级差"为状态转移量。
#: 级进（±1）权重是跳进（±3 以上）的 4~5 倍 —— 人写旋律就是这个比例。
_MARKOV_STEPS: dict[int, float] = {
    0: 1.1,
    1: 3.0, -1: 3.0,
    2: 1.5, -2: 1.5,
    3: 0.6, -3: 0.6,
    4: 0.28, -4: 0.28,
    5: 0.1, -5: 0.1,
}

_LEAD_DURATIONS: tuple[tuple[int, float], ...] = ((2, 2.2), (4, 2.6), (6, 1.4), (8, 0.9))


def _snap_to_chord(index: int, chord_pcs: set[int], seq: list[int]) -> int:
    """把音级序号吸附到最近的和弦音（强拍用）。"""
    for radius in range(0, 5):
        for cand in (index - radius, index + radius):
            if 0 <= cand < len(seq) and (seq[cand] % 12) in chord_pcs:
                return cand
    return index


def _next_lead_index(rng: random.Random, index: int, prev_delta: int, n: int,
                     strong: bool, chord_pcs: set[int], seq: list[int],
                     density: float) -> tuple[int, int]:
    """马尔可夫一步：给定当前音级与上一跳，掷出下一个音级。

    状态是 ``(当前音级, 上一跳的幅度)`` 而不是只有当前音级 —— 因为"跳进之后
    要级进反向解决"这条习惯必须记住上一跳才成立，而它正是旋律听上去"有逻辑"
    的关键。密度越大，跳进权重越高（更花哨），级进仍是主体。
    """
    weights: dict[int, float] = {}
    for delta, weight in _MARKOV_STEPS.items():
        if not 0 <= index + delta < n:
            continue                      # 越界的方向直接不允许走
        if abs(delta) >= 3:
            weight *= 0.6 + 1.2 * density
        if abs(prev_delta) >= 3 and delta != 0:
            same_dir = (delta > 0) == (prev_delta > 0)
            weight *= 0.22 if same_dir else 1.7   # 跳进后反向级进解决
        weights[delta] = weight

    if not weights:
        return index, 0
    delta = rng.choices(list(weights), weights=list(weights.values()))[0]
    new_index = max(0, min(n - 1, index + delta))

    # 强拍吸附到和弦音：旋律与和声"咬合"的地方就在这几拍上
    if strong and rng.random() < 0.45 + 0.35 * density:
        new_index = _snap_to_chord(new_index, chord_pcs, seq)
    return new_index, new_index - index


def _pick_lead_duration(rng: random.Random, density: float, remaining: int,
                        final: bool) -> int:
    if final and remaining <= 8:
        return max(1, remaining)          # 末句收长音，不然听着像没写完
    choices = list(_LEAD_DURATIONS)
    if density > 0.4:
        choices.append((1, 0.35 + 0.9 * density))
        choices.append((3, 0.35 + 0.7 * density))
    total = sum(w for _, w in choices)
    r = rng.random() * total
    acc = 0.0
    for dur, weight in choices:
        acc += weight
        if r <= acc:
            return max(1, min(dur, remaining))
    return max(1, min(4, remaining))


def _build_lead(rng: random.Random, harmony: Harmony, spb: int,
                bars: int, density: float, color: str) -> Track:
    """旋律：马尔可夫链在音阶级数上走，强拍吸附和弦音。"""
    synth = SynthParams(
        wave="square",
        amp=ADSR(0.012, 0.18, 0.55, 0.26, 2.0),
        filter=FilterParams(kind="lp", cutoff=3400.0, resonance=1.5, env_amount=0.34,
                            keytrack=0.5, env=ADSR(0.006, 0.28, 0.20, 0.22, 2.4)),
        volume=0.66,
    )
    track = Track(name="AI 旋律", kind="synth", icon="trumpet", color=color,
                  synth=synth, volume=0.68, pan=0.14,
                  send_reverb=0.22, send_delay=0.24)
    track.effects = [Effect.delay(time_beats=0.375, feedback=0.26, mix=0.16,
                                  ping_pong=True, tone=4200.0)]

    intervals = MODES_BY_KEY[harmony.mode].intervals
    # 旋律音域：下标 -3（主音下方三个音级）到 +12，约一个半八度
    root = pc_near(harmony.key_pc, 67)
    seq = [degree_pitch(intervals, i, root) for i in range(-3, 13)]

    index = _snap_to_chord(min(5, len(seq) - 1),
                           {p % 12 for p in harmony.bar_tones[0]}, seq)
    prev_delta = 0
    used: set[tuple[int, int]] = set()
    beat = max(2, spb // 2)

    for bar in range(bars):
        base = bar * spb
        chord_pcs = {p % 12 for p in harmony.bar_tones[bar]}
        cursor = 0
        placed = 0
        final_bar = bar == bars - 1

        while cursor < spb:
            # 留白：密度低时更爱空着，旋律有呼吸才不吵
            if placed and rng.random() < (1.0 - density) * 0.32:
                cursor += rng.choice((1, 2))
                if cursor >= spb:
                    break
            dur = _pick_lead_duration(rng, density, spb - cursor, final_bar)
            strong = cursor % beat == 0
            index, prev_delta = _next_lead_index(
                rng, index, prev_delta, len(seq), strong, chord_pcs, seq, density)
            vel = 0.92 if cursor == 0 else (0.78 if strong else 0.68)
            _put(track, used, base + cursor, seq[index], dur,
                 vel + rng.uniform(-0.05, 0.05))
            cursor += dur
            placed += 1

        if placed == 0:
            # 极端情况下（rest 把整小节吃掉了）至少补一个音，别留空小节
            index, prev_delta = _next_lead_index(
                rng, index, prev_delta, len(seq), True, chord_pcs, seq, density)
            _put(track, used, base, seq[index], beat, 0.7)

    return track


# --------------------------------------------------------------------------
# 组装
# --------------------------------------------------------------------------


def params_from_profile(profile, *, bars: int | None = None, seed: int | None = None,
                        palette_offset: int | None = None) -> GenParams:
    """把参考音频的指纹（``app.audio.replicate.ReferenceProfile``）翻成生成参数。

    这是"复刻"在**数据上**的落点，也是本模块与 AI 页之间唯一的接口：
    速度、调式、织体密度、和弦走向全部来自参考音频；只有"具体落哪些音符"
    由种子决定 —— 同一个种子必然得到同一首（这是复刻该有的性质：不满意就换
    种子重掷，而不是每次点都得到无从比较的结果）。

    为什么放在这里而不是 ``app.audio.replicate`` 里：音频层不该认识
    ``GenParams``（那是界面层的编曲约定）。反过来依赖界面层的音频模块
    迟早会为了"顺手多算一点"把 UI 概念拖进 DSP 里。
    """
    mode = str(getattr(profile, "mode", "minor"))
    if mode not in MODES_BY_KEY:
        # 指纹只给 major/minor；万一别处给了别的名字，退回小调而不是崩
        mode = "minor" if "min" in mode else "major"
    seed_val = int(seed) if seed is not None else None
    degrees = profile.pick_progression(seed_val if seed_val is not None else 0)
    return GenParams(
        key_pc=int(getattr(profile, "key_pc", 9)) % 12,
        mode=mode,
        degrees=tuple(degrees),
        bars=int(bars if bars is not None else getattr(profile, "bars", 8)),
        bpm=float(getattr(profile, "bpm", 96.0)),
        # 参考里几乎没有鼓就别硬加鼓：这是"像不像"里最明显的一条
        parts={"drums": float(getattr(profile, "drum_ratio", 1.0)) > 0.12,
               "bass": True, "pad": True, "lead": True},
        density=float(getattr(profile, "density", 0.5)),
        seed=seed_val,
        palette_offset=palette_offset,
    )


def compose(project: Project, params: GenParams) -> GenResult:
    """按参数生成四条 AI 轨道。**纯函数**：不改 project，只返回新轨道。"""
    bars = max(1, min(16, int(params.bars)))
    density = max(0.0, min(1.0, float(params.density)))
    seed = int(params.seed) if params.seed is not None else random.SystemRandom().randrange(1, 10 ** 6)
    rng = random.Random(seed)

    harmony = build_harmony(params, bars)
    spb = max(1, project.steps_per_bar)

    palette = accents.track_palette()
    base = (len(project.tracks) if params.palette_offset is None
            else int(params.palette_offset))          # 从已有轨道数接着取色，新旧不撞色
    builders = {
        "drums": _build_drums,
        "bass": _build_bass,
        "pad": _build_pad,
        "lead": _build_lead,
    }

    tracks: list[Track] = []
    for i, (slot, _zh, _name, _icon) in enumerate(PARTS):
        if not params.parts.get(slot):
            continue
        color = palette[(base + i) % len(palette)].name()
        tracks.append(builders[slot](rng, harmony, spb, bars, density, color))

    return GenResult(seed=seed, params=params, harmony=harmony, tracks=tracks)


# ==========================================================================
# 流式布局（Qt 没有内置，而芯片组必须能随宽度换行）
# ==========================================================================


class FlowLayout(QLayout):
    """从左到右排，排不下就换行。

    为什么值得自己写：调式 7 个、主音 12 个、走向 4 个芯片，用 QGridLayout
    就得为每种窗口宽度手工算列数；用 QHBoxLayout 则会被挤扁。
    """

    def __init__(self, parent: QWidget | None = None, margin: int = 0,
                 h_spacing: int = 4, v_spacing: int = 4):
        super().__init__(parent)
        self._items: list = []
        self._h = int(h_spacing)
        self._v = int(v_spacing)
        self.setContentsMargins(margin, margin, margin, margin)

    # -- QLayout 接口 ----------------------------------------------------

    def addItem(self, item):        # noqa: N802
        self._items.append(item)
        self.invalidate()

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index):        # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):        # noqa: N802
        if 0 <= index < len(self._items):
            item = self._items.pop(index)
            self.invalidate()
            return item
        return None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:      # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:   # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), test_only=True)

    def setGeometry(self, rect: QRect) -> None:    # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, test_only=False)

    def sizeHint(self) -> QSize:                   # noqa: N802
        return self.minimumSize()

    def minimumSize(self) -> QSize:                # noqa: N802
        size = QSize()
        for item in self._items:
            size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    # -- 排布 ------------------------------------------------------------

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = eff.x(), eff.y(), 0
        for item in self._items:
            hint = item.sizeHint()
            next_x = x + hint.width() + self._h
            if next_x - self._h > eff.right() + 1 and line_h > 0:
                x = eff.x()
                y = y + line_h + self._v
                next_x = x + hint.width() + self._h
                line_h = 0
            if not test_only:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x = next_x
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def _chip_row(parent: QWidget, chips: list[GlassChip]) -> QWidget:
    host = QWidget(parent)
    host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    lay = FlowLayout(host, h_spacing=4, v_spacing=4)
    for chip in chips:
        lay.addWidget(chip)
    return host


# ==========================================================================
# 结果预览
# ==========================================================================


class ResultView(GlassCard):
    """生成结果预览：和弦链 + 每轨音符数 + 音阶音名。

    和弦链不是一行文本而是**可点的芯片**：点一下就跳到那一小节，
    这是"看到走向 → 立刻听那一段"最短的路径。鼠标命中区域在绘制时缓存，
    因为芯片位置依赖字体度量，布局系统算不出来。
    """

    seekRequested = Signal(float)

    PAD = 16.0
    CHIP_H = 24.0

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.card, material=theme.black(0.20))
        self.harmony: Harmony | None = None
        self.result: GenResult | None = None
        self.tracks: list[Track] = []
        self.play_bar: int | None = None
        self.seconds_per_bar = 2.0
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumHeight(260)
        self._hits: list[tuple[QRectF, int]] = []

    # -- 数据 ------------------------------------------------------------

    def set_harmony(self, harmony: Harmony | None) -> None:
        self.harmony = harmony
        self.update()

    def set_result(self, result: GenResult | None, tracks: list[Track]) -> None:
        self.result = result
        self.tracks = list(tracks)
        self.update()

    def set_play_bar(self, bar: int | None) -> None:
        if bar != self.play_bar:
            self.play_bar = bar
            self.update()

    # -- 交互 ------------------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            pos = event.position()
            for rect, bar in self._hits:
                if rect.contains(pos):
                    self.seekRequested.emit(bar * self.seconds_per_bar)
                    return
        super().mousePressEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        self._hits = []

        if self.harmony is None:
            painter.setFont(type_scale.body.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter,
                             "选好参数，点「生成」")
            painter.end()
            return

        x = self.PAD
        y = self.PAD

        # ---- 标题行 ----------------------------------------------------
        f = type_scale.headline.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(x, y, 240, 20),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         "和弦走向")
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        title = f"{self.harmony.key_name} {self.harmony.mode_label} · {self.harmony.bars} 小节"
        if self.result is not None:
            title += f" · 种子 {self.result.seed}"
        painter.drawText(QRectF(x + 80, y, self.width() - x - 96, 20),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight, title)
        y += 26

        # ---- 和弦链（按小节，可点跳转）----------------------------------
        fm = QFontMetricsF(type_scale.footnote.font())
        arrow_w = 16.0
        row_x = x
        for bar, name in enumerate(self.harmony.bar_names):
            w = fm.horizontalAdvance(name) + 22
            if row_x + w > self.width() - self.PAD and row_x > x:
                row_x = x
                y += self.CHIP_H + 6
            rect = QRectF(row_x, y, w, self.CHIP_H)
            active = self.play_bar is not None and self.play_bar == bar
            accent = accents.yellow if active else accents.blue
            tint = QColor(accent)
            tint.setAlphaF(0.28 if active else 0.13)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(tint)
            painter.drawRoundedRect(rect, radii.chip, radii.chip)
            painter.setPen(QPen(QColor(accent).lighter(150) if active else labels.secondary))
            painter.setFont(type_scale.footnote.font())
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, name)
            self._hits.append((rect, bar))

            if bar < len(self.harmony.bar_names) - 1 and row_x + w + arrow_w <= self.width() - self.PAD:
                painter.setFont(type_scale.caption2.font())
                painter.setPen(QPen(labels.quaternary))
                painter.drawText(QRectF(row_x + w, y, arrow_w, self.CHIP_H),
                                 Qt.AlignmentFlag.AlignCenter, "→")
            row_x += w + arrow_w
        y += self.CHIP_H + 18

        # ---- 音阶 ------------------------------------------------------
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.tertiary))
        painter.drawText(QRectF(x, y, 60, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, "音阶")
        painter.setPen(QPen(labels.secondary))
        painter.drawText(QRectF(x + 44, y, self.width() - x - 60, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         " ".join(self.harmony.scale_names))
        y += 26
        if self.result is not None and self.harmony.bars > len(self.harmony.degrees):
            painter.setFont(type_scale.caption2.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(x + 44, y, self.width() - x - 60, 16),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             f"走向 {len(self.harmony.degrees)} 个和弦，"
                             f"按小节循环 {self.harmony.bars} 遍")
            y += 20

        # ---- 每轨音符数 -------------------------------------------------
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(x, y, 200, 18),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         "生成结果")
        y += 24

        if not self.tracks:
            painter.setFont(type_scale.body.font())
            painter.setPen(QPen(labels.quaternary))
            painter.drawText(QRectF(x, y, self.width() - 2 * x, 20),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             "还没有生成内容 —— 上面的走向就是这次会用的和弦")
            painter.end()
            return

        row_h = 22.0
        for track in self.tracks:
            if y + row_h > self.height() - 8:
                painter.setFont(type_scale.caption2.font())
                painter.setPen(QPen(labels.quaternary))
                painter.drawText(QRectF(x, y, 200, row_h),
                                 Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                                 "…")
                break
            color = QColor(track.color)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(color)
            painter.drawEllipse(QRectF(x, y + 7, 7, 7))
            pm = icons.pixmap(track.icon, 14, opacity=0.9, dpr=self.devicePixelRatioF())
            painter.drawPixmap(int(x + 13), int(y + 3), pm)
            painter.setFont(type_scale.caption1.font())
            painter.setPen(QPen(labels.secondary))
            painter.drawText(QRectF(x + 33, y, 160, row_h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                             track.name)
            painter.setPen(QPen(labels.label))
            painter.drawText(QRectF(x + 33, y, self.width() - x - 49, row_h),
                             Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignRight,
                             f"{len(track.notes)} 音符")
            y += row_h

        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


class _AssistantBadge(GlassSurface):
    """标题条上的机器人徽标。

    为什么**不用** GlassImageButton：那是个真按钮，而标题上这颗头像只是页面标识，
    按下它没有任何该做的事。给它随便接一个动作（"点一下生成四条轨道"之类）会让
    一个纯装饰的位置带上不可预期的副作用 —— 生成是这页最高频、也最"重"的操作，
    绝不该由一个位置看起来像头像的东西触发；留着不接又正好是契约里明令禁止的
    "点了没反应"。所以它退回成**纯绘制**：外观沿用 GlassButton 的 tinted 常态
    （半透明强调色圆角块 + 顶部高光），但不再接受点击、也不再抢键盘焦点。

    图标在 paintEvent 里按**当前** dpr 现取，而不是构造时取一次 —— 窗口被拖到
    另一块缩放不同的显示器上时，预先取好的位图会糊掉。
    """

    def __init__(self, icon_name: str, parent: QWidget | None = None, *,
                 size: int = 34, icon_size: int | None = None,
                 accent: QColor | None = None):
        super().__init__(parent)
        self._icon_name = icon_name
        self._icon_px = icon_size or int(round(size * 0.60))
        self._tint = QColor(accent) if accent is not None else QColor(accents.yellow)
        # 鼠标事件直接穿透：否则 hover 会被 GlassSurface 追踪，看着像能点
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.setFixedSize(size, size)

    def glass_style(self) -> GlassStyle:
        # 0.17 是 GlassButton 里 tinted 常态的透明度，照抄它才能和旁边那些
        # 真的按钮看起来是同一套材质
        tint = QColor(self._tint)
        tint.setAlphaF(0.17)
        return GlassStyle(material=tint, radius=radii.control, specular=0.35,
                          border=1.0, inner_gradient=1.0, grain=0.7)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        pm = icons.pixmap(self._icon_name, self._icon_px, opacity=0.95,
                          dpr=self.devicePixelRatioF())
        # QPixmap 的 width() 是**物理**像素，HiDPI 下要除掉 dpr 才是逻辑尺寸
        pw = int(round(pm.width() / max(1.0, pm.devicePixelRatio())))
        ph = int(round(pm.height() / max(1.0, pm.devicePixelRatio())))
        painter.drawPixmap((self.width() - pw) // 2, (self.height() - ph) // 2, pm)
        painter.end()


class ComposerPage(QWidget):
    """编曲助手页面：左参数、右预览、下一键生成。"""

    #: 契约里的名字
    edited = Signal()
    #: 音序器页历史上用的名字 —— 两个都发，避免编排者接线时接错那个而静默失效
    notesEdited = Signal()
    seekRequested = Signal(float)

    PARAM_W = 372

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self._controller = None
        self._syncing = False
        self._result: GenResult | None = None

        # ---- 参数状态（控件是它的视图，这里是唯一事实）------------------
        self._key_pc = 9
        self._mode = "minor"
        self._preset = "amfcg"
        self._custom_degrees: list[int] = [1, 6, 3, 7]
        self._parts: dict[str, bool] = {k: True for k, _z, _n, _i in PARTS}

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        root.addWidget(self._build_header())

        body = QHBoxLayout()
        body.setSpacing(spacing.sm)
        body.addWidget(self._build_params(), 0)
        body.addWidget(self._build_result(), 1)
        root.addLayout(body, 1)

        root.addWidget(self._build_status())

        self._apply_initial_state()

    # ------------------------------------------------------------------
    # 构建
    # ------------------------------------------------------------------

    def _build_header(self) -> GlassPanel:
        """标题条：一句话说清这个页面干什么，比一串参数名有用。"""
        bar = GlassPanel(self, material=theme.black(0.18), radius=radii.control)
        bar.setFixedHeight(52)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(spacing.sm)

        self._head_icon = _AssistantBadge("robot", bar, size=34, icon_size=24,
                                          accent=accents.yellow)
        lay.addWidget(self._head_icon)

        col = QVBoxLayout()
        col.setSpacing(0)
        title = make_label("编曲助手", type_scale.title3, labels.label, bar)
        sub = make_label("选调式与和弦走向 → 一键生成鼓 / 贝斯 / 铺底 / 旋律，"
                         "写进新轨道，可以接着手改", type_scale.caption1,
                         labels.tertiary, bar)
        col.addWidget(title)
        col.addWidget(sub)
        lay.addLayout(col)
        lay.addStretch(1)
        self._head_summary = make_label("", type_scale.caption1, labels.tertiary, bar)
        lay.addWidget(self._head_summary)
        return bar

    def _build_params(self) -> QScrollArea:
        """左侧参数栏。用滚动区而不是硬塞 —— 窗口变矮时不该把按钮挤没。"""
        panel = GlassPanel(self, material=theme.black(0.22), radius=radii.card)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 12, 14, 12)
        lay.setSpacing(6)

        # ---- 主音 ------------------------------------------------------
        lay.addWidget(make_label("主音", type_scale.headline, labels.secondary, panel))
        self.key_chips: list[GlassChip] = []
        for pc in range(12):
            chip = GlassChip(PITCH_NAMES[pc], panel, accent=accents.blue,
                             checkable=True, selected=(pc == self._key_pc))
            chip.toggled.connect(lambda on, p=pc: self._on_key_chip(p, on))
            self.key_chips.append(chip)
        lay.addWidget(_chip_row(panel, self.key_chips))

        # ---- 调式 ------------------------------------------------------
        lay.addSpacing(4)
        lay.addWidget(make_label("调式", type_scale.headline, labels.secondary, panel))
        self.mode_chips: list[GlassChip] = []
        for mode in MODES:
            chip = GlassChip(mode.label, panel, accent=accents.purple, checkable=True,
                             selected=(mode.key == self._mode))
            chip.setToolTip(mode.hint)
            chip.toggled.connect(lambda on, k=mode.key: self._on_mode_chip(k, on))
            self.mode_chips.append(chip)
        lay.addWidget(_chip_row(panel, self.mode_chips))

        # ---- 和弦走向 --------------------------------------------------
        lay.addSpacing(4)
        lay.addWidget(make_label("和弦走向", type_scale.headline, labels.secondary, panel))
        self.seg = GlassSegmented(["预设", "自定义"], panel, accent=accents.yellow)
        self.seg.setFixedWidth(158)
        self.seg.changed.connect(self._on_seg_changed)
        lay.addWidget(self.seg, 0, Qt.AlignmentFlag.AlignLeft)

        self.preset_chips: list[GlassChip] = []
        for preset in PRESETS:
            chip = GlassChip(preset.label, panel, accent=accents.yellow, checkable=True)
            chip.setToolTip(f"{preset.hint}（级数 {preset.degree_text()}）")
            chip.toggled.connect(lambda on, k=preset.key: self._on_preset_chip(k, on))
            self.preset_chips.append(chip)
        self.preset_row = _chip_row(panel, self.preset_chips)
        lay.addWidget(self.preset_row)

        # 自定义：点级数往上加，4~8 个
        self.custom_box = QWidget(panel)
        self.custom_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        cbox = QVBoxLayout(self.custom_box)
        cbox.setContentsMargins(0, 2, 0, 0)
        cbox.setSpacing(4)
        self.degree_chips: list[GlassChip] = []
        for deg in range(1, 8):
            chip = GlassChip(str(deg), self.custom_box, accent=accents.orange,
                             checkable=True)
            chip.toggled.connect(lambda on, d=deg: self._on_degree_chip(d, on))
            self.degree_chips.append(chip)
        cbox.addWidget(_chip_row(self.custom_box, self.degree_chips))

        row = QHBoxLayout()
        row.setSpacing(4)
        self._seq_label = make_label("", type_scale.mono, labels.label, self.custom_box)
        row.addWidget(self._seq_label, 1)
        self.btn_back = GlassChip("退格", self.custom_box, accent=accents.orange)
        self.btn_back.clicked.connect(self._pop_degree)
        self.btn_seq_clear = GlassChip("清空", self.custom_box, accent=accents.red)
        self.btn_seq_clear.clicked.connect(lambda: self.set_custom_degrees([]))
        row.addWidget(self.btn_back)
        row.addWidget(self.btn_seq_clear)
        cbox.addLayout(row)
        cbox.addWidget(make_label("点级数加入（4~8 个），走向按小节循环",
                                  type_scale.caption2, labels.quaternary, self.custom_box))
        lay.addWidget(self.custom_box)

        # ---- 小节 / 速度 ------------------------------------------------
        lay.addSpacing(4)
        self.bars_slider, bars_row = self._labelled_slider(
            panel, "小节数", 1, 16, self.project.bars, accents.green,
            str(min(16, max(1, int(self.project.bars)))), "bars")
        lay.addLayout(bars_row)
        default_bpm = max(60.0, min(180.0, float(self.project.bpm)))
        self.bpm_slider, bpm_row = self._labelled_slider(
            panel, "速度", 60, 180, default_bpm, accents.orange,
            str(int(round(default_bpm))), "bpm")
        lay.addLayout(bpm_row)

        # ---- 声部 ------------------------------------------------------
        lay.addSpacing(4)
        lay.addWidget(make_label("声部（至少勾一个）", type_scale.headline,
                                 labels.secondary, panel))
        self.part_chips: dict[str, GlassChip] = {}
        chips: list[GlassChip] = []
        for slot, zh, _name, icon in PARTS:
            chip = GlassChip(f"{zh}", panel, accent=accents.teal, checkable=True,
                             selected=True)
            chip.setToolTip(f"生成「{_name}」轨道")
            chip.toggled.connect(lambda on, s=slot: self._on_part_chip(s, on))
            self.part_chips[slot] = chip
            chips.append(chip)
        lay.addWidget(_chip_row(panel, chips))

        # ---- 复杂度 ----------------------------------------------------
        lay.addSpacing(4)
        self.density_slider, density_row = self._labelled_slider(
            panel, "复杂度 / 密度", 0.0, 1.0, 0.5, accents.pink, "0.50", "density")
        lay.addLayout(density_row)

        # ---- 随机种子 --------------------------------------------------
        lay.addSpacing(4)
        seed_row = QHBoxLayout()
        seed_row.setSpacing(4)
        seed_row.addWidget(make_label("随机种子", type_scale.caption1,
                                      labels.tertiary, panel))
        self.seed_edit = QLineEdit(panel)
        self.seed_edit.setPlaceholderText("留空 = 每次都不一样")
        self.seed_edit.setValidator(QIntValidator(0, 99999999, self))
        self.seed_edit.setFixedHeight(24)
        self.seed_edit.setFont(type_scale.caption1.font())
        c = labels.label
        self.seed_edit.setStyleSheet(
            "QLineEdit{background:rgba(255,255,255,0.06);"
            "border:1px solid rgba(255,255,255,0.10);border-radius:6px;"
            f"color:rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});"
            "padding:0 8px;} QLineEdit:focus{border:1px solid rgba(255,214,10,0.55);}")
        seed_row.addWidget(self.seed_edit, 1)
        self.btn_dice = GlassImageButton("dice", panel, size=24, icon_size=16,
                                         tooltip="随机一个种子（便于复现同一版）")
        self.btn_dice.clicked.connect(self._roll_seed)
        seed_row.addWidget(self.btn_dice)
        lay.addLayout(seed_row)

        # ---- 三个动作按钮 ----------------------------------------------
        lay.addSpacing(8)
        actions = QHBoxLayout()
        actions.setSpacing(6)
        self.btn_generate = GlassButton("生成", panel, accent=accents.yellow,
                                        style="filled")
        self.btn_generate.setToolTip("按当前参数生成四条 AI 轨道（Ctrl+G）")
        self.btn_generate.clicked.connect(self.generate)
        # 快捷键挂在**页面**上而不是按钮上（setShortcut）：页面级 QShortcut 只在
        # 本页可见时才生效，所以在音序器页按 Ctrl+G 不会在背后偷偷生成一批轨道
        # （主窗口把 7 个页面放在 QStackedWidget 里，挂在按钮上会留这个隐患）。
        # 主窗口的 _install_shortcuts 没注册 Ctrl+G，本页自己负责。
        self.shortcut_generate = QShortcut(QKeySequence("Ctrl+G"), self)
        self.shortcut_generate.activated.connect(self.generate)
        self.btn_audition = GlassButton("试听", panel, accent=accents.green,
                                       style="tinted")
        self.btn_audition.setToolTip("只渲染本次生成的内容并播放")
        self.btn_audition.clicked.connect(self.audition_generated)
        actions.addWidget(self.btn_generate, 1)
        actions.addWidget(self.btn_audition, 0)
        lay.addLayout(actions)

        self.btn_clear = GlassButton("清空本次生成", panel, accent=accents.red,
                                    style="destructive", compact=True)
        self.btn_clear.clicked.connect(self.clear_generated)
        lay.addWidget(self.btn_clear)

        lay.addStretch(1)

        scroll = QScrollArea(self)
        scroll.setWidget(panel)
        scroll.setWidgetResizable(True)
        scroll.setFixedWidth(self.PARAM_W)
        scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        scroll.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        scroll.setStyleSheet(
            "QScrollArea{background:transparent;border:none;}"
            "QScrollBar:vertical{background:transparent;width:8px;margin:0;}"
            "QScrollBar::handle:vertical{background:rgba(255,255,255,45);"
            "border-radius:4px;min-height:30px;}"
            "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
            "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}")
        self.params_scroll = scroll
        return scroll

    def _labelled_slider(self, parent: QWidget, title: str, lo: float, hi: float,
                         value: float, accent: QColor, text: str, key: str
                         ) -> tuple[GlassSlider, QHBoxLayout]:
        """一行"标题 + 数值 + 滑块"。数值贴右对齐，扫一眼就知道当前值。

        ``key`` 用来把数值标签登记到页面上（``_bars_value`` 等），
        而不是靠标题字符串去猜 —— 换个文案就会静默失联。
        """
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(make_label(title, type_scale.caption1, labels.tertiary, parent))
        row.addStretch(1)
        value_label = make_label(text, type_scale.mono, labels.label, parent)
        row.addWidget(value_label)
        slider = GlassSlider(parent, lo, hi, value, accent=accent)
        # 跨度大的量（小节/速度）显示整数，0~1 的连续量显示两位小数
        slider.valueChanged.connect(
            lambda v, lb=value_label, a=lo, b=hi: lb.setText(
                f"{int(round(v))}" if (b - a) > 2 else f"{v:.2f}"))
        if key == "bars":
            self._bars_value = value_label
            slider.valueChanged.connect(lambda _v: self._refresh_preview())
        elif key == "bpm":
            self._bpm_value = value_label
        elif key == "density":
            self._density_value = value_label
            slider.valueChanged.connect(lambda _v: self._refresh_preview())
        return slider, row

    def _build_result(self) -> QWidget:
        host = QWidget(self)
        host.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        lay = QVBoxLayout(host)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(spacing.xs)
        self.result_view = ResultView(host)
        self.result_view.seekRequested.connect(self.seekRequested)
        lay.addWidget(self.result_view, 1)
        return host

    def _build_status(self) -> GlassCard:
        bar = GlassCard(self, radius=radii.control)
        bar.setFixedHeight(30)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 0, 12, 0)
        self._status = make_label("还没有生成内容", type_scale.caption1,
                                  labels.tertiary, bar)
        lay.addWidget(self._status, 1)
        self._status_right = make_label("", type_scale.caption1, labels.secondary, bar)
        lay.addWidget(self._status_right)
        return bar

    # ------------------------------------------------------------------
    # 状态同步
    # ------------------------------------------------------------------

    def _set_status(self, text: str, right: str = "") -> None:
        self._status.setText(text)
        if right:
            self._status_right.setText(right)
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.statusMessage.emit(text, 4000)
            except Exception:
                pass

    def _on_key_chip(self, pc: int, checked: bool) -> None:
        if self._syncing:
            return
        if not checked:
            # 主音必须有值 —— 点已选中的芯片不该把它点没了
            self._syncing = True
            self.key_chips[pc].setChecked(True)
            self._syncing = False
            return
        self.set_key(pc)

    def set_key(self, pc: int) -> None:
        self._key_pc = int(pc) % 12
        self._syncing = True
        for i, chip in enumerate(self.key_chips):
            chip.setChecked(i == self._key_pc)
        self._syncing = False
        self._refresh_preview()

    def _on_mode_chip(self, mode: str, checked: bool) -> None:
        if self._syncing:
            return
        if not checked:
            self._syncing = True
            for chip, m in zip(self.mode_chips, MODES):
                chip.setChecked(m.key == self._mode)
            self._syncing = False
            return
        self.set_mode(mode)

    def set_mode(self, mode: str) -> None:
        if mode not in MODES_BY_KEY:
            return
        self._mode = mode
        self._syncing = True
        for chip, m in zip(self.mode_chips, MODES):
            chip.setChecked(m.key == self._mode)
        self._syncing = False
        self._refresh_preview()

    def _on_seg_changed(self, index: int) -> None:
        """分段控件切到"预设"时回到上一个用过的预设（而不是永远跳第一个）。"""
        if self._syncing:
            return
        if index == 1:
            self.set_preset(CUSTOM)
        else:
            last = self._preset if self._preset != CUSTOM else PRESETS[0].key
            self.set_preset(last)

    def _on_preset_chip(self, key: str, checked: bool) -> None:
        if self._syncing:
            return
        if not checked:
            self._syncing = True
            self.preset_chips[[p.key for p in PRESETS].index(key)].setChecked(True)
            self._syncing = False
            return
        self.set_preset(key)

    def set_preset(self, key: str, apply_hints: bool = True) -> None:
        """选预设走向。预设自带的主音/调式/小节建议会一并应用 ——

        比如"Am–F–C–G"在 C 大调里是不成立的，选中它就该把主音切到 A 小调，
        否则用户得到的和弦名跟他点的预设对不上，那是界面在骗人。

        ``apply_hints=False`` 只用于初始状态：刚打开页面时不该把工程原本的
        小节数改掉（用户还没做任何选择）。
        """
        if key != CUSTOM and key not in PRESET_BY_KEY:
            return
        self._preset = key
        self._syncing = True
        try:
            self.seg.set_current_index(1 if key == CUSTOM else 0)
            for chip, preset in zip(self.preset_chips, PRESETS):
                chip.setChecked(preset.key == key)
            if key != CUSTOM and apply_hints:
                preset = PRESET_BY_KEY[key]
                if preset.key_pc is not None and preset.key_pc != self._key_pc:
                    self._key_pc = preset.key_pc
                    for i, chip in enumerate(self.key_chips):
                        chip.setChecked(i == self._key_pc)
                if preset.mode and preset.mode != self._mode:
                    self._mode = preset.mode
                    for chip, m in zip(self.mode_chips, MODES):
                        chip.setChecked(m.key == self._mode)
                if preset.suggest_bars:
                    self.set_bars(preset.suggest_bars)
        finally:
            self._syncing = False
        self.custom_box.setVisible(key == CUSTOM)
        self._sync_custom_label()
        self._refresh_preview()

    def _apply_initial_state(self) -> None:
        """初始状态：用工程自身的速度/小节数，只把走向预设成默认那条。"""
        self.set_preset(self._preset, apply_hints=False)
        self.set_bpm(self.project.bpm)
        self.set_bars(min(16, max(1, int(self.project.bars))))
        self._sync_custom_label()
        self._refresh_preview()

    # -- 自定义走向 ------------------------------------------------------

    def _on_degree_chip(self, degree: int, checked: bool) -> None:
        # 级数芯片是"按一下就加一个"，本身不该留住选中态，所以立刻还原
        self._syncing = True
        self.degree_chips[degree - 1].setChecked(False)
        self._syncing = False
        if checked:
            self.append_degree(degree)

    def append_degree(self, degree: int) -> None:
        self.set_preset(CUSTOM)
        if len(self._custom_degrees) >= 8:
            self._set_status("自定义走向最多 8 个和弦")
            return
        self._custom_degrees.append(max(1, min(7, int(degree))))
        self._sync_custom_label()
        self._refresh_preview()

    def _pop_degree(self) -> None:
        if self._custom_degrees:
            self._custom_degrees.pop()
            self._sync_custom_label()
            self._refresh_preview()

    def set_custom_degrees(self, degrees) -> None:
        self._custom_degrees = [max(1, min(7, int(d))) for d in degrees][:8]
        self.set_preset(CUSTOM)
        self._sync_custom_label()
        self._refresh_preview()

    def _sync_custom_label(self) -> None:
        text = " ".join(str(d) for d in self._custom_degrees) or "（空）"
        self._seq_label.setText(f"级数  {text}")
        ok = 4 <= len(self._custom_degrees) <= 8
        c = labels.label if ok else accents.orange.lighter(120)
        self._seq_label.setStyleSheet(
            f"color: rgba({c.red()},{c.green()},{c.blue()},"
            f"{c.alphaF():.3f}); background: transparent;")

    # -- 其它控件 --------------------------------------------------------

    def _on_part_chip(self, slot: str, on: bool) -> None:
        if self._syncing:
            return
        self._parts[slot] = bool(on)
        if not any(self._parts.values()):
            # 全不勾就什么也生成不出来 —— 与其等用户点生成再报错，不如当场说
            self._set_status("至少勾选一个声部，否则没有东西可生成")

    def set_parts(self, parts: dict) -> None:
        for slot, on in parts.items():
            if slot in self._parts:
                self._parts[slot] = bool(on)
        self._syncing = True
        for slot, chip in self.part_chips.items():
            chip.setChecked(self._parts[slot])
        self._syncing = False

    def set_bars(self, bars: int) -> None:
        bars = max(1, min(16, int(bars)))
        self.bars_slider.set_value(bars)
        self._bars_value.setText(str(bars))
        self._refresh_preview()

    def set_bpm(self, bpm: float) -> None:
        bpm = max(60.0, min(180.0, float(bpm)))
        self.bpm_slider.set_value(bpm)
        self._bpm_value.setText(str(int(round(bpm))))
        self._sync_bar_seconds()

    def set_density(self, value: float) -> None:
        value = max(0.0, min(1.0, float(value)))
        self.density_slider.set_value(value)
        self._density_value.setText(f"{value:.2f}")
        self._refresh_preview()

    def set_seed(self, seed) -> None:
        self.seed_edit.setText("" if seed is None else str(int(seed)))

    def _roll_seed(self) -> None:
        import random as _random

        self.seed_edit.setText(str(_random.SystemRandom().randrange(1, 10 ** 6)))

    def _seed_value(self) -> int | None:
        text = self.seed_edit.text().strip()
        if not text:
            return None
        try:
            return int(text)
        except ValueError:
            return None

    # -- 当前参数 --------------------------------------------------------

    def current_params(self) -> GenParams:
        if self._preset == CUSTOM:
            degrees = tuple(self._custom_degrees)
        else:
            degrees = PRESET_BY_KEY[self._preset].degrees
        # 取色起点要排除上一次生成的轨道：否则"同一份参数重生成"颜色会漂移，
        # 而颜色是用户识别轨道的第一信号，漂移会让人以为生成了别的东西
        offset = len(self.project.tracks) - len(self.generated_tracks())
        return GenParams(
            key_pc=self._key_pc,
            mode=self._mode,
            degrees=degrees,
            bars=int(round(self.bars_slider.value())),
            bpm=float(round(self.bpm_slider.value())),
            parts=dict(self._parts),
            density=float(self.density_slider.value()),
            seed=self._seed_value(),
            palette_offset=max(0, offset),
        )

    # ------------------------------------------------------------------
    # 生成 / 清空 / 试听
    # ------------------------------------------------------------------

    def generated_tracks(self) -> list[Track]:
        """扫工程找回本页生成的轨道。

        为什么不直接存 Track 对象：撤销/重做会把整个工程从 JSON 重建，
        对象全部换新，存下来的引用会变成"幽灵轨道"（不在工程里，却还被
        当成生成结果）。

        为什么也不存一份 id 列表：id 同样活不过重建 —— 更糟的是它会**静默地**
        失效。撤销把上一批轨道原样还回来之后，页面手里的 id 已经对不上任何东西，
        于是再也认不出那批轨道；如果这时还按"把 id 列表过滤一遍"来维护归属，
        列表只会越来越短，永远回不来。表现为「生成 → 生成 → 撤销 → 再生成」
        把两批 AI 轨叠进工程，而且「清空本次生成」只删得掉新的一批，
        留下永远清不掉的孤儿轨。

        归属写在 ``Track.ai_generated`` 上、跟着工程 JSON 一起往返，这里只做一次
        幂等的扫描：工程里有什么，就认得什么。
        """
        return [t for t in self.project.tracks if t.ai_generated]

    def generate(self) -> bool:
        """生成并写入工程。返回是否真的生成了。"""
        params = self.current_params()
        if not any(params.parts.values()):
            self._set_status("至少要勾选一个声部")
            return False
        if self._preset == CUSTOM and not (4 <= len(params.degrees) <= 8):
            self._set_status(
                f"自定义走向需要 4~8 个和弦（当前 {len(params.degrees)} 个）")
            return False
        if not params.degrees:
            self._set_status("和弦走向是空的")
            return False

        try:
            # 生成是纯计算，先算完再压撤销点 —— 万一算出异常，撤销栈里
            # 就不会留下一个"什么都没发生"的空点
            result = compose(self.project, params)
        except Exception as exc:                      # pragma: no cover - 兜底
            self._set_status(f"生成失败：{exc}")
            return False

        self._commit("AI 生成")
        self._remove_generated()
        self.project.bars = max(int(self.project.bars), int(params.bars))
        self.project.bpm = float(params.bpm)
        for track in result.tracks:
            # 生成时就把标记打上：从这一刻起，这批轨道的归属由工程数据自己回答
            track.ai_generated = True
            self.project.add_track(track)
        self._result = result

        counts = " · ".join(f"{t.name} {len(t.notes)}" for t in result.tracks)
        self._refresh_preview()
        self._set_status(
            f"已生成 {len(result.tracks)} 条轨道（种子 {result.seed}）", counts)
        self.edited.emit()
        self.notesEdited.emit()
        return True

    def clear_generated(self) -> bool:
        """删掉本页生成的轨道。没生成过就什么都不做（也不压撤销点）。"""
        if not self.generated_tracks():
            self._set_status("还没有生成内容，无需清空")
            return False
        self._commit("清空 AI 生成")
        removed = self._remove_generated()
        self._result = None
        self._refresh_preview()
        self._set_status(f"已清空本次生成的 {removed} 条轨道")
        self.edited.emit()
        self.notesEdited.emit()
        return True

    def _remove_generated(self) -> int:
        """移走工程里所有 AI 生成轨道。

        按**标记**删而不是按 id 删：撤销一次"清空"会把轨道原样还回来（标记还在），
        页面当然还认得它们；反过来，撤销一次"生成"之后工程里残留的那批旧 AI 轨
        也会被如实认出来 —— 判据始终是"工程里长什么样"，不是"页面记得什么"。
        这样就不存在"页面认不出的孤儿轨"这种状态。
        """
        before = len(self.project.tracks)
        self.project.tracks = [t for t in self.project.tracks if not t.ai_generated]
        return before - len(self.project.tracks)

    def render_generated(self):
        """只渲染本次生成的轨道，返回 ``(audio, stats)``。

        刻意不渲染整首：试听要回答的是"这次生成的到底好不好听"，
        把用户原有的轨道混进来只会掩盖问题。渲染用的是一份**只含生成轨道
        的快照工程**，母带链照常走（限幅、淡入淡出），所以听到的和导出的一致。
        """
        tracks = self.generated_tracks()
        if not tracks:
            return None, None
        snapshot = Project(
            name="AI 试听",
            bpm=float(self.project.bpm),
            steps_per_beat=self.project.steps_per_beat,
            beats_per_bar=self.project.beats_per_bar,
            bars=int(self.project.bars),
            tracks=list(tracks),
            master=self.project.master,
        )
        return render.render_project(snapshot)

    def audition_generated(self) -> bool:
        """试听：渲染生成的轨道并立刻播放。返回是否真的响起来了。"""
        audio, stats = self.render_generated()
        if audio is None:
            self._set_status("还没有生成内容，先点「生成」")
            return False
        try:
            from app.audio.player import audition

            player = audition()
            if player is None:
                self._set_status("没有可用的音频输出设备",
                                 f"已渲染 {stats.peak_db:+.1f} dBFS")
                return False
            player.play_note(audio)
        except Exception as exc:
            self._set_status(f"试听失败：{exc}")
            return False
        self._set_status("正在试听本次生成的内容",
                         f"{audio.shape[1] / render.SR:.1f} 秒 · "
                         f"峰值 {stats.peak_db:+.1f} dBFS")
        return True

    def _commit(self, label: str) -> None:
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.push_undo(label)
            except Exception:
                pass

    # ------------------------------------------------------------------
    # 预览 / 契约接口
    # ------------------------------------------------------------------

    def preview_text(self) -> str:
        """生成结果的纯文本版本（预览面板画的就是这些内容）。

        单独留一个文本出口：离屏截图里中文会变豆腐块，测试与调试都得靠文本。
        """
        params = self.current_params()
        try:
            harmony = build_harmony(params, max(1, int(params.bars)))
        except Exception:
            return "和弦走向是空的"
        lines = [f"走向      {harmony.progression_text()}",
                 f"音阶      {' '.join(harmony.scale_names)}"
                 f"（{harmony.key_name} {harmony.mode_label}）"]
        tracks = self.generated_tracks()
        if not tracks:
            lines.append("生成结果  尚未生成")
        else:
            lines.append(f"生成结果  种子 {self._result.seed if self._result else '-'}")
            for track in tracks:
                lines.append(f"  {track.name}  {len(track.notes)} 音符")
        return "\n".join(lines)

    def _refresh_preview(self) -> None:
        params = self.current_params()
        try:
            harmony = build_harmony(params, max(1, int(params.bars)))
        except Exception:
            harmony = None
        # 小节时长必须先同步：预览里的和弦芯片是靠它把"第 N 小节"换算成秒的，
        # 不同步的话第一次点芯片会跳到一个凭默认值算出来的错误位置
        self._sync_bar_seconds()
        self.result_view.set_harmony(harmony)
        self.result_view.set_result(self._result, self.generated_tracks())
        if harmony is not None:
            self._head_summary.setText(
                f"{harmony.key_name} {harmony.mode_label} · {harmony.progression_text()}")

    def _sync_bar_seconds(self) -> None:
        self.result_view.seconds_per_bar = (
            max(1, self.project.steps_per_bar) * self.project.seconds_per_step)

    # -- 契约接口 --------------------------------------------------------

    def bind(self, controller) -> None:
        self._controller = controller
        try:
            controller.undoStateChanged.connect(self._on_undo_state)
        except Exception:
            pass

    def _on_undo_state(self, can_undo: bool, can_redo: bool) -> None:
        self.btn_clear.setEnabled(can_undo)

    def set_project(self, project: Project) -> None:
        self.project = project
        # 换工程后本次生成的**结果对象**就失效了（它描述的是另一个工程里那批轨道）；
        # 但归属不用搬 —— 打开的新工程里若本来就带着 AI 轨，靠标记照样认得出来
        self._result = None
        self.set_bpm(project.bpm)
        self.set_bars(min(16, max(1, project.bars)))
        self._refresh_preview()

    def rebuild(self) -> None:
        """工程结构变了：归属按标记重新推导，本次结果对不上就丢掉。"""
        self._sync_generated_state()
        self._refresh_preview()

    def refresh_notes(self) -> None:
        self._sync_generated_state()
        self._refresh_preview()

    def _sync_generated_state(self) -> None:
        """让"本次结果"与工程实际内容对齐。

        轨道归属是扫出来的（见 :meth:`generated_tracks`），不需要同步；这里只处理
        ``_result``：撤销掉一次生成之后，工程里剩下的可能是上一批轨道，而
        ``_result`` 还指着刚被撤掉的那一批 —— 再留着它，预览标题就会显示一个
        跟轨道内容对不上的随机种子。
        """
        if self._result is None:
            return
        alive = {t.id for t in self.generated_tracks()}
        if not alive & {t.id for t in self._result.tracks}:
            self._result = None

    def set_position(self, seconds: float, playing: bool = False) -> None:
        """播放位置 → 高亮当前小节的和弦芯片。"""
        spb = max(1, self.project.steps_per_bar)
        per_bar = spb * self.project.seconds_per_step
        if per_bar <= 0:
            return
        # 加一个极小量：否则"正好落在小节线"的位置会因为浮点误差算成上一小节
        bar = int((max(0.0, float(seconds)) + 1e-9) / per_bar)
        if self._result is not None:
            bar = min(bar, len(self._result.harmony.bar_names) - 1)
        self.result_view.seconds_per_bar = per_bar
        self.result_view.set_play_bar(bar)


__all__ = [
    "ComposerPage", "ResultView", "FlowLayout",
    "MODES", "MODES_BY_KEY", "ModeDef", "PRESETS", "PRESET_BY_KEY", "Progression",
    "PARTS", "CUSTOM", "PITCH_NAMES",
    "GenParams", "GenResult", "Harmony",
    "build_harmony", "compose", "chord_name", "chord_tones", "scale_names",
    "scale_pc_set", "scale_pitches", "pc_near", "degree_pitch",
    "params_from_profile",
]
