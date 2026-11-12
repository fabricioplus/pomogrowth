"""Modo compacto: mini ventana siempre visible con timer y play/pausa.

Ventana separada sincronizada por el controller (más limpio que reempaquetar
la principal, como hacía v8). Mismo lenguaje que la pantalla principal: mini
anillo de progreso con el tiempo dentro, modo Enfoque/Descanso/Libre con su color,
tarea activa y controles redondos (play/pausa y saltar descanso): el ciclo
completo se maneja sin maximizar.

La ventana es transparente y sin marco; lo visible es una tarjeta con
esquinas redondeadas y sombra, y el margen alrededor deja sitio a la sombra
(como los toasts). Por eso no se piden esquinas a DWM: redondearía el
rectángulo transparente, no la tarjeta.
"""
from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QFrame, QGraphicsDropShadowEffect, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QVBoxLayout,
                               QWidget)

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon
from ui.widgets.progress_ring import ProgressRing


class _ElidedLabel(QLabel):
    """Etiqueta de una línea que recorta con «…» al ancho disponible."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._full = ""
        # Ignored: el ancho lo decide el layout, no la longitud del nombre
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._refresh()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        self.setText(self.fontMetrics().elidedText(self._full, Qt.ElideRight,
                                                   max(0, self.width())))


class CompactWindow(QWidget):
    toggle_requested = Signal()
    skip_requested = Signal()
    expand_requested = Signal()

    CARD_W, CARD_H = 312, 116
    SHADOW = 16   # margen transparente alrededor de la tarjeta para la sombra
    RING = 92

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        self._is_break = False
        self._free = False  # modo Libre: «Libre» con el icono del infinito
        self._running = False
        self._task_name = None
        self._time_size = None  # tamaño de cifra aplicado (cambia con «h:mm:ss»)

        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
        self.setAttribute(Qt.WA_TranslucentBackground)
        m = self.SHADOW
        self.setFixedSize(self.CARD_W + 2 * m, self.CARD_H + 2 * m)

        outer = QVBoxLayout(self)
        # La sombra cae un poco hacia abajo: menos margen arriba que abajo
        outer.setContentsMargins(m, m - 4, m, m + 4)
        self.card = QFrame(self)
        self.card.setObjectName("compactCard")
        outer.addWidget(self.card)
        self._shadow = QGraphicsDropShadowEffect(self.card)
        self._shadow.setBlurRadius(26)
        self._shadow.setOffset(0, 4)
        self.card.setGraphicsEffect(self._shadow)

        row = QHBoxLayout(self.card)
        row.setContentsMargins(12, 12, 10, 12)
        row.setSpacing(14)

        # Mini anillo con el tiempo dentro (sin el relleno interior del
        # anillo grande: aquí las cifras ocupan todo el hueco)
        self.ring = ProgressRing(self.RING, 6, parent=self.card)
        self.ring.inner.setContentsMargins(0, 0, 0, 0)
        self.time_label = QLabel("25:00", self.ring)
        self.time_label.setAlignment(Qt.AlignCenter)
        self.ring.inner.addWidget(self.time_label)
        self._set_time("25:00")
        row.addWidget(self.ring, 0, Qt.AlignVCenter)

        col = QVBoxLayout()
        col.setContentsMargins(0, 0, 0, 0)
        col.setSpacing(2)

        # Modo (icono + texto en su color) y botón para volver a la principal
        top = QHBoxLayout()
        top.setSpacing(6)
        self.mode_icon = QLabel(self.card)
        top.addWidget(self.mode_icon)
        self.mode_label = QLabel("Enfoque", self.card)
        self.mode_label.setFont(fonts.font(fonts.XS, bold=True))
        top.addWidget(self.mode_label)
        top.addStretch(1)
        self.expand_btn = IconButton("maximize-2", "Volver a la ventana completa",
                                     color=t.fg_muted, hover_color=t.fg, size=14,
                                     parent=self.card)
        self.expand_btn.setFixedSize(26, 26)
        self.expand_btn.clicked.connect(self.expand_requested.emit)
        top.addWidget(self.expand_btn)
        col.addLayout(top)

        self.task_label = _ElidedLabel(self.card)
        self.task_label.setFont(fonts.font(fonts.SMALL, weight=QFont.Weight.Medium))
        col.addWidget(self.task_label)
        col.addStretch(1)

        # Controles redondos: play/pausa en el color del modo y saltar
        # descanso (solo en descanso), como en el timer principal
        controls = QHBoxLayout()
        controls.setSpacing(8)
        self.play_btn = QPushButton(self.card)
        self.play_btn.setObjectName("compactPlay")
        self.play_btn.setFixedSize(36, 36)
        self.play_btn.setIconSize(QSize(16, 16))
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.clicked.connect(self.toggle_requested.emit)
        controls.addWidget(self.play_btn)
        self.skip_btn = IconButton("skip", "Saltar descanso", color=t.fg_dim,
                                   hover_color=t.break_accent, size=15,
                                   variant="round", parent=self.card)
        self.skip_btn.setFixedSize(32, 32)
        # El QSS global redondea «round» para 44 px; aquí mide 32
        self.skip_btn.setStyleSheet("QToolButton { border-radius: 16px; }")
        self.skip_btn.clicked.connect(self.skip_requested.emit)
        controls.addWidget(self.skip_btn)
        self.skip_btn.setVisible(False)
        controls.addStretch(1)
        col.addLayout(controls)
        row.addLayout(col, 1)

        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    # ---- estilo ----
    def _retheme(self) -> None:
        t = tokens()
        self.card.setStyleSheet(
            f"#compactCard {{ background: {t.bg}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}")
        self._shadow.setColor(QColor(0, 0, 0, 150))
        self.time_label.setStyleSheet(f"color: {t.fg};")
        self.expand_btn.set_colors(t.fg_muted, t.fg)
        self.skip_btn.set_colors(t.fg_dim, t.break_accent)
        self._style_mode()
        self._style_task()

    def _style_mode(self) -> None:
        t = tokens()
        accent = t.break_accent if self._is_break else t.accent
        hover = (QColor(t.break_accent).lighter(115).name() if self._is_break
                 else t.accent_hover)
        if self._is_break:
            icon, text = "coffee", "Descanso"
        elif self._free:
            icon, text = "infinity", "Libre"
        else:
            icon, text = "tomato", "Enfoque"
        self.mode_icon.setPixmap(tinted_icon(icon, accent, 14).pixmap(14, 14))
        self.mode_label.setText(text)
        self.mode_label.setStyleSheet(f"color: {accent};")
        self.play_btn.setStyleSheet(
            f"#compactPlay {{ background: {accent}; border: none; border-radius: 18px;"
            f" padding: 0; }}"
            f"#compactPlay:hover {{ background: {hover}; }}")
        self.play_btn.setIcon(tinted_icon("pause" if self._running else "play",
                                          t.on_accent, 16))
        self.play_btn.setToolTip("Pausar" if self._running else "Iniciar")
        self.skip_btn.setVisible(self._is_break)

    def _style_task(self) -> None:
        t = tokens()
        name = self._task_name
        self.task_label.set_full_text(name or "Ninguna tarea seleccionada")
        self.task_label.setStyleSheet(f"color: {t.fg if name else t.fg_muted};")
        self.task_label.setToolTip(name or "")

    def _set_time(self, text: str) -> None:
        self.time_label.setText(text)
        # «h:mm:ss» no cabe a 17 pt dentro del anillo de 92 px
        size = 17 if len(text) <= 5 else 13 if len(text) <= 7 else 11
        if size != self._time_size:
            self._time_size = size
            self.time_label.setFont(fonts.display(size))

    # ---- API para el controller ----
    def set_state(self, time_text: str, running: bool, task_name, is_break: bool,
                  free: bool = False) -> None:
        self._set_time(time_text)
        # Se llama en cada tick: solo se re-estila lo que cambió
        if is_break != self._is_break:
            self.ring.set_break(is_break)
        if (is_break, running, free) != (self._is_break, self._running, self._free):
            self._is_break, self._running, self._free = is_break, running, free
            self._style_mode()
        name = task_name or None
        if name != self._task_name:
            self._task_name = name
            self._style_task()

    def set_progress(self, fraction: float, animate: bool = True) -> None:
        """Progreso de la sesión (0..1) en el mini anillo."""
        self.ring.set_fraction(fraction, animate=animate)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            win = self.windowHandle()
            if win is not None:
                win.startSystemMove()
                return
        super().mousePressEvent(event)
