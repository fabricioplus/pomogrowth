"""Toasts no bloqueantes: entran deslizándose desde abajo a la derecha y se
desvanecen solos. Reemplazo de toast_notification de v8.

Dónde salen: en la esquina inferior derecha de su ventana, apilados. Sobre
una ventana pequeña o translúcida (la compacta) esa esquina son sus
controles, y el toast, siempre encima y con los clics para él, los tapaba:
ahí sale fuera de su tarjeta, debajo (o encima si abajo no cabe), recortado
a la pantalla.

show_*() devuelven el toast; dismiss() lo quita al momento, para que quien
avisa de lo mismo varias veces (la música) sustituya su aviso en vez de
apilar otro.
"""
from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                            QPropertyAnimation, QRect, Qt, QTimer)
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication
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
    # Ventana madre «pequeña» (la compacta mide 344×148; la principal, como
    # poco 460×520): el toast va fuera
    SMALL_W, SMALL_H = 480, 300

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
        self._slide_dy = 14  # entra subiendo; encima de la compacta, bajando
        self._place(parent_window)

        self.setWindowOpacity(0.0)
        self.show()
        start = self.pos() + QPoint(0, self._slide_dy)
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
        # Con el toast como contexto: si se quita antes (dismiss), Qt cancela
        # la llamada en vez de hacerla sobre un objeto destruido
        QTimer.singleShot(duration, self, self._fade_out)

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
        stack = sum(w.height() - 2 * w.PAD + self.GAP for w in _active_toasts)
        if self._goes_outside(parent_window):
            self._place_outside(parent_window, stack)
            return
        geo = parent_window.geometry()
        x = geo.right() - self.width() + self.PAD - self.MARGIN
        y = geo.bottom() - self.height() + self.PAD - self.MARGIN - stack
        self.move(x, y)

    def _goes_outside(self, parent_window) -> bool:
        """¿Sale fuera de su ventana? Sí si es pequeña (en los dos sentidos:
        la principal plegada es estrecha pero alta) o translúcida: su esquina
        inferior derecha son sus controles (la compacta)."""
        geo = parent_window.geometry()
        return (parent_window.testAttribute(Qt.WA_TranslucentBackground)
                or (geo.width() < self.SMALL_W and geo.height() < self.SMALL_H))

    @staticmethod
    def _visible_rect(window) -> QRect:
        """Lo que se ve de la ventana, en coordenadas de pantalla: su tarjeta
        (`card` en la compacta, `_card` en los diálogos) si la tiene; si no,
        la ventana entera."""
        for name in ("card", "_card"):
            card = getattr(window, name, None)
            if isinstance(card, QWidget):
                return QRect(card.mapToGlobal(QPoint(0, 0)), card.size())
        return window.geometry()

    def _place_outside(self, parent_window, stack: int) -> None:
        """Debajo de la tarjeta de su ventana, alineado a su derecha; encima
        si abajo no cabe en la pantalla. Recortado a la pantalla."""
        anchor = self._visible_rect(parent_window)
        card_w, card_h = self.width() - 2 * self.PAD, self.height() - 2 * self.PAD
        # La pantalla con funciones estáticas: en PySide6 el envoltorio que
        # devuelve QWidget.screen() pasa a depender del widget y, al
        # destruirse este (un diálogo cerrado, una compacta de un test), queda
        # inservible; en la suite rompía las screens() de los tests siguientes
        screen = (QGuiApplication.screenAt(anchor.center())
                  or QGuiApplication.primaryScreen())
        area = screen.availableGeometry() if screen is not None else None
        left = anchor.right() + 1 - card_w
        top = anchor.bottom() + 1 + self.GAP + stack        # debajo
        if area is not None and top + card_h - 1 > area.bottom():
            top = anchor.top() - self.GAP - card_h - stack  # encima
            self._slide_dy = -14  # que al entrar no pase por encima de la tarjeta
        if area is not None:
            left = max(area.left(), min(left, area.right() + 1 - card_w))
            top = max(area.top(), min(top, area.bottom() + 1 - card_h))
        self.move(left - self.PAD, top - self.PAD)

    def dismiss(self) -> None:
        """Lo quita ya, sin el fundido (lo sustituye otro aviso)."""
        if self in _active_toasts:
            _active_toasts.remove(self)
        self.hide()
        self.deleteLater()

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


def dismiss(toast) -> None:
    """Quita un toast si sigue en pantalla. Admite None o uno que Qt ya
    destruyó (terminó su fundido): entonces no hace nada."""
    if toast is None:
        return
    try:
        toast.dismiss()
    except RuntimeError:  # objeto C++ ya destruido por Qt
        pass


def show_success(parent_window, message: str, duration: int = 3000) -> Toast:
    return Toast(parent_window, message, tokens().accent, duration, icon="check-circle")


def show_info(parent_window, message: str, duration: int = 3000) -> Toast:
    return Toast(parent_window, message, tokens().info, duration, icon="info")


def show_warning(parent_window, message: str, duration: int = 3000) -> Toast:
    return Toast(parent_window, message, tokens().warning, duration, icon="triangle-alert")


def show_error(parent_window, message: str, duration: int = 3500) -> Toast:
    return Toast(parent_window, message, tokens().error, duration, icon="circle-x")
