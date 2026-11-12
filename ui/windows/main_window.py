"""Ventana principal frameless de PomoGrowth v9.

Drag: TitleBar → startSystemMove (Aero Snap funciona).
Resize: bordes de 6 px → startSystemResize.
Esquinas redondeadas: DWM (utils/win_native).
Minimizar / ocultar / mostrar con desvanecido: una ventana sin marco no tiene
las animaciones de Windows y el cambio era brusco.
Alt+F4: pasa por `close_guard` (el controller pregunta si salir, como la X).
"""
import ctypes
from ctypes import wintypes

from PySide6.QtCore import QEasingCurve, QEvent, QPropertyAnimation, QRect, Qt, QTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QHBoxLayout, QMainWindow, QSplitter,
                               QVBoxLayout, QWidget)

from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.progress_gradient import ProgressGradient
from ui.widgets.title_bar import TitleBar
from utils.win_native import round_corners

RESIZE_MARGIN = 6
SPLITTER_HANDLE = 7
# Ancho mínimo con y sin el panel de tareas. Plegado manda la cabecera: logo,
# cinco acciones (con «Música») y la cápsula de ventana (minimizar, compacto
# y cerrar) piden ~424 px con el nombre y la barra de XP recogidos (~446 con
# el compacto entre las acciones). Con el panel abierto, el timer conserva su
# ancho de plegado y las tareas necesitan ~430 px (la fila de filtros es de
# ancho fijo)
MIN_WIDTH, MIN_WIDTH_COLLAPSED, MIN_HEIGHT = 900, 460, 520
# Ancho del timer, siempre el mismo: la ventana plegada encaja justo en él y,
# al abrir las tareas, la ventana crece por su lado sin ensancharlo
TIMER_WIDTH = MIN_WIDTH_COLLAPSED - 2 * RESIZE_MARGIN
TASKS_MIN_WIDTH = MIN_WIDTH - 2 * RESIZE_MARGIN - SPLITTER_HANDLE - TIMER_WIDTH
# Una geometría guardada más estrecha (de cuando el mínimo era 420) se
# ensancha hasta el mínimo en vez de descartarse
_OLD_MIN_WIDTH = 420
FADE_OUT_MS, FADE_IN_MS = 170, 200
_WM_SYSCOMMAND, _SC_RESTORE = 0x0112, 0xF120


