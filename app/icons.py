"""图标系统 —— 直接渲染 Twemoji 矢量源。

图标风格基准来自工作区 `emojis/`（Twemoji v17.0.3），已由 `curate_icons.py`
策展 127 个进 ``assets/icons/``。选它的理由不只是好看：

* **真矢量**（36×36 viewBox）—— 任何 DPI、任何尺寸都锐利，高 DPI 屏不掉分。
* **形态圆润、色块扁平** —— 和 iOS 玻璃材质放一起不抢戏，反而把材质的通透衬出来。
* **自带完整调色板** —— 界面强调色直接从图标里取（见 :data:`app.theme.TWEMOJI`），
  图标与界面永远同色系，不会出现"图标是外来客"的割裂感。

本模块负责：加载 → 按设备像素比渲染 → 缓存 → 可选单色化/降饱和。
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPixmap

ICON_DIR = Path(__file__).resolve().parent.parent / "assets" / "icons"

#: 应用主图标：把两种素材合成一个"戴耳机的猫"式徽标
MASCOT = "cat"

_manifest: dict | None = None


def manifest() -> dict:
    global _manifest
    if _manifest is None:
        path = ICON_DIR / "manifest.json"
        _manifest = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"icons": {}}
    return _manifest


def has(name: str) -> bool:
    return (ICON_DIR / f"{name}.svg").exists()


def names() -> list[str]:
    return sorted(p.stem for p in ICON_DIR.glob("*.svg"))


def info(name: str) -> dict:
    return manifest().get("icons", {}).get(name, {})


def label(name: str) -> str:
    """图标的中文名（用于提示气泡与无障碍名称）。"""
    return info(name).get("zh") or name


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------


def _render_svg(name: str, px: int) -> QImage:
    """把 SVG 渲染成指定像素边长的 QImage（透明底）。"""
    from PySide6.QtSvg import QSvgRenderer

    path = ICON_DIR / f"{name}.svg"
    if not path.exists():
        return _placeholder(px)

    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        return _placeholder(px)

    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    # Twemoji 的图形撑满 36×36 画布，留一点内边距会更透气
    renderer.render(painter, QRectF(0, 0, px, px))
    painter.end()
    return img


def _placeholder(px: int) -> QImage:
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(255, 255, 255, 40))
    painter.drawEllipse(QRectF(px * 0.15, px * 0.15, px * 0.7, px * 0.7))
    painter.end()
    return img


@lru_cache(maxsize=512)
def _pixmap_cached(name: str, size: int, dpr_key: int, tint: int, opacity: float) -> QPixmap:
    dpr = dpr_key / 100.0
    px = max(1, int(round(size * dpr)))
    img = _render_svg(name, px)

    if tint or opacity < 1.0:
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        if tint:
            # SourceIn + 纯色填充 = 干净的剪影，用于单色场景
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceIn)
            painter.fillRect(img.rect(), QColor.fromRgba(tint))
        if opacity < 1.0:
            painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_DestinationIn)
            a = int(round(255 * max(0.0, min(1.0, opacity))))
            painter.fillRect(img.rect(), QColor(0, 0, 0, a))
        painter.end()

    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def pixmap(
    name: str,
    size: int = 24,
    *,
    tint: QColor | None = None,
    opacity: float = 1.0,
    dpr: float | None = None,
) -> QPixmap:
    """取图标位图。``size`` 是逻辑像素边长（高 DPI 自动按 dpr 放大渲染）。"""
    if dpr is None:
        dpr = _current_dpr()
    tint_key = tint.rgba() if isinstance(tint, QColor) else 0
    return _pixmap_cached(name, int(size), int(round(dpr * 100)), tint_key, round(opacity, 3))


@lru_cache(maxsize=256)
def _icon_cached(name: str, size: int, tint: int) -> QIcon:
    ico = QIcon()
    for dpr in (1.0, 1.25, 1.5, 2.0, 3.0):
        color = QColor.fromRgba(tint) if tint else None
        ico.addPixmap(_pixmap_cached(name, int(size), int(round(dpr * 100)), tint, 1.0))
    return ico


def icon(name: str, size: int = 24, tint: QColor | None = None) -> QIcon:
    return _icon_cached(name, int(size), tint.rgba() if isinstance(tint, QColor) else 0)


def _current_dpr() -> float:
    try:
        from PySide6.QtGui import QGuiApplication

        app = QGuiApplication.instance()
        if app is not None:
            screen = app.primaryScreen()
            if screen is not None:
                return float(screen.devicePixelRatio())
    except Exception:
        pass
    return 1.0


def clear_cache() -> None:
    _pixmap_cached.cache_clear()
    _icon_cached.cache_clear()


# --------------------------------------------------------------------------
# 合成图标
# --------------------------------------------------------------------------


def badge_pixmap(
    name: str,
    size: int = 28,
    *,
    bg: QColor | None = None,
    radius_ratio: float = 0.30,
    padding_ratio: float = 0.18,
    dpr: float | None = None,
) -> QPixmap:
    """把图标放进一个 iOS 连续曲率的圆角底里 —— 用来做工具栏按钮的"徽标"。

    图标本身是彩色的，所以底色只用极淡的一层；靠形状而非颜色来统一。
    """
    from app import theme

    dpr = dpr or _current_dpr()
    px = max(1, int(round(size * dpr)))
    img = QImage(px, px, QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    rect = QRectF(0, 0, px, px)
    if bg is not None:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(bg)
        painter.drawPath(theme.squircle(rect, px * radius_ratio))

    inner = max(1, int(px * (1.0 - padding_ratio * 2)))
    icon_img = _render_svg(name, inner)
    painter.drawImage(int((px - inner) / 2), int((px - inner) / 2), icon_img)
    painter.end()

    pm = QPixmap.fromImage(img)
    pm.setDevicePixelRatio(dpr)
    return pm


def app_icon(size: int = 256) -> QIcon:
    """应用图标：圆角玻璃底 + 吉祥物 + 右下角音符徽章。

    自己合成而不是直接拿现成 emoji —— 这样图标才真正属于这个软件。
    """
    from app import theme

    ico = QIcon()
    for s in (16, 32, 48, 64, 128, 256):
        img = QImage(s, s, QImage.Format.Format_ARGB32_Premultiplied)
        img.fill(Qt.GlobalColor.transparent)
        painter = QPainter(img)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(0, 0, s, s)
        # 玻璃底
        painter.setPen(Qt.PenStyle.NoPen)
        from PySide6.QtGui import QLinearGradient

        g = QLinearGradient(rect.topLeft(), rect.bottomRight())
        g.setColorAt(0.0, QColor("#4A9BD8"))
        g.setColorAt(1.0, QColor("#2C6E9E"))
        painter.setBrush(g)
        painter.drawPath(theme.squircle(rect, s * 0.235))
        # 顶部高光
        hl = QLinearGradient(rect.topLeft(), rect.topRight())
        hl.setColorAt(0.0, QColor(255, 255, 255, 0))
        hl.setColorAt(0.5, QColor(255, 255, 255, 150))
        hl.setColorAt(1.0, QColor(255, 255, 255, 0))
        painter.setBrush(hl)
        painter.drawRect(QRectF(0, 0, s, max(1.0, s * 0.06)))

        # 吉祥物
        inner = max(1, int(s * 0.62))
        mascot_img = _render_svg(MASCOT, inner)
        painter.drawImage(int(s * 0.19), int(s * 0.11), mascot_img)

        # 右下角音符徽章
        badge = max(4, int(s * 0.42))
        note = _render_svg("note", badge)
        bx, by = int(s * 0.54), int(s * 0.56)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(255, 255, 255, 235))
        painter.drawEllipse(QRectF(bx - 1, by - 1, badge + 2, badge + 2))
        painter.drawImage(bx, by, note)
        painter.end()

        ico.addPixmap(QPixmap.fromImage(img))
    return ico


__all__ = [
    "ICON_DIR", "MASCOT", "manifest", "has", "names", "info", "label",
    "pixmap", "icon", "badge_pixmap", "app_icon", "clear_cache",
]
