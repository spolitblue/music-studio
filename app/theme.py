"""iOS 设计令牌 —— 全应用视觉的唯一真相来源。

设计依据：工作区 `apple-design-SKILL.md`（WWDC《设计流畅界面》/《UI 排版细节》整理稿）。
任何硬编码的颜色、字号、圆角、时长都应回到本文件，不要在控件里就地写死。

三条被本文件强制执行的规则
--------------------------
1. **材质重量传达层级**：材质只分三层 —— 窗口(window) / 面板(panel) / 控件(control)。
   每层不透明度差 >= 0.10，杜绝"浅色半透明叠浅色半透明"导致的可读性塌陷。
2. **字距随尺寸变化**：大字号负字距，小字号正字距；行高与字号成反比。
3. **亮顶边模拟光线照射材质**：每个玻璃面顶部一条 rgba(255,255,255,.40) 高光边。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPainterPath

# --------------------------------------------------------------------------
# 颜色工具
# --------------------------------------------------------------------------


def rgba(r: int, g: int, b: int, a: float = 1.0) -> QColor:
    """按 0..1 的 alpha 构造 QColor（比 0..255 更贴近设计稿写法）。"""
    c = QColor(r, g, b)
    c.setAlphaF(max(0.0, min(1.0, a)))
    return c


def white(a: float) -> QColor:
    return rgba(255, 255, 255, a)


def black(a: float) -> QColor:
    return rgba(0, 0, 0, a)


# --------------------------------------------------------------------------
# Twemoji 调色板 —— 全应用颜色的唯一来源
# --------------------------------------------------------------------------

#: 直接抄自 `assets/icons/*.svg` 里出现频率最高的填充色。
#: 这样做的好处是**图标与界面永远同色系** —— 图标自带硬编码的 fill，
#: 如果界面另搞一套配色，彩色图标就会像外来客。现在它们是一家人。
#:
#: 中性色是一条略带蓝调的灰阶（#F5F8FA → #292F33）——Twemoji 把它用在
#: 按键、器具的"金属/塑料"部位。用在玻璃上恰好，冷调不会显脏。
TWEMOJI: dict[str, str] = {
    # 中性（冷调灰阶）
    "neutral_50": "#F5F8FA",
    "neutral_100": "#E1E8ED",
    "neutral_200": "#CCD6DD",
    "neutral_300": "#AAB8C2",
    "neutral_400": "#99AAB5",
    "neutral_500": "#8899A6",
    "neutral_600": "#66757F",
    "neutral_700": "#53626C",
    "neutral_800": "#31373D",
    "neutral_900": "#292F33",
    # 蓝（Twemoji 的"按键蓝"，走带图标就是它）
    "blue": "#3B88C3",
    "blue_bright": "#4292E0",
    "blue_light": "#55ACEE",
    "blue_pale": "#BBDDF5",
    # 黄 / 橙
    "yellow": "#FFCC4D",
    "yellow_deep": "#FDCB58",
    "yellow_pale": "#FEE7B8",
    "amber": "#FFAC33",
    "orange": "#F4900C",
    "orange_deep": "#EE7C0E",
    # 红 / 粉
    "red": "#DD2E44",
    "red_deep": "#BE1931",
    "red_dark": "#A0041E",
    "pink": "#EA596E",
    "pink_pale": "#F4ABBA",
    # 绿
    "green": "#77B255",
    "green_deep": "#5C913B",
    "green_dark": "#3F7123",
    "green_pale": "#C6E5B3",
    # 紫
    "purple": "#9266CC",
    "purple_light": "#AA8DD8",
    "purple_deep": "#60379A",
    "purple_dark": "#553788",
    # 棕 / 肤
    "brown": "#C1694F",
    "brown_light": "#D99E82",
}


def tw(name: str, alpha: float = 1.0) -> QColor:
    """按名字取 Twemoji 调色板颜色。"""
    hexv = TWEMOJI.get(name, "#FFFFFF")
    c = QColor(hexv)
    if alpha < 1.0:
        c.setAlphaF(alpha)
    return c


# --------------------------------------------------------------------------
# 材质 —— 严格三层
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Materials:
    """三层玻璃材质。alpha 值经过排序，保证层与层之间始终有可见的明度台阶。

    窗口层由 Windows DWM 亚克力在系统侧完成（真实背景虚化）；
    本结构描述的是 Qt 内部在其之上叠加的自绘层。
    """

    # 第一层：窗口底色。极淡 —— 让 DWM 亚克力的桌面虚化透上来。
    window_tint: QColor = field(default_factory=lambda: rgba(16, 16, 20, 0.28))
    # 环境光晕：iOS 式的低饱和色斑，给纯色玻璃一点"活力"
    ambient_blooms: tuple[tuple[float, float, float, QColor], ...] = field(
        default_factory=lambda: (
            # (cx, cy, radius, color) —— 归一化坐标；取 Twemoji 的蓝/紫/粉
            (0.10, -0.06, 0.62, rgba(85, 172, 238, 0.20)),   # blue_light
            (0.94, 0.12, 0.52, rgba(146, 102, 204, 0.16)),   # purple
            (0.34, 1.04, 0.58, rgba(234, 89, 110, 0.09)),    # pink
        )
    )

    # 第二层：结构面板（侧边栏、卡片、弹层）。比控件层更厚重。
    panel: QColor = field(default_factory=lambda: white(0.055))
    panel_strong: QColor = field(default_factory=lambda: white(0.085))
    panel_sidebar: QColor = field(default_factory=lambda: white(0.042))

    # 第三层：控件（按钮、芯片、输入）。最轻，突出可交互性。
    control: QColor = field(default_factory=lambda: white(0.10))
    control_hover: QColor = field(default_factory=lambda: white(0.155))
    control_pressed: QColor = field(default_factory=lambda: white(0.075))
    control_selected: QColor = field(default_factory=lambda: white(0.20))

    # 线
    hairline: QColor = field(default_factory=lambda: white(0.09))
    hairline_strong: QColor = field(default_factory=lambda: white(0.15))
    # 顶边高光 —— 规则 3
    specular: QColor = field(default_factory=lambda: white(0.40))
    specular_soft: QColor = field(default_factory=lambda: white(0.18))


# --------------------------------------------------------------------------
# 标签（Vibrancy）—— 玻璃面上禁止纯灰文本
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Labels:
    """活力效果：半透明材质上的文字靠"高对比 + 稍粗字重 + 微正字距"保证可读，
    而不是靠加深灰色 —— 灰色叠在模糊背景上会糊成一团。"""

    label: QColor = field(default_factory=lambda: white(0.94))
    secondary: QColor = field(default_factory=lambda: white(0.66))
    tertiary: QColor = field(default_factory=lambda: white(0.42))
    quaternary: QColor = field(default_factory=lambda: white(0.26))
    on_accent: QColor = field(default_factory=lambda: rgba(255, 255, 255, 0.98))


# --------------------------------------------------------------------------
# 强调色 —— iOS 系统色（暗色模式取值）
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Accents:
    """强调色 —— 全部取自 Twemoji 调色板（见 :data:`TWEMOJI`）。

    纪律：**颜色只用在有意义的地方**（轨道身份、播放头、选中态、状态提示），
    界面底色一律中性。彩色图标本身已经很热闹了，再给界面加色只会互相打架。
    """

    blue: QColor = field(default_factory=lambda: tw("blue"))
    blue_light: QColor = field(default_factory=lambda: tw("blue_light"))
    green: QColor = field(default_factory=lambda: tw("green"))
    indigo: QColor = field(default_factory=lambda: tw("purple_dark"))
    orange: QColor = field(default_factory=lambda: tw("orange"))
    pink: QColor = field(default_factory=lambda: tw("pink"))
    purple: QColor = field(default_factory=lambda: tw("purple"))
    red: QColor = field(default_factory=lambda: tw("red"))
    teal: QColor = field(default_factory=lambda: tw("blue_pale"))
    yellow: QColor = field(default_factory=lambda: tw("yellow"))
    amber: QColor = field(default_factory=lambda: tw("amber"))
    brown: QColor = field(default_factory=lambda: tw("brown"))

    def track_palette(self) -> list[QColor]:
        """轨道配色顺序 —— 相邻轨道色相拉开，避免混淆。

        刻意把 Twemoji 里区分度最高的八个色排成"冷-暖-冷-暖"交替，
        这样即便轨道在列表里挨着，也能一眼分开。
        """
        return [
            self.blue, self.orange, self.green, self.pink,
            self.purple, self.yellow, self.teal, self.brown,
        ]


# --------------------------------------------------------------------------
# 排版 —— 字距随尺寸变化，行高与字号成反比
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TextStyle:
    """一个排版样式。

    tracking 单位为 em（负=收紧）。渲染时换算成像素绝对字距：
    大标题收紧到 -0.022em，小字放宽到 +0.015em。
    """

    size: float          # pt
    weight: int          # QFont.Weight
    tracking: float = 0.0  # em
    line_height: float = 1.35  # 倍数

    def font(self, fallback_point: float | None = None) -> QFont:
        f = resolve_font()
        f.setPointSizeF(fallback_point or self.size)
        f.setWeight(QFont.Weight(self.weight))
        if self.tracking:
            # AbsoluteSpacing 单位是像素，而 tracking 是 em → 换算
            f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, self.tracking * self.size * 1.333)
        else:
            f.setLetterSpacing(QFont.SpacingType.PercentageSpacing, 100.0)
        return f

    @property
    def px_line_height(self) -> float:
        return self.size * 1.333 * self.line_height


@dataclass(frozen=True)
class Typography:
    """iOS 字号阶梯。注意 tracking 一栏的符号：字号越大越负。"""

    large_title: TextStyle = field(default_factory=lambda: TextStyle(26, 700, -0.022, 1.14))
    title1: TextStyle = field(default_factory=lambda: TextStyle(21, 700, -0.020, 1.18))
    title2: TextStyle = field(default_factory=lambda: TextStyle(17, 600, -0.016, 1.22))
    title3: TextStyle = field(default_factory=lambda: TextStyle(15, 600, -0.012, 1.26))
    headline: TextStyle = field(default_factory=lambda: TextStyle(13, 600, -0.008, 1.30))
    body: TextStyle = field(default_factory=lambda: TextStyle(12.5, 400, 0.0, 1.40))
    callout: TextStyle = field(default_factory=lambda: TextStyle(12, 400, 0.004, 1.40))
    subhead: TextStyle = field(default_factory=lambda: TextStyle(11.5, 400, 0.008, 1.42))
    footnote: TextStyle = field(default_factory=lambda: TextStyle(10.5, 400, 0.012, 1.42))
    caption1: TextStyle = field(default_factory=lambda: TextStyle(10, 500, 0.014, 1.40))
    caption2: TextStyle = field(default_factory=lambda: TextStyle(9, 600, 0.018, 1.38))
    mono: TextStyle = field(default_factory=lambda: TextStyle(11, 500, 0.010, 1.35))


# --------------------------------------------------------------------------
# 字体栈
# --------------------------------------------------------------------------

#: Segoe UI Variable 是 Windows 上的光学尺寸字体，行为最接近 SF Pro；
#: 中文回落到微软雅黑 UI。不内置任何字体文件（避免授权问题）。
_FONT_STACK = (
    "Segoe UI Variable Display",
    "Segoe UI Variable Text",
    "Segoe UI",
    "Microsoft YaHei UI",
    "Microsoft YaHei",
    "PingFang SC",
    "Noto Sans SC",
    "Arial",
)

_MONO_STACK = ("Cascadia Mono", "Consolas", "JetBrains Mono", "SF Mono", "Courier New")

_resolved_family: str | None = None
_resolved_mono: str | None = None


def _pick_family(stack: tuple[str, ...]) -> str:
    try:
        from PySide6.QtGui import QFontDatabase

        available = set(QFontDatabase.families())
    except Exception:
        return stack[0]
    for fam in stack:
        if fam in available:
            return fam
    return stack[0]


def resolve_font() -> QFont:
    global _resolved_family
    if _resolved_family is None:
        _resolved_family = _pick_family(_FONT_STACK)
    f = QFont(_resolved_family)
    f.setHintingPreference(QFont.HintingPreference.PreferFullHinting)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def resolve_mono_font() -> QFont:
    global _resolved_mono
    if _resolved_mono is None:
        _resolved_mono = _pick_family(_MONO_STACK)
    f = QFont(_resolved_mono)
    f.setStyleStrategy(QFont.StyleStrategy.PreferAntialias)
    return f


def font_family() -> str:
    if _resolved_family is None:
        resolve_font()
    return _resolved_family or _FONT_STACK[0]


# --------------------------------------------------------------------------
# 圆角 / 间距 / 时间
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Outline:
    """玻璃的"厚边"—— 借鉴 LumaGlass 的粗亮轮廓。

    这是液态玻璃风格里**最容易迁移、收益也最大**的一个特征：一条 1.5~2px 的亮边
    既让玻璃有了厚度，又顺手把"哪块能点"讲清楚了。相比 iOS 那种几乎看不见的发丝线，
    它在玻璃材质上的可交互性提示强得多。

    三层叠加才有立体感：
    外侧一条暗线（把玻璃从背景里"切"出来）→ 内侧一条亮环（受光的玻璃边）
    → 再往外一圈极淡的泛光（玻璃边缘聚光）。
    """

    width: float = 1.6
    width_strong: float = 2.2
    light_top: QColor = field(default_factory=lambda: white(0.48))
    light_bottom: QColor = field(default_factory=lambda: white(0.15))
    dark_outer: QColor = field(default_factory=lambda: black(0.36))
    bloom: QColor = field(default_factory=lambda: white(0.09))
    bloom_radius: float = 5.0


@dataclass(frozen=True)
class GlassFinish:
    """玻璃的**表面处理**。

    磨砂和光面不是"调参"关系，而是两种不同的材质：

    * **磨砂**：模糊强、凝霜重、高光漫射、表面有微观噪点。像浴室的毛玻璃，
      看不清背后但很柔和。
    * **光面**：几乎不模糊、凝霜极淡、有一道**锐利的镜面反光**、表面光滑无颗粒。
      像抛光的亚克力或水晶，背景清晰地透出来，边缘有强烈的折射与聚光。

    两者共用同一套折射/轮廓管线，只是参数与高光画法不同 —— 所以做成可切换档位，
    而不是把某一套写死。
    """

    name: str = "光面"
    blur: float = 3.5           # 模糊半径（光面几乎不糊）
    frost: float = 0.085        # 凝霜（中性玻璃靠它托住文字，不靠染色）
    saturate: float = 0.62      # **去饱和** —— 见下方说明
    rim_gain: float = 0.17      # 边缘聚光（光面边缘更亮更锐）
    bevel: float = 2.6          # 折射集中度（越大越像硬边透镜）
    grain: float = 0.16         # 噪点倍率（光面几乎无颗粒）
    specular: float = 1.25      # 顶部高光倍率
    inner_gradient: float = 1.15  # 内壁渐变（光面的明暗对比更强）
    sheen: float = 0.75         # 斜向镜面反光条强度 —— 光面的灵魂
    waviness: float = 15.0      # 表面不平整度（像素）
    interior_alpha: float = 0.14  # 面板内部不透明度（越小越通透）

    frost_color: tuple[int, int, int] = (206, 206, 209)
    """凝霜色 —— **中性灰**，不偏暖也不偏冷。

    实时档（纯绘制）需要它把面板抬到比背景更亮，否则去掉液态位图后面板会
    比原来更透，白字反而更糊。颜色必须与
    :data:`app.widgets.liquid.LiquidParams.frost_color` 的默认值一致，
    两个档位才看得出是同一块材质。
    """

    #: 关于 ``saturate``：它应取 **< 1**，不要取 > 1。
    #:
    #: 直觉上"玻璃会强化背后的颜色"，所以一开始取了 1.28。但实际效果是灾难：
    #: 壁纸的暖色被放大后，整块界面泛黄发浊，一眼看过去就是"灰蒙蒙"。
    #: 真正的玻璃是**无色**的 —— 它不添色，反而略微消色。取 < 1 之后，
    #: 桌面透进来但被压成中性灰，玻璃才干净。
    #:
    #: 想把颜色还回来（比如配合彩色壁纸）就把它调高，但默认必须是中性的。


#: 光面：抛光玻璃。**无色**、背景透亮、表面起伏、一道斜向镜面反光。
#:
#: 可读性靠 ``waviness``（表面不平整）而不是靠压暗幕来保证 ——
#: 参见 :class:`app.widgets.liquid.LiquidParams` 里 ``waviness`` 的说明。
GLOSS = GlassFinish(
    name="光面", blur=3.5, frost=0.085, saturate=0.62, rim_gain=0.17,
    bevel=2.6, grain=0.16, specular=1.25, inner_gradient=1.15, sheen=0.75,
    waviness=15.0, interior_alpha=0.14,
)

#: 磨砂：浴室毛玻璃。模糊强、凝霜重、高光漫射、表面有颗粒。
#: 磨砂光靠"模糊"就已经解决了可读性，不需要表面起伏。
FROSTED = GlassFinish(
    name="磨砂", blur=16.0, frost=0.17, saturate=1.45, rim_gain=0.10,
    bevel=2.1, grain=1.0, specular=1.0, inner_gradient=1.0, sheen=0.0,
    waviness=0.0, interior_alpha=0.30,
)


@dataclass(frozen=True)
class GlassQuality:
    """玻璃走哪条渲染路径 —— 全应用的总开关。

    两档**都是真的液态玻璃**（同一套 SDF 折射引擎），差别只在分辨率与刷新策略：

    ``realtime``（默认）
        折射跑在 1/``glass_realtime.refract_step`` 分辨率上，并按
        ``refract_interval_ms`` 限流重算；重绘仍然逐帧。
        液态玻璃本来就是糊的，降采样与几十毫秒的滞后都看不出来，
        而像素量按 step² 掉、拖动时每帧只剩一次 blit。
        这是参考实现（InfiniteGUI 的游戏内 HUD）的真正教训：
        它没有把折射删掉，它是**在片元着色器里把折射做得足够便宜**。

    ``quality``
        全分辨率 SDF 折射：边缘折射 + 表面起伏（waviness）。
        每块面板 12~30ms，只适合截图导出、离线预览这类不在乎帧率的场合。

    **曾经踩过的坑（记在这里免得重犯）**：第一版实时档对 A 类面板直接
    `return None` —— 不折射、不算任何东西，只画一圈高光线。
    性能数字是好看了（液态合成 0 次），但**产品层面的效果被删掉了**：
    面板变成半透明色块，液态玻璃没了。那不是"实时"，那是"移除"。
    """

    mode: str = "realtime"

    @property
    def realtime(self) -> bool:
        return self.mode != "quality"

    def set_mode(self, mode: str) -> None:
        """切换档位（全局生效，调用方负责 ``update()``）。"""
        object.__setattr__(self, "mode", mode)


@dataclass(frozen=True)
class GlassRealtime:
    """**实时档**专用的外观参数（纯 QPainter，不含任何 numpy）。

    实时档拿不到"被折射的背景像素"，所以只能**用绘制去暗示折射**：
    边框内侧一条沿光轴亮起的窄带。这抄的是参考实现的做法 ——
    它的高光严格绑在周长上（归一化内距 1.75px 内衰减到 0），
    并用 ``abs(dot(normalize(p), L))`` 让光轴的**两端同时亮**，
    而不是把背光面画成假的"内阴影"。

    参数含义
    --------
    ``lens_gain``
        透镜带峰值亮度。参考实现是 ``vec3(0.82,0.92,1.0) * 0.55``
        —— 冷白、峰值约 0.55；我们已有的 ``outline`` 更亮更粗，
        所以这里压低到 0.22，两层叠起来才不过曝。
    ``refract_step``
        折射底图的降采样倍率。**这是实时档能成立的关键**：
        液态玻璃本来就是糊的，降采样看不出来，而像素量按 step² 掉。
        取 3 时 9 块面板一次全量折射约十几毫秒（全分辨率是几百毫秒）。
    ``refract_interval_ms``
        折射底图的重算间隔（毫秒）。**重绘仍然逐帧**，受这条限制的只有
        "透过玻璃看到的那层背景"—— 它是模糊的，晚几十毫秒完全看不出来，
        而拖动时每帧的成本因此退化成一次 blit。
    """

    #: 是否在实时档额外画一圈"边缘透镜带"。
    #:
    #: **默认关闭** —— 它原本是"实时档不做折射"时的替代品。
    #: 现在实时档做的是**真折射**（见 `GlassSurface._ensure_realtime_refraction`），
    #: 这一圈画出来的高光是多余甚至打架的。保留它是因为小尺寸控件
    #: （低于 `_LENS_MIN_SIDE`）降采样折射几乎看不见，那时可以打开补一点边缘感。
    lens: float = 0.0
    lens_width: float = 2.0
    lens_gain: float = 0.22
    lens_bleed: float = 0.06
    lens_axis_deg: float = 315.0

    #: 凝霜补偿 0..1。实时档的折射走的是同一套 `liquid_fill`，
    #: 凝霜已经在里面掺过了（`LiquidParams.frost`），所以这里必须是 0；
    #: 非 0 会变成"掺两遍"，实测整窗均值会被抬亮 +6.8/255。
    frost: float = 0.0

    refract_step: int = 3
    refract_interval_ms: float = 48.0

    scrim_knee: float = 0.42
    scrim_slope: float = 0.45
    scrim_max: float = 0.30

    lum_step: int = 12
    """背景亮度探针的降采样倍率。

    自适应暗幕只需要知道"这块面板背后大概多亮"，用不着读全分辨率。
    12 倍降采样后整张桌面只有一百多像素宽，查表是微秒级的，
    而且**窗口一移动不用重算**（图存的是截屏坐标，平移只是换个下标）。
    """

    snapshot_step: int = 4
    """B 类面板模糊快照的降采样倍率（1/4 分辨率）。

    模糊本身就会掩盖低分辨率，所以这一步几乎看不出差别，
    而像素量直接降到 1/16。
    """


@dataclass(frozen=True)
class Radii:
    """连续曲率圆角。

    整体比 iOS 那套放大一档 —— 液态玻璃风格靠"圆"来表现材质张力，
    圆角太小会显得像普通圆角矩形，玻璃感立刻打折。
    """

    chip: float = 10.0
    control: float = 13.0
    card: float = 18.0
    panel: float = 24.0
    window: float = 15.0
    pill: float = 999.0


@dataclass(frozen=True)
class Spacing:
    xxs: float = 3.0
    xs: float = 6.0
    sm: float = 10.0
    md: float = 14.0
    lg: float = 20.0
    xl: float = 28.0


@dataclass(frozen=True)
class Blur:
    """模糊层级。更大的表面用更强的模糊（规则：大表面显得更厚重）。"""

    panel: float = 26.0
    control: float = 12.0
    veil: float = 44.0


@dataclass(frozen=True)
class Motion:
    """动效时长（毫秒）。弹簧参数见 motion.py。

    响应性规则：按下即反馈（press 极短），落位用弹簧。
    """

    press: int = 90
    hover: int = 140
    tap_release: int = 220
    panel_in: int = 380
    page_switch: int = 300


@dataclass(frozen=True)
class Elevation:
    """阴影 —— 上下文感知：玻璃面上用更重的阴影来分隔层次。"""

    def blur_alpha(self, level: int) -> tuple[float, float, float]:
        """返回 (模糊半径, y 偏移, alpha)。"""
        table = {
            0: (10.0, 2.0, 0.16),
            1: (22.0, 6.0, 0.24),
            2: (38.0, 12.0, 0.32),
            3: (60.0, 20.0, 0.40),
        }
        return table.get(level, table[1])


# --------------------------------------------------------------------------
# 几何：iOS 连续曲率圆角（squircle）
# --------------------------------------------------------------------------


@lru_cache(maxsize=512)
def _squircle_path_cached(
    x: float, y: float, w: float, h: float, r: float, n: float, samples: int
) -> QPainterPath:
    """超椭圆角的圆角矩形。

    每个角的曲线满足 ``|X/r|^n + |Y/r|^n = 1``（X、Y 为相对角心的偏移），
    采样式 ``X = r·cos(t)^(2/n)``、``Y = r·sin(t)^(2/n)``。

    * ``n = 2`` → 正圆角（普通圆角矩形）
    * ``n = 4`` → 更"饱满"的连续曲率角 —— iOS 图标/卡片那种手感
    * ``n → ∞`` → 直角

    顺时针走一圈：**每条直边都必须显式 lineTo**，否则角与角之间会被斜线切掉。
    """
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    path = QPainterPath()
    if r <= 0.5:
        path.addRect(QRectF(x, y, w, h))
        return path

    import math

    exp = 2.0 / n
    steps = max(2, samples)

    def emit(cx: float, cy: float, sx: float, sy: float, swap: bool) -> None:
        """沿一个角采样。

        ``sx``/``sy`` 是该角相对于角心的方向符号，``swap`` 决定用
        cos 驱动哪个轴（用来让四个角首尾相接）。
        """
        for i in range(1, steps + 1):
            t = (i / steps) * (math.pi / 2.0)
            c = math.cos(t) ** exp
            s = math.sin(t) ** exp
            if swap:
                path.lineTo(cx + sx * r * s, cy + sy * r * c)
            else:
                path.lineTo(cx + sx * r * c, cy + sy * r * s)

    # 从左上的直边起点开始，顺时针
    path.moveTo(x + r, y)

    # 上边 → 右上角（角心 x+w-r, y+r），从 (x+w-r, y) 到 (x+w, y+r)
    path.lineTo(x + w - r, y)
    emit(x + w - r, y + r, +1.0, -1.0, swap=True)

    # 右边 → 右下角（角心 x+w-r, y+h-r），从 (x+w, y+h-r) 到 (x+w-r, y+h)
    path.lineTo(x + w, y + h - r)
    emit(x + w - r, y + h - r, +1.0, +1.0, swap=False)

    # 下边 → 左下角（角心 x+r, y+h-r），从 (x+r, y+h) 到 (x, y+h-r)
    path.lineTo(x + r, y + h)
    emit(x + r, y + h - r, -1.0, +1.0, swap=True)

    # 左边 → 左上角（角心 x+r, y+r），从 (x, y+r) 到 (x+r, y)
    path.lineTo(x, y + r)
    emit(x + r, y + r, -1.0, -1.0, swap=False)

    path.closeSubpath()
    return path


def squircle(rect: QRectF, radius: float, n: float = 4.0, samples: int = 26) -> QPainterPath:
    """取（或构造）一个 iOS 连续曲率圆角路径。

    `rect` 会被量化到 0.5px 再缓存 —— 避免每个绘制帧都重新求样条。
    """
    x = round(rect.x() * 2) / 2
    y = round(rect.y() * 2) / 2
    w = round(rect.width() * 2) / 2
    h = round(rect.height() * 2) / 2
    return _squircle_path_cached(x, y, w, h, round(radius * 2) / 2, n, samples)


def pill(rect: QRectF) -> QPainterPath:
    """胶囊形 —— 按钮/开关用。"""
    return squircle(rect, min(rect.width(), rect.height()) / 2.0, n=2.0, samples=12)


# --------------------------------------------------------------------------
# 材质细节：玻璃噪点
# --------------------------------------------------------------------------

_grain_cache: dict[tuple[int, int], "object"] = {}


def grain_tile(size: int = 128, strength: int = 7):
    """生成一张可平铺的单色噪点图。

    真实玻璃有微观粗糙度；纯色半透明面看起来像塑料。叠一层极淡噪点后，
    材质会立刻"有颗粒感"。强度远低于可见阈值，只在暗部隐约可辨。
    """
    key = (size, strength)
    if key in _grain_cache:
        return _grain_cache[key]

    import numpy as np
    from PySide6.QtGui import QImage, QPixmap

    rng = np.random.default_rng(20260101)
    noise = rng.integers(0, strength * 2 + 1, size=(size, size), dtype=np.uint8)
    buf = np.empty((size, size, 4), dtype=np.uint8)
    buf[..., 0] = 255
    buf[..., 1] = 255
    buf[..., 2] = 255
    buf[..., 3] = noise
    img = QImage(buf.data, size, size, size * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()
    pix = QPixmap.fromImage(img)
    _grain_cache[key] = pix
    return pix


# --------------------------------------------------------------------------
# 单例访问
# --------------------------------------------------------------------------

materials = Materials()
labels = Labels()
accents = Accents()
type_scale = Typography()
radii = Radii()
spacing = Spacing()
blur = Blur()
motion = Motion()
elevation = Elevation()
outline = Outline()

@dataclass(frozen=True)
class WindowBackdrop:
    """DWM 亚克力（窗口背后那层真实背景虚化）的染色。

    为什么单独给它一个令牌
    ----------------------
    它是**唯一一层 `win.grab()` 拍不到的东西** —— 由 DWM 在窗口**背后**合成，
    Qt 的画面里根本不存在。所以它既不会被截图对比发现，也不能靠"看着调"，
    必须把依据写下来。

    依据（`_out/measure_ref_keycap.py`，量的是参考实现的按键玻璃）
    -------------------------------------------------------------
    参考实现的键帽让**背后画面的绝大部分**透上来：键帽内部与紧邻外侧的
    亮度比中位 **1.72**（范围 1.12~2.03），也就是玻璃基本不改动背景，
    只在边缘加一圈高光、再掺 4% 的冷白（`tintColor.a = 0.039`）。
    它**没有**"把整窗压暗"的这一层。

    而我们原来传的是 ``a = 168`` —— 相当于在整窗上盖一层 **66% 不透明的近黑**，
    把桌面透过率直接压到 ``1 - 168/255 = 34%``。这就是"蒙着一层黑雾"的来源：
    后面所有材质再透亮也没用，因为进来之前已经被砍掉三分之二。

    ``a = 96`` 时桌面透过率 62%。再叠上各页自己的暗色材质、
    面板材质与自适应暗幕之后，文字仍然读得清；
    想要更透就往下调，觉得压不住亮壁纸就往上调。
    """

    tint: tuple[int, int, int, int] = (16, 16, 22, 96)


#: 当前的玻璃表面处理。改成 :data:`theme.FROSTED` 即可切回磨砂。
finish = GLOSS

#: 当前的玻璃渲染档位。``glass.set_mode("quality")`` 可切回 SDF 折射。
glass = GlassQuality()

#: 实时档的外观参数（只在 ``glass.mode == "realtime"`` 时生效）。
glass_realtime = GlassRealtime()

#: DWM 亚克力的染色 —— 见 :class:`WindowBackdrop` 里的实测依据。
window_backdrop = WindowBackdrop()


__all__ = [
    "rgba", "white", "black", "tw", "TWEMOJI",
    "Materials", "Labels", "Accents", "TextStyle", "Typography",
    "Radii", "Spacing", "Blur", "Motion", "Elevation", "Outline", "GlassFinish",
    "GlassQuality", "GlassRealtime", "WindowBackdrop",
    "materials", "labels", "accents", "type_scale", "radii", "spacing", "blur",
    "motion", "elevation", "outline", "finish", "GLOSS", "FROSTED",
    "glass", "glass_realtime", "window_backdrop",
    "squircle", "pill", "grain_tile",
    "resolve_font", "resolve_mono_font", "font_family",
    "QColor", "QPainterPath", "QPointF", "QRectF", "Qt",
]
