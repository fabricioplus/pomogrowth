"""Puntos del ciclo Pomodoro (●●○○) con micro-animación al completar uno."""
from PySide6.QtCore import QEasingCurve, QVariantAnimation, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QWidget

from ui.theme.tokens import ThemeManager


class CycleDots(QWidget):
    DOT = 8
    GAP = 10

    def __init__(self, parent=None):
        super().__init__(parent)
        self._total = 4
        self._filled = 0
        self._pop = 1.0  # escala del último punto rellenado
        self._pop_anim = QVariantAnimation(self)
        self._pop_anim.setDuration(420)
        self._pop_anim.setEasingCurve(QEasingCurve.OutBack)
        self._pop_anim.valueChanged.connect(self._on_pop)
        self.setFixedHeight(22)
        ThemeManager.instance().theme_changed.connect(self.update)

    def set_progress(self, filled: int, total: int) -> None:
        total = max(1, total)
        filled = max(0, min(filled, total))
        grew = filled > self._filled and total == self._total
        self._total, self._filled = total, filled
        self.setFixedWidth(total * self.DOT + (total - 1) * self.GAP + 8)
        if grew:
            self._pop_anim.stop()
            self._pop_anim.setStartValue(1.8)
            self._pop_anim.setEndValue(1.0)
            self._pop_anim.start()
        self.update()

    def _on_pop(self, v):
        self._pop = float(v)
        self.update()

    def paintEvent(self, event):
        t = ThemeManager.instance().current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        y = self.height() / 2
        x = self.DOT / 2 + 4
        for i in range(self._total):
            r = self.DOT / 2
            if i < self._filled:
                p.setBrush(QColor(t.accent))
                if i == self._filled - 1:
                    r *= self._pop
            else:
                empty = QColor(t.fg_muted)
                empty.setAlpha(110)
                p.setBrush(empty)
            p.drawEllipse(int(x - r), int(y - r), int(2 * r), int(2 * r))
            x += self.DOT + self.GAP
        p.end()
