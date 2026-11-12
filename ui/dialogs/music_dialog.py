"""Música: tus listas (radios, enlaces y archivos) y la carpeta lofi.

Diálogo de una sola instancia (lo abre AppController.open_music) sobre
MusicService, que hace todo el trabajo; aquí solo se pinta y se piden cosas:
- Izquierda: «Carpeta lofi» (solo lectura: la genera el servicio escaneando
  assets/music/lofi) y las listas del usuario, con «Nueva lista». Renombrar
  y eliminar desde el menú «…» de la cabecera o con el botón derecho.
- Derecha: cabecera de la lista (reproducir, aleatorio, repetir), barra para
  pegar un enlace (radio, MP3 o lista .m3u/.pls/.xspf) o elegir archivos y
  carpetas, y los elementos: arrastrar para reordenar, doble clic o Enter
  reproduce, Supr quita y, al pasar el ratón, reproducir/renombrar/quitar.
  En la lofi, además, «Abrir carpeta» y «Releer»; lo que se pega o suelta
  ahí va a una lista que elige o crea el servicio. Archivos, carpetas, listas
  y enlaces se pueden soltar en cualquier parte del diálogo, y Ctrl+V con un
  enlace en el portapapeles lo añade.
- Abajo, «Ahora suena»: título, canción de la radio (ICY), emisora o «Pista
  3 de 12», insignia de calidad («MP3 · 320 kbps»), estado (cargando,
  reconectando, el motivo de un error en hasta dos líneas, «sonará al empezar
  un pomodoro»), controles, progreso con seek («● En directo» en su lugar, y
  «Búfer 5 s» al lado si tras un corte suena con búfer), volumen y «Solo
  durante el enfoque».
- Con el servicio de ambiente (ambient=), arriba de la columna de listas la
  entrada fija «Sonidos de ambiente»: al elegirla, el hueco del panel de la
  lista lo ocupa el mezclador (ui/dialogs/ambient_mixer.py). «Ahora suena»
  sigue siendo la música: los dos suenan a la vez.

Las filas se pintan con delegados (una lista admite 2000 elementos): nada de
un widget por fila. Los enlaces solo muestran el host (o la URL sin query en
el tooltip): pueden llevar tokens.
"""
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

from PySide6.QtCore import (QEvent, QPoint, QPointF, QRect, QRectF, QSize,
                            QStandardPaths, Qt, QTimer, QUrl, Signal)
from PySide6.QtGui import (QBrush, QColor, QDesktopServices, QFont, QFontMetrics,
                           QGuiApplication, QKeySequence, QLinearGradient, QPainter, QPen)
from PySide6.QtWidgets import (QAbstractItemView, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QLineEdit, QListWidget,
                               QListWidgetItem, QMenu, QPushButton, QScrollArea,
                               QSizePolicy, QSlider, QStackedWidget, QStyle,
                               QStyledItemDelegate, QStyleOptionSlider, QToolTip,
                               QVBoxLayout, QWidget)

from core.music_library import (AUDIO_EXTS, LOFI_LIST_ID, MAX_LIST_NAME, MAX_TITLE,
                                PLAYLIST_EXTS, codec_name)
from ui.dialogs import branded_dialogs as dialogs
from ui.dialogs.ambient_mixer import AMBIENT_ICON, AMBIENT_ID, AMBIENT_NAME, AmbientMixer
from ui.dialogs.branded_dialogs import (SHADOW_MARGINS, BrandedDialog, icon_chip,
                                        style_icon_chip)
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon
from ui.widgets.mini_player import STATE_TEXT, ElidedLabel, buffer_text, quality_text
from ui.widgets import toast
from ui.widgets.toggle_switch import ToggleSwitch

ROLE = Qt.UserRole
ROW_H, LIST_ROW_H = 52, 40
LEFT_W = 216
CARD_SIZE = QSize(880, 600)       # tamaño de la tarjeta si cabe en pantalla
CARD_MIN = QSize(880, 540)
LINK_PLACEHOLDER = "Pega un enlace: radio, MP3 o lista (.m3u, .pls)…"
FOCUS_TIP = ("La música solo suena con un pomodoro en marcha: se pausa sola en los "
             "descansos y al pausar o parar el timer, y vuelve al reanudar el enfoque.")
UNSUPPORTED_NOTE = ("YouTube, Spotify y similares no dejan reproducir su audio fuera "
                    "de su app.")
_EXTS = " ".join(f"*{e}" for e in sorted(AUDIO_EXTS | PLAYLIST_EXTS))
FILE_FILTER = f"Audio y listas ({_EXTS});;Todos los archivos (*)"
STATUS_OK_MS = 6000


# ================================================================ utilidades
def fmt_ms(ms: int) -> str:
    """3:07, 12:45 o 1:02:03."""
    s = max(0, int(ms or 0) // 1000)
    h, rest = divmod(s, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _added_text(n: int, live: int = 0) -> str:
    """«Añadida 1 pista», «Añadidas 2 radios», «Añadidos 3 elementos» (live:
    cuántas de las n son radios en directo)."""
    if 0 < live < n:
        return f"Añadidos {n} elementos"
    what = _plural(n, 'radio', 'radios') if live else _plural(n, 'pista', 'pistas')
    return f"{'Añadida' if n == 1 else 'Añadidas'} {what}"


def _rgba(color: str, alpha: int) -> QColor:
    c = QColor(color)
    c.setAlpha(alpha)
    return c


def _mix(a: str, b: str, f: float) -> QColor:
    ca, cb = QColor(a), QColor(b)
    return QColor(round(ca.red() + (cb.red() - ca.red()) * f),
                  round(ca.green() + (cb.green() - ca.green()) * f),
                  round(ca.blue() + (cb.blue() - ca.blue()) * f))


def _host(url: str) -> str:
    try:
        host = urlsplit(url).hostname or ''
    except ValueError:
        host = ''
    return host[4:] if host.startswith('www.') else host


def display_source(item: Dict[str, Any]) -> str:
    """Origen para el tooltip: la ruta del archivo o la URL sin usuario,
    query ni fragmento (un enlace puede llevar tokens)."""
    src = item.get('source', '')
    if item.get('kind') != 'url':
        return src
    try:
        parts = urlsplit(src)
        netloc = parts.hostname or ''
        if parts.port:
            netloc += f":{parts.port}"
        return f"{parts.scheme}://{netloc}{parts.path}"
    except ValueError:
        return _host(src)


def item_icon(item: Dict[str, Any]) -> str:
    if item.get('kind') == 'file':
        return "file-music"
    return "radio" if item.get('live') else "music"


def item_subtitle(item: Dict[str, Any], duration_ms: int = 0) -> str:
    """«En directo · radio.com · MP3 · 320 kbps», «Lofi · 3:20 · MP3»…"""
    parts: List[str] = []
    if item.get('kind') == 'url':
        if item.get('live'):
            parts.append("En directo")
        parts.append(_host(item.get('source', '')) or "Enlace")
    else:
        parts.append(Path(item.get('source', '')).parent.name or "Archivo")
    if duration_ms > 0 and not item.get('live'):
        parts.append(fmt_ms(duration_ms))
    quality = quality_text(codec_name(item.get('mime', ''), item.get('source', '')),
                           int(item.get('bitrate') or 0))
    if quality:
        parts.append(quality)
    return " · ".join(parts)


def _elided(parent: QWidget, font: QFont) -> ElidedLabel:
    """Etiqueta que se acorta con «…»; sin pedir ancho, para que un texto
    largo (una URL, un título de radio) no ensanche el diálogo."""
    label = ElidedLabel('', parent)
    label.setFont(font)
    label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return label


def ask_name(parent, title: str, label: str, text: str = '', *, ok_text: str = "Guardar",
             icon: str = "edit", max_len: int = MAX_TITLE) -> Optional[str]:
    """Pide un nombre en un diálogo de la casa. None si se cancela o queda vacío."""
    dlg = BrandedDialog(parent, title, icon=icon)
    caption = QLabel(label, dlg._card)
    caption.setFont(fonts.font(fonts.SMALL))
    caption.setProperty("role", "dim")
    edit = QLineEdit(text, dlg._card)
    edit.setMaxLength(max_len)
    edit.setMinimumWidth(360)
    box = QVBoxLayout()
    box.setSpacing(6)
    box.addWidget(caption)
    box.addWidget(edit)
    dlg._lay.addLayout(box)
    _cancel, ok = dlg.add_buttons([("Cancelar", "ghost", 0), (ok_text, "accent", 1)])
    ok.setDefault(True)
    edit.returnPressed.connect(lambda: dlg.done(1))
    edit.selectAll()
    edit.setFocus()
    accepted = dlg.exec() == 1
    value = edit.text().strip()
    dlg.deleteLater()
    return value if accepted and value else None


# ================================================================ piezas pequeñas
class _Badge(QWidget):
    """Insignia de texto pequeño (calidad, «En directo» con su punto)."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self._text = ''
        self._dot: Optional[str] = None
        self.setFont(fonts.font(fonts.XS))
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)

    def set_badge(self, text: str, dot: Optional[str] = None) -> None:
        if (text, dot) != (self._text, self._dot):
            self._text, self._dot = text, dot
            self.updateGeometry()
            self.update()
        if self.isHidden() == bool(text):
            self.setVisible(bool(text))

    def text(self) -> str:
        return self._text

    def sizeHint(self) -> QSize:
        fm = self.fontMetrics()
        return QSize(fm.horizontalAdvance(self._text) + 16 + (11 if self._dot else 0),
                     fm.height() + 4)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(QColor(t.bg_header))
        p.drawRoundedRect(r, r.height() / 2, r.height() / 2)
        x = 8.0
        if self._dot:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(self._dot))
            p.drawEllipse(QPointF(x + 3, r.center().y()), 3, 3)
            x += 11
        p.setPen(QColor(t.fg_dim))
        p.setFont(self.font())
        p.drawText(QRectF(x, 0, self.width() - x - 6, self.height()),
                   Qt.AlignVCenter | Qt.AlignLeft, self._text)
        p.end()


class _Cover(QWidget):
    """Carátula: degradado del acento con el icono (radio o nota); neutra
    cuando no suena nada."""

    def __init__(self, size: int, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(size, size)
        self._icon, self._active = "music", False

    def set_cover(self, icon: str, active: bool) -> None:
        if (icon, active) != (self._icon, self._active):
            self._icon, self._active = icon, active
            self.update()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect())
        p.setPen(Qt.NoPen)
        if self._active:
            grad = QLinearGradient(r.topLeft(), r.bottomRight())
            grad.setColorAt(0.0, QColor(t.accent))
            grad.setColorAt(1.0, QColor(t.accent_grad_end))
            p.setBrush(QBrush(grad))
            color = t.on_accent
        else:
            p.setBrush(QColor(t.surface_active))
            color = t.fg_muted
        p.drawRoundedRect(r, t.radius_small + 2, t.radius_small + 2)
        s = int(r.width() * 0.44)
        p.drawPixmap(QPointF(r.center().x() - s / 2, r.center().y() - s / 2),
                     tinted_icon(self._icon, color, s).pixmap(s, s))
        p.end()


class _SeekSlider(QSlider):
    """Barra de progreso: clic en cualquier punto salta ahí (y se puede arrastrar)."""
    seek_requested = Signal('qint64')

    def __init__(self, parent: QWidget):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(0, 0)
        self.setCursor(Qt.PointingHandCursor)
        self.sliderReleased.connect(lambda: self.seek_requested.emit(self.value()))

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton and self.isEnabled() and self.maximum() > 0:
            opt = QStyleOptionSlider()
            self.initStyleOption(opt)
            style = self.style()
            handle = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderHandle, self)
            pos = event.position().toPoint()
            if not handle.contains(pos):
                groove = style.subControlRect(QStyle.CC_Slider, opt, QStyle.SC_SliderGroove, self)
                span = max(1, groove.width() - handle.width())
                x = pos.x() - groove.x() - handle.width() // 2
                value = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(),
                                                       max(0, min(span, x)), span)
                self.setValue(value)
                self.seek_requested.emit(value)
        super().mousePressEvent(event)


class _Composer(QFrame):
    """Campo del enlace: superficie con borde que se enciende al enfocarlo
    (como el de añadir tareas), icono, texto y «Añadir»."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedHeight(40)
        self.setCursor(Qt.IBeamCursor)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 5, 0)
        lay.setSpacing(10)
        self.icon = QLabel(self)
        self.icon.setFixedSize(16, 16)
        lay.addWidget(self.icon, 0, Qt.AlignVCenter)
        self.edit = QLineEdit(self)
        self.edit.setPlaceholderText(LINK_PLACEHOLDER)
        self.edit.setFont(fonts.font(fonts.SMALL))
        self.edit.setClearButtonEnabled(False)
        self.edit.setAccessibleName("Enlace para añadir a la lista")
        lay.addWidget(self.edit, 1)
        self.add_btn = QPushButton("Añadir", self)
        self.add_btn.setProperty("variant", "accent")
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.setAutoDefault(False)
        self.add_btn.setFixedHeight(30)
        self.add_btn.setFont(fonts.font(fonts.XS + 0.5, bold=True))
        self.add_btn.setToolTip("Añadir el enlace a la lista (Enter)")
        lay.addWidget(self.add_btn, 0, Qt.AlignVCenter)
        # «Añadir» aparece al escribir o pegar: vacío, el sitio es del marcador
        self.add_btn.setVisible(False)
        self.edit.textChanged.connect(
            lambda text: self.add_btn.setVisible(bool(text.strip())))
        self.edit.installEventFilter(self)
        self.restyle()

    def restyle(self) -> None:
        t = tokens()
        self.icon.setPixmap(tinted_icon("link", t.fg_muted, 16).pixmap(16, 16))
        self.edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; padding: 0px;"
            f" color: {t.fg}; placeholder-text-color: {t.fg_muted};"
            f" selection-background-color: {t.accent}; selection-color: {t.on_accent}; }}")
        self.add_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px 14px; border-radius: {t.radius_small - 2}px; }}")
        self.update()

    def eventFilter(self, obj, event):
        if obj is self.edit and event.type() in (QEvent.FocusIn, QEvent.FocusOut):
            self.update()
        return super().eventFilter(obj, event)

    def mousePressEvent(self, event):
        self.edit.setFocus(Qt.MouseFocusReason)
        super().mousePressEvent(event)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        border = _mix(t.border, t.accent, 0.6) if self.edit.hasFocus() else QColor(t.border)
        p.setPen(QPen(border, 1))
        p.setBrush(QColor(t.surface))
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5),
                          t.radius_small + 2, t.radius_small + 2)
        p.end()


