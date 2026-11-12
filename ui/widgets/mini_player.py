"""Mini reproductor de música para el panel del timer.

Vive en la fila de estado de arriba (junto al botón del panel de tareas):
icono, título elidido con «En directo» o la calidad, reproducir/pausar y
siguiente. No tiene alto propio, así que nunca empuja el anillo del timer.
Solo se ve con algo cargado (sonando, en pausa, armado a la espera del
enfoque o con error); a la espera del enfoque no suena, así que lo dice: un
reloj, «Al enfocar» y la pausa atenuada. Clic en el icono o en el título abre
el diálogo de Música. Con poco ancho cede por orden: la insignia, el título y,
por último, «siguiente» (desactivado con un solo elemento).

Vista tonta: AppController le pasa current()/is_playing del MusicService
(show_now_playing) y escucha sus señales.
"""
from typing import Any, Dict, Optional

from PySide6.QtCore import QEvent, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QLayout, QSizePolicy, QWidget

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon

# Estados del servicio que se nombran en el tooltip
STATE_TEXT = {
    'loading': "Cargando…",
    'buffering': "Almacenando en búfer…",
    'reconnecting': "Reconectando…",
    'paused': "En pausa",
    'error': "No se pudo reproducir",
}


def quality_text(codec: str, kbps: int) -> str:
    """«MP3 · 320 kbps», «MP3», «320 kbps» o ''."""
    return " · ".join(p for p in (codec or '', f"{kbps} kbps" if kbps else '') if p)


def buffer_text(seconds) -> str:
    """Búfer de un directo (current()['buffer_s']): «5 s», «0,5 s» o '' (directo)."""
    if not seconds:
        return ''
    return f"{float(seconds):g}".replace('.', ',') + " s"


class ElidedLabel(QLabel):
    """QLabel de una línea que se acorta con «…» al ancho que le den. Su
    mínimo es 0: cede sitio en vez de empujar a sus vecinos."""

    def __init__(self, text: str = '', parent: Optional[QWidget] = None):
        super().__init__(parent)
        self._full = ''
        self._mode = Qt.ElideRight
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.set_full_text(text)

    def set_elide_mode(self, mode) -> None:
        """Qt.ElideMiddle para rutas: conserva el final, que es lo útil."""
        self._mode = mode
        self._apply()

    def set_full_text(self, text: str) -> None:
        text = text or ''
        if text != self._full:
            self._full = text
            self.updateGeometry()
        self._apply()

    def full_text(self) -> str:
        return self._full

    def is_elided(self) -> bool:
        return self.text() != self._full

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._full) + 2, super().sizeHint().height())

    def minimumSizeHint(self) -> QSize:
        return QSize(0, super().minimumSizeHint().height())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._apply()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange:
            self.updateGeometry()
            self._apply()

    def _apply(self) -> None:
        # Por debajo de unos 3 caracteres, mejor nada que «M…»
        fm = self.fontMetrics()
        width = self.width()
        if width < fm.horizontalAdvance("Mú…") and self._full:
            shown = ''
        else:
            shown = fm.elidedText(self._full, self._mode, max(0, width))
        if shown != self.text():
            super().setText(shown)


