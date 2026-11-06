"""窗口环境背景 —— 给玻璃"提供可折射的东西"。

为什么必须有这一层
------------------
液态玻璃的全部意义在于**它折射背后的画面**。如果窗口背景是一块纯色或一片
平滑渐变，那折射和不折射看起来一模一样 —— 玻璃就退化成了一块半透明色板。

LumaGlass 之所以好看，是因为它背后是细节丰富的游戏世界。我们没有那样的背景，
所以**自己造一个**：以深色渐变为底，叠一层散景点光（bokeh）、几条光带和噪点。
点光是最合适的选择 —— 它既有清晰的边界（折射看得出来），又足够柔和（不会抢戏）。

整张图**只渲染一次**并缓存，之后所有玻璃面板都从它上面按区域裁切。
"""

from __future__ import annotations

import math
import time
from functools import lru_cache

import numpy as np
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import (
    QBrush, QColor, QImage, QLinearGradient, QPainter, QPixmap, QPolygonF,
    QRadialGradient,
)

from app import theme

#: 散景点光的配色 —— 沿用 Twemoji 调色板，保证与环境一致
_BOKEH_COLORS: tuple[tuple[int, int, int], ...] = (
    (85, 172, 238),    # blue_light
    (146, 102, 204),   # purple
    (234, 89, 110),    # pink
    (255, 172, 51),    # amber
    (119, 178, 85),    # green
    (59, 136, 195),    # blue
    (244, 144, 12),    # orange
)


def _bokeh_points(w: int, h: int, count: int, seed: int = 20260214) -> list[tuple[float, float, float, QColor]]:
    rng = np.random.default_rng(seed)
    pts = []
    for _ in range(count):
        cx = float(rng.random()) * w
        cy = float(rng.random()) * h
        r = (0.055 + 0.16 * float(rng.random())) * max(w, h) * 0.55
        r, g, b = _BOKEH_COLORS[int(rng.integers(0, len(_BOKEH_COLORS)))]
        alpha = 0.10 + 0.20 * float(rng.random())
        c = QColor(r, g, b)
        c.setAlphaF(alpha)
        pts.append((cx, cy, r, c))
    return pts


