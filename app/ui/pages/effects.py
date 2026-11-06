"""效果器链编辑 —— 每条轨道的插入式效果链（增删 / 排序 / 调参 / 旁通）。

为什么是"左轨右链"而不是一条轨道一个页签
----------------------------------------
调效果时人的动作是**A/B 对比**：改一下混响，回去听；切到另一条轨听听它怎么处理。
做成"一页一轨"就必须来回切页签，对比的节奏立刻断掉。所以左列常驻轨道列表，
右列是当前轨道的链 —— 切轨是一次点击，不是一次页面跳转。

为什么参数界面用数据表生成，而不是为六种效果各写一套控件
--------------------------------------------------------
六种效果一共 27 个参数，逐个手写控件意味着 27 处重复的"建标签 / 建滑块 / 接信号 /
写回字典"，改一个格式要动六处。这里把参数**声明成一串** :class:`ParamSpec`
（键、中文名、范围、类型、单位），界面、读数格式、默认值全部由它派生 ——
以后加第七种效果只需要往 :data:`EFFECT_PARAMS` 里加一条数据，不用碰控件代码。

频率类参数一律**对数刻度**
--------------------------
耳朵是按倍频程听的：40Hz → 80Hz 是"低了一个八度"，8000Hz → 8040Hz 几乎听不出来。
线性滑块会把 40~1000Hz 这一整段能听出巨大差异的区间挤在左边 5% 的行程里，
而把 10k~18k（几乎只有"有没有"的区别）铺满右半边。所以截止频率、音色、速率
这些量映射到滑块的 0..1 **指数**位置上（:func:`log_to_pos` / :func:`log_to_value`）。

试听为什么要防抖、为什么只渲染开头几秒
--------------------------------------
"边调边听"是这一页存在的理由。但整轨渲染对 8 小节的曲子要几百毫秒到几秒，
挂在滑块的 ``valueChanged`` 上会变成"每动一像素重渲染一次"，界面直接卡死。
两道闸门：

1. **防抖**：改动后停手 :data:`AUDITION_DEBOUNCE_MS` 毫秒才真正渲染；
2. **只渲染开头**：造一个"只有这一条轨、只算前几小节"的**缩微工程**喂给
   :func:`app.audio.render.render_track`。bpm / steps_per_beat / swing 全部照抄，
   所以音符的秒级位置与整曲渲染**完全一致**，只是缓冲区短了；
3. **尾巴留着**：返回的片段长度是 :data:`AUDITION_SECONDS` **加上**
   :data:`AUDITION_TAIL`，不是只有前者。这一段里放的是混响/延迟的余韵 ——
   试听这一段的目的正是听效果的**性格**，而在余韵还在响的时候硬切一刀，
   听到的就不再是这个效果了（切点本身还是一次阶跃，会"啪"一声）。

改动的撤销点压在"一次编辑开始"那一刻
------------------------------------
滑块拖动过程中 ``valueChanged`` 会发几十次，每次都压撤销点的话撤销栈（上限 60）
会被一次拖动灌满。所以给滑块装了事件过滤器：**按下鼠标 / 滚轮**时压一次，
之后整个拖动过程都只占一步 —— 与音序器"拖一整笔只占一步"的语义一致。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from app import icons, theme
from app.audio import effects as fx
from app.audio import render
from app.audio.project import Effect, Project, Track
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.controls import GlassSlider
from app.widgets.glass import (
    GlassCard, GlassChip, GlassImageButton, GlassPanel, GlassSegmented, make_label,
)

# --------------------------------------------------------------------------
# 尺寸与时长常量
# --------------------------------------------------------------------------

AUDITION_SECONDS = 2.5
"""试听片段的**内容**长度（秒）。取 2.5 而不是"第一个小节"：96bpm 下一小节就有 2.5s，
而大部分效果（尤其混响、延迟）要两拍以上才听得出性格。

注意这不是返回缓冲的长度 —— 见 :data:`AUDITION_TAIL`。"""

AUDITION_TAIL = 1.2
"""额外多渲染并**保留在返回缓冲里**的尾巴（秒）。

混响/延迟的余韵落在最后一个音符之后，不留这段就等于把效果剪掉了 ——
这里的"留"必须一直留到播放流，只多渲染再截掉是没有意义的
（早先的实现正是这样：多渲染了 1.2s，却在返回前按 ``seconds`` 截断，
于是 :data:`AUDITION_TAIL` 完全没生效，注释与行为对不上）。"""

AUDITION_DEBOUNCE_MS = 350
"""停手多久才重渲染。350ms 是"松手即听到"和"别重复渲染"之间的平衡点：
比这更短会让连续拖动仍然触发多次渲染，更长就会觉得迟滞。"""

ROW_HEADER_H = 42
"""效果行的表头高度。也是"点这里展开参数"的判定范围。"""

TRACK_PANEL_W = 196
ADD_PANEL_H = 100
TRACK_ROW_H = 46
ADD_TILE_H = 58

AUDITION_CEILING = 0.95
"""试听片段的峰值上限。

