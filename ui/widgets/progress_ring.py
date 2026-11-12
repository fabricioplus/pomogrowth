"""Anillo de progreso de la sesión con gradiente animado (menta→cian; azul→
violeta en descanso). Aloja dentro el contenido central del timer: el
progreso queda pegado a las cifras, no en una línea lejana arriba.

Como ProgressGradient: el valor SIEMPRE se anima y el color interpola
entre trabajo y descanso.
"""
from PySide6.QtCore import (Property, QEasingCurve, QPropertyAnimation, QRectF,
                            QVariantAnimation, Qt)
from PySide6.QtGui import QColor, QConicalGradient, QPainter, QPen
from PySide6.QtWidgets import QVBoxLayout, QWidget

from ui.theme.tokens import ThemeManager

# El arco empieza arriba (90°) y avanza en sentido horario. La costura del
# gradiente cónico se deja unos grados antes del inicio para que no la pise
# el remate redondeado del arco
_SEAM_DEG = 96.0


def _mix(c1: str, c2: str, f: float) -> QColor:
    a, b = QColor(c1), QColor(c2)
    return QColor(round(a.red() + (b.red() - a.red()) * f),
                  round(a.green() + (b.green() - a.green()) * f),
                  round(a.blue() + (b.blue() - a.blue()) * f))


class ProgressRing(QWidget):
    def __init__(self, diameter: int = 276, thickness: int = 10, parent=None):
        super().__init__(parent)
        self.setFixedSize(diameter, diameter)
        self._thickness = thickness
        self._fraction = 0.0
        self._break_blend = 0.0  # 0 = trabajo, 1 = descanso
        self._anim = QPropertyAnimation(self, b"fraction", self)
        self._anim.setDuration(240)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self._mode_anim = QVariantAnimation(self)
        self._mode_anim.setDuration(600)
        self._mode_anim.setEasingCurve(QEasingCurve.InOutCubic)
        self._mode_anim.valueChanged.connect(self._on_mode_value)
        # Contenido interior (modo, cifras, puntos del ciclo)
        self.inner = QVBoxLayout(self)
        pad = thickness + 22
        self.inner.setContentsMargins(pad, pad, pad, pad)
        self.inner.setSpacing(2)
        self.inner.setAlignment(Qt.AlignCenter)
        ThemeManager.instance().theme_changed.connect(self.update)

    # ---- valor animado ----
    def get_fraction(self) -> float:
        return self._fraction

    def set_fraction_raw(self, value: float) -> None:
        self._fraction = max(0.0, min(1.0, value))
        self.update()

    fraction = Property(float, get_fraction, set_fraction_raw)

    def set_fraction(self, value: float, animate: bool = True) -> None:
        value = max(0.0, min(1.0, value))
        self._anim.stop()
        if not animate or not self.isVisible():
            self.set_fraction_raw(value)
            return
        self._anim.setStartValue(self._fraction)
        self._anim.setEndValue(value)
        self._anim.start()

    def set_break(self, is_break: bool) -> None:
        self._mode_anim.stop()
        self._mode_anim.setStartValue(self._break_blend)
        self._mode_anim.setEndValue(1.0 if is_break else 0.0)
        self._mode_anim.start()

    def _on_mode_value(self, v) -> None:
        self._break_blend = float(v)
        self.update()

    # ---- pintado ----
    def paintEvent(self, event):
        t = ThemeManager.instance().current
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        th = self._thickness
        margin = th / 2 + 6  # sitio para el halo
        rect = QRectF(margin, margin, self.width() - 2 * margin,
                      self.height() - 2 * margin)

        # Carril
        track = QPen(QColor(t.surface_active), th)
        track.setCapStyle(Qt.FlatCap)
        p.setPen(track)
        p.drawEllipse(rect)

        if self._fraction > 0.0005:
            start = _mix(t.accent, t.break_accent, self._break_blend)
            end = _mix(t.accent_grad_end, t.break_grad_end, self._break_blend)
            top = (90.0 - _SEAM_DEG) / 360.0 % 1.0       # posición de las 12 en punto
            tail = max(0.0, top - self._fraction)
            grad = QConicalGradient(rect.center(), _SEAM_DEG)
            grad.setColorAt(0.0, end)
            grad.setColorAt(tail, end)
            grad.setColorAt(top, start)
            grad.setColorAt(1.0, start)
            span = -int(self._fraction * 360 * 16)       # horario
            # Halo suave bajo el arco
            glow_start, glow_end = QColor(start), QColor(end)
            glow_start.setAlpha(46)
            glow_end.setAlpha(46)
            glow = QConicalGradient(rect.center(), _SEAM_DEG)
            glow.setColorAt(0.0, glow_end)
            glow.setColorAt(tail, glow_end)
            glow.setColorAt(top, glow_start)
            glow.setColorAt(1.0, glow_start)
            halo = QPen(glow, th + 8)
            halo.setCapStyle(Qt.RoundCap)
            p.setPen(halo)
            p.drawArc(rect, 90 * 16, span)
            arc = QPen(grad, th)
            arc.setCapStyle(Qt.RoundCap)
            p.setPen(arc)
            p.drawArc(rect, 90 * 16, span)
        p.end()
