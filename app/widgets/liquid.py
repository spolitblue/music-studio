"""液态玻璃 —— 边缘折射引擎。

普通"毛玻璃"只是把背景模糊一下，看起来像一块磨砂塑料。LumaGlass 那种**液态玻璃**
多了一道关键工序：**边缘折射** —— 玻璃的圆角边缘像一块厚透镜，把周围的画面往里
掰弯，于是边框附近出现一条明亮的、被挤压变形的光带。

实现原理
--------
1. 求圆角矩形的**有符号距离场**（SDF）：场里每一点到边界的距离，内部为负。
2. 对 SDF 求梯度得到**边缘法线**（指向外侧）。
3. 法线 × 一个"靠边越强"的权重 = **折射位移**。
4. 用这个位移对背景做**双线性重映射** —— 于是越靠边，采样点越往外偏，
   画面在边缘被拉出弧形的弯折。
5. 再叠一层边缘提亮与饱和度提升（玻璃边缘聚光）。

性能
----
位移图只跟尺寸/圆角/强度有关，与画面内容无关 → 按尺寸缓存，重绘时只需做一次
重映射。而重映射是纯 numpy 的 gather 运算，几百微秒级。
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPixmap


@dataclass(frozen=True)
class LiquidParams:
    """液态玻璃的外观参数。

    调参心得：``edge_width`` 决定形变带有多宽 —— 取太大（>30px）会变成鱼眼，
    整块面板都在扭曲；LumaGlass 那种"液态"是**紧贴边框的窄带**，所以取
    14~20px 配合较高的 ``bevel``，让形变集中在最外圈一两像素的可见范围内。
    """

    blur: float = 2.5           # 模糊半径（光面几乎不糊；磨砂取 16 左右）
    refraction: float = 13.0    # 折射位移上限（像素）
    edge_width: float = 18.0    # 边缘折射带宽度（像素）—— 窄一点才像玻璃而不是鱼眼
    saturate: float = 1.28      # 背景饱和度提升 —— 玻璃会"吃进"颜色并强化
    rim_gain: float = 0.17      # 边缘提亮
    brightness: float = 1.0
    bevel: float = 2.6          # 折射强度的边缘集中度：越大越像硬边透镜

    frost: float = 0.045
    """凝霜程度 0..1：把画面往浅色方向混合。

    这一项决定面板是"一块玻璃"还是"一个洞"。完全没有凝霜时，面板看起来就是
    背景被掰弯了而已；掺入一点点浅色后，面板才**比背景更亮**，层级关系才清楚，
    文字也才有可读性底座。**光面取很淡（0.045），磨砂取 0.17。**
    """

    frost_color: tuple[int, int, int] = (206, 206, 209)
    """凝霜的色相。**中性灰**，不偏暖也不偏冷。

    早先取的是偏冷的 (198,210,224)，结果是它和暖色壁纸一混就变成脏灰 ——
    冷暖相撞最容易出"灰蒙蒙"。中性灰不会跟任何背景打架。
    """

    interior_alpha: float = 0.30
    """面板**内部**的不透明度。

    这是"背景透明"的关键一档。若取 1.0，面板内部会把截下来的桌面**原样画一遍** ——
    看着像玻璃，实际是一张贴图，真实桌面根本透不过来，窗口也不是真透明。
    取小值后，内部让真实桌面透过（系统亚克力 + 自适应暗幕负责可读性），
    只有边缘那一圈画折射 —— 这正是"液态玻璃浮在透明窗口上"该有的样子。
    """

    waviness: float = 11.0
    """**表面不平整度**：全幅位移场的振幅（像素）。

    这是替代"压暗幕"来保证可读性的手段，也是本引擎最反直觉的一个参数：

    文字看不清的根源不是背景亮，而是背景里有**尖锐的高对比轮廓**在抢注意力。
    把玻璃表面做成起伏不平的，画面整体被揉出低频波纹 —— 轮廓被弯成认不出的
    柔和形状、亮斑被摊开削峰，文字于是自己浮出来，而玻璃**不需要变暗**。
    实测：waviness 从 0 提到 11，面板可以在更透明的同时把文字最差对比度
    从 2.5:1 拉到 5:1 以上。
    """

    waviness_scale: float = 62.0
    """波纹的波长（像素）。太大看不出起伏，太小会像噪点而非玻璃。"""

    @classmethod
    def from_finish(cls, finish) -> "LiquidParams":
        """按 :class:`app.theme.GlassFinish` 的表面处理档位构造参数。

        光面与磨砂共用同一套折射管线，差别只在模糊/凝霜/聚光/表面平整度几个量上，
        所以这里做一层映射，而不是让调用方记住两套数字。

        凝霜色也一并取过来：实时档（纯绘制路径）用的是同一个色值，
        两档必须共用同一个来源，否则切换档位时面板会变色。
        """
        return cls(
            blur=finish.blur,
            frost=finish.frost,
            saturate=finish.saturate,
            rim_gain=finish.rim_gain,
            bevel=finish.bevel,
            waviness=getattr(finish, "waviness", 0.0),
            interior_alpha=getattr(finish, "interior_alpha", 0.30),
            frost_color=getattr(finish, "frost_color", cls.frost_color),
        )


# --------------------------------------------------------------------------
# 几何：圆角矩形的有符号距离场
# --------------------------------------------------------------------------


@lru_cache(maxsize=64)
def _waviness_field(
    w: int, h: int, amplitude: float, scale: float, seed: int = 7717
) -> tuple[np.ndarray, np.ndarray]:
    """**表面不平整**产生的全幅位移场。

    与边缘折射的区别：边缘折射是"厚玻璃倒角把轮廓外的画面折进来"，
    只在边框一圈；这里是"整块玻璃表面起伏不平"，画面整体被揉出波纹。

    为什么它能解决可读性：文字读不清的根源不是背景"亮"，而是背景里有
    **尖锐的高对比轮廓**（图标边缘、文字、按钮）在和白字抢注意力。
    一个低频起伏的位移场会把这些轮廓弯成柔和、认不出的形状，同时把亮斑
    摊开、削峰 —— 于是**不必压暗幕**，文字自己就浮出来了。

    实现上叠几组波长随机、方向随机的正弦波。比逐像素噪声便宜得多，
    而且天然平滑，不会有颗粒感。
    """
    if amplitude < 0.05:
        return (np.zeros((h, w), dtype=np.float32), np.zeros((h, w), dtype=np.float32))

    rng = np.random.default_rng(seed)
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = np.arange(w, dtype=np.float32)[None, :]
    dx = np.zeros((h, w), dtype=np.float32)
    dy = np.zeros((h, w), dtype=np.float32)

    # 四组不同波长/方向的波叠加 —— 组数太少会看出规则的条纹
    for _ in range(4):
        ang = float(rng.random()) * 2.0 * np.pi
        wl = float(scale) * (0.7 + 0.7 * float(rng.random()))   # 波长抖动，避免重复感
        k = 2.0 * np.pi / max(4.0, wl)
        phase = float(rng.random()) * 2.0 * np.pi
        wave = np.sin(xs * (k * np.cos(ang)) + ys * (k * np.sin(ang)) + phase)
        # 位移方向垂直于波前，才像"表面被推挤"
        dx += (-np.sin(ang) * wave).astype(np.float32)
        dy += (np.cos(ang) * wave).astype(np.float32)

    peak = float(np.sqrt(dx * dx + dy * dy).max())
    if peak < 1e-6:
        return dx, dy
    s = amplitude / peak
    return (dx * s).astype(np.float32), (dy * s).astype(np.float32)


@lru_cache(maxsize=64)
def _sdf_rounded_box(w: int, h: int, radius: float) -> np.ndarray:
    """返回 ``(h, w)`` 的 SDF，内部为负、边界为 0、外部为正。"""
    return _sdf_placed(w, h, w, h, 0, 0, radius)


@lru_cache(maxsize=64)
def _sdf_placed(cw: int, ch: int, pw: int, ph: int, ox: int, oy: int,
                radius: float) -> np.ndarray:
    """把 ``pw × ph`` 的圆角矩形放在 ``cw × ch`` 画布的 ``(ox, oy)`` 处，求整张画布的 SDF。

    为什么要在**整张画布**上求：边缘折射必须能采到面板轮廓**之外**的像素，
    否则采样被钳制在面板边界内，效果就退化成"面板自己的画面被扭曲"，
    而不是真正的玻璃倒角折射。
    """
    r = max(0.0, min(radius, pw / 2.0, ph / 2.0))
    cx = ox + (pw - 1) / 2.0
    cy = oy + (ph - 1) / 2.0
    xs = np.arange(cw, dtype=np.float32) - cx
    ys = np.arange(ch, dtype=np.float32) - cy
    ax = np.abs(xs) - (pw / 2.0 - r)
    ay = np.abs(ys) - (ph / 2.0 - r)
    qx = np.maximum(ax[None, :], 0.0)
    qy = np.maximum(ay[:, None], 0.0)
    outside = np.sqrt(qx * qx + qy * qy)
    inside = np.minimum(np.maximum(ax[None, :], ay[:, None]), 0.0)
    return (outside + inside - r).astype(np.float32)


@lru_cache(maxsize=64)
def _refraction_maps(
    w: int, h: int, radius: float, strength: float, edge_width: float, bevel: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """面板占满画布时的折射图（``_refraction_maps_placed`` 的简写）。"""
    return _refraction_maps_placed(w, h, w, h, 0, 0, radius, strength, edge_width, bevel)


@lru_cache(maxsize=64)
def _refraction_maps_placed(
    cw: int, ch: int, pw: int, ph: int, ox: int, oy: int,
    radius: float, strength: float, edge_width: float, bevel: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 ``(dx, dy, rim)``：采样偏移图与边缘权重图（尺寸为整张画布）。

    * ``dx/dy`` 是"从哪儿取色"的偏移（正方向为右/下）。
    * ``rim`` ∈ [0,1]，面板边界处为 1，向内 ``edge_width`` 处衰减到 0。
    """
    sdf = _sdf_placed(cw, ch, pw, ph, ox, oy, radius)

    # 边缘权重：边界处 1，向内线性衰减后再取幂 —— 幂次让强度向边缘集中，
    # 形成"厚玻璃倒角"那种锐利的光带
    t = np.clip(1.0 + sdf / max(1.0, edge_width), 0.0, 1.0)
    rim = (t ** bevel).astype(np.float32)

    # 法线 = SDF 的梯度（axis0=行/y，axis1=列/x）
    gy, gx = np.gradient(sdf)
    norm = np.sqrt(gx * gx + gy * gy)
    norm[norm < 1e-6] = 1e-6
    nx = (gx / norm).astype(np.float32)
    ny = (gy / norm).astype(np.float32)

    # 向外采样 ⇒ 边框附近"吸"到轮廓外侧的画面，形成外凸的透镜感
    scale = rim * float(strength)
    dx = (nx * scale).astype(np.float32)
    dy = (ny * scale).astype(np.float32)
    return dx, dy, rim