class MainWindow(QMainWindow):
    def __init__(self, settings: dict):
        super().__init__()
        t = tokens()
        self.setWindowFlag(Qt.FramelessWindowHint, True)
        self.setWindowTitle("PomoGrowth")
        collapsed = bool(settings.get('task_panel_collapsed'))
        self.setMinimumSize(MIN_WIDTH_COLLAPSED if collapsed else MIN_WIDTH, MIN_HEIGHT)
        self.resize(1100, 720)
        self.place(settings.get('window_geometry'))
        self._fade_anim = None
        self._restore_fade_pending = False
        # Cierre del sistema (Alt+F4): callable → True para dejar cerrar
        # (ver closeEvent). Lo pone el controller
        self.close_guard = None

        # Preferencias de ventana (mismas claves que v8)
        if settings.get('always_on_top', True):
            self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        # 1.0 por defecto: a 0.98 todo, texto incluido, se veía lavado
        self._base_opacity = 1.0
        self.set_base_opacity(settings.get('window_opacity', 1.0))

        central = QWidget()
        central.setObjectName("central")
        central.setStyleSheet(f"#central {{ background: {t.bg}; }}")
        self.setCentralWidget(central)
        ThemeManager.instance().theme_changed.connect(
            lambda: central.setStyleSheet(f"#central {{ background: {tokens().bg}; }}"))
        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.title_bar = TitleBar()
        root.addWidget(self.title_bar)

        # El progreso de la sesión vive ahora en el anillo del timer; la
        # barra se conserva (API del controller) pero no se muestra
        self.progress_bar = ProgressGradient()
        root.addWidget(self.progress_bar)
        self.progress_bar.hide()

        # Contenido: panel de tareas | panel de temporizador
        content = QWidget()
        content_lay = QHBoxLayout(content)
        content_lay.setContentsMargins(RESIZE_MARGIN, RESIZE_MARGIN,
                                       RESIZE_MARGIN, RESIZE_MARGIN)
        content_lay.setSpacing(0)
        self.splitter = QSplitter(Qt.Horizontal)
        self.splitter.setChildrenCollapsible(False)
        self.splitter.setHandleWidth(SPLITTER_HANDLE)
        content_lay.addWidget(self.splitter)
        root.addWidget(content, 1)

        self.title_bar.minimize_requested.connect(self.fade_minimize)
        self.title_bar.close_requested.connect(self.close)

        # Resize por bordes en ventana frameless
        self.setMouseTracking(True)
        central.setMouseTracking(True)
        content.setMouseTracking(True)

    def place(self, saved) -> None:
        """Posición inicial: la guardada si sigue cayendo en alguna pantalla
        (un monitor pudo desconectarse); si no, centrada en la principal.
        Sin esto, Windows llegó a abrirla medio fuera de la pantalla."""
        if isinstance(saved, dict):
            try:
                rect = QRect(int(saved['x']), int(saved['y']),
                             int(saved['w']), int(saved['h']))
            except (KeyError, TypeError, ValueError):
                rect = None
            if rect is not None and _OLD_MIN_WIDTH <= rect.width() < MIN_WIDTH_COLLAPSED:
                rect.setWidth(MIN_WIDTH_COLLAPSED)
            if (rect is not None and rect.width() >= MIN_WIDTH_COLLAPSED
                    and rect.height() >= MIN_HEIGHT):
                for screen in QGuiApplication.screens():
                    seen = screen.availableGeometry().intersected(rect)
                    # Que se vea lo bastante para poder agarrarla
                    if seen.width() >= 200 and seen.height() >= 120:
                        self.setGeometry(rect)
                        return
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            area = screen.availableGeometry()
            self.move(area.center() - self.rect().center())

    # --- Opacidad y desvanecidos ---
    def set_base_opacity(self, value) -> None:
        """Opacidad elegida en Configuración (los desvanecidos vuelven a ella)."""
        try:
            self._base_opacity = max(0.5, min(1.0, float(value)))
        except (TypeError, ValueError):
            return
        if self._fade_anim is None or self._fade_anim.state() != QPropertyAnimation.Running:
            self.setWindowOpacity(self._base_opacity)

    def _fade(self, start: float, end: float, ms: int, on_done=None) -> None:
        if self._fade_anim is not None:
            self._fade_anim.stop()
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(ms)
        anim.setStartValue(start)
        anim.setEndValue(end)
        anim.setEasingCurve(QEasingCurve.OutCubic if end > start else QEasingCurve.InCubic)
        if on_done:
            anim.finished.connect(on_done)
        self._fade_anim = anim
        anim.start()

    def fade_minimize(self) -> None:
        """Se desvanece y después se minimiza (sin marco, Windows no anima)."""
        if self.isMinimized():
            return

        def done():
            self.showMinimized()
            # Ya minimizada, recuperar la opacidad: así la miniatura de la
            # barra de tareas no sale vacía
            QTimer.singleShot(120, lambda: self.isMinimized()
                              and self.setWindowOpacity(self._base_opacity))
        self._fade(self.windowOpacity(), 0.0, FADE_OUT_MS, done)

    def fade_hide(self, on_done=None) -> None:
        """Se desvanece y se oculta (p. ej. al ir a la bandeja)."""
        if not self.isVisible():
            if on_done:
                on_done()
            return

        def done():
            self.hide()
            self.setWindowOpacity(self._base_opacity)
            if on_done:
                on_done()
        self._fade(self.windowOpacity(), 0.0, FADE_OUT_MS, done)

    def fade_show(self) -> None:
        """Muestra la ventana apareciendo suavemente."""
        if self.isMinimized():
            self._restore_fade_pending = True
            self.setWindowOpacity(0.0)
            self.showNormal()
        elif not self.isVisible():
            self.setWindowOpacity(0.0)
            self.show()
            self._fade(0.0, self._base_opacity, FADE_IN_MS)
        self.raise_()
        self.activateWindow()

    def _ensure_opaque(self) -> None:
        """Red de seguridad: nunca dejar la ventana invisible."""
        if (not self.isMinimized() and self.windowOpacity() < self._base_opacity
                and (self._fade_anim is None
                     or self._fade_anim.state() != QPropertyAnimation.Running)):
            self._restore_fade_pending = False
            self._fade(self.windowOpacity(), self._base_opacity, FADE_IN_MS)

    def nativeEvent(self, event_type, message):
        # Restaurar desde la barra de tareas / Alt+Tab: dejarla transparente
        # ANTES de que Windows la muestre, para entrar con desvanecido sin un
        # fotograma opaco previo
        if event_type == b"windows_generic_MSG" and self.isMinimized():
            try:
                msg = wintypes.MSG.from_address(int(message))
                if (msg.message == _WM_SYSCOMMAND
                        and (msg.wParam & 0xFFF0) == _SC_RESTORE):
                    self._restore_fade_pending = True
                    self.setWindowOpacity(0.0)
                    QTimer.singleShot(600, self._ensure_opaque)
            except (ValueError, OSError, ctypes.ArgumentError):
                pass
        return super().nativeEvent(event_type, message)

    def changeEvent(self, event):
        if (event.type() == QEvent.WindowStateChange and not self.isMinimized()
                and self._restore_fade_pending):
            self._restore_fade_pending = False
            self._fade(0.0, self._base_opacity, FADE_IN_MS)
        super().changeEvent(event)

    def geometry_settings(self) -> dict:
        """Geometría actual para guardarla en settings['window_geometry']."""
        g = self.geometry()
        return {'x': g.x(), 'y': g.y(), 'w': g.width(), 'h': g.height()}

    def showEvent(self, event):
        super().showEvent(event)
        round_corners(self.winId())

    def closeEvent(self, event):
        # Alt+F4 o «Cerrar ventana» de la barra de tareas (cierres del
        # sistema, espontáneos): `close_guard` decide (el controller pregunta
        # si salir) y, si devuelve False, la ventana sigue como estaba. Un
        # close() desde el código (tests, QApplication.quit) no pregunta
        if (event.spontaneous() and self.close_guard is not None
                and not self.close_guard()):
            event.ignore()
            return
        super().closeEvent(event)

    # --- Resize frameless: detección de borde + startSystemResize ---
    def _edge_at(self, pos):
        edges = Qt.Edge(0)
        r = self.rect()
        if pos.x() <= RESIZE_MARGIN:
            edges |= Qt.LeftEdge
        elif pos.x() >= r.width() - RESIZE_MARGIN:
            edges |= Qt.RightEdge
        if pos.y() <= RESIZE_MARGIN:
            edges |= Qt.TopEdge
        elif pos.y() >= r.height() - RESIZE_MARGIN:
            edges |= Qt.BottomEdge
        return edges

    _CURSORS = {
        Qt.LeftEdge.value: Qt.SizeHorCursor,
        Qt.RightEdge.value: Qt.SizeHorCursor,
        Qt.TopEdge.value: Qt.SizeVerCursor,
        Qt.BottomEdge.value: Qt.SizeVerCursor,
        (Qt.LeftEdge | Qt.TopEdge).value: Qt.SizeFDiagCursor,
        (Qt.RightEdge | Qt.BottomEdge).value: Qt.SizeFDiagCursor,
        (Qt.RightEdge | Qt.TopEdge).value: Qt.SizeBDiagCursor,
        (Qt.LeftEdge | Qt.BottomEdge).value: Qt.SizeBDiagCursor,
    }

    def mouseMoveEvent(self, event):
        if not self.isMaximized():
            edges = self._edge_at(event.position().toPoint())
            cursor = self._CURSORS.get(edges.value)
            if cursor:
                self.setCursor(cursor)
            else:
                self.unsetCursor()
        super().mouseMoveEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and not self.isMaximized():
            edges = self._edge_at(event.position().toPoint())
            if edges:
                win = self.windowHandle()
                if win is not None:
                    win.startSystemResize(edges)
                    return
        super().mousePressEvent(event)

    def event(self, e):
        # Restaurar el cursor al salir de los bordes
        if e.type() == QEvent.HoverLeave:
            self.unsetCursor()
        return super().event(e)