def render_ambient(w: int, h: int, *, seed: int = 20260214) -> QPixmap:
    """渲染一张窗口尺寸的环境背景。"""
    w = max(64, int(w))
    h = max(64, int(h))

    img = QImage(w, h, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    # ---- 1. 底色：深蓝紫到近黑，带一点纵向变化 -------------------------
    base = QLinearGradient(0, 0, w * 0.35, h)
    base.setColorAt(0.0, QColor(22, 24, 40, 255))
    base.setColorAt(0.45, QColor(14, 15, 26, 255))
    base.setColorAt(1.0, QColor(9, 10, 17, 255))
    p.fillRect(0, 0, w, h, QBrush(base))

    # ---- 2. 散景点光 ---------------------------------------------------
    # 大而淡的一层先铺开，作为"背景虚化"的底
    for cx, cy, r, c in _bokeh_points(w, h, 14, seed):
        big = QColor(c)
        big.setAlphaF(c.alphaF() * 0.75)
        g = QRadialGradient(QPointF(cx, cy), r)
        g.setColorAt(0.0, big)
        mid = QColor(big)
        mid.setAlphaF(big.alphaF() * 0.55)
        g.setColorAt(0.55, mid)
        edge = QColor(big)
        edge.setAlphaF(0.0)
        g.setColorAt(1.0, edge)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), r, r)

    # 小而亮的一层：这些才是折射时"看得见弯曲"的关键，边界更明确
    rng = np.random.default_rng(seed + 1)
    for _ in range(22):
        cx = float(rng.random()) * w
        cy = float(rng.random()) * h
        r = (0.014 + 0.030 * float(rng.random())) * max(w, h)
        rr, gg, bb = _BOKEH_COLORS[int(rng.integers(0, len(_BOKEH_COLORS)))]
        c = QColor(rr, gg, bb)
        c.setAlphaF(0.30 + 0.34 * float(rng.random()))
        g = QRadialGradient(QPointF(cx, cy), r)
        g.setColorAt(0.0, c)
        inner = QColor(c)
        inner.setAlphaF(c.alphaF() * 0.86)
        g.setColorAt(0.62, inner)
        edge = QColor(c)
        edge.setAlphaF(0.0)
        g.setColorAt(1.0, edge)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        p.drawEllipse(QPointF(cx, cy), r, r)

    # ---- 3. 斜向光带：给画面一个方向感，避免构图散乱 -------------------
    for i, (y_frac, width_frac, color, alpha) in enumerate((
        (0.16, 0.30, QColor(85, 172, 238), 0.075),
        (0.52, 0.42, QColor(146, 102, 204), 0.060),
        (0.86, 0.34, QColor(234, 89, 110), 0.045),
    )):
        c = QColor(color)
        c.setAlphaF(alpha)
        band_h = h * width_frac
        y0 = h * y_frac
        g = QLinearGradient(0, y0 - band_h / 2, 0, y0 + band_h / 2)
        clear = QColor(color)
        clear.setAlphaF(0.0)
        g.setColorAt(0.0, clear)
        g.setColorAt(0.5, c)
        g.setColorAt(1.0, clear)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QBrush(g))
        # 轻微斜切，避免完全水平显得呆板。
        # 注意 PySide6 的 drawPolygon 不收可变参数，必须传 QPolygonF。
        skew = h * 0.10
        p.drawPolygon(QPolygonF([
            QPointF(0, y0 - band_h / 2 + skew),
            QPointF(w, y0 - band_h / 2 - skew),
            QPointF(w, y0 + band_h / 2 - skew),
            QPointF(0, y0 + band_h / 2 + skew),
        ]))

    p.end()

    # ---- 4. 噪点：打散渐变的"数字感" ----------------------------------
    a = np.frombuffer(img.constBits(), dtype=np.uint8, count=w * h * 4).reshape(h, w, 4).copy()
    rng = np.random.default_rng(seed + 7)
    noise = rng.integers(-4, 5, size=(h, w, 1), dtype=np.int16)
    a[..., :3] = np.clip(a[..., :3].astype(np.int16) + noise, 0, 255).astype(np.uint8)
    img = QImage(a.tobytes(), w, h, w * 4, QImage.Format.Format_ARGB32_Premultiplied).copy()

    return QPixmap.fromImage(img)