# --------------------------------------------------------------------------
# 图像运算
# --------------------------------------------------------------------------


def _box1d(a: np.ndarray, r: int, axis: int) -> np.ndarray:
    """沿单轴盒式模糊（前缀和实现，耗时与半径无关）。"""
    if r < 1:
        return a
    n = a.shape[axis]
    pad = [(0, 0)] * a.ndim
    pad[axis] = (r + 1, r)
    p = np.pad(a, pad, mode="edge")
    c = np.cumsum(p, axis=axis)
    z = list(c.shape)
    z[axis] = 1
    c = np.concatenate([np.zeros(z, dtype=c.dtype), c], axis=axis)
    hi = [slice(None)] * a.ndim
    lo = [slice(None)] * a.ndim
    hi[axis] = slice(2 * r + 1, 2 * r + 1 + n)
    lo[axis] = slice(0, n)
    return (c[tuple(hi)] - c[tuple(lo)]) / float(2 * r + 1)


def blur_array(a: np.ndarray, radius: float, passes: int = 3) -> np.ndarray:
    """三次盒式模糊 ≈ 高斯。``a`` 为 (h, w, c) 的 float32。"""
    if radius < 0.5:
        return a
    r = max(1, int(round(radius / 2.0)))
    out = a
    for _ in range(passes):
        out = _box1d(out, r, axis=1)
        out = _box1d(out, r, axis=0)
    return out


