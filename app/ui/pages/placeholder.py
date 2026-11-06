"""尚未实现的模块的占位页。

刻意不写成"Coming soon"那种敷衍的空页，而是把**这个模块将要做什么、为什么这样设计**
讲清楚 —— 占位页也应该是有信息量的，用户点进来不该觉得被骗。
"""

from __future__ import annotations

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QFont, QFontMetricsF, QPainter, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from app import icons, theme
from app.theme import accents, labels, radii, spacing, type_scale
from app.widgets.glass import GlassCard, GlassChip


class PlaceholderPage(QWidget):
    """带图标、说明与功能清单的占位页。"""

    def __init__(
        self,
        title: str,
        icon_name: str,
        summary: str,
        bullets: list[str],
        accent,
        round_label: str = "",
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._title = title
        self._icon = icon_name
        self._summary = summary
        self._bullets = bullets
        self._accent = accent
        self._round = round_label

        root = QVBoxLayout(self)
        root.setContentsMargins(spacing.md, spacing.sm, spacing.md, spacing.sm)
        root.addStretch(1)

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        card_w = min(560, self.width() - 2 * spacing.xl)
        card_h = 300
        rect = QRectF((self.width() - card_w) / 2.0, (self.height() - card_h) / 2.0,
                      card_w, card_h)

        from app.widgets.glass import GlassStyle, paint_glass

        paint_glass(painter, rect, GlassStyle(
            material=theme.materials.panel, radius=radii.panel,
            specular=1.0, border=1.0, inner_gradient=1.0, grain=0.8,
            bloom=self._accent,
        ))

        # 图标
        isz = 56
        pm = icons.pixmap(self._icon, isz, dpr=self.devicePixelRatioF())
        painter.drawPixmap(int(rect.center().x() - isz / 2), int(rect.top() + 30), pm)

        # 标题（大字号 + 负字距）
        f = type_scale.title1.font()
        painter.setFont(f)
        painter.setPen(QPen(labels.label))
        painter.drawText(QRectF(rect.left(), rect.top() + 96, rect.width(), 30),
                         Qt.AlignmentFlag.AlignCenter, self._title)

        if self._round:
            f2 = type_scale.caption1.font()
            painter.setFont(f2)
            fm = QFontMetricsF(f2)
            tw = fm.horizontalAdvance(self._round)
            chip = QRectF(rect.center().x() - tw / 2 - 10, rect.top() + 128, tw + 20, 20)
            c = self._accent
            painter.setPen(Qt.PenStyle.NoPen)
            tint = type(c)(c)
            tint.setAlphaF(0.22)
            painter.setBrush(tint)
            painter.drawRoundedRect(chip, radii.chip, radii.chip)
            painter.setPen(QPen(type(c)(c).lighter(140)))
            painter.drawText(chip, Qt.AlignmentFlag.AlignCenter, self._round)

        # 说明
        painter.setFont(type_scale.body.font())
        painter.setPen(QPen(labels.secondary))
        painter.drawText(
            QRectF(rect.left() + 36, rect.top() + 158, rect.width() - 72, 44),
            Qt.AlignmentFlag.AlignHCenter | Qt.AlignmentFlag.AlignTop | Qt.TextFlag.TextWordWrap,
            self._summary,
        )

        # 功能清单
        painter.setFont(type_scale.footnote.font())
        y = rect.top() + 208
        for b in self._bullets[:3]:
            painter.setPen(QPen(labels.tertiary))
            painter.drawText(QRectF(rect.left() + 46, y, rect.width() - 92, 18),
                             Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                             f"·  {b}")
            y += 20
        painter.end()


__all__ = ["PlaceholderPage"]
