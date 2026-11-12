"""Barra de progreso con gradiente animado (verde→cian) — la firma visual de PomoGrowth.

Reemplaza el canvas por-píxel de v8. El valor SIEMPRE se anima (nunca salta) y
el color del gradiente interpola entre trabajo y descanso.
"""
from PySide6.QtCore import (Property, QEasingCurve, QPropertyAnimation,
                            QVariantAnimation, Qt)
from PySide6.QtGui import QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import QWidget

from ui.theme.tokens import ThemeManager


class ProgressGradient(QWidget):
    """Barra horizontal delgada. set_fraction(0..1) anima; set_break_mode cambia paleta."""

    def __init__(self, height: int = 5, parent=None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self._fraction = 0.0
        self._break_blend = 0.0  # 0 = trabajo, 1 = descanso
        self._custom_colors = None  # (inicio, fin) fijos, ignoran tema y modo
        self._track_color = None    # carril propio (None = bg_header del tema)
        self._rounded = False
        self._anim = QPropertyAnimation(self, b"fraction", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._mode_anim = QVariantAnimation(self)
        self._mode_anim.setDuration(600)
        self._mode_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._mode_anim.valueChanged.connect(self._on_mode_value)
        ThemeManager.instance().theme_changed.connect(self.update)

    def get_fraction(self) -> float:
        return self._fraction

    def set_fraction_raw(self, value: float) -> None:
        self._fraction = max(0.0, min(1.0, value))
        self.update()

    fraction = Property(float, get_fraction, set_fraction_raw)

    def set_fraction(self, value: float, animate: bool = True) -> None:
        value = max(0.0, min(1.0, value))
        if not animate or not self.isVisible():
            self._anim.stop()
            self.set_fraction_raw(value)
            return
        self._anim.stop()
        self._anim.setStartValue(self._fraction)
        self._anim.setEndValue(value)
        self._anim.start()

    def set_custom_colors(self, start: str, end: str) -> None:
        """Fija un gradiente propio (p. ej. dorado para la barra de monedas/XP);
        anula la paleta del tema y el cambio trabajo/descanso."""
        self._custom_colors = (start, end)
        self.update()

    def set_track(self, color: str = None, rounded: bool = False) -> None:
        """Carril visible y extremos redondeados (p. ej. la XP sobre la
        titlebar, donde el carril por defecto se confundía con el fondo)."""
        self._track_color = color
        self._rounded = rounded
        self.update()

    def set_break_mode(self, is_break: bool) -> None:
        self._mode_anim.stop()
        self._mode_anim.setStartValue(self._break_blend)
        self._mode_anim.setEndValue(1.0 if is_break else 0.0)
        self._mode_anim.start()

    def _on_mode_value(self, v):
        self._break_blend = float(v)
        self.update()

    @staticmethod
    def _mix(c1: str, c2: str, f: float) -> QColor:
        a, b = QColor(c1), QColor(c2)
        return QColor(round(a.red() + (b.red() - a.red()) * f),
                      round(a.green() + (b.green() - a.green()) * f),
                      round(a.blue() + (b.blue() - a.blue()) * f))

    def paintEvent(self, event):
        t = ThemeManager.instance().current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect()
        radius = r.height() / 2 if self._rounded else 0
        # Fondo del carril
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(self._track_color or t.bg_header))
        p.drawRoundedRect(r, radius, radius)
        w = int(r.width() * self._fraction)
        if w > 0:
            if self._custom_colors:
                start, end = QColor(self._custom_colors[0]), QColor(self._custom_colors[1])
            else:
                start = self._mix(t.accent, t.break_accent, self._break_blend)
                end = self._mix(t.accent_grad_end, t.break_grad_end, self._break_blend)
            grad = QLinearGradient(0, 0, r.width(), 0)
            grad.setColorAt(0.0, start)
            grad.setColorAt(1.0, end)
            p.setBrush(grad)
            p.drawRoundedRect(0, 0, max(w, int(2 * radius)), r.height(), radius, radius)
        p.end()