class MiniPlayer(QWidget):
    toggle_requested = Signal()
    next_requested = Signal()
    open_requested = Signal()

    # Más bajo que los demás de la fila (botón del panel, estado): no la agranda
    HEIGHT = 24
    _MARGINS = (8, 0, 0, 0)
    _SPACING = 6
    _BUTTON = 24

    def __init__(self, parent: Optional[QWidget] = None):
        super().__init__(parent)
        t = tokens()
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFixedHeight(self.HEIGHT)
        self.setMaximumWidth(360)
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        self._cur: Optional[Dict[str, Any]] = None
        self._playing = False

        lay = QHBoxLayout(self)
        # El mínimo lo decide minimumSizeHint (y _fit recoge piezas): sin
        # esto el layout fijaría como mínimo el ancho de todas ellas
        lay.setSizeConstraint(QLayout.SetNoConstraint)
        lay.setContentsMargins(*self._MARGINS)
        lay.setSpacing(self._SPACING)
        self.icon = QLabel(self)
        self.icon.setFixedSize(16, 16)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.title = ElidedLabel('', self)
        self.title.setFont(fonts.font(fonts.SMALL))
        lay.addWidget(self.title, 1)
        self.badge = QLabel(self)
        self.badge.setFont(fonts.font(fonts.XS))
        self.badge.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        lay.addWidget(self.badge)
        # Reproducir y siguiente, juntos
        buttons = QHBoxLayout()
        buttons.setContentsMargins(0, 0, 0, 0)
        buttons.setSpacing(0)
        self.play_btn = IconButton("play", "Reproducir la música", color=t.fg_dim,
                                   hover_color=t.fg, size=15, parent=self)
        self.play_btn.setFixedSize(self._BUTTON, self._BUTTON)
        self.play_btn.clicked.connect(self.toggle_requested.emit)
        buttons.addWidget(self.play_btn)
        self.next_btn = IconButton("skip-forward", "Siguiente", color=t.fg_dim,
                                   hover_color=t.fg, size=15, parent=self)
        self.next_btn.setFixedSize(self._BUTTON, self._BUTTON)
        self.next_btn.clicked.connect(self.next_requested.emit)
        buttons.addWidget(self.next_btn)
        lay.addLayout(buttons)

        for w in (self.icon, self.title, self.badge):
            w.setAttribute(Qt.WA_TransparentForMouseEvents)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(self._restyle)

    # ---------------------------------------------------------- API
    def show_now_playing(self, cur: Optional[Dict[str, Any]], playing: bool) -> None:
        """cur: MusicService.current() (o None); playing: is_playing. Se ve
        mientras haya algo cargado: no con la app recién abierta (el último
        item queda en 'idle') ni tras parar."""
        self._cur, self._playing = cur, bool(playing)
        visible = cur is not None and (self._playing or cur.get('state') != 'idle')
        if visible:
            self._apply()
        if self.isHidden() == visible:
            self.setVisible(visible)

    def is_shown(self) -> bool:
        return not self.isHidden()

    # ---------------------------------------------------------- contenido
    def _badge_text(self) -> str:
        cur = self._cur or {}
        if cur.get('live'):
            return "En directo"
        return f"{cur['bitrate_kbps']} kbps" if cur.get('bitrate_kbps') else ''

    def _apply(self) -> None:
        t = tokens()
        cur = self._cur or {}
        state = cur.get('state', 'idle')
        # Armada a la espera del enfoque: no suena, y no debe parecerlo
        waiting = bool(cur.get('waiting_focus'))
        self.title.set_full_text(cur.get('title', ''))
        badge = "Al enfocar" if waiting else self._badge_text()
        self.badge.setText(badge)
        if state == 'error':
            name, color = "circle-alert", t.error
        elif waiting:
            name, color = "timer", t.info
        elif state == 'reconnecting':
            name, color = ("radio" if cur.get('live') else "music"), t.warning
        elif self._playing and state == 'playing':
            name, color = "audio-lines", t.accent
        else:
            name, color = ("radio" if cur.get('live') else "music"), t.fg_muted
        self.icon.setPixmap(tinted_icon(name, color, 16).pixmap(16, 16))
        self.play_btn.set_icon_name("pause" if self._playing else "play")
        # Esperando, la pausa va atenuada: lo que hace es desarmarla
        self.play_btn.set_colors(t.fg_muted if waiting else t.fg_dim, t.fg)
        self.play_btn.setToolTip(
            "Pausar: no sonará al empezar el pomodoro" if waiting
            else "Pausar la música" if self._playing else "Reproducir la música")
        # Con un solo elemento, «siguiente» no tiene adónde ir
        self.next_btn.setEnabled(cur.get('count', 0) > 1)
        lines = [cur.get('title', '')]
        if cur.get('subtitle') and cur.get('subtitle') != cur.get('title'):
            lines.append(cur['subtitle'])
        quality = quality_text(cur.get('codec', ''), cur.get('bitrate_kbps', 0))
        buffered = buffer_text(cur.get('buffer_s'))
        live = "En directo" if cur.get('live') else ''
        if live and buffered and state == 'playing':
            live += f" · búfer {buffered}"     # tras un corte va unos segundos por detrás
        extra = [x for x in (live, quality) if x]
        if waiting:
            extra.append("sonará al enfocar")
        elif state in STATE_TEXT and not (state == 'paused' and self._playing):
            extra.append(STATE_TEXT[state].rstrip('…').lower()
                         + (f" ({buffered})" if state == 'buffering' and buffered else ''))
        if extra:
            lines.append(" · ".join(extra))
        lines.append("Clic para abrir Música")
        self.setToolTip("\n".join(x for x in lines if x))
        self.updateGeometry()
        self._fit()

    # ---------------------------------------------------------- ancho
    def _widths(self):
        """(mínimo, sin título ni insignia, sin insignia, completo)."""
        left, _, right, _ = self._MARGINS
        sp = self._SPACING
        base = left + 16 + sp + self._BUTTON + right          # icono + reproducir
        with_next = base + self._BUTTON                       # + siguiente (pegado)
        title = sp + self.title.sizeHint().width()
        badge = (sp + self.badge.sizeHint().width()) if self.badge.text() else 0
        return base, with_next, with_next + title, with_next + title + badge

    def minimumSizeHint(self) -> QSize:
        return QSize(self._widths()[0], self.HEIGHT)

    def sizeHint(self) -> QSize:
        # Siempre el completo: si dependiera de lo oculto, oscilaría
        return QSize(min(self.maximumWidth(), self._widths()[3]), self.HEIGHT)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        _, with_next, with_title, full = self._widths()
        w = self.width()
        # El título se queda con lo que haya (elidido) en cuanto quepa algo
        title_room = w >= with_next + self._SPACING + self.fontMetrics().horizontalAdvance("Música")
        for widget, show in ((self.next_btn, w >= with_next),
                             (self.title, title_room),
                             (self.badge, bool(self.badge.text()) and w >= full)):
            if widget.isHidden() == show:
                widget.setVisible(show)

    # ---------------------------------------------------------- estilo
    def _restyle(self) -> None:
        t = tokens()
        self.title.setStyleSheet(f"color: {t.fg_dim};")
        self.badge.setStyleSheet(f"color: {t.fg_muted};")
        for b in (self.play_btn, self.next_btn):
            b.set_colors(t.fg_dim, t.fg)
        if self._cur is not None:
            self._apply()   # el de reproducir puede ir atenuado
        self.update()

    def event(self, e):
        if e.type() in (QEvent.HoverEnter, QEvent.HoverLeave):
            t = tokens()
            self.title.setStyleSheet(
                f"color: {t.fg if e.type() == QEvent.HoverEnter else t.fg_dim};")
            self.update()
        return super().event(e)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.open_requested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    def paintEvent(self, event):
        if not self.underMouse():
            return
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(t.surface_hover))
        p.drawRoundedRect(QRectF(self.rect()), t.radius_small, t.radius_small)
        p.end()