class _DropHint(QWidget):
    """Aviso al arrastrar archivos o enlaces sobre el diálogo."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_TransparentForMouseEvents)
        self.text = ''
        self.hide()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)
        p.setBrush(_rgba(t.bg, 225))
        pen = QPen(QColor(t.accent), 1.5)
        pen.setStyle(Qt.DashLine)
        p.setPen(pen)
        p.drawRoundedRect(r, t.radius, t.radius)
        size = 28
        cy = r.center().y()
        p.drawPixmap(QPointF(r.center().x() - size / 2, cy - size - 6),
                     tinted_icon("download", t.accent, size).pixmap(size, size))
        p.setPen(QColor(t.fg))
        p.setFont(fonts.font(fonts.SMALL, bold=True))
        p.drawText(QRectF(r.left() + 16, cy + 2, r.width() - 32, 24), Qt.AlignCenter, self.text)
        p.end()


# ================================================================ listas (izquierda)
class _ListsDelegate(QStyledItemDelegate):
    def __init__(self, view: '_ListsView'):
        super().__init__(view)
        self._view = view
        self._font = fonts.font(fonts.SMALL)
        self._font_sel = fonts.font(fonts.SMALL, bold=True)
        self._font_count = fonts.font(fonts.XS)

    def sizeHint(self, option, index):
        return QSize(max(0, option.rect.width()), LIST_ROW_H)

    def paint(self, p, option, index):
        info = index.data(ROLE)
        if not info:
            return
        t = tokens()
        view = self._view
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(0, 2, -4, -2)
        selected = bool(option.state & QStyle.State_Selected)
        hover = index.row() == view.hover_row
        if selected or hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.surface_active if selected else t.surface_hover))
            p.drawRoundedRect(r, t.radius_small, t.radius_small)
        playing = info['id'] == view.playing_list
        if info.get('ambient'):   # sonidos de ambiente: las ondas, en el acento si suenan
            name = AMBIENT_ICON
            color = t.accent if view.ambient_playing else (t.fg if selected else t.fg_dim)
        elif playing:
            name, color = "audio-lines", t.accent
        else:
            name = "folder" if info.get('readonly') else "list-music"
            color = t.fg if selected else t.fg_dim
        cy = r.center().y()
        x = r.left() + 10
        tinted_icon(name, color, 16).paint(p, QRect(int(x), int(cy - 8), 16, 16))
        x += 16 + 10
        count = str(info.get('count', 0))
        if info.get('ambient') and not info.get('count'):
            count = ''   # ambiente: cuántos suenan, nada si ninguno
        p.setFont(self._font_count)
        fm_c = QFontMetrics(self._font_count)
        cw = fm_c.horizontalAdvance(count)
        p.setPen(QColor(t.fg_muted))
        p.drawText(QRectF(r.right() - 10 - cw, r.top(), cw, r.height()),
                   Qt.AlignVCenter | Qt.AlignRight, count)
        font = self._font_sel if selected else self._font
        p.setFont(font)
        p.setPen(QColor(t.fg if selected or hover else t.fg_dim))
        width = r.right() - 10 - cw - 8 - x
        name = info.get('name', '')
        # Por el medio: «Música para estudiar… 2» (el final suele distinguirlas)
        shown = QFontMetrics(font).elidedText(name, Qt.ElideMiddle, int(width))
        if shown != name:
            view.elided_rows.add(index.row())
        else:
            view.elided_rows.discard(index.row())
        p.drawText(QRectF(x, r.top(), width, r.height()), Qt.AlignVCenter | Qt.AlignLeft,
                   shown)
        p.restore()


class _ListsView(QListWidget):
    menu_requested = Signal(str, QPoint)

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.playing_list: Optional[str] = None
        self.ambient_playing = False      # la entrada de ambiente, con sonidos sonando
        self.hover_row = -1
        self.elided_rows: set = set()     # filas con el nombre recortado (tooltip)
        self.setItemDelegate(_ListsDelegate(self))
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setUniformItemSizes(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._on_context)

    def row_of(self, list_id: str) -> int:
        return next((i for i in range(self.count())
                     if (self.item(i).data(ROLE) or {}).get('id') == list_id), -1)

    def _on_context(self, pos: QPoint) -> None:
        item = self.itemAt(pos)
        if item is not None:
            self.menu_requested.emit(item.data(ROLE)['id'], self.viewport().mapToGlobal(pos))

    def mouseMoveEvent(self, event):
        row = self.indexAt(event.position().toPoint()).row()
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().update()
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        if event.type() == QEvent.Leave and self.hover_row != -1:
            self.hover_row = -1
            self.viewport().update()
        elif event.type() == QEvent.ToolTip:
            # El nombre completo, solo si se pintó recortado
            index = self.indexAt(event.pos())
            info = index.data(ROLE) if index.isValid() else None
            if info and index.row() in self.elided_rows:
                QToolTip.showText(event.globalPos(), info.get('name', ''), self.viewport())
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)


# ================================================================ elementos (derecha)
class _ItemDelegate(QStyledItemDelegate):
    def __init__(self, view: '_ItemsView'):
        super().__init__(view)
        self._view = view
        self._title = fonts.font(fonts.SMALL + 1, weight=QFont.Weight.Medium)
        self._title_cur = fonts.font(fonts.SMALL + 1, bold=True)
        self._sub = fonts.font(fonts.XS)

    def sizeHint(self, option, index):
        return QSize(max(0, option.rect.width()), ROW_H)

    def paint(self, p, option, index):
        item = index.data(ROLE)
        if not item:
            return
        t = tokens()
        view = self._view
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(option.rect).adjusted(4, 2, -4, -2)
        hover = index.row() == view.hover_row
        selected = bool(option.state & QStyle.State_Selected)
        current = item['id'] == view.current_id
        if selected or hover:
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.surface_active if selected else t.surface_hover))
            p.drawRoundedRect(r, t.radius_small, t.radius_small)
        cy = r.center().y()
        x = r.left() + 6
        if hover and view.reorderable:
            tinted_icon("grip", t.fg_muted, 14).paint(p, QRect(int(x), int(cy - 7), 14, 14))
        x += 22
        if current:
            name = "audio-lines" if view.current_playing else item_icon(item)
            color = t.accent
        else:
            name, color = item_icon(item), t.fg_dim
        tinted_icon(name, color, 18).paint(p, QRect(int(x), int(cy - 9), 18, 18))
        x += 18 + 14
        right = r.right() - 10
        if hover:
            rects = view.action_rects(option.rect)
            right = rects[0][1].left() - 8 if rects else right
            for key, rect in rects:
                hot = view.hot == (index.row(), key)
                if hot:
                    p.setPen(Qt.NoPen)
                    p.setBrush(QColor(t.border if selected else t.surface_active))
                    p.drawRoundedRect(QRectF(rect), 6, 6)
                icon_color = ((t.danger if key == 'remove' else t.fg) if hot else t.fg_dim)
                icon = _ItemsView.ACTIONS[key][0]
                tinted_icon(icon, icon_color, 16).paint(
                    p, QRect(rect.center().x() - 7, rect.center().y() - 7, 16, 16))
        width = max(0.0, right - x)
        title_font = self._title_cur if current else self._title
        fm_t = QFontMetrics(title_font)
        fm_s = QFontMetrics(self._sub)
        block = fm_t.height() + 1 + fm_s.height()
        top = cy - block / 2
        p.setFont(title_font)
        p.setPen(QColor(t.accent if current else t.fg))
        p.drawText(QRectF(x, top, width, fm_t.height()), Qt.AlignLeft | Qt.AlignVCenter,
                   fm_t.elidedText(item.get('title', ''), Qt.ElideRight, int(width)))
        sub = item_subtitle(item, view.current_duration if current else 0)
        p.setFont(self._sub)
        p.setPen(QColor(t.fg_muted))
        p.drawText(QRectF(x, top + fm_t.height() + 1, width, fm_s.height()),
                   Qt.AlignLeft | Qt.AlignVCenter,
                   fm_s.elidedText(sub, Qt.ElideRight, int(width)))
        p.restore()


class _ItemsView(QListWidget):
    play_requested = Signal(str)
    rename_requested = Signal(str)
    remove_requested = Signal(str)
    reordered = Signal(str, int)

    # clave → (icono, tooltip)
    ACTIONS = {'play': ("play", "Reproducir"), 'edit': ("edit", "Renombrar"),
               'remove': ("delete", "Quitar de la lista")}
    _ACTION_SIZE = 28

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.current_id: Optional[str] = None
        self.current_playing = False
        self.current_duration = 0
        self.readonly = False
        self.reorderable = True
        self.hover_row = -1
        self.hot: Optional[Tuple[int, str]] = None
        self._drag_row = -1           # fila que se está arrastrando (reordenar)
        self.setItemDelegate(_ItemDelegate(self))
        self.setUniformItemSizes(True)
        self.setMouseTracking(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.setStyleSheet("QListWidget { background: transparent; border: none; }")
        self.set_readonly(False)

    def set_readonly(self, readonly: bool) -> None:
        self.readonly = readonly
        self.reorderable = not readonly
        if readonly:
            self.setDragDropMode(QAbstractItemView.NoDragDrop)
        else:
            self.setDragDropMode(QAbstractItemView.InternalMove)
            self.setDefaultDropAction(Qt.MoveAction)
            self.setDropIndicatorShown(True)

    def item_id(self, row: int) -> Optional[str]:
        item = self.item(row) if row >= 0 else None
        data = item.data(ROLE) if item is not None else None
        return data.get('id') if data else None

    def row_of(self, item_id: str) -> int:
        return next((i for i in range(self.count()) if self.item_id(i) == item_id), -1)

    def action_rects(self, rect: QRect) -> List[Tuple[str, QRect]]:
        keys = ('play',) if self.readonly else ('play', 'edit', 'remove')
        size = self._ACTION_SIZE
        x = rect.right() - 10 - size + 1
        y = rect.center().y() - size // 2 + 1
        out = []
        for key in reversed(keys):
            out.append((key, QRect(x, y, size, size)))
            x -= size + 2
        out.reverse()
        return out

    def _hit(self, pos: QPoint) -> Tuple[int, Optional[str]]:
        index = self.indexAt(pos)
        if not index.isValid():
            return -1, None
        for key, rect in self.action_rects(self.visualRect(index)):
            if rect.contains(pos):
                return index.row(), key
        return index.row(), None

    # ---------- ratón y teclado ----------
    def mouseMoveEvent(self, event):
        row, key = self._hit(event.position().toPoint())
        hot = (row, key) if key else None
        if row != self.hover_row or hot != self.hot:
            self.hover_row, self.hot = row, hot
            self.viewport().update()
        self.viewport().setCursor(Qt.PointingHandCursor if key else Qt.ArrowCursor)
        super().mouseMoveEvent(event)

    def viewportEvent(self, event):
        kind = event.type()
        if kind == QEvent.Leave:
            if self.hover_row != -1 or self.hot:
                self.hover_row, self.hot = -1, None
                self.viewport().update()
        elif kind == QEvent.ToolTip:
            row, key = self._hit(event.pos())
            if key:
                QToolTip.showText(event.globalPos(), self.ACTIONS[key][1], self.viewport())
                return True
            item = self.item(row) if row >= 0 else None
            data = item.data(ROLE) if item is not None else None
            if data:
                QToolTip.showText(event.globalPos(),
                                  f"{data.get('title', '')}\n{display_source(data)}",
                                  self.viewport())
            else:
                QToolTip.hideText()
            return True
        return super().viewportEvent(event)

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            row, key = self._hit(event.position().toPoint())
            if key:
                iid = self.item_id(row)
                signal = {'play': self.play_requested, 'edit': self.rename_requested,
                          'remove': self.remove_requested}[key]
                # Fuera del manejador: renombrar abre un diálogo modal
                QTimer.singleShot(0, lambda: signal.emit(iid))
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseDoubleClickEvent(self, event):
        row, key = self._hit(event.position().toPoint())
        if row >= 0 and not key:
            self.play_requested.emit(self.item_id(row))
        event.accept()

    def keyPressEvent(self, event):
        iid = self.item_id(self.currentRow())
        if iid:
            if event.key() == Qt.Key_Delete and not self.readonly:
                self.remove_requested.emit(iid)
                return
            if event.key() in (Qt.Key_Return, Qt.Key_Enter):
                self.play_requested.emit(iid)
                return
        super().keyPressEvent(event)

    def startDrag(self, actions):
        # Durante el arrastre no llegan movimientos del ratón: sin esto la
        # fila de origen seguiría pintada como «bajo el ratón»
        self.hover_row, self.hot = -1, None
        self.viewport().update()
        self._drag_row = self.currentRow()
        try:
            super().startDrag(actions)
        finally:
            self._drag_row = -1

    def dropEvent(self, event):
        """Reordenar: se calcula el destino y lo hace el servicio (que
        persiste y avisa con library_changed); la vista no mueve nada. Lo
        que llega de fuera (archivos, enlaces) lo atiende el diálogo."""
        src = self._drag_row
        iid = self.item_id(src)
        if not iid:
            event.ignore()
            return
        pos = event.position().toPoint()
        index = self.indexAt(pos)
        if index.isValid():
            dest = index.row() + (1 if pos.y() > self.visualRect(index).center().y() else 0)
        else:
            dest = self.count()
        if dest > src:
            dest -= 1  # al sacarla de su sitio, las de debajo suben una
        # CopyAction: que startDrag no borre la fila de origen
        event.setDropAction(Qt.CopyAction)
        event.accept()
        self.setState(QAbstractItemView.NoState)
        self.viewport().update()
        if iid and dest != src:
            QTimer.singleShot(0, lambda: self.reordered.emit(iid, dest))


# ================================================================ «Ahora suena»
class _NowPlaying(QFrame):
    """Barra inferior: qué suena, controles, progreso, volumen y enfoque."""

    def __init__(self, music, parent: QWidget):
        super().__init__(parent)
        t = tokens()
        self.music = music
        self.setObjectName("nowPlaying")
        self._cur: Optional[Dict[str, Any]] = None
        self._playing = False
        self._sub_color = t.fg_dim

        h = QHBoxLayout(self)
        h.setContentsMargins(14, 12, 18, 12)
        h.setSpacing(20)

        # Izquierda: carátula + textos + insignias
        left = QHBoxLayout()
        left.setSpacing(12)
        self.cover = _Cover(52, self)
        left.addWidget(self.cover, 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(1)
        texts.addStretch(1)
        self.title = _elided(self, fonts.font(fonts.SMALL + 1, bold=True))
        texts.addWidget(self.title)
        self.subtitle = _elided(self, fonts.font(fonts.XS))
        texts.addWidget(self.subtitle)
        # Un error se lee entero: hasta dos líneas en vez del subtítulo elidido
        self.detail = QLabel(self)
        self.detail.setFont(fonts.font(fonts.XS))
        self.detail.setWordWrap(True)
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.detail.setMaximumHeight(2 * QFontMetrics(self.detail.font()).lineSpacing() + 2)
        texts.addWidget(self.detail)
        self.detail.setVisible(False)
        badges = QHBoxLayout()
        badges.setContentsMargins(0, 4, 0, 0)
        badges.setSpacing(6)
        self.quality = _Badge(self)
        badges.addWidget(self.quality)
        badges.addStretch(1)
        texts.addLayout(badges)
        texts.addStretch(1)
        left.addLayout(texts, 1)
        h.addLayout(left, 1)

        # Centro: controles y progreso
        center = QVBoxLayout()
        center.setSpacing(4)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addStretch(1)
        self.prev_btn = IconButton("skip-back", "Anterior", color=t.fg_dim, hover_color=t.fg,
                                   size=17, parent=self)
        self.prev_btn.setFixedSize(34, 34)
        self.prev_btn.clicked.connect(music.prev)
        controls.addWidget(self.prev_btn)
        self.play_btn = QPushButton(self)
        self.play_btn.setObjectName("npPlay")
        self.play_btn.setFixedSize(40, 40)
        self.play_btn.setIconSize(QSize(18, 18))
        self.play_btn.setCursor(Qt.PointingHandCursor)
        self.play_btn.setAutoDefault(False)
        self.play_btn.clicked.connect(self._toggle)
        controls.addWidget(self.play_btn)
        self.next_btn = IconButton("skip-forward", "Siguiente", color=t.fg_dim,
                                   hover_color=t.fg, size=17, parent=self)
        self.next_btn.setFixedSize(34, 34)
        self.next_btn.clicked.connect(music.next)
        controls.addWidget(self.next_btn)
        controls.addStretch(1)
        center.addLayout(controls)
        # Progreso; en directo, en su lugar, «● En directo» (un contador que
        # vuelve a 0:00 al pausar y una barra vacía no dicen nada)
        self.progress_stack = QStackedWidget(self)
        bar = QWidget(self.progress_stack)
        progress = QHBoxLayout(bar)
        progress.setContentsMargins(0, 0, 0, 0)
        progress.setSpacing(8)
        time_font = fonts.display(fonts.XS, QFont.Weight.Normal)
        self.pos_label = QLabel("0:00", bar)
        self.pos_label.setFont(time_font)
        self.pos_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.pos_label.setMinimumWidth(QFontMetrics(time_font).horizontalAdvance("00:00") + 2)
        progress.addWidget(self.pos_label)
        self.seek = _SeekSlider(bar)
        self.seek.seek_requested.connect(music.seek)
        progress.addWidget(self.seek, 1)
        self.dur_label = QLabel("0:00", bar)
        self.dur_label.setFont(time_font)
        self.dur_label.setMinimumWidth(self.pos_label.minimumWidth())
        progress.addWidget(self.dur_label)
        self.progress_stack.addWidget(bar)
        live_page = QWidget(self.progress_stack)
        lp = QHBoxLayout(live_page)
        lp.setContentsMargins(0, 0, 0, 0)
        lp.setSpacing(6)
        lp.addStretch(1)
        self.live = _Badge(live_page)
        self.live.setToolTip("En directo: no se puede avanzar ni retroceder")
        lp.addWidget(self.live, 0, Qt.AlignVCenter)
        self.buffer_badge = _Badge(live_page)
        self.buffer_badge.setToolTip("Tras un corte, la radio suena unos segundos por detrás "
                                     "del directo para no volver a cortarse")
        self.buffer_badge.set_badge('')
        lp.addWidget(self.buffer_badge, 0, Qt.AlignVCenter)
        lp.addStretch(1)
        self.progress_stack.addWidget(live_page)
        center.addWidget(self.progress_stack)
        center_box = QWidget(self)
        center_box.setLayout(center)
        center_box.setMinimumWidth(260)
        center_box.setMaximumWidth(330)
        h.addWidget(center_box, 1)

        # Derecha: volumen y «solo durante el enfoque»
        right = QVBoxLayout()
        right.setSpacing(10)
        vol = QHBoxLayout()
        vol.setSpacing(4)
        vol.addStretch(1)
        self.mute_btn = IconButton("volume-2", "Silenciar", color=t.fg_dim, hover_color=t.fg,
                                   size=16, parent=self)
        self.mute_btn.setFixedSize(28, 28)
        self.mute_btn.clicked.connect(self._toggle_mute)
        vol.addWidget(self.mute_btn)
        self.volume = QSlider(Qt.Horizontal, self)
        self.volume.setRange(0, 100)
        self.volume.setFixedWidth(112)
        self.volume.setCursor(Qt.PointingHandCursor)
        self.volume.setValue(int(round(music.volume() * 100)))
        self.volume.valueChanged.connect(self._on_volume)
        vol.addWidget(self.volume)
        right.addLayout(vol)
        focus = QHBoxLayout()
        focus.setSpacing(8)
        focus.addStretch(1)
        self.focus_label = QLabel("Solo durante el enfoque", self)
        self.focus_label.setFont(fonts.font(fonts.XS))
        self.focus_label.setToolTip(FOCUS_TIP)
        focus.addWidget(self.focus_label)
        self.focus_toggle = ToggleSwitch(FOCUS_TIP, parent=self)
        self.focus_toggle.setChecked(music.focus_only())
        self.focus_toggle.toggled.connect(music.set_focus_only)
        focus.addWidget(self.focus_toggle)
        right.addLayout(focus)
        h.addLayout(right)

        self.restyle()

    # ---------- acciones ----------
    def _toggle(self) -> None:
        if self.music.current() is None:
            self.music.play()
        else:
            self.music.toggle_pause()

    def _toggle_mute(self) -> None:
        self.music.set_muted(not self.music.muted())
        self._sync_volume_icon()

    def _on_volume(self, value: int) -> None:
        self.music.set_volume(value / 100)
        if value > 0 and self.music.muted():
            self.music.set_muted(False)  # mover el volumen quita el silencio
        self._sync_volume_icon()

    def _sync_volume_icon(self) -> None:
        muted = self.music.muted() or self.volume.value() == 0
        self.mute_btn.set_icon_name("volume-x" if muted else "volume-2")
        self.mute_btn.setToolTip("Quitar el silencio" if self.music.muted() else "Silenciar")
        self.volume.setToolTip(f"Volumen: {self.volume.value()} %")

    # ---------- estado ----------
    def refresh(self, cur: Optional[Dict[str, Any]], playing: bool, error: str,
                can_play: bool) -> None:
        """error: motivo del último error (current()['error']), para state 'error'."""
        t = tokens()
        self._cur, self._playing = cur, playing
        waiting = bool(cur and cur.get('waiting_focus'))
        if cur is None:
            self.cover.set_cover("music", False)
            self.title.set_full_text("Sin música")  # el ambiente puede estar sonando
            sub, color = "Elige una lista y pulsa «Reproducir»", t.fg_muted
            self.quality.set_badge('')
            self.live.set_badge('')
            self.buffer_badge.set_badge('')
            self.prev_btn.setEnabled(False)
            self.next_btn.setEnabled(False)
            self.play_btn.setEnabled(can_play)
            self._set_progress(0, 0, False, False)
        else:
            live = bool(cur.get('live'))
            state = cur.get('state', 'idle')
            # Degradado de «activo» solo si de verdad suena o va a sonar ya
            self.cover.set_cover("radio" if live else "music",
                                 not waiting and state != 'error')
            self.title.set_full_text(cur.get('title', ''))
            sub, color = self._subtitle(cur, playing, error)
            self.quality.set_badge(quality_text(cur.get('codec', ''), cur.get('bitrate_kbps', 0)))
            self.live.set_badge("En directo" if live else '', t.error)
            buffered = buffer_text(cur.get('buffer_s'))
            self.buffer_badge.set_badge(f"Búfer {buffered}" if live and buffered
                                        and state == 'playing' else '')
            count = cur.get('count', 0)
            # Con un solo elemento, «anterior» solo sirve para volver a empezar una pista
            self.prev_btn.setEnabled(count > 1 or (count == 1 and not live))
            self.next_btn.setEnabled(count > 1)
            self.play_btn.setEnabled(True)
            self._set_progress(cur.get('position_ms', 0), cur.get('duration_ms', 0), live,
                               bool(cur.get('seekable')))
        is_error = bool(cur) and cur.get('state') == 'error'
        self.subtitle.setVisible(not is_error)
        self.detail.setVisible(is_error)
        self.subtitle.set_full_text(sub)
        self.detail.setText(sub if is_error else '')
        self._sub_color = color
        self.subtitle.setStyleSheet(f"color: {color};")
        self.detail.setStyleSheet(f"color: {color};")
        self.title.setToolTip(self.title.full_text() if self.title.is_elided() else '')
        self.subtitle.setToolTip(sub if self.subtitle.is_elided() else '')
        if bool(self.play_btn.property("waiting")) != waiting:
            self.play_btn.setProperty("waiting", waiting)
            self.play_btn.style().unpolish(self.play_btn)
            self.play_btn.style().polish(self.play_btn)
        if not self.play_btn.isEnabled():
            icon_color = t.fg_muted
        else:
            icon_color = t.fg_dim if waiting else t.on_accent
        self.play_btn.setIcon(tinted_icon("pause" if playing else "play", icon_color, 18))
        self.play_btn.setToolTip("Pausar: no sonará al empezar el pomodoro" if waiting
                                 else "Pausar" if playing else "Reproducir")
        if self.focus_toggle.isChecked() != self.music.focus_only():
            self.focus_toggle.blockSignals(True)
            self.focus_toggle.setChecked(self.music.focus_only())
            self.focus_toggle.blockSignals(False)
        self._sync_volume_icon()

    @staticmethod
    def _subtitle(cur: Dict[str, Any], playing: bool, error: str) -> Tuple[str, str]:
        t = tokens()
        state = cur.get('state', 'idle')
        if cur.get('waiting_focus'):
            return "Sonará al enfocar", t.info  # pomodoro o sesión Libre en marcha
        if state == 'error':
            return (error or cur.get('error') or STATE_TEXT['error']), t.error
        if state == 'reconnecting':
            return ("Reconectando con la emisora…" if cur.get('live')
                    else STATE_TEXT['reconnecting']), t.warning
        if state in ('loading', 'buffering'):
            buffered = buffer_text(cur.get('buffer_s')) if state == 'buffering' else ''
            return (f"{STATE_TEXT[state]} ({buffered})" if buffered
                    else STATE_TEXT[state]), t.fg_muted
        sub = cur.get('subtitle') or ''
        if state == 'paused' and not playing:
            return (f"En pausa · {sub}" if sub else "En pausa"), t.fg_muted
        if state == 'idle':   # lo último que sonó (al arrancar no suena solo)
            return sub, t.fg_muted
        return sub, t.fg_dim

    def _set_progress(self, pos: int, dur: int, live: bool, seekable: bool) -> None:
        self.progress_stack.setCurrentIndex(1 if live else 0)
        if live:
            self.seek.setEnabled(False)
            self.seek.setRange(0, 0)
            return
        self.seek.setEnabled(seekable and dur > 0)
        self.seek.setRange(0, max(0, int(dur)))
        if not self.seek.isSliderDown():
            self.seek.setValue(int(pos))
        self.pos_label.setText(fmt_ms(pos))
        self.dur_label.setText(fmt_ms(dur) if dur > 0 else "0:00")

    def set_position(self, ms: int) -> None:
        cur = self._cur
        if cur is None or cur.get('live'):
            return
        if not self.seek.isSliderDown() and self.seek.maximum() > 0:
            self.seek.setValue(int(ms))
        self.pos_label.setText(fmt_ms(self.seek.value() if self.seek.isSliderDown() else ms))

    def restyle(self) -> None:
        t = tokens()
        self.setStyleSheet(
            f"#nowPlaying {{ background: {t.surface}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}")
        # Armada a la espera del enfoque: no suena, así que sin el acento
        self.play_btn.setStyleSheet(
            f"#npPlay {{ background: {t.accent}; border: none; border-radius: 20px; }}"
            f"#npPlay:hover {{ background: {t.accent_hover}; }}"
            f"#npPlay[waiting=\"true\"] {{ background: {t.surface_active}; }}"
            f"#npPlay[waiting=\"true\"]:hover {{ background: {t.border}; }}"
            f"#npPlay:disabled {{ background: {t.surface_active}; }}")
        # En directo (o sin nada cargado) la barra no tiene asa ni relleno
        self.seek.setStyleSheet(
            f"QSlider::handle:horizontal:disabled {{ background: transparent; }}"
            f"QSlider::sub-page:horizontal:disabled {{ background: {t.bg_header}; }}")
        self.title.setStyleSheet(f"color: {t.fg};")
        for lab in (self.pos_label, self.dur_label, self.focus_label):
            lab.setStyleSheet(f"color: {t.fg_muted if lab is not self.focus_label else t.fg_dim};")
        for b in (self.prev_btn, self.next_btn, self.mute_btn):
            b.set_colors(t.fg_dim, t.fg)
        self.cover.update()
        self.quality.update()
        self.live.update()
        self.buffer_badge.update()
        self._sync_volume_icon()


# ================================================================ el diálogo
class MusicDialog(BrandedDialog):
    def __init__(self, parent_window, music, ambient=None):
        super().__init__(parent_window, "Música", icon="music",
                         subtitle="Tus radios, enlaces y archivos, hasta MP3 a 320 kbps")
        self.music = music
        self.ambient = ambient            # sonidos de ambiente (None: sin su entrada)
        self.mixer: Optional[AmbientMixer] = None
        self._live = True
        self._list_id: Optional[str] = None
        # Enlaces comprobándose: (texto, lista pedida; None = la decide el servicio)
        self._pending: List[Tuple[str, Optional[str]]] = []
        self._adding_local = False
        self._status_kind = ''
        self._empty_chips: List[Tuple[QLabel, str]] = []
        self._rows: Dict[str, int] = {}   # id del elemento → fila de la vista
        self._status_timer = QTimer(self)
        self._status_timer.setSingleShot(True)
        self._status_timer.timeout.connect(lambda: self._set_status(''))
        self.setAcceptDrops(True)
        card = self._card
        card.setMinimumSize(CARD_MIN)

        body = QHBoxLayout()
        body.setSpacing(16)
        body.addLayout(self._build_left(card))
        panel = self._build_panel(card)
        # El mezclador de ambiente comparte el hueco del panel de la lista
        self.right_stack = QStackedWidget(card)
        self.right_stack.addWidget(panel)
        if ambient is not None:
            self.mixer = AmbientMixer(ambient, self.right_stack)
            self.right_stack.addWidget(self.mixer)
            self.mixer_hint = _DropHint(self.mixer)
            self.mixer.installEventFilter(self)
        body.addWidget(self.right_stack, 1)
        self._lay.addLayout(body, 1)
        self._lay.addSpacing(4)
        self.now = _NowPlaying(music, card)
        self._lay.addWidget(self.now)
        for b in self.add_buttons([("Cerrar", "ghost", 0)]):
            b.setAutoDefault(False)
            self._close_btn = b

        self._connections = [
            (music.library_changed, self._on_library_changed),
            (music.now_playing_changed, self._on_now_playing),
            (music.playing_changed, self._on_now_playing),
            (music.position_changed, self.now.set_position),
            (music.link_resolved, self._on_link_resolved),
            (music.error_occurred, self._on_error),
            (ThemeManager.instance().theme_changed, self._restyle),
        ]
        if ambient is not None:
            self._connections += [(ambient.changed, self._on_ambient_changed),
                                  (ambient.error_occurred, self._on_ambient_error)]
        for signal, slot in self._connections:
            signal.connect(slot)
        self.finished.connect(self._disconnect_live)

        self._list_id = self._initial_list()
        self._refresh_lists()
        self._refresh_items(reset_scroll=True)
        self._restyle()
        self._set_tab_order()
        cur = music.current()
        if cur and cur.get('list_id') == self._list_id:
            row = self.items_view.row_of(cur['item_id'])
            if row >= 0:
                self.items_view.scrollToItem(self.items_view.item(row),
                                             QAbstractItemView.PositionAtCenter)
        # Listo para pegar un enlace, salvo con pistas a la vista en la lofi
        lofi_with_tracks = not self._user_list_selected() and self.items_view.count()
        (self.items_view if lofi_with_tracks else self.link_edit).setFocus()

    # ---------------------------------------------------------- construcción
    def _build_left(self, card: QWidget) -> QVBoxLayout:
        col = QVBoxLayout()
        col.setSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("Listas", card)
        title.setFont(fonts.font(fonts.SMALL, bold=True))
        head.addWidget(title)
        self.lists_count = QLabel(card)
        self.lists_count.setFont(fonts.font(fonts.XS))
        self.lists_count.setProperty("role", "muted")
        head.addWidget(self.lists_count)
        head.addStretch(1)
        col.addLayout(head)
        self.lists_view = _ListsView(card)
        self.lists_view.setFixedWidth(LEFT_W)
        self.lists_view.currentItemChanged.connect(self._on_list_clicked)
        self.lists_view.menu_requested.connect(self._list_menu)
        col.addWidget(self.lists_view, 1)
        self.new_list_btn = QPushButton("Nueva lista", card)
        self.new_list_btn.setProperty("variant", "ghost")
        self.new_list_btn.setCursor(Qt.PointingHandCursor)
        self.new_list_btn.setAutoDefault(False)
        self.new_list_btn.setIconSize(QSize(16, 16))
        self.new_list_btn.setFixedWidth(LEFT_W)
        self.new_list_btn.setStyleSheet("QPushButton { text-align: left; padding-left: 10px; }")
        self.new_list_btn.setToolTip("Crear una lista para tus radios, enlaces y archivos")
        self.new_list_btn.clicked.connect(self._new_list)
        col.addWidget(self.new_list_btn)
        return col

    def _build_panel(self, card: QWidget) -> QFrame:
        panel = QFrame(card)
        panel.setObjectName("musicPanel")
        self.panel = panel
        v = QVBoxLayout(panel)
        v.setContentsMargins(16, 14, 16, 16)
        v.setSpacing(12)

        # Cabecera de la lista
        head = QHBoxLayout()
        head.setSpacing(6)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self.list_title = _elided(panel, fonts.display(fonts.MEDIUM, tabular=False))
        titles.addWidget(self.list_title)
        self.list_meta = QLabel(panel)
        self.list_meta.setFont(fonts.font(fonts.XS))
        self.list_meta.setProperty("role", "muted")
        titles.addWidget(self.list_meta)
        head.addLayout(titles, 1)
        t = tokens()

        def tool(icon: str, tip: str, slot) -> IconButton:
            b = IconButton(icon, tip, color=t.fg_dim, hover_color=t.fg, size=17, parent=panel)
            b.setFixedSize(34, 34)
            b.clicked.connect(slot)
            head.addWidget(b, 0, Qt.AlignVCenter)
            return b
        self.shuffle_btn = tool("shuffle", "Aleatorio", self._toggle_shuffle)
        self.repeat_btn = tool("repeat", "Repetir", self._cycle_repeat)
        self.more_btn = tool("ellipsis", "Más opciones de la lista", self._open_more_menu)
        head.addSpacing(6)
        self.play_list_btn = QPushButton("Reproducir", panel)
        self.play_list_btn.setProperty("variant", "accent")
        self.play_list_btn.setCursor(Qt.PointingHandCursor)
        self.play_list_btn.setAutoDefault(False)
        self.play_list_btn.setIconSize(QSize(14, 14))
        self.play_list_btn.setMinimumWidth(116)
        self.play_list_btn.clicked.connect(self._play_list)
        head.addWidget(self.play_list_btn, 0, Qt.AlignVCenter)
        v.addLayout(head)

        # Barra para añadir (listas propias)
        self.composer_row = QWidget(panel)
        cr = QHBoxLayout(self.composer_row)
        cr.setContentsMargins(0, 0, 0, 0)
        cr.setSpacing(8)
        self.composer = _Composer(self.composer_row)
        self.link_edit = self.composer.edit
        self.link_edit.returnPressed.connect(self._submit_link)
        self.composer.add_btn.clicked.connect(self._submit_link)
        cr.addWidget(self.composer, 1)
        self.files_btn = self._side_button("Archivos…", "Añadir archivos de audio o listas "
                                           "(.m3u, .pls) de tu equipo", self._pick_files)
        cr.addWidget(self.files_btn)
        self.folder_btn = self._side_button("Carpeta…", "Añadir todas las pistas de una "
                                            "carpeta", self._pick_folder)
        cr.addWidget(self.folder_btn)
        v.addWidget(self.composer_row)

        # Barra de la carpeta lofi (solo lectura)
        self.lofi_row = QWidget(panel)
        lr = QHBoxLayout(self.lofi_row)
        lr.setContentsMargins(4, 0, 0, 0)
        lr.setSpacing(8)
        self.lofi_icon = QLabel(self.lofi_row)
        self.lofi_icon.setFixedSize(16, 16)
        lr.addWidget(self.lofi_icon)
        self.lofi_text = _elided(self.lofi_row, fonts.font(fonts.XS))
        self.lofi_text.set_elide_mode(Qt.ElideMiddle)   # el final de la ruta es lo útil
        lr.addWidget(self.lofi_text, 1)
        self.open_btn = self._side_button("Abrir carpeta", "Abrir la carpeta en el "
                                          "Explorador para copiar tus pistas", self._open_lofi)
        lr.addWidget(self.open_btn)
        self.rescan_btn = self._side_button("Releer", "Volver a leer la carpeta",
                                            self._rescan_lofi)
        lr.addWidget(self.rescan_btn)
        v.addWidget(self.lofi_row)

        # Línea de estado (resolviendo, añadidos, errores)
        self.status_row = QWidget(panel)
        sr = QHBoxLayout(self.status_row)
        sr.setContentsMargins(4, 0, 0, 0)
        sr.setSpacing(8)
        self.status_icon = QLabel(self.status_row)
        self.status_icon.setFixedSize(14, 16)
        sr.addWidget(self.status_icon, 0, Qt.AlignTop)
        self.status_label = QLabel(self.status_row)
        self.status_label.setFont(fonts.font(fonts.XS))
        self.status_label.setWordWrap(True)
        sr.addWidget(self.status_label, 1)
        v.addWidget(self.status_row)
        self.status_row.setVisible(False)

        # Elementos (o el estado vacío)
        self.items_card = QFrame(panel)
        self.items_card.setProperty("card", True)
        ic = QVBoxLayout(self.items_card)
        ic.setContentsMargins(4, 4, 4, 4)
        self.items_stack = QStackedWidget(self.items_card)
        self.items_view = _ItemsView(self.items_stack)
        self.items_view.play_requested.connect(self._play_item)
        self.items_view.rename_requested.connect(self._rename_item)
        self.items_view.remove_requested.connect(self._remove_item)
        self.items_view.reordered.connect(self._on_reordered)
        self.items_stack.addWidget(self.items_view)
        self.items_stack.addWidget(self._empty_user(self.items_stack))
        self.items_stack.addWidget(self._empty_lofi(self.items_stack))
        ic.addWidget(self.items_stack)
        v.addWidget(self.items_card, 1)

        self.drop_hint = _DropHint(panel)
        panel.installEventFilter(self)
        return panel

    def _side_button(self, text: str, tip: str, slot) -> QPushButton:
        b = QPushButton(text, self._card)
        b.setCursor(Qt.PointingHandCursor)
        b.setAutoDefault(False)
        b.setIconSize(QSize(15, 15))
        b.setFixedHeight(40)
        b.setStyleSheet("QPushButton { padding: 0px 12px; }")
        b.setToolTip(tip)
        b.clicked.connect(slot)
        return b

    def _empty_state(self, parent: QWidget, icon: str, title: str, text: str,
                     extra: Optional[QWidget] = None, note: Optional[str] = None) -> QWidget:
        # Dentro de un scroll sin marco: con poco alto (1366×768, 1920×1080 al
        # 150 %) el diálogo se encoge y aplastaría el texto y los ejemplos
        scroll = QScrollArea(parent)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        page = QWidget(scroll)
        scroll.setWidget(page)
        outer = QHBoxLayout(page)
        outer.setContentsMargins(16, 12, 16, 12)
        outer.addStretch(1)
        col = QWidget(page)
        col.setFixedWidth(440)
        v = QVBoxLayout(col)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(6)
        v.addStretch(1)
        chip = icon_chip(icon, None, size=44, icon_size=22, parent=col)
        self._empty_chips.append((chip, icon))
        v.addWidget(chip, 0, Qt.AlignHCenter)
        v.addSpacing(6)
        head = QLabel(title, col)
        head.setFont(fonts.font(fonts.NORMAL, bold=True))
        head.setAlignment(Qt.AlignHCenter)
        v.addWidget(head)
        body = QLabel(text, col)
        body.setFont(fonts.font(fonts.SMALL))
        body.setProperty("role", "dim")
        body.setWordWrap(True)
        body.setAlignment(Qt.AlignHCenter)
        v.addWidget(body)
        if extra is not None:
            v.addSpacing(8)
            v.addWidget(extra)
        if note:
            v.addSpacing(6)
            n = QLabel(note, col)
            n.setFont(fonts.font(fonts.XS))
            n.setProperty("role", "muted")
            n.setWordWrap(True)
            n.setAlignment(Qt.AlignHCenter)
            v.addWidget(n)
        v.addStretch(1)
        outer.addWidget(col)
        outer.addStretch(1)
        return scroll

    def _empty_user(self, parent: QWidget) -> QWidget:
        examples = QFrame(parent)
        examples.setObjectName("musicExamples")
        self._examples = examples
        g = QGridLayout(examples)
        g.setContentsMargins(14, 10, 14, 10)
        g.setHorizontalSpacing(16)
        g.setVerticalSpacing(4)
        rows = (("https://radio.ejemplo.com/stream.mp3", "radio en directo"),
                ("https://ejemplo.com/musica/tema.mp3", "canción, hasta 320 kbps"),
                ("https://ejemplo.com/emisora.pls", "lista de una radio (.pls, .m3u)"))
        for i, (link, what) in enumerate(rows):
            a = QLabel(link, examples)
            a.setFont(fonts.font(fonts.XS))
            a.setProperty("role", "dim")
            a.setTextInteractionFlags(Qt.TextSelectableByMouse)
            g.addWidget(a, i, 0)
            b = QLabel(what, examples)
            b.setFont(fonts.font(fonts.XS))
            b.setProperty("role", "muted")
            g.addWidget(b, i, 1)
        g.setColumnStretch(1, 1)
        return self._empty_state(
            parent, "link", "Añade tu primera radio o canción",
            "Pega arriba el enlace directo de una radio, de un archivo de audio o de una "
            "lista de reproducción, o suelta aquí archivos, carpetas y listas .m3u o .pls.",
            extra=examples, note=UNSUPPORTED_NOTE)

    def _empty_lofi(self, parent: QWidget) -> QWidget:
        return self._empty_state(
            parent, "folder-open", "La carpeta lofi está vacía",
            "Copia tus pistas (MP3, AAC, OGG, FLAC, WAV…) con «Abrir carpeta» y pulsa "
            "«Releer». Para radios y enlaces, pega arriba su enlace o crea una lista "
            "con «Nueva lista».")

    # ---------------------------------------------------------- tamaño
    def sizeHint(self) -> QSize:
        hint = super().sizeHint()
        left, top, right, bottom = SHADOW_MARGINS
        want = QSize(CARD_SIZE.width() + left + right, CARD_SIZE.height() + top + bottom)
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            want.setHeight(min(want.height(), area.height()))
        return hint.expandedTo(want)

    def eventFilter(self, obj, event):
        if obj is getattr(self, 'panel', None) and event.type() == QEvent.Resize:
            self.drop_hint.setGeometry(self.panel.rect())
        elif (event.type() == QEvent.Resize and obj is not None
              and obj is getattr(self, 'mixer', None)):
            self.mixer_hint.setGeometry(self.mixer.rect())
        return super().eventFilter(obj, event)

    def _set_tab_order(self) -> None:
        """Tab sigue el orden visual: listas → cabecera de la lista → añadir
        → elementos → «Ahora suena» → Cerrar (lo oculto se salta solo). El
        mezclador de ambiente, si está, ocupa el hueco de la lista: va tras
        las listas."""
        now = self.now
        mixer = self.mixer.focus_chain() if self.mixer is not None else []
        chain = [self.lists_view, self.new_list_btn, *mixer, self.shuffle_btn, self.repeat_btn,
                 self.more_btn, self.play_list_btn, self.link_edit, self.composer.add_btn,
                 self.files_btn, self.folder_btn, self.open_btn, self.rescan_btn,
                 self.items_view, now.prev_btn, now.play_btn, now.next_btn, now.seek,
                 now.mute_btn, now.volume, now.focus_toggle, self._close_btn]
        for first, second in zip(chain, chain[1:]):
            QWidget.setTabOrder(first, second)

    # ---------------------------------------------------------- ciclo de vida
    def _disconnect_live(self, *_args) -> None:
        """Al cerrar: suelta las señales del servicio y del tema para no
        repintarse oculto mientras espera a destruirse."""
        if not self._live:
            return
        self._live = False
        self._status_timer.stop()
        if self.mixer is not None:
            self.mixer.disconnect_live()
        for signal, slot in self._connections:
            try:
                signal.disconnect(slot)
            except (RuntimeError, TypeError):
                pass

    def _restyle(self) -> None:
        t = tokens()
        self.panel.setStyleSheet(
            f"#musicPanel {{ background: {t.bg}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}")
        self._examples.setStyleSheet(
            f"#musicExamples {{ background: {t.bg_header}; border: 1px solid {t.border};"
            f" border-radius: {t.radius_small}px; }}")
        self.list_title.setStyleSheet(f"color: {t.fg};")
        self.new_list_btn.setIcon(tinted_icon("add", t.accent, 16))
        for b, icon in ((self.files_btn, "file-music"), (self.folder_btn, "folder-plus"),
                        (self.open_btn, "folder-open"), (self.rescan_btn, "sync")):
            b.setIcon(tinted_icon(icon, t.fg_dim, 15))
        self.lofi_icon.setPixmap(tinted_icon("folder", t.fg_muted, 16).pixmap(16, 16))
        self.lofi_text.setStyleSheet(f"color: {t.fg_muted};")
        self.more_btn.set_colors(t.fg_dim, t.fg)
        for chip, icon in self._empty_chips:
            style_icon_chip(chip, icon)
        self.composer.restyle()
        self.now.restyle()
        self._set_status(self.status_label.text() if self.status_row.isVisibleTo(self) else '',
                         self._status_kind, restart=False)
        self._on_now_playing()
        self.lists_view.viewport().update()
        self.items_view.viewport().update()

    # ---------------------------------------------------------- datos
    def _lists(self) -> List[Dict[str, Any]]:
        return self.music.lists()

    def _list_info(self, list_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        list_id = self._list_id if list_id is None else list_id
        return next((x for x in self._lists() if x['id'] == list_id), None)

    def _user_list_selected(self) -> bool:
        info = self._list_info()
        return info is not None and not info['readonly']

    def _item(self, item_id: str) -> Optional[Dict[str, Any]]:
        return next((i for i in self.music.items(self._list_id) if i['id'] == item_id), None)

    def _initial_list(self) -> str:
        cur = self.music.current()
        if cur and cur.get('list_id') and self._list_info(cur['list_id']):
            return cur['list_id']
        user = [x for x in self._lists() if not x['readonly']]
        return user[0]['id'] if user else LOFI_LIST_ID

    def _can_play(self) -> bool:
        return (self.music.current() is not None
                or any(x['count'] for x in self._lists()))

    # ---------------------------------------------------------- refresco
    def _on_library_changed(self) -> None:
        self._refresh_lists()
        self._refresh_items()

    def _refresh_lists(self) -> None:
        lists = self._lists()
        rows = self._ambient_rows() + lists
        if not any(x['id'] == self._list_id for x in rows):
            user = [x for x in lists if not x['readonly']]
            self._list_id = user[0]['id'] if user else LOFI_LIST_ID
        view = self.lists_view
        bar = view.verticalScrollBar().value()
        view.blockSignals(True)
        view.elided_rows.clear()
        view.clear()
        for info in rows:
            item = QListWidgetItem(view)
            item.setData(ROLE, info)
            item.setSizeHint(QSize(0, LIST_ROW_H))
            if info['id'] == self._list_id:
                view.setCurrentItem(item)
        view.ambient_playing = bool(self.ambient is not None and self.ambient.is_playing())
        view.blockSignals(False)
        view.verticalScrollBar().setValue(bar)
        user = len(lists) - 1
        self.lists_count.setText(str(user) if user else '')

    def _refresh_items(self, reset_scroll: bool = False) -> None:
        if self._show_ambient():
            return
        info = self._list_info() or {'id': self._list_id, 'name': '', 'count': 0,
                                     'readonly': True}
        readonly = bool(info['readonly'])
        items = self.music.items(self._list_id)
        self.list_title.set_full_text(info['name'])
        self.list_title.setToolTip(info['name'] if self.list_title.is_elided() else '')
        self.list_meta.setText(self._meta_text(items, readonly))
        # La barra del enlace también en la lofi: lo pegado allí crea su lista
        # (o va a «Mis enlaces»), como al soltarlo
        self.lofi_row.setVisible(readonly)
        self.more_btn.setVisible(not readonly)
        if readonly:
            folder = str(self.music.lofi_dir)
            self.lofi_text.set_full_text(f"Pistas de {folder}")
            self.lofi_text.setToolTip(folder)
        view = self.items_view
        keep = view.item_id(view.currentRow())
        bar = 0 if reset_scroll else view.verticalScrollBar().value()
        view.set_readonly(readonly)
        view.setUpdatesEnabled(False)
        view.blockSignals(True)
        view.clear()
        for data in items:
            item = QListWidgetItem(view)
            item.setData(ROLE, data)
            item.setSizeHint(QSize(0, ROW_H))
        self._rows = {data['id']: n for n, data in enumerate(items)}
        row = self._rows.get(keep, -1) if keep else -1
        if row >= 0:
            view.setCurrentRow(row)
        view.blockSignals(False)
        view.setUpdatesEnabled(True)
        view.verticalScrollBar().setValue(bar)
        self.items_stack.setCurrentIndex(0 if items else (2 if readonly else 1))
        self.play_list_btn.setEnabled(bool(items))
        self._on_now_playing()

    @staticmethod
    def _meta_text(items: List[Dict[str, Any]], readonly: bool) -> str:
        n = len(items)
        if readonly:
            return f"{_plural(n, 'pista', 'pistas')} · solo lectura"
        parts = [_plural(n, "elemento", "elementos")]
        live = sum(1 for i in items if i.get('live'))
        if live:
            parts.append(_plural(live, "radio en directo", "radios en directo"))
        return " · ".join(parts)

    def _on_now_playing(self, *_args) -> None:
        if not self._live:
            return
        music = self.music
        cur = music.current()
        playing = music.is_playing
        state = cur.get('state') if cur else 'idle'
        self.now.refresh(cur, playing, (cur or {}).get('error', ''), self._can_play())
        view = self.items_view
        here = cur is not None and cur.get('list_id') == self._list_id
        if here:
            self._sync_current_row(cur['item_id'])
        view.current_id = cur['item_id'] if here else None
        view.current_playing = bool(here and playing and state == 'playing')
        view.current_duration = cur.get('duration_ms', 0) if here else 0
        view.viewport().update()
        # El ecualizador, solo si de verdad suena (armada esperando al enfoque, no)
        self.lists_view.playing_list = (cur['list_id'] if cur and playing and state == 'playing'
                                        else None)
        self.lists_view.viewport().update()
        self._update_controls(cur, playing)

    def _sync_current_row(self, item_id: str) -> None:
        """Lo que se descubre sonando (en directo, bitrate, códec de la radio)
        se guarda sin library_changed: se copia a su fila y al recuento."""
        row = self._rows.get(item_id, -1)
        item = self.items_view.item(row) if row >= 0 else None
        if item is None:
            return
        items = self.music.items(self._list_id)
        fresh = next((i for i in items if i['id'] == item_id), None)
        if fresh is not None and fresh != item.data(ROLE):
            item.setData(ROLE, fresh)
            info = self._list_info()
            self.list_meta.setText(self._meta_text(items, bool(info and info['readonly'])))

    def _update_controls(self, cur, playing: bool) -> None:
        t = tokens()
        here = cur is not None and cur.get('list_id') == self._list_id
        pause = here and playing
        self.play_list_btn.setText("Pausar" if pause else "Reproducir")
        self.play_list_btn.setIcon(tinted_icon(
            "pause" if pause else "play",
            t.on_accent if self.play_list_btn.isEnabled() else t.fg_muted, 14))
        shuffle = self.music.shuffle()
        self.shuffle_btn.set_colors(t.accent if shuffle else t.fg_muted,
                                    t.accent_hover if shuffle else t.fg)
        self.shuffle_btn.setToolTip("Aleatorio: activado" if shuffle
                                    else "Aleatorio: desactivado")
        mode = self.music.repeat()
        self.repeat_btn.set_icon_name("repeat-1" if mode == 'one' else "repeat")
        self.repeat_btn.set_colors(t.fg_muted if mode == 'off' else t.accent,
                                   t.fg if mode == 'off' else t.accent_hover)
        self.repeat_btn.setToolTip({'all': "Repetir la lista", 'one': "Repetir la pista",
                                    'off': "Sin repetir: para al acabar la lista"}[mode])

    # ---------------------------------------------------------- estado
    def _set_status(self, text: str, kind: str = 'info', restart: bool = True) -> None:
        t = tokens()
        self._status_kind = kind if text else ''
        if restart:
            self._status_timer.stop()
        if not text:
            self.status_row.setVisible(False)
            self.status_label.clear()
            return
        icon, color = {'busy': ("sync", t.fg_muted), 'ok': ("check-circle", t.accent),
                       'error': ("circle-alert", t.error)}.get(kind, ("info", t.fg_muted))
        self.status_icon.setPixmap(tinted_icon(icon, color, 14).pixmap(14, 14))
        self.status_label.setText(text)
        self.status_label.setStyleSheet(f"color: {t.fg_dim if kind == 'busy' else color};")
        self.status_row.setVisible(True)
        if restart and kind in ('ok', 'info'):
            self._status_timer.start(STATUS_OK_MS)

    def _on_error(self, message: str) -> None:
        # Lo que no es un fallo de reproducción (lista vacía, nada añadido…)
        # va a la línea de estado; el resto lo cuenta «Ahora suena» (el
        # servicio guarda el motivo: sigue ahí al reabrir el diálogo)
        if self._adding_local or self.music.state != 'error':
            self._set_status(message, 'error')
        self._on_now_playing()

    # ---------------------------------------------------------- listas
    def _on_list_clicked(self, item, _previous=None) -> None:
        if item is not None:
            self._select_list(item.data(ROLE)['id'])

    def _select_list(self, list_id: str) -> None:
        if list_id == self._list_id:
            return
        self._list_id = list_id
        row = self.lists_view.row_of(list_id)
        if row >= 0 and self.lists_view.currentRow() != row:
            self.lists_view.blockSignals(True)
            self.lists_view.setCurrentRow(row)
            self.lists_view.blockSignals(False)
        self._set_status('')
        self._refresh_items(reset_scroll=True)

    def _suggest_name(self) -> str:
        names = {x['name'].lower() for x in self._lists()}
        base = "Mi lista"
        if base.lower() not in names:
            return base
        n = 2
        while f"{base} {n}".lower() in names:
            n += 1
        return f"{base} {n}"

    def _new_list(self) -> None:
        name = ask_name(self, "Nueva lista", "Nombre de la lista", self._suggest_name(),
                        ok_text="Crear", icon="list-music", max_len=MAX_LIST_NAME)
        if not name:
            return
        list_id = self.music.create_list(name)
        if list_id:
            self._select_list(list_id)
            self.link_edit.setFocus()

    def _open_more_menu(self) -> None:
        btn = self.more_btn
        self._list_menu(self._list_id, btn.mapToGlobal(QPoint(0, btn.height() + 4)))

    def _list_menu(self, list_id: str, pos: QPoint) -> None:
        info = self._list_info(list_id)
        if info is None:
            return
        t = tokens()
        menu = QMenu(self)
        play = menu.addAction(tinted_icon("play", t.fg_dim, 15), "Reproducir")
        play.setEnabled(info['count'] > 0)
        actions = {play: lambda: self.music.play(list_id)}
        if info['readonly']:
            menu.addSeparator()
            actions[menu.addAction(tinted_icon("folder-open", t.fg_dim, 15),
                                   "Abrir carpeta")] = self._open_lofi
            actions[menu.addAction(tinted_icon("sync", t.fg_dim, 15), "Releer")] = \
                self._rescan_lofi
        else:
            menu.addSeparator()
            actions[menu.addAction(tinted_icon("edit", t.fg_dim, 15), "Renombrar…")] = \
                lambda: self._rename_list(list_id)
            actions[menu.addAction(tinted_icon("delete", t.danger, 15), "Eliminar lista…")] = \
                lambda: self._delete_list(list_id)
        chosen = menu.exec(pos)
        menu.deleteLater()
        if chosen in actions:
            actions[chosen]()

    def _rename_list(self, list_id: str) -> None:
        info = self._list_info(list_id)
        if info is None or info['readonly']:
            return
        name = ask_name(self, "Renombrar lista", "Nombre de la lista", info['name'],
                        icon="edit", max_len=MAX_LIST_NAME)
        if name and name != info['name']:
            self.music.rename_list(list_id, name)

    def _delete_list(self, list_id: str) -> None:
        info = self._list_info(list_id)
        if info is None or info['readonly']:
            return
        n = info['count']
        what = f"«{info['name']}»" + (f" y sus {_plural(n, 'elemento', 'elementos')}"
                                      if n else '')
        message = (f"Se quitará {what}. Los archivos de tu equipo no se borran.")
        if dialogs.confirm(self, "Eliminar lista", message, ok_text="Eliminar", danger=True):
            self.music.delete_list(list_id)

    # ---------------------------------------------------------- reproducción
    def _play_list(self) -> None:
        cur = self.music.current()
        if cur and cur.get('list_id') == self._list_id:
            if self.music.is_playing:
                self.music.pause()
            else:
                self.music.play(self._list_id, cur['item_id'])
        else:
            self.music.play(self._list_id)

    def _toggle_shuffle(self) -> None:
        self.music.set_shuffle(not self.music.shuffle())
        self._on_now_playing()

    def _cycle_repeat(self) -> None:
        order = ('all', 'one', 'off')
        mode = self.music.repeat()
        self.music.set_repeat(order[(order.index(mode) + 1) % 3] if mode in order else 'all')
        self._on_now_playing()

    def _play_item(self, item_id: str) -> None:
        if item_id:
            self.music.play(self._list_id, item_id)

    def _rename_item(self, item_id: str) -> None:
        item = self._item(item_id)
        if item is None or not self._user_list_selected():
            return
        title = ask_name(self, "Renombrar", "Título", item['title'], max_len=MAX_TITLE)
        if title and title != item['title']:
            self.music.rename_item(self._list_id, item_id, title)

    def _remove_item(self, item_id: str) -> None:
        item = self._item(item_id)
        if item is None or not self._user_list_selected():
            return
        if self.music.remove_item(self._list_id, item_id):
            self._set_status(f"Quitado «{item['title']}» de la lista", 'info')

    def _on_reordered(self, item_id: str, new_index: int) -> None:
        if self._user_list_selected():
            self.music.move_item(self._list_id, item_id, new_index)

    # ---------------------------------------------------------- añadir
    def _submit_link(self) -> None:
        text = self.link_edit.text().strip()
        if not text:
            self.link_edit.setFocus()
            return
        # En la lofi (solo lectura) el servicio elige o crea la lista, como al soltar
        target = self._list_id if self._user_list_selected() else None
        if (text, target) in self._pending:
            return  # ya se está comprobando (Enter dos veces seguidas)
        self._add_link(text, target)

    def _add_link(self, text: str, list_id: Optional[str]) -> None:
        self._pending.append((text, list_id))
        self._set_status("Comprobando el enlace…", 'busy')
        self.music.add_link(text, list_id)

    def keyPressEvent(self, event):
        # Ctrl+V que no atendió nadie (en el campo pega él): un enlace del
        # portapapeles se añade sin tener que buscar dónde pegarlo
        if event.matches(QKeySequence.Paste):
            self._paste_link()
            return
        super().keyPressEvent(event)

    def _paste_link(self) -> None:
        """Ctrl+V con el foco fuera del campo (dentro, pega él): si el
        portapapeles trae un enlace http(s), se pone en el campo y se añade."""
        text = (QGuiApplication.clipboard().text() or '').strip()
        if not text.startswith(('http://', 'https://')) or any(c.isspace() for c in text):
            return
        self._leave_mixer()
        self.link_edit.setText(text)
        self.link_edit.setFocus()
        self._submit_link()

    def _take_pending(self, list_id: str) -> Tuple[str, Optional[str]]:
        """La petición a la que responde link_resolved: la que pidió esa lista
        o, si no, una de las que dejaban la lista al servicio."""
        req = next((p for p in self._pending if p[1] and p[1] == list_id), None)
        if req is None:
            req = next((p for p in self._pending if p[1] is None), None)
        if req is None:
            return '', list_id
        self._pending.remove(req)
        return req

    def _on_link_resolved(self, list_id: str, added: int, error: str) -> None:
        text, asked = self._take_pending(list_id)
        if error:
            self._set_status(error, 'error')
            return
        info = self._list_info(list_id)
        # Solo se cambia de lista si el servicio eligió o creó el destino (el
        # enlace se pegó o soltó en la lofi) y el usuario sigue ahí: si se fue
        # a otra lista mientras se comprobaba, no se le saca de ella
        if (asked is None and info is not None and list_id != self._list_id
                and not self._user_list_selected() and not self._ambient_selected()):
            self._select_list(list_id)
        items = self.music.items(list_id)[-added:] if added and info else []
        message = _added_text(added, sum(1 for i in items if i.get('live')))
        self._set_status(f"{message} a «{info['name']}»" if info else message, 'ok')
        if text and self.link_edit.text().strip() == text:
            self.link_edit.clear()
        if list_id == self._list_id and self.items_view.count():
            self.items_view.scrollToBottom()

    def _start_dir(self) -> str:
        return QStandardPaths.writableLocation(QStandardPaths.MusicLocation) or str(Path.home())

    def _pick_files(self) -> None:
        paths, _ = QFileDialog.getOpenFileNames(self, "Añadir archivos de audio",
                                                self._start_dir(), FILE_FILTER)
        if paths:
            self._add_paths(paths)

    def _pick_folder(self) -> None:
        path = QFileDialog.getExistingDirectory(self, "Añadir una carpeta de música",
                                                self._start_dir())
        if path:
            self._add_paths([path])

    def _add_paths(self, paths: List[str]) -> int:
        """Archivos, carpetas o listas locales. Con una lista propia a la
        vista, todo va a ella; con la lofi, el servicio crea las listas (o
        usa «Mis enlaces») y se muestra la que cambió."""
        target = self._list_id if self._user_list_selected() else None
        before = {x['id']: x['count'] for x in self._lists()}
        self._adding_local = True
        try:
            added = self.music.add_paths(paths, target)
        finally:
            self._adding_local = False
        if not added:
            return 0
        changed = [x for x in self._lists()
                   if x['id'] not in before or x['count'] != before[x['id']]]
        dest = target or (changed[0]['id'] if changed else None)
        info = self._list_info(dest) if dest else None
        # Primero la lista y luego el aviso: cambiar de lista limpia la línea de estado
        if dest and dest != self._list_id:
            self._select_list(dest)
        self._set_status(f"{_added_text(added)} a «{info['name']}»" if info
                         else _added_text(added), 'ok')
        return added

    def _open_lofi(self) -> None:
        folder = self.music.lofi_dir
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def _rescan_lofi(self) -> None:
        n = self.music.rescan_lofi()
        self._set_status(_plural(n, "pista encontrada", "pistas encontradas") if n
                         else "No hay pistas en la carpeta", 'ok' if n else 'info')

    # ---------------------------------------------------------- arrastrar y soltar
    @staticmethod
    def _drop_payload(mime) -> Tuple[List[str], List[str]]:
        """(rutas locales, enlaces http/https) de lo que se arrastra."""
        paths: List[str] = []
        links: List[str] = []
        if mime.hasUrls():
            for url in mime.urls():
                if url.isLocalFile():
                    paths.append(url.toLocalFile())
                elif url.scheme() in ('http', 'https'):
                    links.append(url.toString())
        elif mime.hasText():
            text = mime.text().strip()
            if text.startswith(('http://', 'https://')) and not any(c.isspace() for c in text):
                links.append(text)
        return paths, links

    def _drop_target_text(self) -> str:
        info = self._list_info()
        if info is not None and not info['readonly']:
            return f"Suelta para añadir a «{info['name']}»"
        # Sin lista propia a la vista (lofi o mezclador) decide el servicio
        return (f"Suelta para añadir a «{self.music.loose_target_name()}»"
                " (las carpetas y listas crean la suya)")

    def dragEnterEvent(self, event):
        paths, links = self._drop_payload(event.mimeData())
        if not (paths or links):
            event.ignore()
            return
        event.acceptProposedAction()
        hint = self._visible_drop_hint()
        hint.text = self._drop_target_text()
        hint.setGeometry(hint.parentWidget().rect())
        hint.raise_()
        hint.show()

    def dragMoveEvent(self, event):
        event.acceptProposedAction()

    def dragLeaveEvent(self, event):
        self._hide_drop_hints()

    def dropEvent(self, event):
        self._hide_drop_hints()
        paths, links = self._drop_payload(event.mimeData())
        event.acceptProposedAction()
        self._leave_mixer()
        target = self._list_id if self._user_list_selected() else None
        if paths:
            # Fuera del evento: el Explorador espera a que acabe el drop
            QTimer.singleShot(0, lambda: self._add_paths(paths))
        for link in links:
            self._add_link(link, target)

    def _visible_drop_hint(self) -> '_DropHint':
        """El aviso de soltar sobre lo que se ve: el panel de la lista o el mezclador."""
        if self._ambient_selected():
            return self.mixer_hint
        return self.drop_hint

    def _hide_drop_hints(self) -> None:
        self.drop_hint.hide()
        if self.mixer is not None:
            self.mixer_hint.hide()

    # ---------------------------------------------------------- sonidos de ambiente
    def _ambient_selected(self) -> bool:
        return self.mixer is not None and self._list_id == AMBIENT_ID

    def _leave_mixer(self) -> None:
        """Antes de añadir algo pegado o soltado sobre el mezclador: a la
        carpeta lofi, donde se ven el campo del enlace y la línea de estado
        («Comprobando…», el error) y el destino lo elige el servicio, igual
        que si se hubiera soltado ahí."""
        if self._ambient_selected():
            self._select_list(LOFI_LIST_ID)

    def _on_ambient_error(self, message: str) -> None:
        """Un sonido que no se puede reproducir: con el diálogo abierto, el
        controlador no avisa (lo haría por debajo), así que avisa el diálogo."""
        if self._live and self.isVisible():
            toast.show_warning(self, message, duration=4000)

    def _ambient_rows(self) -> List[Dict[str, Any]]:
        """La entrada fija de la columna de listas (arriba del todo)."""
        if self.ambient is None:
            return []
        return [{'id': AMBIENT_ID, 'name': AMBIENT_NAME, 'readonly': True, 'ambient': True,
                 'count': len(self.ambient.active_ids())}]

    def _show_ambient(self) -> bool:
        """Pone en el hueco de la derecha el mezclador o el panel de la lista
        según lo elegido; True si es el mezclador."""
        if self.mixer is None:
            return False
        on = self._ambient_selected()
        self.right_stack.setCurrentWidget(self.mixer if on else self.panel)
        if on:
            self.mixer.refresh()
        return on

    def _on_ambient_changed(self) -> None:
        """Cuántos sonidos suenan, en la entrada de ambiente (sin rehacer la
        columna: cambia con cada deslizador)."""
        if not self._live or self.ambient is None:
            return
        view = self.lists_view
        row = view.row_of(AMBIENT_ID)
        if row >= 0:
            view.item(row).setData(ROLE, self._ambient_rows()[0])
        view.ambient_playing = self.ambient.is_playing()
        view.viewport().update()
