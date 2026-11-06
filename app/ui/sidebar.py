"""侧边栏导航。

按"材质重量传达层级"的规则，侧边栏是**最厚重**的一层结构材质：它的不透明度
最低、几乎没有高光，安安静静地待在左边；而里面的选中项才是轻而亮的控件材质。
这样一眼就能看出"哪是本体的结构，哪是我能点的东西"。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, QSize, Qt, Property, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QSizePolicy, QVBoxLayout, QWidget

from app import icons, theme
from app.motion import SPRING_SNAPPY, animator
from app.theme import accents, labels, materials, radii, spacing, type_scale
from app.widgets.glass import (
    GlassDivider, GlassPanel, GlassStyle, paint_glass, paint_mini_specular,
)


class NavItem(QWidget):
    """一个导航项。选中态用强调色晕染 + 位置弹簧。"""

    activated = Signal(str)

    HEIGHT = 36

    def __init__(self, key: str, label: str, icon_name: str,
                 accent: QColor | None = None, parent: QWidget | None = None):
        super().__init__(parent)
        self.key = key
        self._label = label
        self._icon_name = icon_name
        self._accent = accent or accents.blue
        self._selected = False
        self._hover = False
        self._glow = 0.0
        self._anim = animator(self)

        self.setFixedHeight(self.HEIGHT)
        self.setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self.setToolTip(label)

    # -- 可动画属性：选中光晕 --------------------------------------------

    def _get_glow(self) -> float:
        return self._glow

    def _set_glow(self, v: float) -> None:
        self._glow = v
        self.update()

    glow = Property(float, _get_glow, _set_glow)

    # -- 状态 ------------------------------------------------------------

    def set_selected(self, value: bool) -> None:
        if value == self._selected:
            return
        self._selected = value
        self._anim.to(b"glow", 1.0 if value else 0.0, SPRING_SNAPPY)
        self.update()

    @property
    def selected(self) -> bool:
        return self._selected

    def set_accent(self, color: QColor) -> None:
        self._accent = color
        self.update()

    # -- 事件 ------------------------------------------------------------

    def enterEvent(self, event):
        self._hover = True
        self.update()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._hover = False
        self.update()
        super().leaveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.activated.emit(self.key)

    # -- 绘制 ------------------------------------------------------------

    def sizeHint(self) -> QSize:
        return QSize(170, self.HEIGHT)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        rect = QRectF(self.rect()).adjusted(6.0, 1.0, -6.0, -1.0)
        radius = radii.control

        # 悬停底
        if self._hover and self._glow < 0.02:
            paint_glass(painter, rect, GlassStyle(
                material=materials.control_pressed, radius=radius, specular=0.5,
                border=0.7, inner_gradient=0.5, grain=0.4,
            ))

        # 选中底：强调色晕染，强度由弹簧驱动（可中断的"具象化"出现）
        if self._glow > 0.01:
            tint = QColor(self._accent)
            tint.setAlphaF(0.16 * self._glow)
            paint_glass(painter, rect, GlassStyle(
                material=tint, radius=radius, specular=0.85 * self._glow,
                border=0.9 * self._glow, inner_gradient=0.4, grain=0.35,
                bloom=QColor(self._accent),
            ))
            # 左侧强调条 —— 比整块染色更克制，也不吃可读性
            bar = QRectF(rect.x() + 1.5, rect.y() + 8.0 * (1.0 - self._glow) + 1.0,
                         2.5, rect.height() - 2.0 - 16.0 * (1.0 - self._glow))
            painter.setPen(Qt.PenStyle.NoPen)
            c = QColor(self._accent)
            c.setAlphaF(0.95 * self._glow)
            painter.setBrush(c)
            painter.drawRoundedRect(bar, 1.25, 1.25)

        # 图标：Twemoji 本身是彩色的，选中时轻微放大更像"活"起来
        isz = 20
        icon_opacity = 1.0 if self._selected else (0.82 if self._hover else 0.62)
        pm = icons.pixmap(self._icon_name, isz, opacity=icon_opacity,
                          dpr=self.devicePixelRatioF())
        ix = rect.x() + 10
        iy = rect.y() + (rect.height() - isz) / 2.0
        painter.drawPixmap(int(ix), int(iy), pm)

        # 文字 —— 活力效果：玻璃面上不用纯灰，靠字重与对比撑可读性
        f = type_scale.subhead.font()
        f.setWeight(QFont.Weight.DemiBold if self._selected else QFont.Weight.Medium)
        painter.setFont(f)
        if self._selected:
            color = labels.label
        elif self._hover:
            color = labels.secondary
        else:
            color = labels.tertiary
        painter.setPen(QPen(color))
        tx = ix + isz + 10
        painter.drawText(
            QRectF(tx, rect.y(), max(0.0, rect.right() - tx - 8), rect.height()),
            Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft,
            self._label,
        )
        painter.end()


class Sidebar(GlassPanel):
    """导航容器。"""

    navigated = Signal(str)

    WIDTH = 178

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent, material=theme.black(0.22),
                         radius=radii.panel, liquid=True)
        self.setFixedWidth(self.WIDTH)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)

        self._items: dict[str, NavItem] = {}
        self._order: list[str] = []
        self._compact = False

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 10, 6, 10)
        lay.setSpacing(2)
        self._layout = lay

        self._section_gap = spacing.sm

    # -- 构建 ------------------------------------------------------------

    def add_section(self, title: str) -> None:
        """插入一个小节标题。

        iOS 的做法是"大写 + 拉开字距 + 极小字号 + 低对比"。但低对比在玻璃上很容易
        低到看不见 —— 实测用 quaternary（0.26）几乎消失，所以取 tertiary（0.42）
        并额外拉开字距，靠字形而不是靠亮度来区分层级。
        """
        from PySide6.QtWidgets import QLabel

        lbl = QLabel(title, self)
        f = type_scale.caption2.font()
        f.setCapitalization(QFont.Capitalization.AllUppercase)
        f.setWeight(QFont.Weight.DemiBold)
        f.setLetterSpacing(QFont.SpacingType.AbsoluteSpacing, 1.1)
        lbl.setFont(f)
        c = labels.tertiary
        lbl.setStyleSheet(
            f"color: rgba({c.red()},{c.green()},{c.blue()},{c.alphaF():.3f});"
            "background: transparent; padding: 12px 0 4px 12px;"
        )
        self._layout.addWidget(lbl)

    def add_item(self, key: str, label: str, icon_name: str,
                 accent: QColor | None = None) -> NavItem:
        item = NavItem(key, label, icon_name, accent, self)
        item.activated.connect(self.navigated)
        self._layout.addWidget(item)
        self._items[key] = item
        self._order.append(key)
        return item

    def add_stretch(self) -> None:
        self._layout.addStretch(1)

    def add_divider(self) -> None:
        d = GlassDivider(self)
        wrapper = QWidget(self)
        wrapper.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        wl = QVBoxLayout(wrapper)
        wl.setContentsMargins(14, 8, 14, 8)
        wl.addWidget(d)
        self._layout.addWidget(wrapper)

    # -- 状态 ------------------------------------------------------------

    def set_current(self, key: str) -> None:
        for k, item in self._items.items():
            item.set_selected(k == key)

    def current(self) -> str | None:
        for k, item in self._items.items():
            if item.selected:
                return k
        return None

    def item(self, key: str) -> NavItem | None:
        return self._items.get(key)


__all__ = ["Sidebar", "NavItem"]
