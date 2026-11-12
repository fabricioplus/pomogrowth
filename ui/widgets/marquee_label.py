"""Label con desplazamiento horizontal para textos largos (port de MarqueeLabelV2).

Si el texto cabe, se pinta estático y centrado; si no, hace ping-pong suave
con pausas en los extremos.
"""
from PySide6.QtCore import QEasingCurve, Qt, QTimer, QVariantAnimation
from PySide6.QtGui import QColor, QFontMetrics, QLinearGradient, QPainter, QPixmap
from PySide6.QtWidgets import QWidget

from ui.theme.tokens import ThemeManager


class MarqueeLabel(QWidget):
    PAUSE_MS = 1200
    PX_PER_SEC = 40

    def __init__(self, text: str = "", parent=None, color: str = None):
        super().__init__(parent)
        self._text = text
        self._offset = 0.0
        self._color_override = color
        self._anim = QVariantAnimation(self)
        self._anim.setEasingCurve(QEasingCurve.InOutSine)
        self._anim.valueChanged.connect(self._on_value)
        self._anim.finished.connect(self._schedule_next)
        self._pause = QTimer(self)
        self._pause.setSingleShot(True)
        self._pause.timeout.connect(self._run_leg)
        self._direction = 1
        self.setMinimumHeight(24)
        ThemeManager.instance().theme_changed.connect(self.update)

    def set_text(self, text: str) -> None:
        if text == self._text:
            return
        self._text = text
        self._offset = 0.0
        self._direction = 1
        self._anim.stop()
        self._pause.stop()
        self._restart_if_needed()
        self.update()

    def set_color(self, color: str) -> None:
        self._color_override = color
        self.update()

    def _overflow(self) -> int:
        fm = QFontMetrics(self.font())
        return max(0, fm.horizontalAdvance(self._text) - self.width())

    def _restart_if_needed(self) -> None:
        if self._overflow() > 0 and self.isVisible():
            self._pause.start(self.PAUSE_MS)

    def _run_leg(self) -> None:
        over = self._overflow()
        if over <= 0:
            return
        target = float(over) if self._direction == 1 else 0.0
        dist = abs(target - self._offset)
        if dist < 1:
            self._direction *= -1
            self._pause.start(self.PAUSE_MS)
            return
        self._anim.setStartValue(self._offset)
        self._anim.setEndValue(target)
        self._anim.setDuration(int(dist / self.PX_PER_SEC * 1000))
        self._anim.start()

    def _schedule_next(self) -> None:
        self._direction *= -1
        self._pause.start(self.PAUSE_MS)

    def _on_value(self, v) -> None:
        self._offset = float(v)
        self.update()

    def showEvent(self, event):
        super().showEvent(event)
        self._restart_if_needed()

    def hideEvent(self, event):
        super().hideEvent(event)
        self._anim.stop()
        self._pause.stop()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._offset = 0.0
        self._anim.stop()
        self._restart_if_needed()

    def paintEvent(self, event):
        t = ThemeManager.instance().current
        color = self._color_override or t.fg
        fm = QFontMetrics(self.font())
        text_w = fm.horizontalAdvance(self._text)
        y = int((self.height() + fm.ascent() - fm.descent()) / 2)
        if text_w <= self.width():
            p = QPainter(self)
            p.setPen(color)
            p.setFont(self.font())
            p.drawText(int((self.width() - text_w) / 2), y, self._text)  # centrado si cabe
            p.end()
            return
        # Desplazándose: se pinta en un pixmap y se desvanecen los bordes (el
        # texto entra y sale suave en vez de cortar las letras por la mitad)
        dpr = self.devicePixelRatioF()
        pm = QPixmap(round(self.width() * dpr), round(self.height() * dpr))
        pm.setDevicePixelRatio(dpr)
        pm.fill(Qt.transparent)
        pp = QPainter(pm)
        pp.setPen(color)
        pp.setFont(self.font())
        pp.drawText(-int(self._offset), y, self._text)
        fade = min(24, self.width() // 6)
        pp.setCompositionMode(QPainter.CompositionMode_DestinationOut)
        for x0, x1 in ((0, fade), (self.width(), self.width() - fade)):
            grad = QLinearGradient(x0, 0, x1, 0)
            grad.setColorAt(0.0, QColor(0, 0, 0, 255))
            grad.setColorAt(1.0, QColor(0, 0, 0, 0))
            pp.fillRect(min(x0, x1), 0, fade, self.height(), grad)
        pp.end()
        p = QPainter(self)
        p.drawPixmap(0, 0, pm)
        p.end()