试听走的是**轨道直出**，不过母带限幅器 —— 六种效果串在一起时峰值能到十几倍
满刻度（实测 17.4），直接丢给声卡就是刺耳的爆音，还会掩盖"效果本身好不好听"
这个判断。所以超出时按峰值**等比**缩到 0.95：只降电平、不动音色，
和所有 DAW 的试听行为一致。
"""

HZ_MIN_DIGITS = 1


# ==========================================================================
# 对数刻度
# ==========================================================================


def log_to_pos(value: float, lo: float, hi: float) -> float:
    """把对数刻度的量（Hz、速率…）映射到 0..1 的**滑块位置**。

    这是 UI 与数据之间唯一的换算点：滑块本身只有 0..1，语义靠这里补。
    超出范围的输入被钳住而不是报错 —— 旧工程里可能存着 25Hz 这种越界值。
    """
    lo = max(1e-9, float(lo))
    hi = max(lo * 1.000001, float(hi))
    v = min(max(float(value), lo), hi)
    return math.log(v / lo) / math.log(hi / lo)


def log_to_value(pos: float, lo: float, hi: float) -> float:
    """滑块位置（0..1）→ 对数刻度的量。:func:`log_to_pos` 的逆运算。"""
    lo = max(1e-9, float(lo))
    hi = max(lo * 1.000001, float(hi))
    p = min(max(float(pos), 0.0), 1.0)
    return lo * (hi / lo) ** p


# ==========================================================================
# 参数声明表
# ==========================================================================


@dataclass(frozen=True)
class ParamSpec:
    """一个可编辑参数的**全部**元信息。

    ``kind`` 决定界面形态：
    * ``lin`` —— 线性滑块（0..1 的比例类参数，如 mix、depth）
    * ``log`` —— 对数滑块（频率、速率、驱动量）
    * ``enum`` —— 分段控件（三选一）
    * ``bool`` —— 开关芯片
    """

    key: str
    label: str
    kind: str = "lin"
    lo: float = 0.0
    hi: float = 1.0
    unit: str = ""
    fmt: str = ".2f"
    integer: bool = False
    choices: tuple[str, ...] = ()
    choice_names: tuple[str, ...] = ()


EFFECT_PARAMS: dict[str, tuple[ParamSpec, ...]] = {
    "reverb": (
        ParamSpec("size", "空间大小", "lin", 0.0, 1.0),
        ParamSpec("damping", "高频衰减", "lin", 0.0, 1.0),
        ParamSpec("mix", "干湿比", "lin", 0.0, 1.0),
        # 预延迟最多 150ms —— 再长就不像"空间"而像"回声"了，那是延迟器的活
        ParamSpec("predelay", "预延迟", "lin", 0.0, 0.15, unit=" s", fmt=".3f"),
        ParamSpec("width", "立体声宽度", "lin", 0.0, 1.0),
    ),
    "delay": (
        # 时值以**拍**为单位（1/16 拍 ~ 2 拍）：变速时延迟自动跟着走
        ParamSpec("time_beats", "延迟时值", "log", 0.0625, 2.0, unit=" 拍", fmt="g"),
        ParamSpec("feedback", "反馈", "lin", 0.0, 0.9),
        ParamSpec("mix", "干湿比", "lin", 0.0, 1.0),
        ParamSpec("tone", "回声音色", "log", 500.0, 12000.0, unit="Hz"),
        ParamSpec("ping_pong", "乒乓", "bool"),
    ),
    "distortion": (
        # 驱动量 0.1~20 跨两个数量级，线性刻度下面 0.1~2 这一段会被压没
        ParamSpec("drive", "驱动量", "log", 0.1, 20.0, fmt="g"),
        ParamSpec("tone", "音色", "log", 200.0, 12000.0, unit="Hz"),
        ParamSpec("mix", "干湿比", "lin", 0.0, 1.0),
        ParamSpec("mode", "削波方式", "enum",
                  choices=("soft", "hard", "tube"),
                  choice_names=("软削波", "硬削波", "电子管")),
    ),
    "filter": (
        ParamSpec("kind", "类型", "enum",
                  choices=("lp", "hp", "bp"),
                  choice_names=("低通", "高通", "带通")),
        ParamSpec("cutoff", "截止频率", "log", 40.0, 18000.0, unit="Hz"),
        ParamSpec("resonance", "谐振", "log", 0.1, 12.0),
    ),
    "chorus": (
        ParamSpec("rate", "速率", "log", 0.05, 5.0, unit=" Hz"),
        ParamSpec("depth", "深度", "lin", 0.0, 1.0),
        ParamSpec("mix", "干湿比", "lin", 0.0, 1.0),
        ParamSpec("spread", "左右错相", "lin", 0.0, 1.0),
    ),
    "bitcrush": (
        ParamSpec("bits", "位深", "lin", 2.0, 16.0, unit=" bit", integer=True),
        ParamSpec("downsample", "降采样", "lin", 1.0, 32.0, unit=" 倍", integer=True),
        ParamSpec("mix", "干湿比", "lin", 0.0, 1.0),
    ),
}

#: 添加区里列出的顺序 —— 从"空间感"到"破坏性"，也是新手最可能依次尝试的顺序
EFFECT_ORDER: tuple[str, ...] = (
    "reverb", "delay", "distortion", "filter", "chorus", "bitcrush",
)

#: 效果工厂。**默认值只在这里出现一次** —— 界面读不到的键回落到工厂造出来的
#: 那份参数，这样"工厂改了默认值而界面还显示老数字"这种不一致不可能发生。
EFFECT_FACTORY = {
    "reverb": Effect.reverb,
    "delay": Effect.delay,
    "distortion": Effect.distortion,
    "filter": Effect.filter,
    "chorus": Effect.chorus,
    "bitcrush": Effect.bitcrush,
}

#: 每种效果的身份色。图标是 Twemoji 彩色位图，底色只做极轻的一层，
#: 但在"添加"区的描边与效果行的图标底上用得着。
EFFECT_ACCENT = {
    "reverb": accents.purple,
    "delay": accents.teal,
    "distortion": accents.red,
    "filter": accents.blue,
    "chorus": accents.pink,
    "bitcrush": accents.orange,
}


def effect_label(kind: str) -> str:
    """效果的中文名。未知类型退回原始键名 —— 旧工程遇到新效果不会崩。"""
    return fx.EFFECT_LABELS.get(kind, kind)


def effect_icon(kind: str) -> str:
    return fx.EFFECT_ICONS.get(kind, "wrench")


def effect_accent(kind: str) -> QColor:
    return EFFECT_ACCENT.get(kind, accents.blue)


#: 徽章上要显示的那个"决定性格"的枚举参数：滤波器是类型，失真是削波方式。
BADGE_ENUM_KEY = {"filter": "kind", "distortion": "mode"}


def badge_detail(effect: Effect) -> str:
    """效果行徽章上那句"一眼看出这条链在干什么"的枚举值（没有则空串）。

    滤波器显示 ``LP / HP / BP`` —— 这三个是国际通用缩写，换成中文反而更长，
    而且音乐人读它们和读 "Hz" 一样自然。失真**没有**通用缩写，引擎里的值就是
    ``soft / hard / tube`` 这种英文单词，直接贴上去与全中文界面格格不入，
    所以走 :func:`format_value` 取 :attr:`ParamSpec.choice_names`
    （软削波 / 硬削波 / 电子管）—— 界面别处（分段控件）显示的就是这三个词，
    同一件事不该有两种说法。
    """
    key = BADGE_ENUM_KEY.get(effect.kind)
    if key is None:
        return ""
    spec = next((s for s in specs_of(effect.kind) if s.key == key), None)
    if spec is None:                 # 声明表里没有这条 —— 退回原始值，绝不崩
        return str(effect.params.get(key, ""))
    value = read_param(effect, spec)
    return str(value).upper() if effect.kind == "filter" else format_value(spec, value)


def default_params(kind: str) -> dict:
    """工厂默认参数的一份拷贝。"""
    factory = EFFECT_FACTORY.get(kind)
    if factory is None:
        return {}
    return dict(factory().params)


def format_value(spec: ParamSpec, value) -> str:
    """把一个参数值格式化成界面读数。

    频率自适应单位：1000Hz 以下按 Hz 给整数（用户关心的是"切到哪"），
    以上按 kHz 给两位（此时整数 Hz 已经是噪声，没人在意 8123 还是 8124）。
    """
    if spec.kind == "bool":
        return "开启" if value else "旁通"
    if spec.kind == "enum":
        text = str(value)
        try:
            return spec.choice_names[spec.choices.index(text)]
        except (ValueError, IndexError):
            return text
    try:
        v = float(value)
    except (TypeError, ValueError):
        return str(value)
    if spec.integer:
        return f"{int(round(v))}{spec.unit}"
    if spec.unit == "Hz":
        return f"{v:.0f} Hz" if v < 1000.0 else f"{v / 1000.0:.2f} kHz"
    return f"{format(v, spec.fmt)}{spec.unit}"


def snap_value(spec: ParamSpec, value):
    """把浮点值对齐到界面能显示的精度。

    不做这一步的话 ``params`` 里会存进 0.30000000000000004 这类数：JSON 里难看，
    而且"改回原值"会因为最后一位不同而判定失败（撤销栈里出现一堆无意义的步）。

    枚举（"lp"）与布尔不参与数值对齐 —— 早期版本在这里无脑 ``float(value)``，
    结果一选滤波器类型就抛 ValueError。
    """
    if spec.kind in ("enum", "bool"):
        return value
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return value
    if spec.integer:
        return int(round(numeric))
    if spec.unit == "Hz":
        return round(numeric, HZ_MIN_DIGITS)
    digits = {"g": 4, ".2f": 2, ".3f": 3}.get(spec.fmt, 2)
    return round(numeric, digits)


def specs_of(kind: str) -> tuple[ParamSpec, ...]:
    return EFFECT_PARAMS.get(kind, ())


def _fallback_value(effect: Effect, spec: ParamSpec):
    """这个参数"读不出合法值"时该拿什么顶上：工厂默认值，再不行用范围下界。"""
    default = default_params(effect.kind).get(spec.key)
    return spec.lo if default is None else default


def normalize_value(spec: ParamSpec, value, fallback=None):
    """把任意来源的值收进 :class:`ParamSpec` 声明的合法域。

    为什么必须有这一步
    ------------------
    ``Effect.params`` 是**自由字典**：存盘文件、人手改过的 JSON、别的版本写进去的
    值，都可能是 ``None`` / ``"abc"`` / ``NaN``。只需要"键不存在"才兜底是不够的 ——
    键存在而值是坏值时，``float(raw)`` 会在 :meth:`ParamEditor._make_slider` 里抛
    TypeError / ValueError，表现是"打开这个工程页面就崩"，而坏值可能来自用户
    根本不该关心的字段。所以坏值与缺键必须走**同一条**兜底路径。

    为什么读数和滑块都必须走它
    --------------------------
    一处钳位、另一处不钳位，同一个参数就会出现两种说法：``cutoff=25``（下界 40）
    时滑块停在 40Hz 而读数写着 ``25 Hz``；``bits=4.6`` 时滑块停在 4.6 而读数已是
    ``5 bit``（写进模型的是取整后的 5）。规范化只做一次，界面各处都从它派生。

    ``fallback`` 由调用方给（见 :func:`read_param`）—— 这里不认识 ``effect``，
    所以它只负责"值本身合不合法"，"该退回什么"是上层的事。
    """
    if spec.kind == "bool":
        return bool(value)
    if spec.kind == "enum":
        text = str(value)
        if text in spec.choices:
            return text
        # 认不出来的枚举值退回默认项：分段控件本来就只能落在某个选项上
        # （_choice_index 早已这样回落），不在这里统一的话，徽章会显示 "NONE"
        # 而分段控件显示"低通" —— 同一个参数两种说法。
        fb = str(fallback)
        if fb in spec.choices:
            return fb
        return spec.choices[0] if spec.choices else text

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        numeric = None
    if numeric is None or not math.isfinite(numeric):
        # NaN / inf 也算坏值：NaN 会让钳位整个失效（``max(NaN, lo)`` 在 CPython 里
        # 仍然返回 NaN），一路传进滑块位置与绘制里，画出的是无意义的图元。
        try:
            numeric = float(fallback) if fallback is not None else float(spec.lo)
        except (TypeError, ValueError):
            numeric = float(spec.lo)
        if not math.isfinite(numeric):
            numeric = float(spec.lo)
    # 钳位：旧工程里存着 25Hz 这种越界值是常态（见 log_to_pos 的注释）
    numeric = min(max(numeric, float(spec.lo)), float(spec.hi))
    return int(round(numeric)) if spec.integer else numeric


def read_param(effect: Effect, spec: ParamSpec):
    """读参数 —— 界面读值的**唯一**入口。

    缺键、坏值（``None`` / 非法字符串 / NaN）、越界三种情况在这里一次性收拢：
    前两种回落到工厂默认值（再不行用范围下界），第三种钳进 ``[lo, hi]``。
    """
    return normalize_value(spec, effect.params.get(spec.key),
                           _fallback_value(effect, spec))


# ==========================================================================
# 试听：渲染 + 播放
# ==========================================================================


def render_audition_clip(
    project: Project,
    track: Track,
    seconds: float = AUDITION_SECONDS,
    tail: float = AUDITION_TAIL,
    sr: int = render.SR,
) -> np.ndarray:
    """渲染"这条轨道 + 这条效果链"的开头几秒，返回 (2, n)。

    关键是那个**缩微工程**：直接渲染整曲要几百毫秒到几秒（8 小节 × 几条轨），
    而这里只要开头三秒。缩微工程照抄 bpm / steps_per_beat / beats_per_bar / swing，
    所以 :meth:`Project.step_time` 的结果与整曲渲染逐位相同 —— 音符位置不会有任何偏移，
    变的只是缓冲区长度。

    返回长度是 ``seconds + tail``，``tail`` 留在缓冲里交给播放流（理由见模块头
    与 :data:`AUDITION_TAIL`）—— 只多渲染再截掉，等于这段尾巴从来没存在过。
    """
    spb = max(1, project.steps_per_bar)
    step_seconds = max(1e-6, project.seconds_per_step)
    need = max(0.05, float(seconds)) + max(0.0, float(tail))
    bars = max(1, int(math.ceil(need / (spb * step_seconds))))

    mini = Project(
        name="audition",
        bpm=project.bpm,
        steps_per_beat=project.steps_per_beat,
        beats_per_bar=project.beats_per_bar,
        bars=bars,
        swing=project.swing,
    )
    mini.tracks = [track]

    buf = render.render_track(track, mini, sr)
    n = max(2, int(need * sr))
    if buf.shape[1] > n:
        buf = buf[:, :n]
    # 试听也过一遍轨道音量：不然"效果让声音变小了"和"轨道音量小"会被混为一谈。
    # 声像不过（试听是单轨 A/B，左右摆不改变对音色的判断）。
    vol = float(getattr(track, "volume", 1.0))
    if abs(vol - 1.0) > 1e-6:
        buf = buf * vol

    # 安全增益：这条路上没有母带限幅器兜底（见 AUDITION_CEILING 的说明）
    peak = float(np.max(np.abs(buf))) if buf.size else 0.0
    if peak > AUDITION_CEILING:
        buf = buf * (AUDITION_CEILING / peak)

    return np.ascontiguousarray(buf, dtype=np.float32)


def play_audition_clip(
    project: Project, track: Track,
    seconds: float = AUDITION_SECONDS,
    tail: float = AUDITION_TAIL,
) -> bool:
    """渲染并立刻播放一小段。返回是否真的发出了声音。

    空轨（没有音符）会渲染出一段纯静音，此时**不播** —— 播放静音除了占住
    试听流什么也证明不了，而且会让用户以为"效果把声音弄没了"。
    """
    try:
        buf = render_audition_clip(project, track, seconds, tail)
    except Exception:
        return False
    if buf.size == 0 or float(np.max(np.abs(buf))) < 1e-4:
        return False
    try:
        from app.audio.player import audition

        player = audition()
        if player is None:
            return False
        return bool(player.play_note(buf))
    except Exception:
        return False


# ==========================================================================
# 参数编辑区
# ==========================================================================


class BoolChip(GlassChip):
    """布尔参数的开关芯片。

    只为一件事存在：:class:`GlassChip` 有 ``setText`` 却没有 ``text`` 读取口，
    而"开关的当前状态"必须能被读出来（界面要显示它，测试要断言它）。
    不去改 ``glass.py``（公共设施，改了会影响别人），在自己模块里补一个子类最省事。
    """

    def text(self) -> str:
        return self._text


class ParamEditor(QWidget):
    """一个效果的参数编辑区，控件全部由 :data:`EFFECT_PARAMS` 生成。"""

    #: 某个参数被改了（参数键）
    changed = Signal(str)
    #: 一次编辑**即将**发生（用于压撤销点）。拖动全程只在按下时发一次。
    editStarted = Signal(str)

    def __init__(self, effect: Effect, accent: QColor, parent: QWidget | None = None):
        super().__init__(parent)
        self.effect = effect
        self._accent = accent
        #: 装载期间屏蔽信号 —— 用代码回填控件值时会触发 valueChanged，
        #: 不屏蔽就会把"显示"当成"用户改动"，压出一堆空撤销点。
        self._loading = False
        self._specs = specs_of(effect.kind)
        self._by_key = {s.key: s for s in self._specs}
        self.sliders: dict[str, GlassSlider] = {}
        self.segments: dict[str, GlassSegmented] = {}
        self.switches: dict[str, GlassChip] = {}
        #: 读数标签；布尔参数这里放的是芯片本身（芯片文字就是读数）
        self.readouts: dict[str, QWidget] = {}

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 2, 0, 2)
        root.setSpacing(2)

        if not self._specs:
            # 未知效果器（比如新版本存盘的文件）：给出说明而不是空白，
            # 更不能崩 —— 旧工程必须能打开。
            hint = make_label(f"「{effect.kind}」还没有参数界面", type_scale.caption1,
                              labels.quaternary, self)
            root.addWidget(hint)
            return

        for spec in self._specs:
            root.addWidget(self._build_row(spec))

    # -- 构建 ------------------------------------------------------------

    def _build_row(self, spec: ParamSpec) -> QWidget:
        row = QWidget(self)
        row.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        row.setFixedHeight(30)
        lay = QHBoxLayout(row)
        lay.setContentsMargins(30, 0, 4, 0)     # 左边留出与效果图标对齐的缩进
        lay.setSpacing(spacing.xs)

        lay.addWidget(make_label(spec.label, type_scale.caption1, labels.tertiary, row))

        if spec.kind == "enum":
            seg = GlassSegmented(list(spec.choice_names), row, accent=self._accent)
            seg.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
            seg.set_current_index(self._choice_index(spec), animate=False)
            seg.changed.connect(lambda idx, s=spec: self._on_enum(s, idx))
            self.segments[spec.key] = seg
            lay.addWidget(seg, 1)
            # 分段控件自己写着中文名，再放一个读数标签是纯冗余；留出同宽的空白，
            # 让六种效果的参数行在视觉上对齐
            lay.addSpacing(76)
        elif spec.kind == "bool":
            chip = BoolChip(format_value(spec, read_param(self.effect, spec)),
                            row, accent=self._accent, checkable=True,
                            selected=bool(read_param(self.effect, spec)))
            chip.clicked.connect(lambda s=spec: self._on_bool(s))
            self.switches[spec.key] = chip
            lay.addStretch(1)
            lay.addWidget(chip)
            self.readouts[spec.key] = chip          # 芯片文字就是读数
        else:
            slider = self._make_slider(spec, row)
            lay.addWidget(slider, 1)
            lay.addWidget(self._make_readout(row, spec))
        return row

    def _make_slider(self, spec: ParamSpec, parent: QWidget) -> GlassSlider:
        # 位置从 read_param 的**规范化结果**派生（钳位、整数取整都在里面），
        # 读数走的是同一个值 —— 两处各算一遍就会出现"滑块在 40Hz、读数写 25 Hz"
        raw = read_param(self.effect, spec)
        if spec.kind == "log":
            lo, hi, value = 0.0, 1.0, log_to_pos(float(raw), spec.lo, spec.hi)
        else:
            lo, hi, value = spec.lo, spec.hi, min(max(float(raw), spec.lo), spec.hi)
        slider = GlassSlider(parent, lo, hi, value, accent=self._accent, height=22)
        slider.valueChanged.connect(lambda v, s=spec: self._on_slider(s, v))
        # 事件过滤器：按下鼠标 / 滚轮那一刻压撤销点（见模块头部说明）
        setattr(slider, "_fx_param_key", spec.key)
        slider.installEventFilter(self)
        self.sliders[spec.key] = slider
        return slider

    def _make_readout(self, parent: QWidget, spec: ParamSpec) -> QWidget:
        # 与 _make_slider 同源：同一个 read_param 结果，界面里不存在"第二个真值"
        lbl = QLabel(format_value(spec, read_param(self.effect, spec)), parent)
        lbl.setFont(type_scale.mono.font())
        lbl.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        lbl.setFixedWidth(76)
        c = labels.secondary
        lbl.setStyleSheet(
            f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});"
            "background: transparent;")
        self.readouts[spec.key] = lbl
        return lbl

    def _choice_index(self, spec: ParamSpec) -> int:
        text = str(read_param(self.effect, spec))
        try:
            return max(0, spec.choices.index(text))
        except ValueError:
            return 0

    # -- 写入 ------------------------------------------------------------

    def _write(self, spec: ParamSpec, value) -> None:
        value = snap_value(spec, value)
        if self.effect.params.get(spec.key, object()) == value:
            return                              # 值没变就别惊动工程与试听
        self.effect.params[spec.key] = value
        readout = self.readouts.get(spec.key)
        if readout is not None:
            # QLabel 与 GlassChip 都有 setText —— 布尔参数直接把芯片文字当读数
            readout.setText(format_value(spec, value))
        self.changed.emit(spec.key)

    def _on_slider(self, spec: ParamSpec, pos: float) -> None:
        if self._loading:
            return
        value = log_to_value(pos, spec.lo, spec.hi) if spec.kind == "log" else float(pos)
        self._write(spec, value)
        # 整数参数写进模型时会取整（bits=5.37 → 5）：滑块必须跟着停在整数上。
        # 不收这一步的话，滑块在 5.37、读数写"5 bit"、模型存 5 —— 三处三个数，
        # 用户看到手柄明明在 5 和 6 中间，界面却说 5。notify=False 防止回灌成
        # 一次新编辑（也不能放进 _write：那里会因为"值没变"提前返回，滑不回来）。
        if spec.integer:
            self.sliders[spec.key].set_value(float(normalize_value(spec, value)),
                                             notify=False)

    def _on_enum(self, spec: ParamSpec, index: int) -> None:
        if self._loading:
            return
        # 引擎还没被碰过（分段控件只改了自己的索引），所以此刻压撤销点顺序正确
        self.editStarted.emit(spec.label)
        if 0 <= index < len(spec.choices):
            self._write(spec, spec.choices[index])

    def _on_bool(self, spec: ParamSpec) -> None:
        if self._loading:
            return
        chip = self.switches[spec.key]
        self.editStarted.emit(spec.label)
        self._write(spec, bool(chip.isChecked()))

    def eventFilter(self, obj, event):
        """滑块按下/滚轮 → 只在这一刻压一次撤销点。"""
        etype = event.type()
        if etype == QEvent.Type.MouseButtonPress or etype == QEvent.Type.Wheel:
            key = getattr(obj, "_fx_param_key", None)
            if key is not None and not self._loading:
                spec = self._by_key.get(key)
                self.editStarted.emit(spec.label if spec is not None else key)
        return super().eventFilter(obj, event)

    # -- 回读 ------------------------------------------------------------

    def reload(self) -> None:
        """从 ``effect.params`` 回填控件（撤销 / 外部改动之后调用）。"""
        self._loading = True
        try:
            for spec in self._specs:
                value = read_param(self.effect, spec)
                if spec.key in self.sliders:
                    slider = self.sliders[spec.key]
                    pos = (log_to_pos(float(value), spec.lo, spec.hi)
                           if spec.kind == "log" else float(value))
                    slider.set_value(pos, notify=False)
                if spec.key in self.segments:
                    self.segments[spec.key].set_current_index(
                        self._choice_index(spec), animate=False)
                if spec.key in self.switches:
                    self.switches[spec.key].setChecked(bool(value))
                readout = self.readouts.get(spec.key)
                if readout is not None and spec.key not in self.switches:
                    readout.setText(format_value(spec, value))
        finally:
            self._loading = False


# ==========================================================================
# 效果行
# ==========================================================================


class EffectRow(GlassCard):
    """效果链里的一行：图标 + 中文名 + 开关 + 上移/下移 + 删除（+ 可展开的参数区）。

    点表头空白处（图标与名字那一段）或右侧箭头展开参数。**不整行接管点击**：
    行内还叠着五个按钮和一个滑块，整行当按钮会让它们全部失效。
    """

    moveRequested = Signal(object, int)     # (effect, ±1)
    removeRequested = Signal(object)
    toggleRequested = Signal(object)
    expandRequested = Signal(object)
    paramChanged = Signal(object, str)
    paramEditStarted = Signal(object, str)

    def __init__(self, effect: Effect, index: int, total: int, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.control, material=theme.black(0.24))
        self.effect = effect
        self.index = index
        self.total = total
        self._accent = effect_accent(effect.kind)
        self.set_accent(self._accent)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)

        root = QVBoxLayout(self)
        root.setContentsMargins(8, 5, 8, 5)
        root.setSpacing(4)

        head = QWidget(self)
        head.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        head.setFixedHeight(30)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(4)

        self.btn_icon = GlassImageButton(
            effect_icon(effect.kind), head, size=26, icon_size=18,
            accent=self._accent, style="tinted", tooltip="展开参数",
        )
        self.btn_icon.clicked.connect(lambda: self.expandRequested.emit(self.effect))
        hl.addWidget(self.btn_icon)

        self.name = make_label(effect_label(effect.kind), type_scale.subhead, labels.label, head)
        hl.addWidget(self.name)

        self.badge = make_label("", type_scale.caption2, labels.quaternary, head)
        hl.addWidget(self.badge)

        hl.addStretch(1)

        self.btn_toggle = GlassImageButton(
            "check" if effect.enabled else "cross", head, size=24, icon_size=15,
            style="tinted" if effect.enabled else "plain",
            accent=accents.green, tooltip="启用 / 旁通这个效果",
        )
        self.btn_toggle.clicked.connect(lambda: self.toggleRequested.emit(self.effect))
        hl.addWidget(self.btn_toggle)

        self.btn_up = GlassImageButton("arrow_up", head, size=24, icon_size=13,
                                       tooltip="上移（更早经过）")
        self.btn_up.clicked.connect(lambda: self.moveRequested.emit(self.effect, -1))
        self.btn_up.set_dimmed(index <= 0)
        hl.addWidget(self.btn_up)

        self.btn_down = GlassImageButton("arrow_down", head, size=24, icon_size=13,
                                         tooltip="下移（更晚经过）")
        self.btn_down.clicked.connect(lambda: self.moveRequested.emit(self.effect, 1))
        self.btn_down.set_dimmed(index >= total - 1)
        hl.addWidget(self.btn_down)

        self.btn_delete = GlassImageButton("delete", head, size=24, icon_size=14,
                                           style="destructive", tooltip="从链上删除")
        self.btn_delete.clicked.connect(lambda: self.removeRequested.emit(self.effect))
        hl.addWidget(self.btn_delete)

        self.btn_chevron = GlassImageButton("arrow_right", head, size=22, icon_size=12,
                                            tooltip="展开 / 收起参数")
        self.btn_chevron.clicked.connect(lambda: self.expandRequested.emit(self.effect))
        hl.addWidget(self.btn_chevron)

        root.addWidget(head)

        self.editor = ParamEditor(effect, self._accent, self)
        self.editor.changed.connect(lambda key: self.paramChanged.emit(self.effect, key))
        self.editor.editStarted.connect(
            lambda key: self.paramEditStarted.emit(self.effect, key))
        self.editor.setVisible(False)
        root.addWidget(self.editor)

        self._sync_state()

    # -- 状态 ------------------------------------------------------------

    def _sync_state(self) -> None:
        """把 model 状态画进界面。

        旁通的效果必须**一眼**看出来：调了一半的链里最容易搞混的就是
        "为什么这个效果没起作用" —— 答案是它被旁通了，而界面没显示。
        """
        on = bool(self.effect.enabled)
        self.btn_toggle.set_icon("check" if on else "cross")
        self.btn_toggle.set_style_kind("tinted" if on else "plain")
        self.name.setStyleSheet(
            f"color: rgba({(labels.label if on else labels.quaternary).red()},"
            f"{(labels.label if on else labels.quaternary).green()},"
            f"{(labels.label if on else labels.quaternary).blue()},"
            f"{(labels.label if on else labels.quaternary).alphaF():.3f});"
            "background: transparent;")
        self.badge.setStyleSheet(
            f"color: rgba({labels.tertiary.red()},{labels.tertiary.green()},"
            f"{labels.tertiary.blue()},{labels.tertiary.alphaF():.3f});"
            "background: transparent;")
        # 徽章顺带显示"决定性格"的那个枚举参数（低通/高通、软削波…），
        # 收起状态下也能一眼看出这条链在干什么
        detail = badge_detail(self.effect)
        self.badge.setText(" · ".join(([detail] if detail else [])
                                      + ([] if on else ["已旁通"])))

    def set_expanded(self, value: bool) -> None:
        self.editor.setVisible(value)
        self.btn_chevron.set_icon("arrow_down" if value else "arrow_right")
        self.btn_icon.set_style_kind("tinted" if value else "plain")
        self.updateGeometry()

    @property
    def expanded(self) -> bool:
        """是否展开。

        用 ``isHidden()`` 而不是 ``isVisible()``：后者还要求整条父链都已显示，
        于是"行刚建好、还没插进布局"时它会误报未展开。``isHidden()`` 对子控件
        的语义就是"是否被显式隐藏"，正是这里要问的问题。
        """
        return not self.editor.isHidden()

    def reload(self) -> None:
        self._sync_state()
        self.editor.reload()
        self.update()

    # -- 交互 ------------------------------------------------------------

    def mousePressEvent(self, event):
        # 只有表头那一条带算"点击展开"；点参数区里的空白不该把编辑器收起来
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= ROW_HEADER_H:
            self.expandRequested.emit(self.effect)
        super().mousePressEvent(event)


# ==========================================================================
# 左侧轨道列表 / 底部添加区
# ==========================================================================


class TrackPick(GlassCard):
    """轨道列表里的一行。点一下 = 把右边换成这条轨道的效果链。"""

    clicked = Signal(object)

    def __init__(self, track: Track, index: int, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.control, material=theme.black(0.26))
        self.track = track
        self.index = index
        self._color = QColor(track.color)
        self.set_accent(self._color)
        self.setFixedHeight(TRACK_ROW_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit(self.track)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        # 轨道色条：轨道身份的第一识别信号，比读名字快得多
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(self._color)
        painter.drawRoundedRect(QRectF(6.0, 12.0, 3.0, self.height() - 24.0), 1.5, 1.5)

        pm = icons.pixmap(self.track.icon, 20, opacity=0.9, dpr=self.devicePixelRatioF())
        painter.drawPixmap(QPointF(15.0, (self.height() - 20) / 2.0), pm)

        f = type_scale.caption1.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        fm = QFontMetricsF(f)
        name_w = self.width() - 46.0
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(42.0, 6.0, name_w, 16.0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
                         fm.elidedText(self.track.name, Qt.TextElideMode.ElideRight, name_w))

        n = len(self.track.effects)
        used = sum(1 for e in self.track.effects if e.enabled)
        if n == 0:
            text = "无效果"
        elif used == n:
            text = f"{n} 个效果"
        else:
            text = f"{n} 个效果 · {used} 启用"
        painter.setFont(type_scale.caption2.font())
        painter.setPen(QPen(labels.quaternary))
        painter.drawText(QRectF(42.0, 23.0, name_w, 14.0),
                         Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, text)
        painter.end()


class AddEffectTile(GlassCard):
    """「添加效果」区里的一格。点一下把这个效果加到链尾。"""

    clicked = Signal(str)

    def __init__(self, kind: str, parent: QWidget | None = None):
        super().__init__(parent, radius=radii.control, material=theme.black(0.22))
        self.kind = kind
        self._accent = effect_accent(kind)
        self.set_accent(self._accent)
        self.setFixedHeight(ADD_TILE_H)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setMaximumWidth(220)      # 单格别宽到"图标漂在中间看不见"的程度
        self.setMinimumWidth(84)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip(f"添加「{effect_label(kind)}」到链尾")

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.isEnabled():
            self.clicked.emit(self.kind)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        rect = QRectF(self.rect())

        active = self.isEnabled()
        if active and self._hover:
            # 悬停给一圈强调色描边：这类"加东西"的格子必须让人确信能点
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.setPen(QPen(QColor(self._accent), 1.2))
            painter.drawPath(theme.squircle(rect.adjusted(1, 1, -1, -1), radii.control))

        pm = icons.pixmap(effect_icon(self.kind), 26, opacity=1.0 if active else 0.4,
                          dpr=self.devicePixelRatioF())
        painter.drawPixmap(QPointF(rect.center().x() - 13.0, 9.0), pm)

        f = type_scale.caption1.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        if active and self._hover:
            painter.setPen(QPen(labels.label))
        else:
            painter.setPen(QPen(labels.secondary if active else labels.quaternary))
        painter.drawText(QRectF(0, 37, rect.width(), 16),
                         Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignVCenter,
                         effect_label(self.kind))

        # 右上角一个"＋"，明确这是"加"而不是"选"
        painter.setFont(type_scale.caption1.font())
        painter.setPen(QPen(labels.quaternary if active else theme.white(0.12)))
        painter.drawText(QRectF(rect.width() - 22.0, 4.0, 16.0, 16.0),
                         Qt.AlignmentFlag.AlignCenter, "＋")
        painter.end()


# ==========================================================================
# 页面
# ==========================================================================


def _clear_layout(layout) -> None:
    """把一个布局里的条目全部拆掉。

    必须先 ``setParent(None)`` 再 ``deleteLater()``：只调 deleteLater() 的话控件
    在事件循环转起来之前仍挂在布局里，紧接着 insertWidget 会看到新旧混在一起。
    """
    while layout.count():
        item = layout.takeAt(0)
        widget = item.widget()
        if widget is not None:
            widget.setParent(None)
            widget.deleteLater()
        elif item.layout() is not None:
            _clear_layout(item.layout())


def _scroll_host(inner: QWidget, parent: QWidget) -> QScrollArea:
    """套一个没有边框、滚动条极淡的滚动区（与音序器/总览页保持一致的外观）。"""
    area = QScrollArea(parent)
    area.setWidget(inner)
    area.setWidgetResizable(True)
    area.setFrameShape(QScrollArea.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    area.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    area.setStyleSheet(
        "QScrollArea{background:transparent;border:none;}"
        "QScrollBar:vertical{background:transparent;width:9px;margin:0;}"
        "QScrollBar::handle:vertical{background:rgba(255,255,255,45);"
        "border-radius:4px;min-height:28px;}"
        "QScrollBar::handle:vertical:hover{background:rgba(255,255,255,75);}"
        "QScrollBar::add-line,QScrollBar::sub-line{height:0;}"
        "QScrollBar::add-page,QScrollBar::sub-page{background:transparent;}"
    )
    return area


class EffectsPage(QWidget):
    """效果器链编辑页。

    左列选轨道，右列是该轨的效果链（可展开参数），底部是六种效果的添加区。
    所有改动都先进撤销栈、再改 ``project``、再 ``emit edited``。
    """

    #: 用户改了工程内容，需要重渲染。改动前**已经**调过 controller.push_undo
    edited = Signal()

    #: 契约要求：请求把播放头移到某个时间（秒）。本页没有时间轴，保留接口不发射。
    seekRequested = Signal(float)

    def __init__(self, project: Project, parent: QWidget | None = None):
        super().__init__(parent)
        self.project = project
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._controller = None
        #: bind() 时接上的 projectChanged 信号对象，换控制器时要摘掉旧的那根线
        self._bound_signal = None
        #: 被兜住的异常（撤销点压不进栈、状态栏发不出去…）。空列表 = 一切正常。
        #: 存在的理由见 :meth:`_note_error`：这些降级原来是彻底静默的。
        self._error_log: list[str] = []
        self._track_id: str | None = project.tracks[0].id if project.tracks else None
        self._expanded_effect: Effect | None = None
        self._rows: list[EffectRow] = []
        self._picks: list[TrackPick] = []
        self._add_tiles: dict[str, AddEffectTile] = {}
        self._auto_audition = True
        self._position = 0.0
        self._playing = False

        # 防抖试听：start() 每次都会重启计时器，所以"连续拖动"只会渲染最后一次
        self._audition_timer = QTimer(self)
        self._audition_timer.setSingleShot(True)
        self._audition_timer.setInterval(AUDITION_DEBOUNCE_MS)
        self._audition_timer.timeout.connect(self._run_audition)

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.setSpacing(spacing.sm)

        root.addWidget(self._build_header())
        body = QHBoxLayout()
        body.setSpacing(spacing.sm)
        body.addWidget(self._build_track_panel(), 0)
        body.addWidget(self._build_chain_panel(), 1)
        root.addLayout(body, 1)
        root.addWidget(self._build_add_panel())

        self.rebuild()

    # -- 构建 ------------------------------------------------------------

    def _build_header(self) -> QWidget:
        bar = GlassPanel(self, material=theme.black(0.20), radius=radii.control)
        bar.setFixedHeight(44)
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(12, 6, 12, 6)
        lay.setSpacing(spacing.sm)

        title = make_label("效果器链", type_scale.title3, labels.label, bar)
        lay.addWidget(title)
        sub = make_label("增删 · 排序 · 调参 · 旁通 —— 顺序就是信号经过的顺序",
                         type_scale.caption1, labels.tertiary, bar)
        lay.addWidget(sub)
        lay.addStretch(1)

        self._pos_label = make_label("0.00s", type_scale.mono, labels.quaternary, bar)
        lay.addWidget(self._pos_label)
        lay.addSpacing(spacing.xs)

        # 自动试听开关：默认开（这是本页的核心体验），但必须能关 ——
        # 深夜调参的人会需要一个安静的工作方式
        self.chip_auto = GlassChip("自动试听", bar, accent=accents.green,
                                   checkable=True, selected=True)
        self.chip_auto.toggled.connect(self._on_auto_toggled)
        lay.addWidget(self.chip_auto)

        self.btn_audition = GlassImageButton("headphones", bar, size=28, icon_size=17,
                                             style="tinted", accent=accents.green,
                                             tooltip="现在渲染并试听这一段")
        self.btn_audition.clicked.connect(lambda: self.audition_now())
        lay.addWidget(self.btn_audition)
        self._header = bar
        return bar

    def _build_track_panel(self) -> QWidget:
        panel = GlassPanel(self, material=theme.black(0.16), radius=radii.panel, liquid=True)
        panel.setFixedWidth(TRACK_PANEL_W)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(10, 10, 10, 10)
        lay.setSpacing(spacing.xs)
        lay.addWidget(make_label("轨道", type_scale.caption2, labels.quaternary, panel))

        self._track_box = QWidget()
        self._track_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._track_layout = QVBoxLayout(self._track_box)
        self._track_layout.setContentsMargins(0, 0, 4, 0)
        self._track_layout.setSpacing(spacing.xs)
        self._track_layout.addStretch(1)
        lay.addWidget(_scroll_host(self._track_box, panel), 1)
        self._track_panel = panel
        return panel

    def _build_chain_panel(self) -> QWidget:
        panel = GlassPanel(self, material=theme.black(0.16), radius=radii.panel, liquid=True)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(spacing.xs)

        head = QWidget(panel)
        head.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(0, 0, 0, 0)
        hl.setSpacing(spacing.xs)
        self._chain_title = make_label("—", type_scale.title3, labels.label, head)
        hl.addWidget(self._chain_title)
        self._chain_sub = make_label("", type_scale.caption1, labels.tertiary, head)
        hl.addWidget(self._chain_sub)
        hl.addStretch(1)
        lay.addWidget(head)

        self._chain_box = QWidget()
        self._chain_box.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._chain_layout = QVBoxLayout(self._chain_box)
        self._chain_layout.setContentsMargins(0, 0, 4, 0)
        self._chain_layout.setSpacing(spacing.xs)
        self._chain_layout.addStretch(1)
        lay.addWidget(_scroll_host(self._chain_box, panel), 1)
        self._chain_panel = panel
        return panel

    def _build_add_panel(self) -> QWidget:
        panel = GlassPanel(self, material=theme.black(0.18), radius=radii.panel)
        panel.setFixedHeight(ADD_PANEL_H)
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(12, 8, 12, 8)
        lay.setSpacing(spacing.xs)

        head = QHBoxLayout()
        head.setSpacing(spacing.sm)
        head.addWidget(make_label("添加效果", type_scale.caption1, labels.secondary, panel))
        head.addWidget(make_label("（加在链尾，之后可用 ↑↓ 调顺序）",
                                  type_scale.caption2, labels.quaternary, panel))
        head.addStretch(1)
        self._status_label = make_label("", type_scale.caption1, labels.tertiary, panel)
        head.addWidget(self._status_label)
        lay.addLayout(head)

        tiles = QHBoxLayout()
        tiles.setSpacing(spacing.xs)
        for kind in EFFECT_ORDER:
            tile = AddEffectTile(kind, panel)
            tile.clicked.connect(self.add_effect)
            self._add_tiles[kind] = tile
            tiles.addWidget(tile, 1)
        # 不加尾部弹簧：六格平分整幅宽度，右下角才不会留一块无意义的空档
        lay.addLayout(tiles)
        self._add_panel = panel
        return panel

    # -- 与控制器对接 ----------------------------------------------------

    def bind(self, controller) -> None:
        """接上 AppController：撤销栈、状态栏提示，以及 ``projectChanged``。

        为什么页面**自己**接 ``projectChanged``
        --------------------------------------
        撤销走的是 ``controller._restore`` → ``project.__dict__.update(...)``：
        整棵轨道树被换成**新对象**，而 ``project`` 实例本身没换。所以本页
        :meth:`current_track` 按 id 重新解析是安全的，但已经建好的 ``EffectRow``
        还攥着旧 ``Effect``。宿主只要漏接一次 ``projectChanged``（契约里写的是
        "编排者会接"），界面就会留下指向已被丢弃的 Effect 的旧行 ——
        实测：撤销后模型里 0 个效果、界面仍挂着 1 行，用户拖那一行的滑块
        只会写进没人再读的对象，而且界面上**看不出任何异常**。

        接线前提（编排者需要知道）：本页现在**自接**这一根线，所以宿主可以不接；
        若宿主仍然照旧接一次 ``connect(page.rebuild)``，只是多重绘一遍（本页的
        处理函数也会 rebuild），无副作用，不会互相打架。换控制器时会先摘掉旧线，
        避免上一个控制器继续驱动本页。
        """
        self._controller = controller
        if controller is None:
            return
        changed = getattr(controller, "projectChanged", None)
        if changed is None:                    # 假控制器 / 精简测试替身：不接也不报错
            return
        old = self._bound_signal
        if old is not None:
            try:
                old.disconnect(self._on_project_changed)
            except (RuntimeError, TypeError):
                pass                           # 没接上 / 接收者已销毁：目的已达成
        self._bound_signal = changed
        changed.connect(self._on_project_changed)

    def _on_project_changed(self) -> None:
        """工程**结构**变了（撤销 / 重做 / 打开新工程）。"""
        ctrl = self._controller
        new_project = getattr(ctrl, "project", None)
        if new_project is not None and new_project is not self.project:
            # 整个工程对象被换掉（打开新工程）：必须换引用再重建，否则本页读的
            # 是一份已经没人再写的旧对象
            self.set_project(new_project)
        else:
            self.rebuild()

    # -- 异常留痕 --------------------------------------------------------

    @property
    def error_log(self) -> tuple[str, ...]:
        """所有被兜住的异常，按发生顺序。空元组 = 一切正常。"""
        return tuple(self._error_log)

    @property
    def last_error(self) -> str:
        """最近一次被兜住的异常；空串表示没有。诊断与测试都用它。"""
        return self._error_log[-1] if self._error_log else ""

    def _record_error(self, message: str) -> str:
        """记一条被兜住的异常。留痕的唯一入口，免得各处自己拼字符串。"""
        text = str(message)
        self._error_log.append(text)
        return text

    def _note_error(self, message: str) -> None:
        """记下来**并且**让用户看见。

        为什么不能只 ``except Exception: pass``：撤销点压不进栈意味着这一步以后
        撤不回来，属于**静默降级** —— 用户按 Ctrl+Z 发现少撤一步，却没有任何线索
        指向这里，日志里也查不到。所以既要留痕，也要在状态区说一句。

        这里直接写标签而**不经过** :meth:`_status`：出错时控制器往往正是坏的那
        一环，而 ``_status`` 的 except 又要往这里写记录，绕回去就是同一层递归。
        """
        text = self._record_error(message)
        self._status_label.setText(text)
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.statusMessage.emit(text, 4000)
            except Exception as exc:
                # 状态栏也坏了 —— 不回调 _note_error（递归），再记一条即可；
                # 提示本身没丢，标签上已经写着同一句话
                self._record_error(f"状态栏消息发送失败（{type(exc).__name__}）：{exc}")

    def _commit(self, label: str) -> None:
        """改动**之前**压撤销点。"""
        ctrl = self._controller
        if ctrl is None:
            return
        try:
            ctrl.push_undo(label)
        except Exception as exc:
            self._note_error(f"撤销点压入失败（{type(exc).__name__}）：{exc}")

    def _status(self, text: str, ms: int = 2500) -> None:
        self._status_label.setText(text)
        ctrl = self._controller
        if ctrl is not None:
            try:
                ctrl.statusMessage.emit(text, ms)
            except Exception as exc:
                # 状态栏不可用：标签上（上一行）已经写着这句话，提示没有丢，
                # 这里只补一条留痕
                self._record_error(f"状态栏消息发送失败（{type(exc).__name__}）：{exc}")

    # -- 数据 ------------------------------------------------------------

    def current_track(self) -> Track | None:
        """当前轨道。

        按 **id** 找而不是缓存对象引用：撤销走的是
        ``project.__dict__.update(...)``，整棵轨道树会被换成新对象，
        缓存下来的旧引用会静静地指向一份没人再读的数据。
        """
        track = self.project.track_by_id(self._track_id) if self._track_id else None
        if track is None:
            track = self.project.tracks[0] if self.project.tracks else None
            self._track_id = track.id if track is not None else None
        return track

    @property
    def rows(self) -> list[EffectRow]:
        return list(self._rows)

    @property
    def audition_pending(self) -> bool:
        """防抖计时器是否还在等（测试与状态提示都用它）。"""
        return self._audition_timer.isActive()

    # -- 重建 ------------------------------------------------------------

    def set_project(self, project: Project) -> None:
        self.project = project
        self._audition_timer.stop()
        self._track_id = project.tracks[0].id if project.tracks else None
        self._expanded_effect = None
        self.rebuild()

    def rebuild(self) -> None:
        """工程**结构**变了（换轨 / 增删轨道 / 撤销）—— 两侧都重建。"""
        self._rebuild_tracks()
        self._rebuild_chain()

    def refresh_notes(self) -> None:
        """只是参数变了（外部改动 / 撤销后的轻量刷新）—— 不重建控件。"""
        for row in self._rows:
            row.reload()
        for pick in self._picks:
            pick.update()

    def set_position(self, seconds: float, playing: bool = False) -> None:
        """播放位置。

        本页没有时间轴，因此**不做自动跟随**；这个读数只是给用户一个参照 ——
        试听放的是整曲开头那一段，用户得知道主播放头现在在哪儿。
        """
        self._position = float(seconds)
        self._playing = bool(playing)
        self._pos_label.setText(f"{self._position:6.2f}s" + (" · 播放中" if playing else ""))

    def _rebuild_tracks(self) -> None:
        _clear_layout(self._track_layout)
        self._picks = []
        current = self.current_track()
        current_id = current.id if current is not None else ""
        for i, track in enumerate(self.project.tracks):
            pick = TrackPick(track, i, self._track_box)
            pick.clicked.connect(self.select_track)
            pick.set_selected(track.id == current_id)
            self._track_layout.insertWidget(i, pick)
            self._picks.append(pick)
        self._track_layout.addStretch(1)

        has_track = bool(self.project.tracks)
        for tile in self._add_tiles.values():
            tile.setEnabled(has_track)
            tile.update()

    def _rebuild_chain(self) -> None:
        _clear_layout(self._chain_layout)
        self._rows = []
        track = self.current_track()

        if track is None:
            self._chain_title.setText("没有轨道")
            self._chain_sub.setText("先在总览页加一条轨道")
            self._chain_layout.addWidget(self._hint_card(
                "工程里还没有任何轨道 —— 效果链是挂在轨道上的，先建一条轨道。"))
            self._chain_layout.addStretch(1)
            return

        self._chain_title.setText(track.name)
        n = len(track.effects)
        used = sum(1 for e in track.effects if e.enabled)
        self._chain_sub.setText(
            "效果链为空" if n == 0 else
            (f"{n} 个效果依次经过" if used == n else f"{n} 个效果 · {used} 个启用"))

        # 展开的效果可能已经在撤销/删除中消失了 —— 引用比对必须按身份来
        if self._expanded_effect is not None and not any(
                e is self._expanded_effect for e in track.effects):
            self._expanded_effect = None

        if n == 0:
            self._chain_layout.addWidget(self._hint_card(
                f"「{track.name}」还没有效果 —— 点下面的六个格子加上一个。"))

        for i, effect in enumerate(track.effects):
            row = EffectRow(effect, i, n, self._chain_box)
            row.moveRequested.connect(self.move_effect)
            row.removeRequested.connect(self.remove_effect)
            row.toggleRequested.connect(self.toggle_effect)
            row.expandRequested.connect(self._on_expand)
            row.paramChanged.connect(self._on_param_changed)
            row.paramEditStarted.connect(self._on_param_edit_started)
            self._chain_layout.insertWidget(i, row)
            # 先插进布局再决定展开：控件只有进了布局，显隐才会参与高度计算
            row.set_expanded(effect is self._expanded_effect)
            self._rows.append(row)
        self._chain_layout.addStretch(1)

    def _hint_card(self, text: str) -> QWidget:
        card = GlassCard(self._chain_box, radius=radii.control, material=theme.black(0.18))
        card.setFixedHeight(58)
        lay = QHBoxLayout(card)
        lay.setContentsMargins(14, 0, 14, 0)
        lay.addWidget(make_label(text, type_scale.callout, labels.tertiary, card))
        lay.addStretch(1)
        return card

    # -- 编辑动作 --------------------------------------------------------

    def select_track(self, track: Track) -> None:
        if track is None:
            return
        self._track_id = track.id
        self._expanded_effect = None
        self._rebuild_tracks()
        self._rebuild_chain()

    def add_effect(self, kind: str) -> Effect | None:
        """把一个效果加到链尾（默认参数来自 :meth:`Effect.reverb` 这类工厂）。"""
        track = self.current_track()
        factory = EFFECT_FACTORY.get(kind)
        if track is None:
            self._status("先选一条轨道")
            return None
        if factory is None:
            self._status(f"未知效果类型：{kind}")
            return None

        self._commit(f"添加{effect_label(kind)}")
        effect = factory()
        track.effects.append(effect)
        # 新加的立刻展开：刚加完正是要调参数的时候，省一次点击
        self._expanded_effect = effect
        self._rebuild_tracks()
        self._rebuild_chain()
        self._after_change(f"已添加「{effect_label(kind)}」到「{track.name}」的链尾")
        return effect

    def _find_index(self, track: Track, effect: Effect) -> int:
        """按**对象身份**找下标。

        不能用 ``list.index`` / ``in``：``Effect`` 是 dataclass，``__eq__`` 是逐字段比较，
        两个参数相同的混响会被判成"相等"，删除就会删错那一个。
        """
        for i, e in enumerate(track.effects):
            if e is effect:
                return i
        return -1

    def remove_effect(self, effect: Effect) -> bool:
        track = self.current_track()
        if track is None:
            return False
        idx = self._find_index(track, effect)
        if idx < 0:
            return False
        self._commit(f"删除{effect_label(effect.kind)}")
        del track.effects[idx]
        if self._expanded_effect is effect:
            self._expanded_effect = None
        self._rebuild_tracks()
        self._rebuild_chain()
        self._after_change(f"已删除「{effect_label(effect.kind)}」")
        return True

    def move_effect(self, effect: Effect, delta: int) -> bool:
        """上移 / 下移。

        顺序决定信号经过的先后 —— 失真再混响和混响再失真，是两种完全不同的音色
        （前者把混响尾巴也弄脏，后者只是给干声加空间）。所以这是一个一等操作，
        不是"整理界面"。
        """
        track = self.current_track()
        if track is None or delta == 0:
            return False
        idx = self._find_index(track, effect)
        if idx < 0:
            return False
        target = idx + int(delta)
        if not (0 <= target < len(track.effects)):
            self._status("已经到头了")
            return False

        self._commit(f"调整{effect_label(effect.kind)}顺序")
        fx_list = track.effects
        fx_list[idx], fx_list[target] = fx_list[target], fx_list[idx]
        self._rebuild_tracks()
        self._rebuild_chain()
        self._after_change(
            f"「{effect_label(effect.kind)}」移到第 {target + 1} 位"
            f"（共 {len(fx_list)} 个）")
        return True

    def toggle_effect(self, effect: Effect) -> bool:
        track = self.current_track()
        if track is None or self._find_index(track, effect) < 0:
            return False
        was_on = bool(effect.enabled)
        self._commit("旁通效果" if was_on else "启用效果")
        effect.enabled = not was_on
        self._rebuild_tracks()
        self._rebuild_chain()
        self._after_change(
            f"「{effect_label(effect.kind)}」已{'旁通' if was_on else '启用'}")
        return True

    def _on_expand(self, effect: Effect) -> None:
        """手风琴：同时只展开一个，列表不会被撑成一条长龙。"""
        self._expanded_effect = None if self._expanded_effect is effect else effect
        for row in self._rows:
            row.set_expanded(row.effect is self._expanded_effect)

    def _on_param_edit_started(self, effect: Effect, key: str) -> None:
        self._commit(f"调整{effect_label(effect.kind)}·{key}")

    def _on_param_changed(self, effect: Effect, key: str) -> None:
        spec = next((s for s in specs_of(effect.kind) if s.key == key), None)
        # 走 read_param 而不是直接读字典：状态栏上写的数必须和界面上读到的数一致
        value = read_param(effect, spec) if spec is not None else effect.params.get(key)
        text = format_value(spec, value) if spec is not None else str(value)
        self._status(f"{effect_label(effect.kind)} · "
                     f"{spec.label if spec is not None else key} = {text}")
        for row in self._rows:                 # 徽章（滤波类型/失真模式）跟着变
            if row.effect is effect:
                row._sync_state()
        self._after_change(None)

    def _after_change(self, message: str | None) -> None:
        if message:
            self._status(message)
        self._schedule_audition()
        self.edited.emit()

    # -- 试听 ------------------------------------------------------------

    def _on_auto_toggled(self, value: bool) -> None:
        self._auto_audition = bool(value)
        if not self._auto_audition:
            self._audition_timer.stop()
        self._status("自动试听已开启" if self._auto_audition else "自动试听已关闭（可点耳机键手动试听）")

    def _schedule_audition(self) -> None:
        """改动后安排一次试听。计时器每次 start() 都会重启 —— 这就是防抖。"""
        if not self._auto_audition:
            return
        self._audition_timer.start(AUDITION_DEBOUNCE_MS)

    def _run_audition(self) -> bool:
        """真的渲染并播放当前轨道。计时器超时与手动按钮都走这里。"""
        self._audition_timer.stop()
        track = self.current_track()
        if track is None:
            return False
        ok = play_audition_clip(self.project, track)
        if not ok:
            self._status("这段没有声音可试听", 2000)
        return ok

    def audition_now(self) -> bool:
        return self._run_audition()

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        """整页压一层暗底。

        玻璃控件是半透明的，背景不压暗的话多层面板叠在一起会互相"洗白"，
        读数文字（尤其 kHz 那种小字）会糊在背景上。
        """
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor(12, 13, 18, 150))
        painter.end()


__all__ = [
    "EffectsPage", "EffectRow", "ParamEditor", "TrackPick", "AddEffectTile",
    "ParamSpec", "EFFECT_PARAMS", "EFFECT_ORDER", "EFFECT_FACTORY",
    "log_to_pos", "log_to_value", "format_value", "snap_value",
    "normalize_value", "read_param", "specs_of", "effect_label", "effect_icon",
    "effect_accent", "badge_detail", "BADGE_ENUM_KEY",
    "default_params", "render_audition_clip", "play_audition_clip",
    "AUDITION_DEBOUNCE_MS", "AUDITION_SECONDS", "AUDITION_TAIL",
]