class AmbientSource:
    """玻璃面板的"可折射背景"来源。

    两种来源，优先用桌面实拍：

    * **桌面实拍**（默认）：把窗口背后的真实桌面截下来当背景。玻璃折射的就是
      用户自己的壁纸 —— 背景因此是**真透明**的，不是我们画的图。
    * **合成散景**（兜底）：截不到桌面时（无窗口句柄、权限受限、离屏测试）
      自绘一层散景点光，保证液态玻璃仍有东西可折射，而不是退化成一块色板。

    对外只有三个能力：按尺寸取图、按矩形裁（带余量）、取预模糊版。
    """

    MARGIN = 52
    """裁切时额外多留的余量（像素）。

    必须 >= ``blur + refraction + edge_width``（光面下 2.5+13+18 = 34），否则边缘
    折射取不到面板轮廓之外的像素。取太大会让带余量的整块面积暴涨 ——
    对"宽而扁"的面板尤其致命。
    """

    #: 截图时在窗口四周多留的余量（逻辑像素）。
    #: 移动窗口时只要仍在余量范围内，就只需**平移采样**、不必重新截图 ——
    #: 这是拖动时高频刷新成立的前提。
    CAPTURE_PAD = 260

    def __init__(self):
        self._pixmap: QPixmap | None = None
        self._blurred: QPixmap | None = None
        self._seed = 20260214
        self._blur_radius = 0.0
        #: 预模糊结果的**内容身份**（采样图 cacheKey + 半径）
        self._blur_key: tuple | None = None
        #: 预模糊结果的 1/step 缩小版（实时档折射用）
        self._blurred_small: QPixmap | None = None
        self._blur_small_key: tuple | None = None
        self._desktop: QPixmap | None = None
        #: 截图覆盖的**屏幕**矩形（用于把窗口相对坐标换算成图像坐标）
        self._capture_rect: QRect | None = None
        #: 窗口当前的屏幕位置
        self._window_pos: tuple[int, int] = (0, 0)
        #: 窗口尺寸（实时档的亮度探针要用它按降采样尺寸自绘背景）
        self._window_size: tuple[int, int] = (0, 0)
        #: 降采样亮度图（图像坐标，与窗口位置无关）
        self._lum_map = None
        self._lum_key: tuple | None = None
        self._lum_scale: tuple[float, float] = (1.0, 1.0)

    # -- 桌面实拍 --------------------------------------------------------

    def set_desktop_capture(self, pm: QPixmap | None, capture_rect: QRect | None = None) -> None:
        """注入一张桌面实拍。`capture_rect` 是它覆盖的**屏幕**矩形。"""
        self._desktop = pm
        self._capture_rect = capture_rect
        self.invalidate()

    def set_window_position(self, x: int, y: int) -> None:
        """告知窗口当前在屏幕上的位置 —— 采样时据此平移。"""
        if (x, y) != self._window_pos:
            self._window_pos = (int(x), int(y))
            # 只有"桌面实拍"的裁切结果才跟窗口位置有关。
            # 自绘兜底背景是**贴着窗口**画的（_to_image_rect 直接返回原矩形），
            # 清掉它纯属浪费 —— 那会让每次 move 都重新自绘一张背景图。
            if self.using_desktop:
                self._pixmap = None

    def set_window_size(self, size) -> None:
        """记录窗口尺寸 —— 实时档的亮度探针据此按降采样尺寸自绘兜底背景。"""
        w, h = int(size.width()), int(size.height())
        if (w, h) != self._window_size:
            self._window_size = (w, h)
            self._lum_key = None

    def covers(self, rect: QRect) -> bool:
        """截图是否仍覆盖窗口位置。

        移动时若还落在截图范围内 → 只需平移采样（不闪、不重算模糊）；
        移出去了 → 只能重新截图（会闪一下）。
        """
        if self._capture_rect is None:
            return not self.using_desktop
        return self._capture_rect.contains(rect)

    def _to_image_rect(self, rect: QRect) -> QRect:
        """窗口相对矩形 → 图像坐标矩形。

        桌面实拍时图像是**带余量**的截图，所以要减去截图原点、加上窗口位置；
        合成散景时图像就是窗口大小，直接对应。
        """
        if self._capture_rect is None:
            return rect
        wx, wy = self._window_pos
        return QRect(rect.x() + wx - self._capture_rect.x(),
                     rect.y() + wy - self._capture_rect.y(),
                     rect.width(), rect.height())

    @property
    def using_desktop(self) -> bool:
        return self._desktop is not None and not self._desktop.isNull()

    def ensure(self, size) -> QPixmap:
        """返回**采样用的图像**。

        桌面实拍：直接用它本身（带余量，不缩放 —— 缩放会让折射带的位置对不上）。
        兜底散景：按窗口尺寸生成。
        """
        if self.using_desktop:
            return self._desktop
        w = max(64, int(size.width()))
        h = max(64, int(size.height()))
        if self._pixmap is None or self._pixmap.width() != w or self._pixmap.height() != h:
            self._pixmap = render_ambient(w, h, seed=self._seed)
            self._blurred = None
        return self._pixmap

    def blurred(self, size, radius: float) -> QPixmap:
        """整张采样图**只模糊一次**，之后所有面板都从它上面裁。

        这是液态玻璃最关键的一笔性能节省：否则每个面板都要对带余量的区域
        重新模糊一遍，那是整个管线里最贵的一步（本机实测
        1220x800 一次约 87~200ms）。

        缓存**按内容认身份**（``QPixmap.cacheKey()`` + 半径），
        而不是靠外面记得调用 :meth:`invalidate`。这一点很要紧：
        窗口每次 resize 都会走 `_invalidate_backdrop()`，如果那里顺手
        `invalidate()` 一下就把模糊结果丢掉，启动期会白糊好几遍 ——
        实测过 27~63 次重糊、500~6500ms 都花在这上面。
        现在只有**采样图真的换了**（换壁纸 / 重拍桌面 / 换尺寸）才会重糊。

        拖动窗口时模糊结果**不需要重算** —— 桌面内容没变，只是采样窗口平移了。
        """
        pm = self.ensure(size)
        if pm is None or pm.isNull():
            return pm
        radius = float(radius)
        key = (pm.cacheKey(), round(radius, 2))
        if self._blurred is not None and self._blur_key == key:
            return self._blurred

        from app.widgets.liquid import blur_array

        img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
        w, h = img.width(), img.height()
        a = np.frombuffer(img.constBits(), dtype=np.uint8, count=w * h * 4) \
              .reshape(h, w, 4).astype(np.float32)
        a[..., :3] = blur_array(a[..., :3], radius)
        out = QImage(np.ascontiguousarray(np.clip(a, 0, 255).astype(np.uint8)).data,
                     w, h, w * 4, QImage.Format.Format_RGBA8888).copy()
        self._blurred = QPixmap.fromImage(out)
        self._blur_radius = radius
        self._blur_key = key
        return self._blurred

    def small_blurred(self, size, radius: float, step: int) -> QPixmap | None:
        """预模糊结果的 **1/step 版本** —— 整张只缩一次，供所有面板裁。

        为什么需要它：实时档的折射是降采样跑的（``refract_step``），
        但第一版是"每块面板各自裁全分辨率的图、再各自缩小"——
        等于把最贵的 blit 按全分辨率做了 9 遍，缩完丢掉 8/9 的像素。
        实测那一版拖动每步要 387ms（`test_glass_neutral.py` 直接判 FAIL）。

        正确的顺序是：**先把整张预模糊图缩一次**（一次开销，所有面板共用），
        再从小图上裁。裁切的像素量随之降到 1/step²。
        """
        base = self.blurred(size, radius)
        if base is None or base.isNull():
            return None
        step = max(1, int(step))
        key = (self._blur_key, step)
        if self._blurred_small is not None and self._blur_small_key == key:
            return self._blurred_small
        w = max(8, base.width() // step)
        h = max(8, base.height() // step)
        # 源图已经糊过，最近邻缩小看不出来，且比 SmoothTransformation 快得多
        self._blurred_small = base.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                                          Qt.TransformationMode.FastTransformation)
        self._blur_small_key = key
        return self._blurred_small

    def crop_small(self, rect: QRect, step: int, margin: int | None = None) -> QPixmap:
        """:meth:`crop_with_margin` 的降采样版：从 1/step 图上裁，坐标同步除以 step。"""
        pm = self._blurred_small
        if pm is None or pm.isNull():
            return QPixmap()
        step = max(1, int(step))
        m = max(2, (self.MARGIN if margin is None else int(margin)) // step)
        iw, ih = pm.width(), pm.height()

        r = self._to_image_rect(rect)
        r = QRect(r.x() // step, r.y() // step,
                  max(1, r.width() // step), max(1, r.height() // step))
        x0, y0 = r.x() - m, r.y() - m
        x1, y1 = r.x() + r.width() + m, r.y() + r.height() + m

        out = QPixmap(x1 - x0, y1 - y0)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.drawPixmap(-x0, -y0, pm)
        painter.end()
        if x0 < 0 or y0 < 0 or x1 > iw or y1 > ih:
            out = _extend_edges(out, x0, y0, iw, ih)
        return out

    @property
    def pixmap(self) -> QPixmap | None:
        return self._pixmap

    def crop_with_margin(self, rect: QRect, margin: int | None = None,
                         *, blurred: bool = False) -> QPixmap:
        """裁出 ``rect`` 周围带 ``margin`` 余量的区域。

        边缘折射要从面板**外面**取样，所以余量是必需品而不是优化。
        超出窗口范围的部分用边缘像素补齐（避免出现黑边被折射进来）。

        ``blurred=True`` 时从预模糊版本上裁 —— 供液态玻璃使用。
        """
        pm = self._blurred if (blurred and self._blurred is not None) else self._desktop or self._pixmap
        if pm is None or pm.isNull():
            return QPixmap()
        m = self.MARGIN if margin is None else int(margin)
        iw, ih = pm.width(), pm.height()

        # 窗口相对坐标 → 图像坐标（桌面实拍带余量，窗口一动采样窗口就平移）
        r = self._to_image_rect(rect)
        x0 = r.x() - m
        y0 = r.y() - m
        x1 = r.x() + r.width() + m
        y1 = r.y() + r.height() + m

        out = QPixmap(x1 - x0, y1 - y0)
        out.fill(Qt.GlobalColor.transparent)
        painter = QPainter(out)
        painter.drawPixmap(-x0, -y0, pm)
        painter.end()

        # 越界区域用最近边缘像素延展：否则透明的黑边会被当成背景折射进来
        if x0 < 0 or y0 < 0 or x1 > iw or y1 > ih:
            out = _extend_edges(out, x0, y0, iw, ih)
        return out

    def backdrop_luminance(self, rect: QRect) -> float:
        """返回 ``rect`` 区域背后画面的平均亮度（0..1）。

        玻璃面板靠这个值做**自适应暗幕**：背景亮就多压一层暗色，
        背景暗就保持通透。没有它，光面玻璃在亮壁纸上会直接把白字淹掉 ——
        而固定一层重暗幕又会让暗壁纸下的玻璃显得脏。

        注意：这是**全分辨率**版本，每次调用都要把整张采样图转成 RGBA8888，
        单次 5~15ms。交互路径上请用 :meth:`luminance_at`。
        """
        pm = self.ensure(QRect(0, 0, 1, 1).size())
        if pm is None or pm.isNull() or rect.isEmpty():
            return 0.35
        r = self._to_image_rect(rect)
        x0 = max(0, r.x())
        y0 = max(0, r.y())
        x1 = min(pm.width(), r.x() + r.width())
        y1 = min(pm.height(), r.y() + r.height())
        try:
            img = pm.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            w, h = img.width(), img.height()
            a = np.frombuffer(img.constBits(), dtype=np.uint8, count=w * h * 4) \
                  .reshape(h, w, 4)
            sub = a[y0:y1, x0:x1, :3].astype(np.float32)
            rel = (sub @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)) / 255.0
            # 取 75 分位而不是均值：文字的对比度取决于**最亮的那部分**背景，
            # 均值会被大片暗区拉低，导致亮斑上的字照样看不清。
            return float(np.percentile(rel, 75))
        except Exception:
            return 0.35

    # -- 廉价的亮度探针（实时档用）---------------------------------------

    def luminance_map(self, step: int | None = None) -> "np.ndarray | None":
        """整张采样图的**降采样亮度图**（float32，0..1）。

        为什么要单独做一张：自适应暗幕原本每次都要把全分辨率的采样图
        转成 RGBA8888 再切片求分位（5~15ms/次，9 块面板就是上百毫秒）。
        而"背后有多亮"这个信息根本不需要全分辨率 —— 降采样 12 倍之后
        整张桌面只有一百多像素宽，查表是微秒级的。

        **关键：这张图存的是图像坐标（截屏坐标），不是窗口坐标。**
        所以窗口在截屏范围内移动时，采样点只是换一个下标，
        完全不需要重算 —— 拖动才不会因为亮度探针而卡住。

        没有桌面实拍时（离屏测试、截屏失败），**按降采样尺寸自绘**一张小背景，
        绝不为了"背后有多亮"去生成一张全分辨率环境图。
        """
        from app import theme

        step = int(theme.glass_realtime.lum_step if step is None else step)
        step = max(1, step)

        small: QPixmap | None = None
        key: tuple | None = None

        if self.using_desktop:
            pm = self._desktop
            key = (pm.cacheKey(), step)
            if self._lum_key == key and self._lum_map is not None:
                return self._lum_map
            w = max(1, pm.width() // step)
            h = max(1, pm.height() // step)
            # 先让 Qt 缩到小图再做 numpy：缩放在 C++ 侧是按块合并的，
            # 比"先转全分辨率再按步长取样"快一个量级。
            small = pm.scaled(w, h, Qt.AspectRatioMode.IgnoreAspectRatio,
                              Qt.TransformationMode.SmoothTransformation)
        else:
            ww, hh = self._window_size
            if ww < 8 or hh < 8:
                return None
            sw = max(8, ww // step)
            sh = max(8, hh // step)
            key = ("synth", sw, sh, self._seed)
            if self._lum_key == key and self._lum_map is not None:
                return self._lum_map
            small = render_ambient(sw, sh, seed=self._seed)

        try:
            img = small.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
            iw, ih = img.width(), img.height()
            a = np.frombuffer(img.constBits(), dtype=np.uint8,
                              count=iw * ih * 4).reshape(ih, iw, 4)
            rel = (a[..., :3].astype(np.float32)
                   @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)) / 255.0
            self._lum_map = rel
            self._lum_key = key
            ref_w = self._desktop.width() if self.using_desktop else ww
            ref_h = self._desktop.height() if self.using_desktop else hh
            self._lum_scale = (iw / max(1, ref_w), ih / max(1, ref_h))
        except Exception:
            self._lum_map = None
            self._lum_key = None
        return self._lum_map

    def luminance_at(self, rect: QRect) -> float:
        """:meth:`backdrop_luminance` 的廉价版：查降采样图，无重算。

        语义（75 分位、图像坐标换算、越界钳制）与全分辨率版保持一致，
        只是分辨率低了 —— 对"要不要压暗幕"这个决策完全够用。
        """
        m = self.luminance_map()
        if m is None or rect.isEmpty():
            return 0.35
        r = self._to_image_rect(rect)
        sx, sy = self._lum_scale
        h, w = m.shape
        x0 = int(max(0, min(w - 1, r.x() * sx)))
        y0 = int(max(0, min(h - 1, r.y() * sy)))
        x1 = int(max(x0 + 1, min(w, (r.x() + r.width()) * sx)))
        y1 = int(max(y0 + 1, min(h, (r.y() + r.height()) * sy)))
        sub = m[y0:y1, x0:x1]
        if sub.size == 0:
            return 0.35
        try:
            return float(np.percentile(sub, 75))
        except Exception:
            return 0.35

    def invalidate(self) -> None:
        """全部失效（换壁纸/重拍桌面时用）。

        **不清 :attr:`_blurred`**：预模糊结果按内容身份（cacheKey + 半径）
        自证有效，采样图真换了的时候 cacheKey 自然对不上。
        这里若顺手把它置 None，启动期每次 resize 都会白糊一遍整窗
        （实测 27~63 次、500~6500ms 全耗在这上面 —— 那正是当初要靠
        80ms 节流兜住的东西）。
        """
        self._pixmap = None
        self._blur_radius = 0.0

    def invalidate_sampling(self) -> None:
        """只失效取样、保留预模糊结果。

        移动窗口时用这个：桌面内容根本没变，只是采样窗口平移了，
        预模糊那张图完全可以复用 —— 重算一次要几十到几百毫秒，拖动时耗不起。

        自绘兜底背景（没有桌面实拍时）**根本不用清**：它是贴着窗口画的，
        与窗口位置无关（`_to_image_rect` 对它恒等）。清掉只会让每次 move
        都重新 `render_ambient` 一张全窗口背景图，进而让
        `blurred()` 的 cacheKey 变化、把整窗模糊也重做一遍 ——
        实测启动期因此白糊 25 次、620ms。
        """
        if self.using_desktop:
            self._pixmap = None


def _extend_edges(pm: QPixmap, x0: int, y0: int, w: int, h: int) -> QPixmap:
    """把越界区域用边缘像素填充（edge-clamp）。

    ``pm`` 是裁切结果，``(x0, y0)`` 是它左上角在源图坐标里的位置，
    ``(w, h)`` 是源图尺寸。越界的那几条边用**最外侧一列/一行拉伸**补齐 ——
    否则透明的黑边会被当成背景折射进面板。

    **为什么不用 numpy 做**：第一版把整块裁切图转成 ndarray、按切片复制、
    再转回 QPixmap，实测单次 1.2ms —— 占掉整个实时档折射成本的近三成，
    而它做的事只是"把一条边拉长"。这里改成 4 次 `drawPixmap`：
    把 1 像素宽的源条拉伸到目标条带，正是 edge-clamp 的定义，
    而且全在 C++ 侧完成，实测 ~0.05ms。
    """
    OW, OH = pm.width(), pm.height()
    sx0, sy0 = max(0, x0), max(0, y0)
    sx1, sy1 = min(w, x0 + OW), min(h, y0 + OH)
    if sx1 <= sx0 or sy1 <= sy0:
        return pm

    # 合法的子块在裁切图里的位置
    ox0, oy0 = sx0 - x0, sy0 - y0
    sw, sh = sx1 - sx0, sy1 - sy0

    out = QPixmap(OW, OH)
    out.fill(Qt.GlobalColor.transparent)
    p = QPainter(out)
    p.drawPixmap(0, 0, pm)          # 先原样搬一份（避免读写同一张 QPixmap）
    if oy0 > 0:
        p.drawPixmap(QRect(ox0, 0, sw, oy0), pm, QRect(ox0, oy0, sw, 1))
    if oy0 + sh < OH:
        p.drawPixmap(QRect(ox0, oy0 + sh, sw, OH - (oy0 + sh)),
                     pm, QRect(ox0, oy0 + sh - 1, sw, 1))
    if ox0 > 0:
        p.drawPixmap(QRect(0, 0, ox0, OH), pm, QRect(ox0, 0, 1, OH))
    if ox0 + sw < OW:
        p.drawPixmap(QRect(ox0 + sw, 0, OW - (ox0 + sw), OH),
                     pm, QRect(ox0 + sw - 1, 0, 1, OH))
    p.end()
    return out


#: 全局单例 —— 一个窗口只需一张背景
ambient = AmbientSource()


def capture_desktop(window) -> bool:
    """把 ``window`` 背后的桌面截下来，作为可折射背景。

    难点在于**别拍到自己**：窗口就压在那块桌面上，直接截屏会把我们自己也照进去。
    做法是先把窗口设成全透明、让 DWM 重新合成一帧，再截屏，然后恢复。

    代价是这一瞬间窗口会"闪"一下，所以只在启动和移动/缩放停止后（防抖）调用，
    **绝不要放进逐帧路径**。
    """
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QApplication

    screen = QGuiApplication.primaryScreen()
    if screen is None or window is None:
        return False

    geo = window.frameGeometry()
    if geo.width() < 8 or geo.height() < 8:
        return False

    # 截一块**比窗口大**的区域。移动窗口时只要还落在这块里，就只需平移采样、
    # 不必重新截图 —— 而重新截图要把窗口透明化，会闪。这是拖动流畅的前提。
    pad = AmbientSource.CAPTURE_PAD
    sg = screen.geometry()
    cap_x = max(sg.x(), geo.x() - pad)
    cap_y = max(sg.y(), geo.y() - pad)
    cap_r = min(sg.x() + sg.width(), geo.x() + geo.width() + pad)
    cap_b = min(sg.y() + sg.height(), geo.y() + geo.height() + pad)
    cap_w, cap_h = cap_r - cap_x, cap_b - cap_y
    if cap_w < 8 or cap_h < 8:
        return False

    prev_opacity = window.windowOpacity()
    try:
        window.setWindowOpacity(0.0)
        # 必须让真实时间流逝：DWM 需要一次实际合成才会把"没有我们"的画面画出来，
        # 光靠 processEvents 空转是等不到的。
        for _ in range(3):
            QApplication.processEvents()
            time.sleep(0.02)
        pm = screen.grabWindow(0, cap_x, cap_y, cap_w, cap_h)
    except Exception:
        return False
    finally:
        try:
            window.setWindowOpacity(prev_opacity if prev_opacity > 0.01 else 1.0)
            QApplication.processEvents()
        except Exception:
            pass

    if pm.isNull():
        return False
    ambient.set_window_position(geo.x(), geo.y())
    ambient.set_desktop_capture(pm, QRect(cap_x, cap_y, cap_w, cap_h))
    return True


__all__ = ["AmbientSource", "ambient", "render_ambient", "capture_desktop"]
