"""Contador discreto de la racha de uso (días seguidos abriendo PomoGrowth):
llama + «12 días seguidos», con la mejor racha en el tooltip. Va en la
primera pantalla del día: la Bienvenida y «Planifica tu día»."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel

from ui.greeting import streak_text, streak_tooltip
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon

ICON_SIZE = 13


class StreakBadge(QFrame):
    def __init__(self, current: int, best: int, parent=None):
        super().__init__(parent)
        self.setObjectName("streakBadge")
        self.current, self.best = int(current), max(int(best), int(current))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 3, 9, 3)
        lay.setSpacing(5)
        self._icon = QLabel(self)
        self._icon.setFixedSize(ICON_SIZE, ICON_SIZE)
        lay.addWidget(self._icon, 0, Qt.AlignVCenter)
        self.label = QLabel(streak_text(self.current), self)
        self.label.setFont(fonts.font(fonts.XS))
        lay.addWidget(self.label, 0, Qt.AlignVCenter)
        self.setToolTip(streak_tooltip(self.best))
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def text(self) -> str:
        return self.label.text()

    def _retheme(self) -> None:
        t = tokens()
        c = QColor(t.warning)
        self._icon.setPixmap(tinted_icon("flame", t.warning, ICON_SIZE)
                             .pixmap(ICON_SIZE, ICON_SIZE))
        self.label.setStyleSheet(f"color: {t.fg_dim}; background: transparent;")
        self.setStyleSheet(f"#streakBadge {{ background: rgba({c.red()}, {c.green()},"
                           f" {c.blue()}, 26); border: none;"
                           f" border-radius: {t.radius_small}px; }}")
