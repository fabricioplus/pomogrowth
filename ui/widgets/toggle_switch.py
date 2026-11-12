"""Interruptor deslizante (switch) para encender/apagar opciones de un vistazo.

Qt Widgets no trae un switch nativo: QAbstractButton checkable con pill +
knob animado (mismo lenguaje visual que ProgressGradient: OutCubic, tokens).
"""
from PySide6.QtCore import Property, QEasingCurve, QPropertyAnimation, Qt
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QAbstractButton

from ui.theme.tokens import ThemeManager, tokens


class ToggleSwitch(QAbstractButton):
    """Switch on/off compacto. Usar isChecked()/setChecked() y la señal toggled."""

    WIDTH, HEIGHT = 34, 18

    def __init__(self, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedSize(self.WIDTH, self.HEIGHT)
        if tooltip:
            self.setToolTip(tooltip)
        self._knob_pos = 0.0  # 0 = apagado, 1 = encendido
        self._anim = QPropertyAnimation(self, b"knobPos", self)
        self._anim.setDuration(160)
        self._anim.setEasingCurve(QEasingCurve.OutCubic)
        self.toggled.connect(self._animate)
        ThemeManager.instance().theme_changed.connect(self.update)

    def get_knob_pos(self) -> float:
        return self._knob_pos

    def set_knob_pos(self, value: float) -> None:
        self._knob_pos = float(value)
        self.update()

    knobPos = Property(float, get_knob_pos, set_knob_pos)

    def setChecked(self, checked: bool) -> None:
        # Ajuste sin animación (estado inicial) si no hay cambio visual pendiente
        super().setChecked(checked)
        self._anim.stop()
        self._knob_pos = 1.0 if checked else 0.0
        self.update()

    def _animate(self, checked: bool) -> None:
        self._anim.stop()
        self._anim.setStartValue(self._knob_pos)
        self._anim.setEndValue(1.0 if checked else 0.0)
        self._anim.start()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self.rect().adjusted(1, 1, -1, -1)
        radius = r.height() / 2
        track = QColor(t.accent) if self.isChecked() else QColor(t.surface_active)
        if not self.isEnabled():
            track.setAlpha(110)
        p.setPen(Qt.NoPen)
        p.setBrush(track)
        p.drawRoundedRect(r, radius, radius)
        # Knob
        d = r.height() - 4
        x = r.left() + 2 + (r.width() - d - 4) * self._knob_pos
        knob = QColor(t.on_accent) if self.isChecked() else QColor(t.fg_dim)
        p.setBrush(knob)
        p.drawEllipse(int(x), r.top() + 2, d, d)
        p.end()