def remap_bilinear(a: np.ndarray, dx: np.ndarray, dy: np.ndarray) -> np.ndarray:
    """按偏移图做双线性重映射。``a`` 为 (h, w, c)，``dx/dy`` 为 (h, w)。"""
    h, w = dx.shape
    ys = np.arange(h, dtype=np.float32)[:, None]
    xs = np.arange(w, dtype=np.float32)[None, :]
    sx = np.clip(xs + dx, 0.0, w - 1.0)
    sy = np.clip(ys + dy, 0.0, h - 1.0)

    x0 = np.floor(sx).astype(np.int32)
    y0 = np.floor(sy).astype(np.int32)
    x1 = np.minimum(x0 + 1, w - 1)
    y1 = np.minimum(y0 + 1, h - 1)
    fx = (sx - x0)[..., None]
    fy = (sy - y0)[..., None]

    a00 = a[y0, x0]
    a01 = a[y0, x1]
    a10 = a[y1, x0]
    a11 = a[y1, x1]
    top = a00 + (a01 - a00) * fx
    bot = a10 + (a11 - a10) * fx
    return top + (bot - top) * fy


def remap_rim_only(
    rgb: np.ndarray, dx: np.ndarray, dy: np.ndarray, rim: np.ndarray,
    band: int, panel: tuple[int, int, int, int],
) -> np.ndarray:
    """只对**面板边缘一圈**做重映射，内部直接沿用原图。

    这是整个液态玻璃引擎里最关键的性能优化。面板内部 ``rim = 0`` ⇒ 位移恒为 0
    ⇒ 重映射退化成恒等变换，算了也是白算。而边缘带的面积只有 ``周长 × 带宽``，
    对 1240×96 的面板是 4.8 万像素，而整块带余量的区域是 39 万像素 —— **八倍差距**。

    ``panel`` 是 ``(ox, oy, pw, ph)``：**边缘带必须贴着面板轮廓摆**，而不是贴着
    源图边界。源图是带余量的（每边几十像素），若按源图边界摆，四条带会全部落在
    面板之外 —— 裁出来的面板里一个被重映射的像素都没有，折射静默失效。
    """
    h, w = dx.shape
    ox, oy, pw, ph = panel
    b = max(1, min(int(band), ph // 2, pw // 2))
    out = rgb.copy()

    def do(y0: int, y1: int, x0: int, x1: int) -> None:
        """重映射 ``out`` 上的一块矩形。**读取走全图** —— 优化只省写入。"""
        y0, y1 = max(0, y0), min(h, y1)
        x0, x1 = max(0, x0), min(w, x1)
        if y1 <= y0 or x1 <= x0:
            return
        if not np.any(rim[y0:y1, x0:x1] > 1e-3):
            return

        ys = np.arange(y0, y1, dtype=np.float32)[:, None]
        xs = np.arange(x0, x1, dtype=np.float32)[None, :]
        sx = np.clip(xs + dx[y0:y1, x0:x1], 0.0, w - 1.0)
        sy = np.clip(ys + dy[y0:y1, x0:x1], 0.0, h - 1.0)

        ix0 = np.floor(sx).astype(np.int32)
        iy0 = np.floor(sy).astype(np.int32)
        ix1 = np.minimum(ix0 + 1, w - 1)
        iy1 = np.minimum(iy0 + 1, h - 1)
        fx = (sx - ix0)[..., None]
        fy = (sy - iy0)[..., None]

        # 注意：这里必须索引完整的 rgb。采样点会落到本条带之外
        # （那正是"从别处取色"的意思），只索引条带会越界。
        a00 = rgb[iy0, ix0]
        a01 = rgb[iy0, ix1]
        a10 = rgb[iy1, ix0]
        a11 = rgb[iy1, ix1]
        top = a00 + (a01 - a00) * fx
        bot = a10 + (a11 - a10) * fx
        out[y0:y1, x0:x1] = top + (bot - top) * fy

    x0p, x1p = ox, ox + pw
    y0p, y1p = oy, oy + ph
    do(y0p, y0p + b, x0p, x1p)          # 上带（贯穿整宽，含左上/右上角）
    do(y1p - b, y1p, x0p, x1p)          # 下带（贯穿整宽，含左下/右下角）
    do(y0p + b, y1p - b, x0p, x0p + b)  # 左带（去掉已处理过的上下角）
    do(y0p + b, y1p - b, x1p - b, x1p)  # 右带
    return out


def _qimage_to_array(img: QImage) -> np.ndarray:
    img = img.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    buf = img.constBits()
    return np.frombuffer(buf, dtype=np.uint8, count=w * h * 4).reshape(h, w, 4).astype(np.float32)


def _array_to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(np.clip(arr, 0, 255).astype(np.uint8))
    h, w = arr.shape[:2]
    return QImage(arr.data, w, h, w * 4, QImage.Format.Format_RGBA8888).copy()


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------


def _compose_liquid(
    src: np.ndarray, W: int, H: int, pw: int, ph: int, ox: int, oy: int,
    radius: float, params: LiquidParams, preblurred: bool = False,
) -> QPixmap:
    """把一整块带余量的背景合成为液态玻璃，最后裁出中心的面板区域。

    关键：**模糊与重映射都在整块上做**，这样边缘折射才吸得到轮廓外的画面；
    如果先裁再算，采样会被钳在面板内，效果退化成普通的内部扭曲。
    """
    # ---- 1. 模糊（整块）------------------------------------------------
    rgb = src[..., :3] if preblurred else blur_array(src[..., :3], params.blur)

    # ---- 2. 位移：边缘折射 + 表面不平整 ---------------------------------
    rim_full = np.zeros((H, W), dtype=np.float32)
    dx = dy = None

    if params.refraction > 0.05:
        dx, dy, rim_full = _refraction_maps_placed(
            W, H, pw, ph, ox, oy, radius,
            params.refraction, params.edge_width, params.bevel,
        )

    # 表面起伏：全幅都要位移，所以这一路会关掉"只重映射边缘"的优化
    if params.waviness > 0.05:
        wdx, wdy = _waviness_field(W, H, params.waviness, params.waviness_scale)
        dx = wdx if dx is None else (dx + wdx)
        dy = wdy if dy is None else (dy + wdy)

    if dx is not None:
        if params.waviness > 0.05:
            # 表面起伏是全幅的，整个面板都要重算
            band = ph
        else:
            # 只覆盖 rim 未衰减到 0 的范围 —— 也就是 edge_width。
            # 位移 dx = 法线 × rim × 强度，rim→0 处位移也→0，再往外就是恒等变换。
            # 早先取了 edge_width*3 + refraction（对 96px 高的走带条就是半块面板），
            # 白算了大量像素。
            band = max(6, int(params.edge_width) + 6)
        rgb = remap_rim_only(rgb, dx, dy, rim_full, band, (ox, oy, pw, ph))

    # ---- 3. 裁出面板 ---------------------------------------------------
    rgb = rgb[oy:oy + ph, ox:ox + pw]
    rim = rim_full[oy:oy + ph, ox:ox + pw]

    # ---- 4. 色彩：饱和度 + 凝霜 + 亮度 + 边缘聚光 ----------------------
    if abs(params.saturate - 1.0) > 1e-3:
        lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
        rgb = lum[..., None] + (rgb - lum[..., None]) * params.saturate
    if params.frost > 1e-4:
        # 往浅色方向掺 —— 面板因此比背景更亮更浊，读得出"这是一层材质"
        fr, fg, fb = params.frost_color
        fw = float(np.clip(params.frost, 0.0, 1.0))
        rgb = rgb * (1.0 - fw) + np.array([fr, fg, fb], dtype=np.float32) * fw
    if abs(params.brightness - 1.0) > 1e-3:
        rgb = rgb * params.brightness
    if params.rim_gain > 1e-4:
        rgb = rgb + rim[..., None] * (255.0 * params.rim_gain)

    out = np.empty((ph, pw, 4), dtype=np.float32)
    out[..., :3] = rgb
    # ---- 5. 透明度：内部透明、边缘不透明 -------------------------------
    # 边缘不透明是为了让"折射带"看得见；内部透明是为了让**真实桌面透过**。
    # 两者之间用 rim 平滑过渡，否则会在折射带内侧留一道生硬的接缝。
    interior = float(np.clip(params.interior_alpha, 0.0, 1.0))
    edge_a = np.clip(rim * 1.45, 0.0, 1.0)
    out[..., 3] = (interior + (1.0 - interior) * edge_a) * 255.0
    return QPixmap.fromImage(_array_to_qimage(out))


def liquid_fill(
    source: QPixmap,
    width: int,
    height: int,
    radius: float,
    params: LiquidParams | None = None,
    *,
    preblurred: bool = False,
) -> QPixmap:
    """把 ``source`` 变成一块 ``width × height`` 的液态玻璃填充。

    ``source`` 必须**比目标大一圈**（每边至少 ``blur + refraction + edge_width``
    像素），否则边缘折射无从取样 —— 那正是这个效果的全部意义所在。

    ``preblurred=True`` 表示 ``source`` 已经预先模糊过（比如窗口环境背景只糊一次
    然后反复裁切），此时跳过模糊步骤 —— 这是最大的一笔性能节省。

    性能说明：不在这里做结果缓存。位移图（几何）已按尺寸缓存，重映射只处理边缘
    一圈，1200×96 的面板约 10ms。但**每次重绘都算仍然太贵**，所以结果是交给
    控件缓存的（见 :class:`app.widgets.glass.GlassSurface`）。
    """
    params = params or LiquidParams()
    if source.isNull() or width < 4 or height < 4:
        pm = QPixmap(max(1, width), max(1, height))
        pm.fill(Qt.GlobalColor.transparent)
        return pm

    img = source.toImage().convertToFormat(QImage.Format.Format_RGBA8888)
    sw, sh = img.width(), img.height()
    if sw < width or sh < height:
        return source.scaled(width, height, Qt.AspectRatioMode.IgnoreAspectRatio,
                             Qt.TransformationMode.SmoothTransformation)

    src = _qimage_to_array(img)
    ox = (sw - width) // 2
    oy = (sh - height) // 2
    return _compose_liquid(src, sw, sh, width, height, ox, oy, radius, params, preblurred)


def clear_cache() -> None:
    _refraction_maps.cache_clear()
    _refraction_maps_placed.cache_clear()
    _sdf_rounded_box.cache_clear()
    _sdf_placed.cache_clear()


__all__ = [
    "LiquidParams", "liquid_fill", "blur_array", "remap_bilinear", "clear_cache",
]
