"""Toasts no bloqueantes: entran deslizándose desde abajo a la derecha y se
desvanecen solos. Reemplazo de toast_notification de v8."""
from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                            QPropertyAnimation, Qt, QTimer)
from PySide6.QtGui import QColor, QFontMetrics
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QWidget)

from ui.theme import fonts
from ui.theme.tokens import tokens
from ui.widgets.icon_button import tinted_icon

_active_toasts = []


class Toast(QWidget):
    MARGIN = 18     # distancia visible de la tarjeta al borde de la ventana
    PAD = 20        # margen transparente alrededor de la tarjeta (sombra)
    GAP = 8         # separación visible entre toasts apilados
    MAX_TEXT = 320  # a partir de aquí el mensaje pasa a varias líneas

    def __init__(self, parent_window, message: str, accent: str, duration: int,
                 icon: str = "info"):
        super().__init__(parent_window)
        t = tokens()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_ShowWithoutActivating)
        self.setAttribute(Qt.WA_TranslucentBackground)

        box = QFrame(self)
        box.setObjectName("toastBox")
        box.setStyleSheet(
            f"#toastBox {{ background: {t.surface}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}")
        shadow = QGraphicsDropShadowEffect(box)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 4)
        shadow.setColor(QColor(0, 0, 0, 100))
        box.setGraphicsEffect(shadow)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(self.PAD, self.PAD, self.PAD, self.PAD)
        lay.addWidget(box)
        inner = QHBoxLayout(box)
        inner.setContentsMargins(14, 11, 18, 11)
        inner.setSpacing(10)
        # Icono semántico (éxito, info, aviso, error) en su color
        icon_label = QLabel(box)
        icon_label.setPixmap(tinted_icon(icon, accent, 18).pixmap(18, 18))
        inner.addWidget(icon_label, 0, Qt.AlignTop)
        label = QLabel(message, box)
        label.setFont(fonts.font(fonts.SMALL))
        label.setStyleSheet(f"color: {t.fg}; background: transparent;")
        if QFontMetrics(label.font()).horizontalAdvance(message) > self.MAX_TEXT:
            label.setWordWrap(True)
            label.setFixedWidth(self.MAX_TEXT)
        inner.addWidget(label, 1, Qt.AlignVCenter)

        self.adjustSize()
        self._place(parent_window)

        self.setWindowOpacity(0.0)
        self.show()
        start = self.pos() + QPoint(0, 14)
        end = self.pos()
        self.move(start)
        group = QParallelAnimationGroup(self)
        slide = QPropertyAnimation(self, b"pos", self)
        slide.setDuration(260)
        slide.setStartValue(start)
        slide.setEndValue(end)
        slide.setEasingCurve(QEasingCurve.OutCubic)
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(260)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        group.addAnimation(slide)
        group.addAnimation(fade)
        group.start(QParallelAnimationGroup.DeleteWhenStopped)

        _active_toasts.append(self)
        QTimer.singleShot(duration, self._fade_out)

    @staticmethod
    def _is_alive(w) -> bool:
        try:
            return w.isVisible()
        except RuntimeError:  # objeto C++ ya destruido por Qt
            return False

    def _place(self, parent_window) -> None:
        # Apilar sobre otros toasts vivos, esquina inferior derecha de la
        # ventana. Las medidas descuentan el margen transparente de la sombra
        _active_toasts[:] = [w for w in _active_toasts if self._is_alive(w)]
        geo = parent_window.geometry()
        stack = sum(w.height() - 2 * w.PAD + self.GAP for w in _active_toasts)
        x = geo.right() - self.width() + self.PAD - self.MARGIN
        y = geo.bottom() - self.height() + self.PAD - self.MARGIN - stack
        self.move(x, y)

    def _fade_out(self) -> None:
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(300)
        fade.setStartValue(self.windowOpacity())
        fade.setEndValue(0.0)
        fade.finished.connect(self._done)
        fade.start(QPropertyAnimation.DeleteWhenStopped)

    def _done(self) -> None:
        if self in _active_toasts:
            _active_toasts.remove(self)
        self.close()
        self.deleteLater()


def show_success(parent_window, message: str, duration: int = 3000) -> None:
    Toast(parent_window, message, tokens().accent, duration, icon="check-circle")


def show_info(parent_window, message: str, duration: int = 3000) -> None:
    Toast(parent_window, message, tokens().info, duration, icon="info")


def show_warning(parent_window, message: str, duration: int = 3000) -> None:
    Toast(parent_window, message, tokens().warning, duration, icon="triangle-alert")


def show_error(parent_window, message: str, duration: int = 3500) -> None:
    Toast(parent_window, message, tokens().error, duration, icon="circle-x")
