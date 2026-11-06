"""玻璃材质控件库。

这里实现"玻璃"的六个视觉要素 —— 缺任何一个都会立刻露馅成半透明色块：

1. **底色**：来自 :mod:`app.theme` 的三层材质之一（严格分层，不许叠加同级）。
2. **内壁渐变**：顶部微亮、底部微暗，模拟玻璃的厚度。
3. **顶部高光边**：1px 接近纯白的长条，横向两端淡出 —— 这是"光线照到材质上"。
4. **轮廓细线**：1px 极淡描边，防止亮背景上边缘消失。
5. **微观噪点**：极低强度的颗粒，让表面不再像塑料。
6. **外投影**：上下文感知 —— 面板越厚、下层越花，阴影越重。

另提供可选的 **真实背景虚化**（:func:`blur_pixmap` + ``GlassSurface.blur_behind``）：
截取控件下方已渲染的画面，用积分图做三次盒式模糊（≈高斯），再作为面板填充。
这是浏览器 `backdrop-filter` 在 Qt 里的等价物。
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import (
    QPoint, QPointF, QRect, QRectF, QSize, Qt, Property, QTimer, Signal,
)
from PySide6.QtGui import (
    QBrush,
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
)
from PySide6.QtWidgets import (
    QAbstractButton,
    QGraphicsDropShadowEffect,
    QSizePolicy,
    QWidget,
)

from app import theme
from app.motion import SPRING_SNAPPY, animator, reduced_motion
from app.theme import accents, blur, labels, materials, radii, spacing, type_scale


# ==========================================================================
# 图像模糊 —— 浏览器 backdrop-filter 的 Qt 等价物
# ==========================================================================


def _box1d(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """沿单轴做一次盒式模糊（用前缀和，复杂度与半径无关）。"""
    if r < 1:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(p, axis=axis)
    zero_shape = list(c.shape)
    zero_shape[axis] = 1
    c = np.concatenate([np.zeros(zero_shape, dtype=c.dtype), c], axis=axis)

    hi = [slice(None)] * a.ndim
    lo = [slice(None)] * a.ndim
    hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
    lo[axis] = slice(0, n)
    return (c[tuple(hi)] - c[tuple(lo)]) / float(2 * r + 1)


def blur_pixmap(pix: QPixmap, radius: float, passes: int = 3, saturate: float = 1.0) -> QPixmap:
    """高斯近似模糊。

    三次盒式模糊已足够接近高斯（中心极限定理），而积分图实现让耗时与半径无关 ——
    所以哪怕 blur 半径 60px，代价也和 6px 差不多。

    ``saturate`` > 1 会提升饱和度：iOS 的材质会"吃进"背景颜色并强化它，
    这是让玻璃看起来通透而非发灰的关键一步。
    """
    if pix.isNull() or radius < 0.5:
        return pix

    img = pix.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    if w < 2 or h < 2:
        return pix

    ptr = img.constBits()
    arr = np.frombuffer(ptr, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4).astype(np.float32)

    r = max(1, int(round(radius / 2.0)))  # 三次盒式的等效半径
    work = arr[..., :3]
    for _ in range(passes):
        work = _box1d(work, r, axis=1)
        work = _box1d(work, r, axis=0)

    if saturate != 1.0:
        lum = work @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        work = lum[..., None] + (work - lum[..., None]) * saturate

    out = arr.copy()
    out[..., :3] = np.clip(work, 0, 255)
    out_bytes = np.ascontiguousarray(out.astype(np.uint8))
    result = QImage(out_bytes.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
    return QPixmap.fromImage(result)


def capture_widget_pixmap(widget: QWidget, extra_margin: int = 0) -> QPixmap:
    """截取控件自身（含子控件）当前渲染结果。"""
    dpr = widget.devicePixelRatioF() or 1.0
    pm = QPixmap(int(widget.width() * dpr), int(widget.height() * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.GlobalColor.transparent)
    widget.render(pm, QPoint(0, 0), QRect(0, 0, widget.width(), widget.height()))
    return pm


def capture_below(widget: QWidget, region: QRect) -> QPixmap:
    """截取窗口内 ``region`` 位置**已经渲染**的画面 —— 作为毛玻璃的输入。

    依赖 ``widget.window()`` 的当前帧；因此调用前应确保父级已完成绘制。
    """
    win = widget.window()
    if win is None:
        return QPixmap()
    dpr = win.devicePixelRatioF() or 1.0
    full = QPixmap(int(win.width() * dpr), int(win.height() * dpr))
    full.setDevicePixelRatio(dpr)
    full.fill(Qt.GlobalColor.transparent)
    win.render(full, QPoint(0, 0), QRect(0, 0, win.width(), win.height()))
    if region.isEmpty():
        return full
    scaled = QRect(
        int(region.x() * dpr), int(region.y() * dpr),
        int(region.width() * dpr), int(region.height() * dpr),
    )
    return full.copy(scaled)


def _capture_below_scaled(widget: QWidget, region: QRect, step: int) -> QPixmap | None:
    """把窗口内 ``region`` 以 **1/step 分辨率**截下来。

    为什么不复用 :func:`capture_below` 再缩小：那样要先把整窗按全分辨率
    渲染一遍（面板浮层场景下这是最贵的一步），再丢掉 15/16 的像素。
    这里直接把 painter 缩放 1/step 再渲染 —— Qt 在 C++ 侧按块合并，
    渲染的像素量本身就是 1/step²。

    ``step`` 同时写进结果的 devicePixelRatio，绘制时按原尺寸铺开即可。
    """
    win = widget.window()
    if win is None or win.width() < 4 or win.height() < 4:
        return None
    step = max(1, int(step))
    dpr = win.devicePixelRatioF() or 1.0
    s = dpr / step
    out = QPixmap(max(1, int(region.width() * s)), max(1, int(region.height() * s)))
    out.setDevicePixelRatio(s)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, False)
    p.scale(s, s)
    p.translate(-region.x(), -region.y())
    win.render(p, QPoint(0, 0), region)
    p.end()
    return out


def _fingerprint(pm: QPixmap, side: int = 8) -> bytes:
    """一张缩略图的灰度指纹 —— 用来判断"面板背后的内容变了没有"。

    8x8 = 64 字节，做一次只要几十微秒。比"每次都重新模糊一遍"便宜三个数量级，
    而且它对噪声不敏感（缩放起到了平均作用），不会因为一两个像素抖动就误判。
    """
    small = pm.scaled(side, side, Qt.AspectRatioMode.IgnoreAspectRatio,
                      Qt.TransformationMode.SmoothTransformation)
    img = small.toImage().convertToFormat(QImage.Format.Format_Grayscale8)
    return bytes(img.constBits())


def ambient_luminance(region: QRect) -> float | None:
    """廉价地取 ``region`` 背后画面的亮度（失败返回 None）。"""
    try:
        from app.widgets.ambient import ambient

        return ambient.luminance_at(region)
    except Exception:
        return None


# ==========================================================================
# 核心绘制
# ==========================================================================


@dataclass
class GlassStyle:
    """一次玻璃绘制的全部参数。"""

    material: QColor = None            # type: ignore[assignment]
    radius: float = radii.card
    specular: float = 1.0              # 顶部高光强度倍率（0=不要）
    border: float = 1.0                # 轮廓强度倍率
    inner_gradient: float = 1.0        # 内壁渐变强度倍率
    grain: float = 1.0                 # 噪点强度倍率
    shadow: int = -1                   # -1=不加阴影, 0..3=层级
    bloom: QColor | None = None        # 额外的色彩晕染（用于被选中的强调面）
    clip: bool = True                  # 是否按 squircle 裁剪

    # ---- 液态玻璃（LumaGlass 风）------------------------------------
    outline: float = 1.0
    """轮廓粗细倍率。0 表示不要轮廓（用于纯装饰面）。"""

    liquid_mix: float = 0.0
    """液态玻璃填充的不透明度 0..1。>0 时必须由调用方传入 liquid 位图。"""

    outline_color: QColor | None = None
    """轮廓基色。默认白色（中性玻璃）；强调面可传自己的色相。"""

    lens: float = 0.0
    """**边缘透镜带**强度 0..1（实时档的"液态感"来源）。

    液态路径靠"把背景画面掰弯"表现边缘折射；实时档没有背景像素可用，
    所以改用**绘制**去暗示同一件事：边框内侧一条沿光轴亮起、向内衰减的窄带。
    0 表示不要（普通轻量控件）。

    依据见 :func:`_paint_lens_band` 与参考实现
    （InfiniteGUI 游戏内 HUD 的 ``HudLiquidGlass`` 片元着色器）。
    """

    lens_axis_deg: float = 315.0
    """光轴角度（度）。光轴的**两端同时亮**，垂直于它的两条边保持中性。"""

    frost: float = 0.0
    """凝霜补偿 0..1：把面板整体往 :data:`app.theme.GlassFinish.frost_color` 掺。

    液态路径本身会掺 ``finish.frost``；实时档没有位图，若不补这一层，
    面板会比原来更透、白字更糊 —— 相当于"去掉模糊"顺手把可读性也去掉了。
    """

    def __post_init__(self):
        if self.material is None:
            self.material = materials.panel


def _axis_gradient(rect: QRectF, axis_deg: float, base: QColor, peak: float) -> QLinearGradient:
    """沿光轴构造"两端亮、中间归零"的对称渐变。

    形状来自参考实现的 ``Glow()``：
    ``abs(dot(normalize(p), L))`` —— 取绝对值让光轴的**两端同时亮**，
    垂直于光轴的两侧保持中性。若不做 ``abs``，就会把背光的一侧压暗，
    变成一道假的"内阴影"，玻璃立刻显脏。
    """
    import math

    ang = math.radians(axis_deg)
    dx, dy = math.cos(ang), math.sin(ang)
    cx, cy = rect.center().x(), rect.center().y()
    half = math.hypot(rect.width(), rect.height()) / 2.0
    p0 = QPointF(cx - dx * half, cy - dy * half)
    p1 = QPointF(cx + dx * half, cy + dy * half)

    def at(a: float) -> QColor:
        c = QColor(base)
        c.setAlphaF(max(0.0, min(1.0, a)))
        return c

    g = QLinearGradient(p0, p1)
    g.setColorAt(0.00, at(peak))
    g.setColorAt(0.26, at(peak * 0.46))
    g.setColorAt(0.50, at(0.0))
    g.setColorAt(0.74, at(peak * 0.46))
    g.setColorAt(1.00, at(peak))
    return g


def _paint_lens_band(painter: QPainter, rect: QRectF, style: "GlassStyle") -> None:
    """实时档的"边缘折射"替代物 —— 一条沿光轴亮起、向内衰减的窄带。

    为什么这样能替代真正的折射
    --------------------------
    人眼从液态玻璃边缘读到的是三件事：**边更亮**、**亮得有方向**、
    **越靠边越强**。三者都是绘制能表达的，不需要真的去采样背景像素。

    参考实现（InfiniteGUI 的 ``HudLiquidGlass``）证实了这一点：它是 GPU
    逐像素算的，但真正的视觉贡献只有
    ``color += vec3(0.82,0.92,1.0) * rimMask * directionalGlow * 0.55``
    —— 一条 1.75px、绑在周长上、沿光轴两端亮起的高光线；
    它的染色 alpha 只有 0.039，模糊甚至默认是关的。

    分层
    ----
    * **拖尾**（宽而淡）：模拟倒角"越靠边越亮"的坡，避免像描边而不像透镜。
    * **亮带**（窄而亮）：就是上面那条高光线。
    """
    if style.lens <= 0.001 or rect.width() < 6 or rect.height() < 6:
        return
    # 太小的面（芯片、小按钮）根本看不出边缘透镜，白花钱。
    if min(rect.width(), rect.height()) < 26:
        return
    from app import theme as _theme

    rt = _theme.glass_realtime
    # 冷白，与参考实现的 vec3(0.82, 0.92, 1.0) 同族 —— 玻璃边缘聚的是天光
    base = QColor(209, 234, 255)
    gain = rt.lens_gain * style.lens
    width = max(0.6, rt.lens_width * style.lens)

    painter.save()
    painter.setBrush(Qt.BrushStyle.NoBrush)

    # **不要在这里再 setClipPath。** paint_glass 开头已经按形状裁过一次，
    # 再叠一层会让 Qt 去求两个路径裁剪区域的交 —— 实测单面板从 0.43ms
    # 涨到 0.64ms（962x76 的轨道行是 0.36 → 0.67，几乎翻倍）。
    # 正确做法是让描边自己待在形状内侧：描边宽度 w 以路径为中心，
    # 所以内侧缩进必须 >= w/2，否则会溢出到面板外。
    if rt.lens_bleed > 0.001:
        bw = width * 1.8
        inset = bw * 0.5
        # 拖尾用**纯色**笔而不是渐变笔：它本身只有 0.06 的不透明度，
        # 方向感完全看不出来，而渐变笔在 Qt 光栅里要把描边展开成填充路径
        # 再逐像素求渐变 —— 实测这一笔占掉整个透镜带成本的一半。
        bleed = QColor(base)
        bleed.setAlphaF(max(0.0, min(1.0, rt.lens_bleed * style.lens)))
        shape = _theme.squircle(rect.adjusted(inset, inset, -inset, -inset),
                                max(0.0, style.radius - inset))
        painter.setPen(QPen(bleed, bw))
        painter.drawPath(shape)

    inset = max(0.6, width * 0.5)
    shape = _theme.squircle(rect.adjusted(inset, inset, -inset, -inset),
                            max(0.0, style.radius - inset))
    g = _axis_gradient(rect, style.lens_axis_deg, base, gain)
    painter.setPen(QPen(QBrush(g), width))
    painter.drawPath(shape)

    painter.restore()


def paint_bloom(painter: QPainter, rect: QRectF, radius: float, color: QColor,
                strength: float = 1.0, spread: float = 5.0) -> None:
    """在形状外圈画一圈柔和泛光。

    Qt 没有便宜的"外发光"，这里用**若干圈逐渐扩散、逐渐透明的描边**近似。
    对玻璃边缘这种"聚光"效果足够了，而且完全可控、没有 graphics effect 的
    生命周期麻烦。
    """
    if strength <= 0.001:
        return
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setBrush(Qt.BrushStyle.NoBrush)
    rings = 4
    for i in range(rings, 0, -1):
        t = i / rings
        a = color.alphaF() * strength * (1.0 - t) ** 1.6
        if a <= 0.002:
            continue
        c = QColor(color)
        c.setAlphaF(a)
        painter.setPen(QPen(c, 1.2 + spread * t))
        painter.drawPath(theme.squircle(rect.adjusted(-spread * t * 0.5, -spread * t * 0.5,
                                                       spread * t * 0.5, spread * t * 0.5),
                                        radius + spread * t * 0.5))
    painter.restore()


def paint_glass(painter: QPainter, rect: QRectF, style: GlassStyle,
                liquid: QPixmap | None = None) -> QPainterPath:
    """把一块玻璃画到 ``painter`` 上，返回所用的形状路径。

    ``liquid`` 是 :func:`app.widgets.liquid.liquid_fill` 的输出 —— 一块已经
    模糊并做过边缘折射的液态玻璃位图。传了它、且 ``style.liquid_mix > 0`` 时，
    它会作为**底层**铺上去，材质染色与高光再叠在上面。这样既有"背后画面被掰弯"
    的真实感，又保住了材质本身的明度台阶。
    """
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

    path = theme.squircle(rect, style.radius)
    if style.clip:
        painter.setClipPath(path)

    # ---- 0. 液态玻璃底层（含边缘折射）--------------------------------
    if liquid is not None and not liquid.isNull() and style.liquid_mix > 0.001:
        painter.setOpacity(min(1.0, style.liquid_mix))
        target = QRectF(rect.x(), rect.y(), rect.width(), rect.height())
        painter.drawPixmap(target, liquid, QRectF(liquid.rect()))
        painter.setOpacity(1.0)

    # ---- 1. 底色 -------------------------------------------------------
    base = QColor(style.material)
    if style.liquid_mix > 0.001 and liquid is not None:
        # 有液态底时材质染色要克制，但**只对亮色克制**：
        # 亮色染色叠在凝霜底上会把面板洗白、也盖住折射；
        # 暗色染色只是压明度、给白字留出对比，保留它才读得清。
        lum = (0.2126 * base.redF() + 0.7152 * base.greenF() + 0.0722 * base.blueF())
        if lum > 0.5:
            base.setAlphaF(base.alphaF() * 0.55)
    painter.fillPath(path, QBrush(base))

    # ---- 1b. 凝霜补偿（只在实时档出现）--------------------------------
    # 液态路径把画面往浅色掺 finish.frost，面板因此**比背景更亮**，
    # 层级与文字底座都是这么来的。实时档没有位图，这里补回同一层。
    if style.frost > 0.0005:
        fr, fg, fb = theme.finish.frost_color
        painter.fillPath(path, theme.rgba(fr, fg, fb, min(0.6, style.frost)))

    # ---- 2. 内壁渐变：顶部微亮 → 中部透明 → 底部微暗（玻璃厚度感）-----
    ig = style.inner_gradient * theme.finish.inner_gradient
    if ig > 0:
        g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
        g.setColorAt(0.00, theme.white(0.075 * ig))
        g.setColorAt(0.38, theme.white(0.012 * ig))
        g.setColorAt(0.72, theme.black(0.020 * ig))
        g.setColorAt(1.00, theme.black(0.055 * ig))
        painter.fillPath(path, QBrush(g))

    # ---- 2b. 可选色彩晕染 ---------------------------------------------
    if style.bloom is not None and style.bloom.alphaF() > 0.001:
        # 泛光只是**淡淡一层晕染**，必须压强度。
        # 早先直接把 style.bloom 当颜色用，而调用方传的是 accents.yellow 这种
        # 全不透明的强调色 —— 于是整块卡片被铺成一块实心黄，成了截图里那个大色块。
        # 上限钳到 0.14，无论调用方传什么进来都不会失控。
        peak = min(0.14, style.bloom.alphaF() * 0.10)
        if peak > 0.002:
            rg = QRadialGradient(rect.center(), max(rect.width(), rect.height()) * 0.85)
            c = QColor(style.bloom)
            c.setAlphaF(peak)
            rg.setColorAt(0.0, c)
            c2 = QColor(style.bloom)
            c2.setAlphaF(0.0)
            rg.setColorAt(1.0, c2)
            painter.fillPath(path, QBrush(rg))

    # ---- 3. 顶部高光边（光线照射材质）----------------------------------
    if style.specular > 0:
        spec_h = max(1.0, rect.height() * 0.5)
        spec_rect = QRectF(rect.x(), rect.y(), rect.width(), min(1.25, spec_h))
        # 横向两端淡出，避免两端出现生硬的亮角
        lg = QLinearGradient(spec_rect.topLeft(), spec_rect.topRight())
        a = theme.white(0.0)
        b = theme.white(0.42 * style.specular)
        c = theme.white(0.30 * style.specular)
        d = theme.white(0.0)
        lg.setColorAt(0.00, a)
        lg.setColorAt(0.12, b)
        lg.setColorAt(0.50, b)
        lg.setColorAt(0.88, c)
        lg.setColorAt(1.00, d)
        line = QPainterPath()
        line.addRect(QRectF(spec_rect.x() + style.radius * 0.55, spec_rect.y(),
                            max(0.0, spec_rect.width() - style.radius * 1.1), spec_rect.height()))
        painter.fillPath(line, QBrush(lg))

    # ---- 3b. 斜向镜面反光条（光面的灵魂）------------------------------
    # 磨砂面把光散射掉，只剩漫射的亮边；光面则会映出一道**窄而锐的斜向反光**。
    # 没有这一道，"光面"就只是"模糊调小了"，看着还是塑料。
    # 关键是**窄**：峰值集中在很小一段行程上，宽而淡的渐变只会显得脏。
    if theme.finish.sheen > 0.01 and style.specular > 0:
        sheen = theme.finish.sheen * style.specular
        sg = QLinearGradient(
            rect.topLeft(),
            QPointF(rect.left() + rect.width() * 0.55, rect.top() + rect.height() * 2.1),
        )
        sg.setColorAt(0.00, theme.white(0.0))
        sg.setColorAt(0.03, theme.white(0.06 * sheen))
        sg.setColorAt(0.09, theme.white(0.34 * sheen))   # 峰值：窄
        sg.setColorAt(0.16, theme.white(0.12 * sheen))
        sg.setColorAt(0.28, theme.white(0.02 * sheen))
        sg.setColorAt(0.45, theme.white(0.0))
        sg.setColorAt(1.00, theme.white(0.0))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QBrush(sg))
        painter.drawPath(path)

    # ---- 4. 轮廓：三层叠加才有"厚玻璃边"的立体感 ----------------------
    if style.border > 0 or style.outline > 0:
        painter.setBrush(Qt.BrushStyle.NoBrush)
        inset = QRectF(rect).adjusted(0.5, 0.5, -0.5, -0.5)
        shape = theme.squircle(inset, style.radius)

        if style.outline > 0:
            # 注意：style.outline 是**粗细倍率**（float），
            # 而 theme.outline 是轮廓色板 —— 两者同名，别写混。
            ow = theme.outline.width * style.outline

            # (a) 外侧暗线 —— 把玻璃从背景里"切"出来，亮背景上离不开它
            dark = QColor(theme.outline.dark_outer)
            dark.setAlphaF(dark.alphaF() * style.outline)
            painter.setPen(QPen(dark, ow * 2.4))
            painter.drawPath(theme.squircle(rect.adjusted(-0.6, -0.6, 0.6, 0.6), style.radius + 0.6))

            # (b) 内侧亮环 —— 上亮下暗，模拟受光的玻璃倒角
            base_color = style.outline_color or theme.white(1.0)
            top_c = QColor(base_color)
            top_c.setAlphaF(theme.outline.light_top.alphaF() * style.outline)
            bot_c = QColor(base_color)
            bot_c.setAlphaF(theme.outline.light_bottom.alphaF() * style.outline)
            lg = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            lg.setColorAt(0.0, top_c)
            lg.setColorAt(0.45, top_c)
            lg.setColorAt(1.0, bot_c)
            painter.setPen(QPen(QBrush(lg), ow * style.border))
            painter.drawPath(shape)

        elif style.border > 0:
            # 老路：极淡的发丝线（给不想要粗边的轻量控件留的退路）
            pen = QPen()
            pen.setWidthF(1.0)
            g = QLinearGradient(rect.topLeft(), rect.bottomLeft())
            g.setColorAt(0.0, theme.white(0.155 * style.border))
            g.setColorAt(0.5, theme.white(0.060 * style.border))
            g.setColorAt(1.0, theme.black(0.10 * style.border))
            pen.setBrush(QBrush(g))
            painter.setPen(pen)
            painter.drawPath(shape)

    # ---- 4b. 边缘透镜带（实时档替代真正的折射）------------------------
    # 放在轮廓之后：亮带要叠在内侧亮环上，才是"倒角受光"的样子。
    if style.lens > 0.001:
        _paint_lens_band(painter, rect, style)

    # ---- 5. 微观噪点（跟随表面处理）-----------------------------------
    # 噪点模拟的是表面粗糙度：磨砂面有颗粒，光面几乎光滑无痕。
    gr = style.grain * theme.finish.grain
    if gr > 0.01:
        painter.setOpacity(0.035 * gr)
        painter.fillPath(path, QBrush(theme.grain_tile()))
        painter.setOpacity(1.0)

    painter.restore()
    return path


def paint_mini_specular(painter: QPainter, rect: QRectF, radius: float, strength: float = 0.5) -> None:
    """给已有的填充补一条顶部高光（用于不套用整套 GlassStyle 的轻量控件）。"""
    painter.save()
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setClipPath(theme.squircle(rect, radius))
    lg = QLinearGradient(rect.topLeft(), rect.topRight())
    lg.setColorAt(0.0, theme.white(0.0))
    lg.setColorAt(0.15, theme.white(0.38 * strength))
    lg.setColorAt(0.85, theme.white(0.30 * strength))
    lg.setColorAt(1.0, theme.white(0.0))
    painter.fillRect(QRectF(rect.x(), rect.y(), rect.width(), 1.2), QBrush(lg))
    painter.restore()


def apply_shadow(widget: QWidget, level: int = 1, color: QColor | None = None) -> None:
    """给控件加外投影。

    注意 Qt 每个控件只能挂一个 graphics effect —— 已经挂了透明度效果的地方
    不要再调本函数，否则会覆盖。
    """
    b, dy, alpha = theme.elevation.blur_alpha(level)
    eff = QGraphicsDropShadowEffect(widget)
    eff.setBlurRadius(b)
    eff.setOffset(0.0, dy)
    c = color or theme.black(1.0)
    c = QColor(c)
    c.setAlphaF(alpha)
    eff.setColor(c)
    widget.setGraphicsEffect(eff)


# ==========================================================================
# 基类
# ==========================================================================


class GlassSurface(QWidget):
    """所有玻璃面的基类。

    子类只需实现 :meth:`glass_style`；绘制流程由基类统一处理。

    液态玻璃
    --------
    把 ``LIQUID`` 设为 True，这块面就会从窗口环境背景上取一块带余量的区域，
    做模糊 + 边缘折射，作为自己的底层填充 —— 也就是 LumaGlass 那种
    "能看见背后画面被边框掰弯"的效果。

    **结果必须缓存**：一次折射合成是 20~50ms 量级的纯 numpy 运算，放在
    :meth:`paintEvent` 里逐帧重算会直接把界面拖垮。这里按尺寸缓存，
    尺寸不变就一直复用同一张位图。
    """

    clicked = Signal()

    #: 是否启用液态玻璃
    LIQUID: bool = False
    #: 可选的自定义参数（None 用默认）
    LIQUID_PARAMS = None

    # ---- 合成器层：这块面坐在什么上面 ----------------------------------
    #: 坐在**窗口透明底**上 —— 桌面由 Windows DWM 亚克力糊着透上来。
    BACKDROP = "backdrop"
    #: 坐在**应用自己的画面**上（浮层、覆盖在轨道列表/网格之上的卡片）。
    CONTENT = "content"

    #: 本控件属于哪一类。默认 A 类 —— 本项目绝大多数面板都是。
    SURFACE: str = BACKDROP

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._hover = False
        self._pressed = False
        self._liquid_cache: QPixmap | None = None
        self._liquid_key: tuple | None = None
        #: 折射底图上次重算的时刻（毫秒）—— 实时档靠它做时间限流
        self._refr_at: float = 0.0
        #: 折射底图重算次数（供 tools/glass_bench.py 观测，不参与绘制）
        self._refr_calls: int = 0
        #: B 类快照的内容指纹（变了才重算）
        self._snap_hash: bytes | None = None
        self._snap_pending = False

    # -- 供子类覆写 ------------------------------------------------------

    def glass_style(self) -> GlassStyle:
        return GlassStyle()

    def liquid_params(self):
        """液态玻璃参数。跟随 :data:`app.theme.finish` 的表面处理档位。"""
        if self.LIQUID_PARAMS is not None:
            return self.LIQUID_PARAMS
        from app.widgets.liquid import LiquidParams

        return LiquidParams.from_finish(theme.finish)

    # -- 液态玻璃 --------------------------------------------------------

    def invalidate_liquid(self) -> None:
        """内容/位置变化时调用，让下次绘制重新取背景。

        **实时档刻意不清折射底图。** 那张图由时间限流
        （``theme.glass_realtime.refract_interval_ms``）自己刷新；
        清掉它等于让每一次 `move` 都重算一遍 —— 那就是当初 80ms 节流
        之所以要存在的理由，也正是这一次要去掉的东西。
        尺寸/圆角变了会让 key 失配，那条路自然会立刻重算。
        """
        if not theme.glass.realtime:
            self._liquid_cache = None
            self._liquid_key = None
        self._snap_hash = None
        self._snap_pending = False
        self._backdrop_lum = None
        self.update()

    def _ensure_liquid(self) -> QPixmap | None:
        """按"合成器层"决定这块面要不要自己算背景，以及**用哪一档算**。

        三条路：

        * **A 类 · 坐在窗口透明底上**（BACKDROP，本项目绝大多数）→
          :meth:`_ensure_realtime_refraction`：**真折射**，
          但跑在降采样分辨率上并按时间限流。
        * **B 类 · 坐在自己的画面上**（CONTENT）→
          :meth:`_ensure_snapshot`：降采样模糊快照。
        * **高质量档**（``theme.glass.mode == "quality"``）→
          :meth:`_ensure_sdf_liquid`：全分辨率 SDF 折射，供截图导出。

        （曾经这里在实时档对 A 类直接 `return None` —— 等于把液态玻璃
          整个删掉，只留一圈画出来的高光线。那是错的：参考实现是**真的做折射**，
          只不过做得便宜。见 :meth:`_ensure_realtime_refraction`。）
        """
        if not self.LIQUID:
            return None
        if not theme.glass.realtime:
            return self._ensure_sdf_liquid()
        if self.SURFACE == self.BACKDROP:
            return self._ensure_realtime_refraction()
        return self._ensure_snapshot()

    # -- 实时档 · A 类：降采样 + 限流的**真折射** -------------------------

    def _ensure_realtime_refraction(self) -> QPixmap | None:
        """实时档的液态玻璃：**同一套折射引擎，跑在 1/step 分辨率上**。

        为什么这样才既真又实时
        ----------------------
        参考实现（InfiniteGUI 的 ``HudLiquidGlass``，见 `_out/re/FINDINGS.md`）
        在交互路径上只做了两件事，没有一件是"把效果删掉"：

        1. **不模糊**（它的 ``blurEnabled`` 默认 false）；
        2. 折射 = 把采样坐标乘一个随"离边距离"指数衰减的标量
           （``sampleP = mix(p, p * f(dist), amount)``），
           比"求 SDF 梯度 → 造位移图 → 双线性重映射"便宜一个量级。

        CPU 上没有片元着色器，但有个等价且更省的办法：**整条管线降分辨率**。
        液态玻璃本来就是糊的，降采样看不出来，而像素量按 step² 掉。
        再叠两道闸，把"贵"和"频繁"分开：

        * ``waviness = 0``：那个全幅正弦位移场是最贵的一项
          （1240x420 的面板光它就要 34ms），而参考实现根本没有这个东西；
        * **按时间限流**：折射底图最多每 ``refract_interval_ms`` 重算一次，
          中间的帧直接复用上一张。**重绘仍然逐帧**，所以面板照样跟着窗口走 ——
          只是"透过玻璃看到的那层背景"晚几十毫秒，而它是模糊的，看不出来。

        于是拖动时的每帧成本 = 一次 blit，而不是一次重折射。静态时零成本。
        """
        from dataclasses import replace

        rt = theme.glass_realtime
        step = max(1, int(rt.refract_step))
        interval = max(0.0, float(rt.refract_interval_ms))
        style = self.glass_style()
        key = (self.width(), self.height(), round(style.radius, 1), step)
        if self.width() < 4 or self.height() < 4:
            return None

        now = time.monotonic() * 1000.0
        cached = self._liquid_cache
        fresh = (cached is not None and self._liquid_key == key
                 and (now - self._refr_at) < interval)
        if fresh:
            return cached

        try:
            from app.widgets.ambient import ambient
            from app.widgets.liquid import liquid_fill

            win = self.window()
            if win is None or win.width() < 4 or win.height() < 4:
                return cached

            params = replace(self.liquid_params(), waviness=0.0)
            top_left = self.mapTo(win, QPoint(0, 0))
            region = QRect(top_left, self.size())
            ambient.set_window_size(win.size())
            # 整窗预模糊一次 + 缩到 1/step 一次（两者都按内容缓存，所有面板共用），
            # 然后**从小图上裁** —— 第一版是"每块面板各裁全分辨率再各自缩小"，
            # 等于把最贵的 blit 按全分辨率做了 9 遍，拖动每步 387ms。
            if ambient.small_blurred(win.size(), params.blur, step) is None:
                return cached
            src_small = ambient.crop_small(region, step)
            if src_small.isNull():
                return cached

            pw = max(4, self.width() // step)
            ph = max(4, self.height() // step)
            small_params = replace(
                params,
                blur=max(0.5, params.blur / step),
                refraction=params.refraction / step,
                edge_width=max(2.0, params.edge_width / step),
                waviness=0.0,
            )
            out = liquid_fill(src_small, pw, ph, max(1.0, style.radius / step),
                              small_params, preblurred=True)
            if out.isNull():
                return cached

            self._liquid_cache = out
            self._liquid_key = key
            self._refr_at = now
            self._backdrop_lum = ambient.luminance_at(region)
            self._refr_calls += 1
        except Exception:
            # 失败时沿用上一张（若没有就退回"无玻璃底"），绝不把异常抛进 paint
            return cached
        return self._liquid_cache

    # -- 高质量档：原有 SDF 折射路径（默认关闭）--------------------------

    def _ensure_sdf_liquid(self) -> QPixmap | None:
        """SDF 折射合成 —— **只在高质量档调用**。

        默认档位下这条路一次都不会走，所以 12~30ms 的代价不会出现在
        交互路径上；它保留下来供截图导出、离线预览使用。
        """
        if not self.LIQUID:
            return None
        style = self.glass_style()
        key = (self.width(), self.height(), round(style.radius, 1))
        if self._liquid_cache is not None and self._liquid_key == key:
            return self._liquid_cache
        if self.width() < 4 or self.height() < 4:
            return None

        try:
            from app.widgets.ambient import ambient
            from app.widgets.liquid import liquid_fill

            params = self.liquid_params()
            win = self.window()
            top_left = self.mapTo(win, QPoint(0, 0))
            region = QRect(top_left, self.size())
            # 环境图只模糊一次，所有面板共用 —— 这是最大的一笔性能节省
            ambient.set_window_size(win.size())
            ambient.blurred(win.size(), params.blur)
            src = ambient.crop_with_margin(region, blurred=True)
            if src.isNull():
                return None
            # 记下这块面板背后的实际亮度，绘制时据此决定暗幕轻重
            self._backdrop_lum = ambient.backdrop_luminance(region)
            self._liquid_cache = liquid_fill(
                src, self.width(), self.height(), style.radius, params, preblurred=True
            )
            self._liquid_key = key
        except Exception:
            self._liquid_cache = None
            self._liquid_key = None
        return self._liquid_cache

    # -- 实时档 · B 类：降采样快照 ---------------------------------------

    def _ensure_snapshot(self) -> QPixmap | None:
        """模糊"我们自己的画面"（B 类）—— **绝不在这一帧里做**。

        三条纪律（前两条来自参考实现，第三条是本工程的线程/重入纪律）：

        1. **降采样**：只取 1/``snapshot_step`` 分辨率。模糊本身就会掩盖
           低分辨率，而像素量直接降到 1/16。
        2. **内容没变就不重算**：参考实现的原话是
           *"When the UI is static, InfiniteGUI reuses the existing render result."*
           重算前先用一张 8x8 灰度缩略图当指纹比对（见 :meth:`_snapshot_rebuild`）。
        3. **重入安全**：这里只读缓存、必要时**排一个 0ms 定时器**，把真正的
           截图+模糊推到事件循环里去做。

        第 3 条不是优化而是**必须**：截图要 `render()` 整个窗口，而本方法正是
        从 `paintEvent` 里被调到的 —— 边画边截同一个窗口会触发递归重绘，
        Qt 会一路刷 `QWidget::repaint: Recursive repaint detected`，
        最后以 ``0xC0000409`` fail-fast 把进程带走（Python 拦不住）。
        第一次实现就是同步截的，测试当场把进程打崩。
        """
        if self.width() < 4 or self.height() < 4:
            return None
        win = self.window()
        # 自己就是顶层窗口时"下面"没有东西可截，直接放弃（离屏单测会走到这里）
        if win is None or win is self or win.width() < 4 or win.height() < 4:
            return None

        rt = theme.glass_realtime
        step = max(1, int(rt.snapshot_step))
        style = self.glass_style()
        key = (self.width(), self.height(), round(style.radius, 1), step)

        if self._liquid_cache is not None and self._liquid_key == key:
            return self._liquid_cache

        if not self._snap_pending:
            self._snap_pending = True
            QTimer.singleShot(0, self._snapshot_rebuild)
        return None

    def _snapshot_rebuild(self) -> QPixmap | None:
        """真正做一次降采样模糊合成。

        由 :meth:`_ensure_snapshot` 用 0ms 定时器排到事件循环里执行 ——
        此时**不在绘制中**，可以安全地渲染窗口。
        """
        self._snap_pending = False
        win = self.window()
        if win is None or win is self:
            return self._liquid_cache
        try:
            from app.widgets.ambient import ambient
            from app.widgets.liquid import LiquidParams, blur_array

            rt = theme.glass_realtime
            step = max(1, int(rt.snapshot_step))
            style = self.glass_style()
            k = (self.width(), self.height(), round(style.radius, 1), step)
            top_left = self.mapTo(win, QPoint(0, 0))
            region = QRect(top_left, self.size())
            ambient.set_window_size(win.size())

            small = _capture_below_scaled(self, region, step)
            if small is None or small.isNull():
                return self._liquid_cache

            fp = _fingerprint(small)
            if self._liquid_cache is not None and self._liquid_key == k \
                    and self._snap_hash == fp:
                return self._liquid_cache          # 内容没变，白算一遍不如不算

            params = LiquidParams.from_finish(theme.finish)
            img = small.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            w, h = img.width(), img.height()
            a = np.frombuffer(img.constBits(), dtype=np.uint8,
                              count=w * h * 4).reshape(h, w, 4).astype(np.float32)
            # 模糊半径也要按降采样倍率缩小，否则糊过头
            a[..., :3] = blur_array(a[..., :3], max(0.5, params.blur / step))
            if abs(params.saturate - 1.0) > 1e-3:
                lum = a[..., :3] @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
                a[..., :3] = lum[..., None] + (a[..., :3] - lum[..., None]) * params.saturate
            out = QImage(np.ascontiguousarray(np.clip(a, 0, 255).astype(np.uint8)).data,
                         w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
            pm = QPixmap.fromImage(out)

            self._liquid_cache = pm
            self._liquid_key = k
            self._snap_hash = fp
            self._backdrop_lum = ambient_luminance(region)
        except Exception:
            self._liquid_cache = None
            self._liquid_key = None
        self.update()
        return self._liquid_cache

    # -- 事件 ------------------------------------------------------------

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self._pressed = False
        self.update()
        super().leaveEvent(event)

    def resizeEvent(self, event):
        self._backdrop_lum = None
        self.invalidate_liquid()
        super().resizeEvent(event)

    def moveEvent(self, event):
        # 位置变了 → 背后那块桌面也换了，亮度缓存作废。
        # 实时档这里只清一个缓存值，不清液态位图（本来就没有），
        # 所以拖动时每一次 move 都负担得起。
        self._backdrop_lum = None
        self.invalidate_liquid()
        super().moveEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        style = self.glass_style()

        liquid = self._ensure_liquid()
        if liquid is not None:
            style = self._with_adaptive_scrim(style)
        else:
            # 实时档：没有液态位图，用**绘制**补回液态路径贡献的两样东西 ——
            # 边缘透镜带与凝霜。否则"去掉模糊"会顺手把材质也去掉。
            self._apply_realtime_look(style)
            style = self._with_adaptive_scrim(style)
        paint_glass(painter, rect, style, liquid)
        painter.end()

    def _apply_realtime_look(self, style: GlassStyle) -> None:
        """就地补上实时档需要的两项（只在 LIQUID 且非高质量档时）。"""
        if not self.LIQUID or not theme.glass.realtime:
            return
        rt = theme.glass_realtime
        style.lens = rt.lens
        style.lens_axis_deg = rt.lens_axis_deg
        style.frost = rt.frost

    def _ensure_backdrop_lum(self) -> float | None:
        """取一次"背后有多亮" —— 实时档走降采样查表，几乎免费。"""
        if getattr(self, "_backdrop_lum", None) is not None:
            return self._backdrop_lum
        if not self.LIQUID or self.width() < 4 or self.height() < 4:
            return None
        win = self.window()
        if win is None:
            return None
        try:
            from app.widgets.ambient import ambient

            ambient.set_window_size(win.size())
            region = QRect(self.mapTo(win, QPoint(0, 0)), self.size())
            self._backdrop_lum = ambient.luminance_at(region)
        except Exception:
            self._backdrop_lum = None
        return self._backdrop_lum

    def _with_adaptive_scrim(self, style: GlassStyle) -> GlassStyle:
        """按面板背后画面的明暗，**轻微**加重暗幕（兜底手段）。

        可读性主要由表面不平整负责 —— 把背景轮廓揉散比压暗更根本，
        而且不牺牲通透度。暗幕只在极端亮的背景上补一点。

        亮度取 75 分位而不是均值：决定可读性的是背景里**最亮的那块**，
        均值会被大片暗区拉低，导致亮斑上的字照样看不清。
        """
        lum = getattr(self, "_backdrop_lum", None)
        if lum is None:
            lum = self._ensure_backdrop_lum()
        if lum is None:
            return style
        rt = theme.glass_realtime
        # 可读性主要由表面不平整（waviness）负责：把背景轮廓揉散比压暗更根本，
        # 而且不牺牲通透度。暗幕只作兜底，在极端亮的背景上补一点。
        extra = max(0.0, min(rt.scrim_max, (lum - rt.scrim_knee) * rt.scrim_slope))
        if extra <= 0.01:
            return style
        base = QColor(style.material)
        a = base.alphaF()
        # 越暗的底越接近纯黑；已有暗色染色的再叠一点
        new_a = 1.0 - (1.0 - a) * (1.0 - extra)
        base.setAlphaF(min(0.70, new_a))
        style.material = base
        return style


# ==========================================================================
# 面板 / 卡片
# ==========================================================================


class GlassPanel(GlassSurface):
    """结构面板（侧边栏、区域容器）。材质最厚重的一层。"""

    def __init__(
        self,
        parent: QWidget | None = None,
        material: QColor | None = None,
        radius: float = radii.panel,
        shadow: int = -1,
        *,
        liquid: bool = False,
    ):
        super().__init__(parent)
        self._material = material or materials.panel
        self._radius = radius
        self._shadow = shadow
        if liquid:
            self.LIQUID = True

    def glass_style(self) -> GlassStyle:
        return GlassStyle(
            material=self._material,
            radius=self._radius,
            specular=0.9,
            border=1.0,
            inner_gradient=1.0,
            grain=1.0,
            shadow=self._shadow,
            liquid_mix=1.0 if self.LIQUID else 0.0,
        )


class GlassCard(GlassSurface):
    """卡片：比面板轻，用于承载内容。更大表面 → 更强模糊与更深阴影。"""

    def __init__(self, parent: QWidget | None = None, radius: float = radii.card,
                 accent: QColor | None = None, *, liquid: bool = False,
                 material: QColor | None = None):
        super().__init__(parent)
        self._radius = radius
        self._accent = accent
        self._selected = False
        self._material = material
        if liquid:
            self.LIQUID = True

    def set_selected(self, value: bool) -> None:
        if self._selected != value:
            self._selected = value
            self.update()

    def set_accent(self, color: QColor | None) -> None:
        self._accent = color
        self.update()

    def glass_style(self) -> GlassStyle:
        if self._selected and self._accent is not None:
            tint = QColor(self._accent)
            tint.setAlphaF(0.16)
            return GlassStyle(material=tint, radius=self._radius, specular=1.15,
                              border=1.2, inner_gradient=0.7, grain=0.7,
                              bloom=QColor(self._accent),
                              liquid_mix=1.0 if self.LIQUID else 0.0,
                              outline_color=QColor(self._accent))
        return GlassStyle(material=self._material or materials.panel, radius=self._radius,
                          specular=1.0, border=1.0, inner_gradient=1.0, grain=0.9,
                          liquid_mix=1.0 if self.LIQUID else 0.0)


class GlassButton(QAbstractButton):
    """iOS 手感的按钮。

    三条必须做到的事：
    * **按下即反馈**（``mousePressEvent`` 里就开始，不等松开）
    * 按下时缩小到 0.97 并加深 —— 用弹簧，不是线性缓动
    * 移出控件时取消按下态
    """

    def __init__(
        self,
        text: str = "",
        parent: QWidget | None = None,
        *,
        accent: QColor | None = None,
        style: str = "plain",     # plain | tinted | filled | destructive
        radius: float = radii.control,
        compact: bool = False,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._text = text
        self._accent = accent or accents.blue
        self._style = style
        self._radius = radius
        self._compact = compact
        self._pressed_state = False
        self._hover = False
        self._scale = 1.0
        self._anim = animator(self)
        self._pad_h = 9.0 if compact else 14.0
        self._pad_v = 4.0 if compact else 7.0
        self.setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Fixed)

    # -- 可动画属性 ------------------------------------------------------

    def _get_scale(self) -> float:
        return self._scale

    def _set_scale(self, v: float) -> None:
        self._scale = v
        self.update()

    scale = Property(float, _get_scale, _set_scale)

    # -- 配置 ------------------------------------------------------------

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        return self._text

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

    def set_style_kind(self, kind: str) -> None:
        self._style = kind
        self.update()

    # -- 尺寸 ------------------------------------------------------------

    def _label_font(self) -> QFont:
        style = type_scale.footnote if self._compact else type_scale.subhead
        return style.font()

    def sizeHint(self) -> QSize:
        from PySide6.QtGui import QFontMetricsF

        fm = QFontMetricsF(self._label_font())
        w = fm.horizontalAdvance(self._text) + self._pad_h * 2
        h = fm.height() + self._pad_v * 2 + 2
        return QSize(int(max(w, h + 4)), int(max(h, 26 if self._compact else 30)))

    def minimumSizeHint(self) -> QSize:
        return self.sizeHint()

    # -- 事件：按下即反馈 -------------------------------------------------

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressed_state = True
            self._anim.to(b"scale", 0.965, SPRING_SNAPPY)
            self.update()
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self._pressed_state:
            self._pressed_state = False
            self._anim.to(b"scale", 1.0, SPRING_SNAPPY)
            self.update()
        super().mouseReleaseEvent(event)

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        if self._pressed_state:
            self._pressed_state = False
            self._anim.to(b"scale", 1.0, SPRING_SNAPPY)
        self.update()
        super().leaveEvent(event)

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._pressed_state = True
            self._anim.to(b"scale", 0.965, SPRING_SNAPPY)
            self.update()
        super().keyPressEvent(event)

    def keyReleaseEvent(self, event):
        if event.key() in (Qt.Key.Key_Space, Qt.Key.Key_Return, Qt.Key.Key_Enter):
            self._pressed_state = False
            self._anim.to(b"scale", 1.0, SPRING_SNAPPY)
            self.update()
        super().keyReleaseEvent(event)

    # -- 绘制 ------------------------------------------------------------

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        cx, cy = self.width() / 2.0, self.height() / 2.0
        painter.translate(cx, cy)
        painter.scale(self._scale, self._scale)
        painter.translate(-cx, -cy)

        rect = QRectF(self.rect()).adjusted(0.75, 0.75, -0.75, -0.75)
        accent = self._accent

        # ---- 底色按样式决定 -------------------------------------------
        if self._style == "filled":
            fill = QColor(accent)
            if self._pressed_state:
                fill = fill.darker(118)
            elif self._hover:
                fill = fill.lighter(112)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(fill))
            painter.drawPath(theme.squircle(rect, self._radius))
            paint_mini_specular(painter, rect, self._radius, 0.75)
            text_color = labels.on_accent
        elif self._style == "tinted":
            tint = QColor(accent)
            tint.setAlphaF(0.30 if self._pressed_state else (0.24 if self._hover else 0.17))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(tint))
            painter.drawPath(theme.squircle(rect, self._radius))
            paint_mini_specular(painter, rect, self._radius, 0.35)
            text_color = QColor(accent).lighter(135)
        elif self._style == "destructive":
            tint = theme.rgba(255, 69, 58, 0.30 if self._pressed_state else (0.24 if self._hover else 0.17))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(QBrush(tint))
            painter.drawPath(theme.squircle(rect, self._radius))
            paint_mini_specular(painter, rect, self._radius, 0.35)
            text_color = accents.red.lighter(130)
        else:  # plain
            mat = materials.control_pressed if self._pressed_state else (
                materials.control_hover if self._hover else materials.control)
            paint_glass(painter, rect, GlassStyle(
                material=mat, radius=self._radius, specular=1.0 if self._hover else 0.7,
                border=1.0, inner_gradient=1.0, grain=0.7,
            ))
            text_color = labels.label if self._hover else labels.secondary

        if self.hasFocus():
            painter.setPen(QPen(theme.rgba(accent.red(), accent.green(), accent.blue(), 0.75), 1.4))
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(theme.squircle(rect.adjusted(1, 1, -1, -1), self._radius - 1))

        # ---- 内容（文字或图标）----------------------------------------
        self._paint_content(painter, rect, text_color)
        painter.end()

    def _paint_content(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        """子类覆写此方法以改变按钮内容（默认画居中文字）。"""
        painter.setPen(QPen(color))
        painter.setFont(self._label_font())
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)


class GlassIconButton(GlassButton):
    """纯字形图标按钮（用 Unicode 字符做图标，用于窗口控制这类极简场景）。"""

    def __init__(self, glyph: str, parent: QWidget | None = None, *, accent: QColor | None = None,
                 size: int = 30, style: str = "plain", tooltip: str = ""):
        super().__init__(glyph, parent, accent=accent, style=style, radius=radii.control, compact=True)
        self._glyph = glyph
        self._size = size
        if tooltip:
            self.setToolTip(tooltip)
        self.setFixedSize(size, size)
        self._sig = type_scale.body.font()
        self._sig.setPointSizeF(size * 0.46)

    def sizeHint(self) -> QSize:
        return QSize(self._size, self._size)

    def _label_font(self) -> QFont:
        return self._sig


class GlassImageButton(GlassButton):
    """Twemoji 图标按钮 —— 走带控制、工具条用这个。

    图标本身是彩色的，所以底色只做极轻的一层；靠**形状与弹簧反馈**来统一，
    而不是把图标染成同一个颜色（那样会丢掉"可爱"这个核心观感）。
    """

    def __init__(
        self,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        accent: QColor | None = None,
        size: int = 32,
        icon_size: int | None = None,
        style: str = "plain",
        tooltip: str = "",
    ):
        super().__init__("", parent, accent=accent, style=style,
                         radius=radii.control, compact=True)
        self._icon_name = icon_name
        self._box = size
        self._icon_px = icon_size or int(round(size * 0.60))
        if tooltip:
            self.setToolTip(tooltip)
        else:
            try:
                from app import icons as _icons

                self.setToolTip(_icons.label(icon_name))
            except Exception:
                pass
        self.setFixedSize(size, size)
        self._dimmed = False

    def set_icon(self, icon_name: str) -> None:
        if icon_name == self._icon_name:
            return
        self._icon_name = icon_name
        self.update()

    def icon_name(self) -> str:
        return self._icon_name

    def set_dimmed(self, value: bool) -> None:
        self._dimmed = bool(value)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._box, self._box)

    def _label_font(self) -> QFont:
        f = type_scale.body.font()
        f.setPointSizeF(self._box * 0.4)
        return f

    def _paint_content(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        from app import icons

        opacity = 1.0
        if self._dimmed:
            # 未激活状态：压到 0.5 而不是更低 —— 0.35 时 Twemoji 的细笔画会糊成一团，
            # 用户既看不清图标是什么，也分不出"开着"和"关着"
            opacity = 0.5
        elif self._style == "plain" and not self._hover:
            opacity = 0.9
        pm = icons.pixmap(self._icon_name, self._icon_px, opacity=opacity,
                          dpr=self.devicePixelRatioF())
        w = pm.width() / max(1.0, pm.devicePixelRatio())
        h = pm.height() / max(1.0, pm.devicePixelRatio())
        painter.drawPixmap(
            QPointF(rect.center().x() - w / 2.0, rect.center().y() - h / 2.0), pm
        )




class GlassOrb(GlassButton):
    """**圆形**玻璃按钮 —— LumaGlass 那种"玻璃球"。

    为什么值得单独做一个：纯图标控件用圆形比用圆角方块更耐看，也更接近
    "实物按钮"的直觉（像游戏里的物品格）。它复用 :class:`GlassButton` 的全部
    交互（按下即反馈、弹簧缩放、悬停增亮），只把形状换成圆。

    圆形靠**半径 = 一半边长**实现，而 squircle 的 n=2 恰好退化成完美圆 ——
    不需要另写一套几何。

    注意：本类必须定义在 :class:`GlassButton` **之后**（Python 的类体在执行到
    基类名时就要解析它）。
    """

    def __init__(
        self,
        icon_name: str,
        parent: QWidget | None = None,
        *,
        accent: QColor | None = None,
        size: int = 40,
        icon_size: int | None = None,
        style: str = "plain",
        tooltip: str = "",
    ):
        super().__init__("", parent, accent=accent, style=style,
                         radius=size / 2.0, compact=True)
        self._icon_name = icon_name
        self._box = size
        self._icon_px = icon_size or int(round(size * 0.52))
        self._dimmed = False
        self.setFixedSize(size, size)
        if tooltip:
            self.setToolTip(tooltip)
        else:
            try:
                from app import icons as _icons

                self.setToolTip(_icons.label(icon_name))
            except Exception:
                pass

    def set_icon(self, icon_name: str) -> None:
        if icon_name != self._icon_name:
            self._icon_name = icon_name
            self.update()

    def icon_name(self) -> str:
        return self._icon_name

    def set_dimmed(self, value: bool) -> None:
        self._dimmed = bool(value)
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(self._box, self._box)

    def _label_font(self) -> QFont:
        f = type_scale.body.font()
        f.setPointSizeF(self._box * 0.4)
        return f

    def _paint_content(self, painter: QPainter, rect: QRectF, color: QColor) -> None:
        from app import icons

        opacity = 0.5 if self._dimmed else (1.0 if (self._style != "plain" or self._hover) else 0.92)
        pm = icons.pixmap(self._icon_name, self._icon_px, opacity=opacity,
                          dpr=self.devicePixelRatioF())
        w = pm.width() / max(1.0, pm.devicePixelRatio())
        h = pm.height() / max(1.0, pm.devicePixelRatio())
        painter.drawPixmap(
            QPointF(rect.center().x() - w / 2.0, rect.center().y() - h / 2.0), pm
        )


# ==========================================================================
# 分段控件
# ==========================================================================


class GlassSegmented(GlassSurface):
    """iOS 分段控件。选中态的"滑块"在段间以弹簧滑动 —— 它必须可中断。"""

    changed = Signal(int)

    def __init__(self, items: list[str], parent: QWidget | None = None, *, accent: QColor | None = None):
        super().__init__(parent)
        self._items = list(items)
        self._index = 0
        self._accent = accent or accents.blue
        self._hover_index = -1
        self._thumb = 0.0
        self._anim = animator(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(28)
        self.setMinimumWidth(28 * len(self._items) + 8)
        self._tracking = False

    def _get_thumb(self) -> float:
        return self._thumb

    def _set_thumb(self, v: float) -> None:
        self._thumb = v
        self.update()

    thumb = Property(float, _get_thumb, _set_thumb)

    def count(self) -> int:
        return len(self._items)

    def current_index(self) -> int:
        return self._index

    def set_current_index(self, index: int, animate: bool = True) -> None:
        index = max(0, min(len(self._items) - 1, index))
        if index == self._index and not animate:
            self._thumb = float(index)
            self.update()
            return
        self._index = index
        if animate:
            self._anim.to(b"thumb", float(index), SPRING_SNAPPY)
        else:
            self._thumb = float(index)
            self.update()
        self.changed.emit(index)

    def _seg_width(self) -> float:
        return max(1.0, (self.width() - 8.0) / max(1, len(self._items)))

    def _index_at(self, x: float) -> int:
        return max(0, min(len(self._items) - 1, int((x - 4.0) // self._seg_width())))

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self._tracking = True
            self.set_current_index(self._index_at(event.position().x()))

    def mouseReleaseEvent(self, event):
        self._tracking = False

    def mouseMoveEvent(self, event):
        idx = self._index_at(event.position().x())
        if idx != self._hover_index:
            self._hover_index = idx
            self.update()

    def leaveEvent(self, event):
        self._hover_index = -1
        self.update()

    def glass_style(self) -> GlassStyle:
        return GlassStyle(material=materials.control, radius=radii.control,
                          specular=0.55, border=0.8, inner_gradient=0.55, grain=0.5)

    def sizeHint(self) -> QSize:
        from PySide6.QtGui import QFontMetricsF

        fm = QFontMetricsF(type_scale.footnote.font())
        w = sum(fm.horizontalAdvance(t) + 20 for t in self._items) + 8
        return QSize(int(w), 28)

    def paintEvent(self, event):
        super().paintEvent(event)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        sw = self._seg_width()
        # 选中滑块
        thumb_rect = QRectF(4.0 + self._thumb * sw, 4.0, sw, self.height() - 8.0)
        paint_glass(painter, thumb_rect, GlassStyle(
            material=materials.control_selected, radius=radii.chip,
            specular=1.0, border=1.1, inner_gradient=0.5, grain=0.4,
        ))
        painter.setPen(QPen(theme.white(0.12), 1.0))
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.drawPath(theme.squircle(thumb_rect, radii.chip))

        # 文字
        painter.setFont(type_scale.footnote.font())
        for i, text in enumerate(self._items):
            r = QRectF(4.0 + i * sw, 0, sw, self.height())
            selected = i == self._index
            if selected:
                color = labels.label
            elif i == self._hover_index:
                color = labels.secondary
            else:
                color = labels.tertiary
            f = type_scale.footnote.font()
            f.setWeight(QFont.Weight.DemiBold if selected else QFont.Weight.Medium)
            painter.setFont(f)
            painter.setPen(QPen(color))
            painter.drawText(r, Qt.AlignmentFlag.AlignCenter, text)
        painter.end()


# ==========================================================================
# 芯片 / 标签
# ==========================================================================


class GlassChip(GlassSurface):
    """小标签。材质最轻的一层 —— 用来突出可交互/可切换的元素。"""

    clicked = Signal()
    toggled = Signal(bool)

    def __init__(self, text: str, parent: QWidget | None = None, *, accent: QColor | None = None,
                 checkable: bool = False, selected: bool = False):
        super().__init__(parent)
        self._text = text
        self._accent = accent or accents.blue
        self._checkable = checkable
        self._selected = selected
        self._hover = False
        self.setCursor(Qt.CursorShape.PointingHandCursor if checkable else Qt.CursorShape.ArrowCursor)
        self.setFixedHeight(22)

    def isChecked(self) -> bool:
        return self._selected

    def setChecked(self, value: bool) -> None:
        if value != self._selected:
            self._selected = value
            self.toggled.emit(value)
            self.update()

    def setText(self, text: str) -> None:
        self._text = text
        self.updateGeometry()
        self.update()

    def text(self) -> str:
        # 与 GlassButton 对齐：有 setText 就该有 text，
        # 否则调用方只能去摸 _text（测试里就这么撞过：'GlassChip' has no attribute 'text'）
        return self._text

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            if self._checkable:
                self.setChecked(not self._selected)
            self.clicked.emit()

    def sizeHint(self) -> QSize:
        from PySide6.QtGui import QFontMetricsF

        fm = QFontMetricsF(type_scale.caption1.font())
        return QSize(int(fm.horizontalAdvance(self._text) + 18), 22)

    def glass_style(self) -> GlassStyle:
        if self._selected:
            tint = QColor(self._accent)
            tint.setAlphaF(0.26)
            return GlassStyle(material=tint, radius=radii.chip, specular=0.9,
                              border=1.1, inner_gradient=0.5, grain=0.4,
                              bloom=QColor(self._accent))
        mat = materials.control_hover if self._hover else materials.control
        return GlassStyle(material=mat, radius=radii.chip, specular=0.6,
                          border=0.9, inner_gradient=0.6, grain=0.5)

    def paintEvent(self, event):
        super().paintEvent(event)
        if not self._text:
            return
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if self._selected:
            color = QColor(self._accent).lighter(140)
        elif self._hover:
            color = labels.label
        else:
            color = labels.secondary
        f = type_scale.caption1.font()
        f.setWeight(QFont.Weight.DemiBold)
        painter.setFont(f)
        painter.setPen(QPen(color))
        painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, self._text)
        painter.end()


# ==========================================================================
# 分隔
# ==========================================================================


class GlassDivider(QWidget):
    """极淡分隔线。

    文档明确反对在浮动界面下用硬 1px 分隔线 —— 所以这里只在**确实需要**
    结构分隔时使用，且透明度压到几乎不可见；内容滚动处应改用渐变遮罩。
    """

    def __init__(self, parent: QWidget | None = None, *, vertical: bool = False, inset: float = 0.0):
        super().__init__(parent)
        self._vertical = vertical
        self._inset = inset
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        if vertical:
            self.setFixedWidth(1)
        else:
            self.setFixedHeight(1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        r = QRectF(self.rect())
        if self._vertical:
            g = QLinearGradient(r.topLeft(), r.bottomLeft())
        else:
            g = QLinearGradient(r.topLeft(), r.topRight())
        g.setColorAt(0.0, theme.white(0.0))
        g.setColorAt(0.18, theme.white(0.085))
        g.setColorAt(0.82, theme.white(0.085))
        g.setColorAt(1.0, theme.white(0.0))
        painter.fillRect(r, QBrush(g))
        painter.end()


def make_label(text: str, style=None, color: QColor | None = None, parent: QWidget | None = None):
    """快速造一个遵循排版规范的标签。"""
    from PySide6.QtWidgets import QLabel

    style = style or type_scale.body
    lbl = QLabel(text, parent)
    lbl.setFont(style.font())
    lbl.setStyleSheet(f"color: rgba({(color or labels.secondary).red()},"
                      f"{(color or labels.secondary).green()},"
                      f"{(color or labels.secondary).blue()},"
                      f"{(color or labels.secondary).alphaF():.3f}); background: transparent;")
    return lbl


__all__ = [
    "GlassStyle", "paint_glass", "paint_mini_specular", "paint_bloom", "apply_shadow",
    "blur_pixmap", "capture_widget_pixmap", "capture_below",
    "GlassSurface", "GlassPanel", "GlassCard",
    "GlassButton", "GlassIconButton", "GlassImageButton", "GlassOrb",
    "GlassSegmented", "GlassChip", "GlassDivider",
    "make_label",
]
