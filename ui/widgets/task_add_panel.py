"""Entrada rápida de tareas al pie del panel (al estilo de Todoist o To Do).

Un campo siempre visible con aspecto de tarjeta: «+ Añadir una tarea…». Al
enfocarlo (o mientras haya un borrador) se convierte en el borrador de una
tarjeta: el «+» pasa a ser el círculo de la tarea y debajo del nombre se
despliega una fila de chips: fecha (Hoy, Mañana, Próximo lunes o un
calendario), repetición y etiquetas, en las mismas columnas que el check y
el título de las tarjetas de la lista, y dos botones con el logo de Asana y
de ClickUp para crearla también allí. Las palabras «#etiqueta» del nombre se
convierten en etiquetas, con autocompletado de las conocidas. Enter guarda y
deja el campo listo para la siguiente; Escape descarta el borrador y suelta
el foco.

El marco, el icono, los chips y los menús se pintan en paintEvent: foco,
confirmación y aviso animan colores sin regenerar hojas de estilo.
"""
import re
import time
from datetime import date, timedelta
from typing import List, Optional, Tuple

from PySide6.QtCore import (QDate, QEasingCurve, QEvent, QLocale,
                            QParallelAnimationGroup, QPoint, QPointF,
                            QPropertyAnimation, QRect, QRectF, QSize, Qt, QTimer,
                            QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QIcon, QPainter, QPen,
                           QPixmap, QStandardItem, QStandardItemModel,
                           QTextCharFormat)
from PySide6.QtWidgets import (QAbstractButton, QApplication, QCalendarWidget,
                               QCompleter, QFrame, QGraphicsDropShadowEffect,
                               QGraphicsOpacityEffect, QHBoxLayout, QLabel,
                               QLayout, QLineEdit, QListWidget, QListWidgetItem,
                               QMenu, QPushButton, QScrollArea, QSizePolicy,
                               QTableView, QVBoxLayout, QWidget)

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon

# (texto, ciclo) — '' = tarea normal, no repetitiva
RECURRING_OPTIONS = [("No repetir", ""), ("Diaria", "daily"),
                     ("Semanal", "weekly"), ("Mensual", "monthly"),
                     ("Trimestral", "quarterly")]
_CYCLE_TEXT = {cycle: text for text, cycle in RECURRING_OPTIONS}
_RECURRING_TIP = ("Tarea fija (repetitiva) para actividades cíclicas fuera de "
                  "Asana/ClickUp: al completarla vuelve a Pendientes según el "
                  "ciclo elegido (cada día, semana, mes o trimestre).")

# Crear también en Asana o ClickUp (botones con el logo junto a los chips)
NO_PROJECT_TEXT = "Mis tareas (sin proyecto)"
_ASANA_REPEAT_TIP = ("Asana no admite tareas repetitivas por su API: con «Repetir» "
                     "la tarea se queda solo en PomoGrowth (no se crea en Asana).")

# Atajos del menú de fecha: (clave, texto, icono)
DUE_SHORTCUTS = (("today", "Hoy", "sun"),
                 ("tomorrow", "Mañana", "sunrise"),
                 ("next_monday", "Próximo lunes", "calendar-range"))

_MONTHS = ('ene', 'feb', 'mar', 'abr', 'may', 'jun',
           'jul', 'ago', 'sep', 'oct', 'nov', 'dic')
_MONTHS_LONG = ('enero', 'febrero', 'marzo', 'abril', 'mayo', 'junio', 'julio',
                'agosto', 'septiembre', 'octubre', 'noviembre', 'diciembre')
_WEEKDAYS = ('lunes', 'martes', 'miércoles', 'jueves', 'viernes', 'sábado', 'domingo')
_WEEKDAYS_SHORT = ('lun', 'mar', 'mié', 'jue', 'vie', 'sáb', 'dom')
# Tags internos: no se sugieren
_HIDDEN_TAGS = {'asana', 'asana_deleted'}

# «#etiqueta» al inicio o tras un espacio (así «C#» o «tarea#2» no cuentan).
# Letras con tilde, ñ, cifras, guion y guion bajo; ver _is_tag
_TAG_RE = re.compile(r'(?<!\S)#(\w[\w\-]*)')
_TYPED_TAG_RE = re.compile(r'(?<!\S)#(\w[\w\-]*)(?=\s)')   # ya cerrada con espacio
_PARTIAL_TAG_RE = re.compile(r'(?<!\S)#([\w\-]*)$')        # a medio escribir

ROW_H = 44                 # alto del campo (la entrada colapsada)
# Columnas de task_card (_PAD_H 12 + check de 33 px + 12): el icono queda
# centrado sobre el check (x≈28) y el texto y los chips empiezan donde el
# título de las tarjetas (x=57)
_LEAD_LEFT = 18
_TEXT_LEFT = 57
_PAD_RIGHT = 12            # = _PAD_H de la tarjeta: «Añadir» alineado con la fecha
_LINE_EDIT_HMARGIN = 2     # margen fijo de QLineEdit antes del primer carácter
_COMPLETER_PAD = 5         # relleno de la lista del autocompletado
_REOPEN_GUARD = 0.3        # s: el clic que cierra un menú no lo vuelve a abrir
_QWIDGETSIZE_MAX = 16777215


# ---------------------------------------------------------------- utilidades
def shortcut_date(key: str, today: Optional[date] = None) -> date:
    """Fecha de un atajo del menú, relativa a `today` (hoy por defecto).
    «Próximo lunes» nunca es hoy: un lunes salta al de la semana siguiente."""
    today = today or date.today()
    if key == 'today':
        return today
    if key == 'tomorrow':
        return today + timedelta(days=1)
    if key == 'next_monday':
        return today + timedelta(days=7 - today.weekday())
    raise ValueError(f"atajo de fecha desconocido: {key}")


def due_text(d: date, today: Optional[date] = None) -> str:
    """Texto corto del chip: «Hoy», «Mañana», «lun 2 nov» o, en otro año,
    «2 nov 2027». En español y sin depender del locale del sistema."""
    today = today or date.today()
    delta = (d - today).days
    if delta == 0:
        return "Hoy"
    if delta == 1:
        return "Mañana"
    if d.year != today.year:
        return f"{d.day} {_MONTHS[d.month - 1]} {d.year}"
    return f"{_WEEKDAYS_SHORT[d.weekday()]} {d.day} {_MONTHS[d.month - 1]}"


def _long_date(d: date) -> str:
    """'lunes 2 de noviembre de 2026' (tooltip del chip de fecha)."""
    return f"{_WEEKDAYS[d.weekday()]} {d.day} de {_MONTHS_LONG[d.month - 1]} de {d.year}"


def _is_tag(word: str) -> bool:
    """Una etiqueta lleva al menos una letra: «#12» (un número de incidencia)
    se queda en el nombre."""
    return any(ch.isalpha() for ch in word)


def extract_tags(text: str) -> Tuple[str, List[str]]:
    """(nombre sin las «#etiquetas», etiquetas en minúsculas y sin repetir)."""
    tags: List[str] = []

    def take(match):
        word = match.group(1)
        if not _is_tag(word):
            return match.group(0)
        tag = word.lower()
        if tag not in tags:
            tags.append(tag)
        return ''

    name = ' '.join(_TAG_RE.sub(take, text).split())
    return name, tags


def _tag_color(tag: str) -> str:
    """Mismo color que el chip de la tarjeta (task_card._tag_color)."""
    palette = tokens().tag_colors
    return palette[sum(ord(c) for c in tag) % len(palette)]


def _qcolor(color, alpha: Optional[int] = None) -> QColor:
    c = QColor(color)
    if alpha is not None:
        c.setAlpha(alpha)
    return c


def _mix(a, b, f: float) -> QColor:
    """Interpolación lineal de dos colores (f=0 → a, f=1 → b)."""
    ca, cb = QColor(a), QColor(b)
    f = max(0.0, min(1.0, f))
    return QColor(round(ca.red() + (cb.red() - ca.red()) * f),
                  round(ca.green() + (cb.green() - ca.green()) * f),
                  round(ca.blue() + (cb.blue() - ca.blue()) * f))


_dot_cache: dict = {}


def _dot_icon(color: str, size: int = 14) -> QIcon:
    """Punto del color de una etiqueta (listas de etiquetas)."""
    dpr = QApplication.instance().devicePixelRatio() if QApplication.instance() else 1.0
    key = (color, size, dpr)
    if key in _dot_cache:
        return _dot_cache[key]
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    p.drawEllipse(QRectF(size / 2 - 4, size / 2 - 4, 8, 8))
    p.end()
    icon = QIcon(pm)
    # Sin esto, la fila seleccionada «tiñe» el punto y cambia de color
    icon.addPixmap(pm, QIcon.Selected)
    _dot_cache[key] = icon
    return icon


def _place_above(anchor: QWidget, ceiling: Optional[QWidget], size: QSize,
                 margin: int = 0) -> Tuple[int, int]:
    """Posición global de una emergente: alineada con `anchor` y justo encima
    de `ceiling` (o del propio anchor); debajo si arriba no cabe. `margin`:
    borde transparente de la emergente (el de la sombra)."""
    top_widget = ceiling or anchor
    left = anchor.mapToGlobal(QPoint(0, 0)).x() - margin
    top = top_widget.mapToGlobal(QPoint(0, 0)).y()
    y = top - size.height() + margin - 6
    screen = anchor.screen() or QApplication.primaryScreen()
    area = screen.availableGeometry()
    if y < area.top():
        y = top + top_widget.height() + 6 - margin
    x = max(area.left(), min(left, area.right() - size.width() + 1))
    return x, y


class _Menu(QMenu):
    """QMenu de la entrada, pintado a mano: tarjeta redondeada de verdad
    (fondo translúcido), etiqueta en fg y la pista de la derecha (lo que va
    tras «\\t») atenuada, que el QSS no puede colorear aparte. La opción
    vigente (propiedad «current» de la acción) va en acento. Las medidas
    siguen siendo las de Qt (la hoja de estilo solo fija rellenos)."""
    _ICON = 16

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setWindowFlags(self.windowFlags() | Qt.FramelessWindowHint
                            | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setFont(fonts.font(fonts.SMALL))
        # Solo rellenos (medidas): el fondo y los textos se pintan abajo
        self.setStyleSheet(
            "QMenu { background: transparent; border: 1px solid transparent;"
            " padding: 6px; }"
            "QMenu::item { padding: 7px 14px 7px 10px; }"
            "QMenu::icon { padding-left: 10px; }"
            "QMenu::separator { height: 11px; }")

    def add_option(self, icon: Optional[QIcon], label: str, hint: str = '',
                   current: bool = False, data=None):
        action = self.addAction(icon or QIcon(), f"{label}\t{hint}" if hint else label)
        action.setData(data)
        action.setProperty("current", bool(current))
        self._fit()
        return action

    def _fit(self) -> None:
        """Ancho mínimo para lo que pinta paintEvent: la columna de iconos
        que reserva el estilo es más estrecha y cortaba «No repetir»."""
        fm = QFontMetrics(self.font())
        has_icons = any(not a.icon().isNull() for a in self.actions())
        widest = 0
        for action in self.actions():
            if action.isSeparator():
                continue
            label, _, hint = action.text().partition('\t')
            w = 10 + (self._ICON + 10 if has_icons else 0) + fm.horizontalAdvance(label) + 12
            if hint:
                w += 24 + fm.horizontalAdvance(hint)
            widest = max(widest, w)
        self.setMinimumWidth(widest + 2 * (6 + 1))  # relleno + borde del QMenu

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(QColor(t.surface))
        radius = t.radius_small + 2
        p.drawRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        has_icons = any(not a.icon().isNull() for a in self.actions())
        active = self.activeAction()
        fm = QFontMetrics(self.font())
        for action in self.actions():
            r = self.actionGeometry(action)
            if not action.isVisible() or r.isEmpty():
                continue
            if action.isSeparator():
                p.setPen(QPen(QColor(t.border), 1))
                y = r.center().y() + 0.5
                p.drawLine(QPointF(r.left() + 8, y), QPointF(r.right() - 8, y))
                continue
            enabled = action.isEnabled()
            if action is active and enabled:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(t.surface_active))
                p.drawRoundedRect(QRectF(r), 6, 6)
            current = bool(action.property("current"))
            x = r.left() + 10
            icon = action.icon()
            if not icon.isNull():
                icon.paint(p, QRect(x, r.center().y() - self._ICON // 2 + 1,
                                    self._ICON, self._ICON))
            if has_icons:
                x += self._ICON + 10
            label, _, hint = action.text().partition('\t')
            if current:
                fg = QColor(t.accent)
            else:
                fg = QColor(t.fg if enabled else t.fg_muted)
            text_rect = QRect(x, r.top(), r.right() - 12 - x, r.height())
            p.setFont(self.font())
            p.setPen(fg)
            p.drawText(text_rect, int(Qt.AlignVCenter | Qt.AlignLeft), label)
            if hint:
                p.setPen(QColor(t.fg_muted))
                p.drawText(text_rect, int(Qt.AlignVCenter | Qt.AlignRight),
                           fm.elidedText(hint, Qt.ElideRight, text_rect.width()))
        p.end()


def _styled_menu(parent: QWidget) -> _Menu:
    """Menú de la entrada (lo envuelven los scripts de render)."""
    return _Menu(parent)


# ------------------------------------------------------------------ piezas
class _FlowLayout(QLayout):
    """Coloca los chips en filas y salta de línea si no caben (Qt no trae
    un layout de flujo). Informa su alto para un ancho dado. El widget
    «final» (set_trailing: «Para hoy» + «Añadir») va pegado a la derecha de
    la última fila si cabe, o en una fila propia: en una columna fija
    estrujaba los chips de uno en uno con el panel estrecho."""

    def __init__(self, parent=None, spacing: int = 6, trailing_gap: int = 12):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self._trailing_gap = trailing_gap
        self._trailing: Optional[QWidget] = None
        self.setContentsMargins(0, 0, 0, 0)

    def set_trailing(self, widget: QWidget) -> None:
        self.addWidget(widget)
        self._trailing = widget

    def addItem(self, item):  # noqa: N802 (API de Qt)
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, index):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self):  # noqa: N802
        return True

    def heightForWidth(self, width):  # noqa: N802
        return self._arrange(QRect(0, 0, width, 0), apply=False)

    def setGeometry(self, rect):  # noqa: N802
        super().setGeometry(rect)
        self._arrange(rect, apply=True)

    def sizeHint(self):  # noqa: N802
        return self.minimumSize()

    def minimumSize(self):  # noqa: N802
        size = QSize(0, 0)
        for item in self._items:
            if not item.isEmpty():
                size = size.expandedTo(item.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _arrange(self, rect: QRect, apply: bool) -> int:
        m = self.contentsMargins()
        area = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = area.x(), area.y(), 0
        trailing = None
        for item in self._items:
            if item.widget() is not None and item.widget() is self._trailing:
                trailing = item
                continue
            if item.isEmpty():  # widget oculto
                continue
            hint = item.sizeHint()
            if line_h and x + hint.width() > area.right() + 1:
                x, y, line_h = area.x(), y + line_h + self._gap, 0
            if apply:
                item.setGeometry(QRect(QPoint(x, y), hint))
            x += hint.width() + self._gap
            line_h = max(line_h, hint.height())
        if trailing is not None and not trailing.isEmpty():
            hint = trailing.sizeHint()
            if line_h and x - self._gap + self._trailing_gap + hint.width() > area.right() + 1:
                y, line_h = y + line_h + self._gap, 0
            if apply:
                trailing.setGeometry(QRect(QPoint(area.right() + 1 - hint.width(), y), hint))
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


class _Chip(QAbstractButton):
    """Chip de la fila de opciones: icono + texto. Con valor se tiñe de su
    color (el acento o el de la etiqueta) y muestra una «x» para quitarlo.
    `passive`: solo informativo (la pista «Para hoy»): sin borde ni clic."""
    removed = Signal()
    H = 28
    _PAD, _ICON, _GAP, _CLOSE_W = 10, 14, 6, 26

    def __init__(self, icon: Optional[str], text: str, parent: QWidget, *,
                 passive: bool = False, removable: bool = True):
        super().__init__(parent)
        self._icon = icon
        self._passive = passive
        self._active = False
        self._color: Optional[str] = None
        self.setText(text)
        self.setFont(fonts.font(fonts.XS + 0.5, weight=QFont.Weight.Medium))
        self.setAttribute(Qt.WA_Hover, True)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        # Solo con Tab: un clic no le roba el foco al campo de texto
        self.setFocusPolicy(Qt.NoFocus if passive else Qt.TabFocus)
        self.setCursor(Qt.ArrowCursor if passive else Qt.PointingHandCursor)
        self._close: Optional[IconButton] = None
        if removable and not passive:
            t = tokens()
            self._close = IconButton("close", "Quitar", color=t.fg_dim,
                                     hover_color=t.fg, size=12, parent=self)
            self._close.setFixedSize(18, 18)
            self._close.setFocusPolicy(Qt.NoFocus)
            self._close.clicked.connect(self.removed.emit)
            self._style_close()
            self._close.setVisible(False)

    def set_value(self, text: str, active: bool, color: Optional[str] = None) -> None:
        self._active = bool(active)
        self._color = color
        self.setText(text)
        if self._close is not None:
            self._style_close()
            self._close.setVisible(self._active)
            self._place_close()
        self.updateGeometry()
        self.update()

    def is_active(self) -> bool:
        return self._active

    def retheme(self) -> None:
        if self._close is not None:
            t = tokens()
            self._close.set_colors(t.fg_dim, t.fg)
            self._style_close()
        self.update()

    def _style_close(self) -> None:
        t = tokens()
        hover = _qcolor(self._color or t.accent, 60)
        self._close.setStyleSheet(
            "QToolButton { padding: 0px; border: none; border-radius: 9px;"
            " background: transparent; }"
            f"QToolButton:hover {{ background: rgba({hover.red()},{hover.green()},"
            f"{hover.blue()},{hover.alpha()}); }}")

    def _place_close(self) -> None:
        if self._close is not None:
            self._close.move(self.width() - 22, (self.H - 18) // 2)

    def sizeHint(self):  # noqa: N802
        fm = QFontMetrics(self.font())
        w = self._PAD + fm.horizontalAdvance(self.text())
        if self._icon:
            w += self._ICON + self._GAP
        w += self._CLOSE_W if (self._close is not None and self._active) else self._PAD
        return QSize(w, self.H)

    def minimumSizeHint(self):  # noqa: N802
        return self.sizeHint()

    def resizeEvent(self, event):
        self._place_close()
        super().resizeEvent(event)

    def keyPressEvent(self, event):
        # Enter también abre (con Tab se llega aquí desde el campo)
        if event.key() in (Qt.Key_Return, Qt.Key_Enter) and not self._passive:
            self.click()
            return
        # Retroceso o Supr quitan el valor, como la «x»
        if (event.key() in (Qt.Key_Backspace, Qt.Key_Delete)
                and self._active and self._close is not None):
            self.removed.emit()
            return
        super().keyPressEvent(event)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        hover = not self._passive and self.underMouse()
        tint = QColor(self._color or t.accent)
        if self._passive:
            bg, border, fg, ic = None, None, QColor(t.fg_dim), tint
        elif self._active:
            bg = _qcolor(tint, 54 if hover else 36)
            border = _qcolor(tint, 130 if hover else 85)
            fg = QColor(self._color) if self._color else QColor(t.fg)
            ic = tint
        else:
            bg = QColor(t.surface_hover) if hover else None
            # Algo más marcado que el borde de las tarjetas: son botones (y
            # no se pierden con el destello de «guardada»)
            border = QColor(t.fg_muted) if hover else _mix(t.border, t.fg_muted, 0.3)
            fg = QColor(t.fg if hover else t.fg_dim)
            ic = fg
        if self.hasFocus():
            border = QColor(t.accent)
        if bg is not None or border is not None:
            p.setPen(QPen(border, 1) if border is not None else Qt.NoPen)
            p.setBrush(bg if bg is not None else Qt.NoBrush)
            r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
            p.drawRoundedRect(r, t.radius_small, t.radius_small)
        x = self._PAD
        if self._icon:
            tinted_icon(self._icon, ic.name(), self._ICON).paint(
                p, QRect(x, (self.height() - self._ICON) // 2, self._ICON, self._ICON))
            x += self._ICON + self._GAP
        p.setPen(fg)
        p.setFont(self.font())
        p.drawText(QRect(x, 0, self.width() - x, self.height()),
                   int(Qt.AlignVCenter | Qt.AlignLeft), self.text())
        p.end()


class _BrandToggle(_Chip):
    """Botón cuadrado solo con el logo de una integración (Asana, ClickUp):
    «crear también allí». Apagado, logo fg_dim con el borde de los chips;
    encendido, logo y borde del color de marca y el relleno de un chip
    activo. `dimmed`: integración sin configurar (atenuado, pero se puede
    pulsar: abre Ajustes). Deshabilitado se pinta apagado. Es checkable,
    pero el estado lo decide el panel (un clic no lo invierte solo)."""

    def __init__(self, icon: str, brand: str, parent: QWidget):
        super().__init__(icon, "", parent, removable=False)
        self._brand = brand  # nombre del token de marca ('asana', 'clickup')
        self._dimmed = False
        self.setCheckable(True)
        self.setAccessibleName(brand)

    def nextCheckState(self):  # noqa: N802 (API de Qt)
        pass  # el panel enciende o apaga (con Asana, tras elegir destino)

    def set_dimmed(self, on: bool) -> None:
        if self._dimmed != bool(on):
            self._dimmed = bool(on)
            self.update()

    def is_dimmed(self) -> bool:
        return self._dimmed

    def is_active(self) -> bool:
        return self.isChecked()

    def sizeHint(self):  # noqa: N802
        return QSize(self.H, self.H)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        enabled = self.isEnabled()
        hover = enabled and self.underMouse()
        on = self.isChecked() and enabled and not self._dimmed
        brand = QColor(getattr(t, self._brand, t.accent))
        if on:
            bg = _qcolor(brand, 54 if hover else 36)
            border = _qcolor(brand, 130 if hover else 85)
            ic = brand
        else:
            bg = QColor(t.surface_hover) if hover else None
            border = QColor(t.fg_muted) if hover else _mix(t.border, t.fg_muted, 0.3)
            ic = QColor(t.fg if hover and not self._dimmed else t.fg_dim)
        if self.hasFocus():
            border = QColor(t.accent)
        if self._dimmed or not enabled:
            p.setOpacity(0.45)
        p.setPen(QPen(border, 1))
        p.setBrush(bg if bg is not None else Qt.NoBrush)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, t.radius_small, t.radius_small)
        x, y = (self.width() - self._ICON) // 2, (self.height() - self._ICON) // 2
        tinted_icon(self._icon, ic.name(), self._ICON).paint(
            p, QRect(x, y, self._ICON, self._ICON))
        p.end()


class _ComposerCard(QFrame):
    """Marco de la entrada: superficie + borde de 1 px pintados a mano para
    animar el foco (borde de acento suave), la confirmación (destello) y el
    aviso de nombre vacío sin regenerar hojas de estilo."""
    clicked = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setAttribute(Qt.WA_Hover, True)
        self._focus = 0.0
        self._flash = 0.0
        self._warn = 0.0
        self._focus_anim = self._anim('_focus', 180)
        self._focus_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._flash_anim = self._anim('_flash', 900)
        self._flash_anim.setEasingCurve(QEasingCurve.InQuad)
        self._warn_anim = self._anim('_warn', 1500)

    def _anim(self, attr: str, ms: int) -> QVariantAnimation:
        anim = QVariantAnimation(self)
        anim.setDuration(ms)
        anim.valueChanged.connect(lambda v, attr=attr: self._set(attr, v))
        return anim

    def _set(self, attr: str, value) -> None:
        setattr(self, attr, float(value))
        self.update()

    def set_focused(self, on: bool, animate: bool = True) -> None:
        target = 1.0 if on else 0.0
        self._focus_anim.stop()
        if not animate or abs(self._focus - target) < 1e-3:
            self._set('_focus', target)
            return
        self._focus_anim.setStartValue(self._focus)
        self._focus_anim.setEndValue(target)
        self._focus_anim.start()

    def flash(self) -> None:
        """Destello de acento que se apaga: «guardada»."""
        self._flash_anim.stop()
        self._flash_anim.setStartValue(1.0)
        self._flash_anim.setEndValue(0.0)
        self._flash_anim.start()

    def warn(self) -> None:
        """Borde en rojo suave un momento: «falta el nombre»."""
        self._warn_anim.stop()
        self._warn_anim.setStartValue(0.0)
        self._warn_anim.setKeyValueAt(0.12, 1.0)
        self._warn_anim.setKeyValueAt(0.65, 1.0)
        self._warn_anim.setEndValue(0.0)
        self._warn_anim.start()

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        rest = _mix(t.border, t.fg_muted, 0.45) if self.underMouse() else QColor(t.border)
        border = _mix(rest, _mix(t.border, t.accent, 0.6), self._focus)
        border = _mix(border, t.accent, self._flash)
        border = _mix(border, t.danger, 0.8 * self._warn)
        fill = _mix(t.surface, t.accent, 0.07 * self._flash)
        fill = _mix(fill, t.danger, 0.05 * self._warn)
        p.setPen(QPen(border, 1))
        p.setBrush(fill)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        p.drawRoundedRect(r, t.radius, t.radius)
        p.end()


class _LeadIcon(QWidget):
    """Icono de la izquierda, en la columna del check de las tarjetas:
    «+» de acento en reposo (la acción), el círculo vacío de una tarea
    mientras se escribe (lo que se está creando) y un «✓» de acento un
    momento tras guardar. El único relleno de acento es «Añadir»."""
    S = 20
    MODES = ('add', 'draft', 'saved')

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.setFixedSize(self.S, self.S)
        self._mode = 'add'

    def mode(self) -> str:
        return self._mode

    def set_mode(self, mode: str) -> None:
        if mode != self._mode:
            self._mode = mode
            # El «+» se pulsa; el círculo no es un check: solo enfoca
            self.setCursor(Qt.PointingHandCursor if mode == 'add' else Qt.IBeamCursor)
            self.update()

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        if self._mode == 'draft':
            tinted_icon("circle", t.fg_muted, 20).paint(p, QRect(0, 0, 20, 20))
        elif self._mode == 'saved':
            tinted_icon("check", t.accent, 18).paint(p, QRect(1, 1, 18, 18))
        else:
            tinted_icon("add", t.accent, 18).paint(p, QRect(1, 1, 18, 18))
        p.end()


class _NameEdit(QLineEdit):
    """Campo del nombre: Escape descarta; con el autocompletado de
    «#etiqueta» abierto, Enter/Tab/Escape son para la lista."""
    escape_pressed = Signal()

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.completer_popup: Optional[QWidget] = None

    def tag_anchor(self) -> QRect:
        return self.cursorRect()

    def keyPressEvent(self, event):
        key = event.key()
        popup = self.completer_popup
        if popup is not None and popup.isVisible():
            if key == Qt.Key_Escape or (
                    key in (Qt.Key_Return, Qt.Key_Enter, Qt.Key_Tab)
                    and popup.currentIndex().isValid()):
                event.ignore()  # lo resuelve el QCompleter
                return
        if key == Qt.Key_Escape:
            self.escape_pressed.emit()
            event.accept()
            return
        super().keyPressEvent(event)


# --------------------------------------------------------------- emergentes
class _Popup(QWidget):
    """Ventana emergente con tarjeta redondeada y sombra suave (calendario y
    etiquetas). Se abre ENCIMA del chip (la entrada vive al pie de la
    ventana) y se cierra con un clic fuera o con Escape."""
    closed = Signal()
    M = 14  # margen transparente alrededor de la tarjeta (sombra)

    def __init__(self, parent: QWidget):
        super().__init__(parent, Qt.Popup | Qt.FramelessWindowHint
                         | Qt.NoDropShadowWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_DeleteOnClose, True)
        self._anchor: Optional[QWidget] = None
        self._ceiling: Optional[QWidget] = None
        t = tokens()
        outer = QVBoxLayout(self)
        outer.setContentsMargins(self.M, self.M, self.M, self.M)
        # Siempre a su tamaño justo (el selector de etiquetas crece y encoge)
        outer.setSizeConstraint(QLayout.SetFixedSize)
        self.card = QFrame(self)
        self.card.setObjectName("addPopupCard")
        self.card.setStyleSheet(
            f"#addPopupCard {{ background: {t.surface}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; }}")
        shadow = QGraphicsDropShadowEffect(self.card)
        shadow.setBlurRadius(28)
        shadow.setOffset(0, 6)
        shadow.setColor(QColor(0, 0, 0, 110))
        self.card.setGraphicsEffect(shadow)
        outer.addWidget(self.card)
        self.lay = QVBoxLayout(self.card)
        self.lay.setContentsMargins(10, 10, 10, 10)
        self.lay.setSpacing(6)

    def show_near(self, anchor: QWidget, ceiling: Optional[QWidget] = None) -> None:
        """Alineada con `anchor` (el chip) y encima de `ceiling` (la tarjeta
        entera, para no tapar lo que se está escribiendo)."""
        self._anchor, self._ceiling = anchor, ceiling
        self.reposition()
        self.show()

    def reposition(self) -> None:
        anchor = self._anchor
        if anchor is None:
            return
        # Tamaño al día antes de colocarla, de dentro afuera: sin activar
        # antes el layout de la tarjeta, el de fuera aún ve el alto viejo y
        # al filtrar etiquetas la emergente se despegaba de la entrada
        # (o la tapaba al crecer)
        self.lay.activate()
        self.layout().activate()
        x, y = _place_above(anchor, self._ceiling, self.size(), self.M)
        self.move(x, y)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.close()
            return
        super().keyPressEvent(event)

    def hideEvent(self, event):
        super().hideEvent(event)
        self.closed.emit()


class _Calendar(QCalendarWidget):
    """Calendario con celdas pintadas a mano: día elegido en un círculo de
    acento, hoy con un aro, otros meses y días pasados atenuados y un hover
    suave. Semana de lunes a domingo, sin números de semana."""

    def __init__(self, parent: QWidget):
        super().__init__(parent)
        self.has_value = False
        self._hover: Optional[QDate] = None
        self._cells = {}  # día juliano → QRect (coordenadas del viewport)
        self.setLocale(QLocale(QLocale.Language.Spanish, QLocale.Country.Spain))
        self.setFirstDayOfWeek(Qt.Monday)
        self.setVerticalHeaderFormat(QCalendarWidget.NoVerticalHeader)
        self.setHorizontalHeaderFormat(QCalendarWidget.SingleLetterDayNames)
        self.setNavigationBarVisible(False)
        self.setGridVisible(False)
        self.setDateEditEnabled(False)
        self.setMinimumDate(QDate.currentDate())
        self.setFont(fonts.font(fonts.XS + 0.5))
        self.setFixedSize(7 * 36, 7 * 32)
        self._view = self.findChild(QTableView, "qt_calendar_calendarview")
        if self._view is not None:
            self._view.setMouseTracking(True)
            self._view.viewport().setMouseTracking(True)
            self._view.viewport().setCursor(Qt.PointingHandCursor)
            self._view.viewport().installEventFilter(self)
        self.currentPageChanged.connect(lambda *_: self._cells.clear())
        self.retheme()

    def retheme(self) -> None:
        t = tokens()
        head = QTextCharFormat()
        head.setForeground(QColor(t.fg_muted))
        head.setBackground(Qt.transparent)
        head.setFont(fonts.font(fonts.XS, weight=QFont.Weight.Medium))
        self.setHeaderTextFormat(head)
        # Sin el rojo por defecto de sábado y domingo en la cabecera
        for day in (Qt.Saturday, Qt.Sunday):
            self.setWeekdayTextFormat(day, head)
        self.setStyleSheet(
            "QCalendarWidget QAbstractItemView { background: transparent;"
            " border: none; outline: 0; selection-background-color: transparent; }")

    def eventFilter(self, obj, event):
        if self._view is not None and obj is self._view.viewport():
            if event.type() == QEvent.MouseMove:
                pos = event.position().toPoint()
                hit = next((QDate.fromJulianDay(jd) for jd, r in self._cells.items()
                            if r.contains(pos)), None)
                if hit != self._hover:
                    self._hover = hit
                    obj.update()
            elif event.type() == QEvent.Leave and self._hover is not None:
                self._hover = None
                obj.update()
        return super().eventFilter(obj, event)

    def paintCell(self, painter, rect, qdate):  # noqa: N802 (API de Qt)
        t = tokens()
        self._cells[qdate.toJulianDay()] = QRect(rect)
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        in_month = qdate.month() == self.monthShown()
        enabled = self.minimumDate() <= qdate <= self.maximumDate()
        today = qdate == QDate.currentDate()
        selected = qdate == self.selectedDate() and (self.has_value or not today)
        hovered = enabled and self._hover is not None and qdate == self._hover
        side = min(rect.width(), rect.height()) - 4
        center = QRectF(rect).center()
        circle = QRectF(center.x() - side / 2, center.y() - side / 2, side, side)
        weight = QFont.Weight.Normal
        if selected:
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(t.accent))
            painter.drawEllipse(circle)
            fg = QColor(t.on_accent)
            weight = QFont.Weight.DemiBold
        else:
            if hovered:
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(t.surface_active))
                painter.drawEllipse(circle)
            if today:
                painter.setPen(QPen(QColor(t.accent), 1.2))
                painter.setBrush(Qt.NoBrush)
                painter.drawEllipse(circle.adjusted(0.6, 0.6, -0.6, -0.6))
                fg = QColor(t.accent)
                weight = QFont.Weight.DemiBold
            elif enabled and in_month:
                fg = QColor(t.fg)
            elif enabled:
                fg = QColor(t.fg_dim)
            else:
                fg = _qcolor(t.fg_muted, 150)
        painter.setFont(fonts.font(fonts.XS + 0.5, weight=weight))
        painter.setPen(fg)
        painter.drawText(rect, int(Qt.AlignCenter), str(qdate.day()))
        painter.restore()


class _DatePopup(_Popup):
    """«Elegir fecha…»: mes en español con flechas y el calendario. Un clic
    en un día lo elige y cierra."""
    picked = Signal(object)  # datetime.date

    def __init__(self, parent: QWidget, current: Optional[date]):
        super().__init__(parent)
        t = tokens()
        head = QHBoxLayout()
        head.setContentsMargins(6, 0, 0, 2)
        head.setSpacing(2)
        self.title = QLabel(self.card)
        self.title.setFont(fonts.font(fonts.SMALL, bold=True))
        self.title.setStyleSheet(f"color: {t.fg};")
        head.addWidget(self.title, 1)
        self.prev_btn = IconButton("chevron-left", "Mes anterior", color=t.fg_dim,
                                   hover_color=t.fg, size=16, parent=self.card)
        self.next_btn = IconButton("chevron-right", "Mes siguiente", color=t.fg_dim,
                                   hover_color=t.fg, size=16, parent=self.card)
        head.addWidget(self.prev_btn)
        head.addWidget(self.next_btn)
        self.lay.addLayout(head)
        self.calendar = _Calendar(self.card)
        self.lay.addWidget(self.calendar)
        if current is not None:
            self.calendar.setSelectedDate(QDate(current.year, current.month, current.day))
            self.calendar.has_value = True
        self.prev_btn.clicked.connect(self.calendar.showPreviousMonth)
        self.next_btn.clicked.connect(self.calendar.showNextMonth)
        self.calendar.currentPageChanged.connect(lambda *_: self._sync_title())
        self.calendar.clicked.connect(self._pick)
        self.calendar.activated.connect(self._pick)
        self._sync_title()
        self.calendar.setFocus()

    def _sync_title(self) -> None:
        y, m = self.calendar.yearShown(), self.calendar.monthShown()
        self.title.setText(f"{_MONTHS_LONG[m - 1].capitalize()} {y}")
        today = date.today()
        # No hay nada que elegir antes de hoy
        self.prev_btn.setEnabled((y, m) > (today.year, today.month))

    def _pick(self, qdate: QDate) -> None:
        picked = qdate.toPython()
        if picked < date.today():
            return
        self.picked.emit(picked)
        self.close()


class _TagPopup(_Popup):
    """Selector de etiquetas: busca entre las conocidas o crea una nueva.
    Queda abierto para añadir varias; Escape o un clic fuera lo cierran."""
    chosen = Signal(str)
    ROW = 30
    MAX_ROWS = 6
    W = 250

    def __init__(self, parent: QWidget, known: List[str], taken: List[str]):
        super().__init__(parent)
        t = tokens()
        self._known = list(known)
        self._taken = set(taken)
        self.search = QLineEdit(self.card)
        self.search.setPlaceholderText("Buscar o crear etiqueta")
        self.search.setFont(fonts.font(fonts.SMALL))
        self.lay.addWidget(self.search)
        self.list = QListWidget(self.card)
        self.list.setFocusPolicy(Qt.NoFocus)
        self.list.setFrameShape(QFrame.NoFrame)
        self.list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list.setUniformItemSizes(True)
        self.list.setIconSize(QSize(14, 14))
        self.list.setFont(fonts.font(fonts.SMALL))
        self.list.viewport().setCursor(Qt.PointingHandCursor)
        self.list.setStyleSheet(
            "QListWidget { background: transparent; border: none; }"
            f"QListWidget::item {{ padding: 0px 6px; border-radius: 6px; color: {t.fg}; }}"
            f"QListWidget::item:hover {{ background: {t.surface_hover}; }}"
            f"QListWidget::item:selected {{ background: {t.surface_active}; color: {t.fg}; }}")
        self.lay.addWidget(self.list)
        self.empty = QLabel(self.card)
        self.empty.setFont(fonts.font(fonts.XS))
        self.empty.setStyleSheet(f"color: {t.fg_muted}; padding: 2px 4px;")
        self.empty.setWordWrap(True)
        self.lay.addWidget(self.empty)
        self.card.setFixedWidth(self.W)
        self.search.textChanged.connect(self._refresh)
        self.search.returnPressed.connect(self._choose_current)
        self.search.installEventFilter(self)
        self.list.itemClicked.connect(self._choose)
        self._refresh()
        self.search.setFocus()

    def items(self) -> List[str]:
        """Etiquetas que ofrece ahora la lista (para los tests)."""
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())]

    def _refresh(self) -> None:
        t = tokens()
        query = self.search.text().strip().lstrip('#').strip().lower()
        pool = [x for x in self._known if x not in self._taken]
        if query:
            starts = [x for x in pool if x.startswith(query)]
            pool = starts + [x for x in pool if query in x and x not in starts]
        self.list.clear()
        for name in pool:
            item = QListWidgetItem(_dot_icon(_tag_color(name)), name)
            item.setData(Qt.UserRole, name)
            item.setSizeHint(QSize(0, self.ROW))
            self.list.addItem(item)
        # «Crear» al final: con «coc» y «cocina» en la lista, Enter elige la
        # existente en vez de crear una etiqueta casi repetida
        if query and query not in self._known and query not in self._taken:
            item = QListWidgetItem(tinted_icon("add", t.accent, 14), f"Crear «{query}»")
            item.setData(Qt.UserRole, query)
            item.setSizeHint(QSize(0, self.ROW))
            self.list.addItem(item)
        count = self.list.count()
        if count:
            self.list.setCurrentRow(0)
        self.list.setFixedHeight(min(count, self.MAX_ROWS) * self.ROW + 2)
        self.list.setVisible(count > 0)
        if query in self._taken:
            self.empty.setText("Esa etiqueta ya está en la tarea.")
        else:
            self.empty.setText("Escribe para crear tu primera etiqueta.")
        self.empty.setVisible(count == 0)
        self.reposition()

    def _choose(self, item: QListWidgetItem) -> None:
        tag = item.data(Qt.UserRole)
        if not tag:
            return
        self.chosen.emit(tag)
        self._taken.add(tag)
        if tag not in self._known:
            self._known.append(tag)
            self._known.sort()
        self.search.clear()
        self._refresh()

    def _choose_current(self) -> None:
        item = self.list.currentItem()
        if item is not None:
            self._choose(item)

    def eventFilter(self, obj, event):
        if obj is self.search and event.type() == QEvent.KeyPress:
            key = event.key()
            if key in (Qt.Key_Down, Qt.Key_Up) and self.list.count():
                step = 1 if key == Qt.Key_Down else -1
                row = (self.list.currentRow() + step) % self.list.count()
                self.list.setCurrentRow(row)
                return True
            if key == Qt.Key_Escape:
                self.close()
                return True
        return super().eventFilter(obj, event)


# ------------------------------------------------------------------- panel
class TaskAddPanel(QWidget):
    """Entrada rápida de tareas (ver el docstring del módulo)."""
    # (nombre, tags en minúsculas, ciclo '' o 'daily'/'weekly'/'monthly'/
    # 'quarterly', vencimiento 'YYYY-MM-DD' o '', destinos: {} o con
    # 'asana': {workspace_gid, project_gid ('' = Mis tareas), project_name}
    # y/o 'clickup': True)
    task_submitted = Signal(str, list, str, str, dict)
    # Botones de Asana/ClickUp: su estado para recordarlo ({'asana': destino
    # o None, 'clickup': bool}) y «abrir Ajustes» ('asana' o 'clickup')
    targets_changed = Signal(dict)
    setup_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        self._expanded = False
        self._due: Optional[date] = None
        self._cycle = ''
        self._tags: List[str] = []
        self._tag_chips = {}
        self._known_tags: List[str] = []
        self._plan_hint = False
        self._warning = False
        self._placeholder_color = ''
        self._popup: Optional[QWidget] = None
        self._list_bar = None  # scrollbar de la lista de tareas (alineación)
        # Último menú o emergente cerrado: (chip que lo abrió, instante)
        self._closed_from: Optional[QWidget] = None
        self._closed_at = 0.0
        # Crear también en Asana/ClickUp. Destinos de Asana: None = sin
        # configurar; destino elegido: None = apagado
        self._asana_dests: Optional[List[dict]] = None
        self._asana_target: Optional[dict] = None
        self._clickup_ready = False
        self._clickup_on = False

        outer = QVBoxLayout(self)
        # Derecha 16: alinea el borde con las tarjetas (margen de la lista
        # + scrollbar de task_list)
        outer.setContentsMargins(0, 2, 16, 0)
        outer.setSpacing(0)
        self.card = _ComposerCard(self)
        self.card.clicked.connect(self.focus_input)
        outer.addWidget(self.card)
        card_lay = QVBoxLayout(self.card)
        card_lay.setContentsMargins(0, 0, 0, 0)
        card_lay.setSpacing(0)

        # Fila del campo: icono (columna del check) y nombre (columna del
        # título de las tarjetas)
        row = QWidget(self.card)
        row.setFixedHeight(ROW_H)
        row.setCursor(Qt.IBeamCursor)
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(_LEAD_LEFT, 0, _PAD_RIGHT, 0)
        row_lay.setSpacing(_TEXT_LEFT - _LEAD_LEFT - _LeadIcon.S - _LINE_EDIT_HMARGIN)
        self.lead = _LeadIcon(row)
        self.lead.setCursor(Qt.PointingHandCursor)
        self.lead.setToolTip("Añadir una tarea")
        row_lay.addWidget(self.lead, 0, Qt.AlignVCenter)
        self.name_edit = _NameEdit(row)
        # El tamaño del título de las tarjetas: se escribe lo que se verá
        self.name_edit.setFont(fonts.font(fonts.NORMAL - 0.5))
        self.name_edit.setAccessibleName("Nombre de la nueva tarea")
        row_lay.addWidget(self.name_edit, 1)
        card_lay.addWidget(row)

        # Fila de opciones (se despliega) bajo el nombre, como los metadatos
        # de una tarjeta: chips a la izquierda y, pegado a la derecha de la
        # última fila, «Para hoy» + «Añadir»
        self.options = QWidget(self.card)
        opt_lay = QHBoxLayout(self.options)
        opt_lay.setContentsMargins(_TEXT_LEFT, 0, _PAD_RIGHT, 12)
        opt_lay.setSpacing(0)
        self._chips_host = QWidget(self.options)
        self._flow = _FlowLayout(self._chips_host, spacing=6)
        self.due_chip = _Chip("calendar", "Fecha", self._chips_host)
        self.due_chip.setToolTip("Fecha de vencimiento")
        self.repeat_chip = _Chip("repeat", "Repetir", self._chips_host)
        self.repeat_chip.setToolTip(_RECURRING_TIP)
        self.tags_chip = _Chip("tag", "Etiquetas", self._chips_host, removable=False)
        self.tags_chip.setToolTip("Añadir etiquetas. También puedes escribir "
                                  "#etiqueta en el nombre.")
        # Solo el logo: crear también en Asana o en ClickUp
        self.asana_btn = _BrandToggle("asana-mark", "asana", self._chips_host)
        self.clickup_btn = _BrandToggle("brand-clickup", "clickup", self._chips_host)
        for chip in (self.due_chip, self.repeat_chip, self.tags_chip,
                     self.asana_btn, self.clickup_btn):
            self._flow.addWidget(chip)
        opt_lay.addWidget(self._chips_host, 1)
        actions = QWidget(self._chips_host)
        act_lay = QHBoxLayout(actions)
        act_lay.setContentsMargins(0, 0, 0, 0)
        act_lay.setSpacing(8)
        self.plan_chip = _Chip("calendar-check", "Para hoy", actions, passive=True)
        self.plan_chip.setToolTip("Estás viendo «Hoy»: la tarea entra en el plan del día.")
        act_lay.addWidget(self.plan_chip)
        self.plan_chip.setVisible(False)
        self.submit_btn = QPushButton("Añadir", actions)
        self.submit_btn.setProperty("variant", "accent")
        self.submit_btn.setCursor(Qt.PointingHandCursor)
        self.submit_btn.setFocusPolicy(Qt.TabFocus)
        self.submit_btn.setFixedHeight(28)
        self.submit_btn.setFont(fonts.font(fonts.XS + 0.5, bold=True))
        self.submit_btn.setIconSize(QSize(14, 14))
        self.submit_btn.setStyleSheet(
            f"QPushButton {{ padding: 0px 12px 0px 10px; border-radius: {t.radius_small}px; }}")
        self.submit_btn.setToolTip("Añadir la tarea (Enter)")
        self.submit_btn.setEnabled(False)
        act_lay.addWidget(self.submit_btn)
        self._flow.set_trailing(actions)
        card_lay.addWidget(self.options)

        # Despliegue: alto + opacidad. Plegada además se oculta: sus chips no
        # deben recibir el foco con Tab mientras no se ven
        self.options.setMaximumHeight(0)
        self.options.setVisible(False)
        self._opacity = QGraphicsOpacityEffect(self.options)
        self._opacity.setOpacity(0.0)
        self.options.setGraphicsEffect(self._opacity)
        self._height_anim = QPropertyAnimation(self.options, b"maximumHeight", self)
        self._height_anim.setDuration(220)
        self._height_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._fade_anim = QPropertyAnimation(self._opacity, b"opacity", self)
        self._fade_anim.setDuration(200)
        self._fade_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._expand_group = QParallelAnimationGroup(self)
        self._expand_group.addAnimation(self._height_anim)
        self._expand_group.addAnimation(self._fade_anim)
        self._expand_group.finished.connect(self._on_expand_finished)

        # Autocompletado de «#etiqueta» dentro del nombre
        self._tag_model = QStandardItemModel(self)
        self._completer = QCompleter(self._tag_model, self)
        self._completer.setWidget(self.name_edit)
        self._completer.setCaseSensitivity(Qt.CaseInsensitive)
        self._completer.setCompletionMode(QCompleter.PopupCompletion)
        self._completer.setMaxVisibleItems(6)
        self._completer.activated[str].connect(self._complete_inline_tag)
        completer_popup = self._completer.popup()
        # Esquinas redondeadas de verdad, como los menús
        completer_popup.setWindowFlags(completer_popup.windowFlags()
                                       | Qt.FramelessWindowHint | Qt.NoDropShadowWindowHint)
        completer_popup.setAttribute(Qt.WA_TranslucentBackground, True)
        completer_popup.setFont(fonts.font(fonts.SMALL))
        completer_popup.setIconSize(QSize(14, 14))
        completer_popup.installEventFilter(self)  # pinta su tarjeta
        self.name_edit.completer_popup = completer_popup

        self._lead_timer = QTimer(self)
        self._lead_timer.setSingleShot(True)
        self._lead_timer.timeout.connect(self._update_lead)
        self._warn_timer = QTimer(self)
        self._warn_timer.setSingleShot(True)
        self._warn_timer.timeout.connect(self._end_warning)

        self.name_edit.installEventFilter(self)
        # Salir con Tab desde un chip o «Añadir» también pliega (ver eventFilter)
        for w in (self.due_chip, self.repeat_chip, self.tags_chip, self.asana_btn,
                  self.clickup_btn, self.submit_btn):
            w.installEventFilter(self)
        self.name_edit.textEdited.connect(self._on_text_edited)
        self.name_edit.textChanged.connect(self._sync_submit)
        self.name_edit.returnPressed.connect(self.submit)
        self.name_edit.escape_pressed.connect(self.discard)
        self.submit_btn.clicked.connect(self.submit)
        self.due_chip.clicked.connect(
            lambda: self._chip_clicked(self.due_chip, self._open_date_menu))
        self.due_chip.removed.connect(lambda: self.set_due(None))
        self.repeat_chip.clicked.connect(
            lambda: self._chip_clicked(self.repeat_chip, self._open_repeat_menu))
        self.repeat_chip.removed.connect(lambda: self.set_cycle(''))
        self.tags_chip.clicked.connect(
            lambda: self._chip_clicked(self.tags_chip, self.open_tag_popup))
        self.asana_btn.clicked.connect(
            lambda: self._chip_clicked(self.asana_btn, self._on_asana_clicked))
        self.clickup_btn.clicked.connect(self._on_clickup_clicked)
        self._update_target_buttons()

        self._retheme()
        self._update_placeholder()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    # ---------------------------------------------------------- API pública
    def set_known_tags(self, tags: List[str]) -> None:
        """Etiquetas para el autocompletado (en minúsculas, sin las internas)."""
        known: List[str] = []
        for tag in tags or []:
            tag = str(tag).strip().lower()
            if tag and tag not in _HIDDEN_TAGS and tag not in known:
                known.append(tag)
        known.sort()
        if known == self._known_tags:
            return  # se llama en cada refresco de la lista: no rehacer el modelo
        self._known_tags = known
        self._refresh_tag_model()

    def toggle(self) -> None:
        """Compatibilidad (antes desplegaba un formulario): enfoca el campo y
        muestra las opciones."""
        self.focus_input()

    def focus_input(self) -> None:
        self._set_expanded(True)
        self.card.set_focused(True)
        self.name_edit.setFocus(Qt.OtherFocusReason)

    def set_plan_hint(self, on: bool) -> None:
        """En el filtro «Hoy»: chip «Para hoy» (la tarea entra en el plan)."""
        self._plan_hint = bool(on)
        self.plan_chip.setVisible(self._plan_hint)
        self._update_placeholder()

    @property
    def due(self) -> Optional[date]:
        return self._due

    @property
    def cycle(self) -> str:
        return self._cycle

    @property
    def tags(self) -> List[str]:
        return list(self._tags)

    def is_expanded(self) -> bool:
        return self._expanded

    def set_due(self, value: Optional[date]) -> None:
        self._due = value
        if value is None:
            self.due_chip.set_value("Fecha", False)
            self.due_chip.setToolTip("Fecha de vencimiento")
        else:
            self.due_chip.set_value(due_text(value), True)
            self.due_chip.setToolTip(f"Vence el {_long_date(value)}")
        self._after_option_change()

    def set_cycle(self, cycle: str) -> None:
        self._cycle = cycle if cycle in _CYCLE_TEXT else ''
        if self._cycle:
            self.repeat_chip.set_value(_CYCLE_TEXT[self._cycle], True)
        else:
            self.repeat_chip.set_value("Repetir", False)
        self._update_target_buttons()  # con un ciclo, Asana no
        self._after_option_change()

    def add_tag(self, tag: str) -> None:
        tag = str(tag).strip().lstrip('#').strip().lower()
        if not tag or tag in self._tags:
            return
        self._tags.append(tag)
        chip = _Chip(None, tag, self._chips_host)
        chip.set_value(tag, True, color=_tag_color(tag))
        chip.setToolTip(f"Etiqueta «{tag}»")
        chip.removed.connect(lambda tag=tag: self.remove_tag(tag))
        chip.installEventFilter(self)
        self._flow.addWidget(chip)
        chip.setVisible(True)  # DESPUÉS de addWidget
        # Con Tab, en el orden en que se ven (tras los logos de Asana y
        # ClickUp): antes de «Añadir», no al final
        prev = list(self._tag_chips.values())[-1] if self._tag_chips else self.clickup_btn
        QWidget.setTabOrder(prev, chip)
        QWidget.setTabOrder(chip, self.submit_btn)
        self._tag_chips[tag] = chip
        self._refresh_tag_model()
        self._after_option_change()

    def remove_tag(self, tag: str) -> None:
        chip = self._tag_chips.pop(tag, None)
        if tag in self._tags:
            self._tags.remove(tag)
        if chip is not None:
            had_focus = chip.hasFocus()
            self._flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
            if had_focus:  # quitada con el teclado: de vuelta al nombre
                self.name_edit.setFocus(Qt.OtherFocusReason)
        self._refresh_tag_model()
        self._after_option_change()

    # ---------------------------------- crear también en Asana o ClickUp
    def set_integrations(self, asana_destinations: Optional[List[dict]],
                         clickup_ready: bool) -> None:
        """Qué ofrecen los botones de los logos. Asana: sus destinos en orden
        ({workspace_gid, project_gid ('' = Mis tareas), project_name}) o
        None si no está activa y configurada (el botón se atenúa y abre
        Ajustes). ClickUp: listo si está activa, con token y lista. Si el
        destino elegido ya no está (un proyecto desmarcado), Asana se apaga."""
        dests = [d for d in (self._clean_dest(x) for x in asana_destinations or []) if d]
        self._asana_dests = dests or None
        self._clickup_ready = bool(clickup_ready)
        if self._reconcile_asana_target():
            self._emit_targets()
        self._update_target_buttons()

    def set_target_state(self, asana_target: Optional[dict], clickup_on: bool) -> None:
        """Estado recordado de los botones (al arrancar: no avisa)."""
        self._asana_target = self._clean_dest(asana_target)
        self._clickup_on = bool(clickup_on)
        self._reconcile_asana_target()
        self._update_target_buttons()

    def set_asana_target(self, dest: Optional[dict]) -> None:
        """Enciende Asana con ese destino (None la apaga)."""
        self._asana_target = self._clean_dest(dest)
        self._update_target_buttons()
        self._emit_targets()

    def set_clickup(self, on: bool) -> None:
        self._clickup_on = bool(on)
        self._update_target_buttons()
        self._emit_targets()

    @property
    def asana_target(self) -> Optional[dict]:
        return dict(self._asana_target) if self._asana_target else None

    @property
    def clickup_on(self) -> bool:
        return self._clickup_on

    @property
    def targets(self) -> dict:
        """Destinos del alta: solo los encendidos y configurados (Asana,
        además, sin repetición)."""
        out = {}
        if self._asana_target and self._asana_dests and not self._cycle:
            out['asana'] = dict(self._asana_target)
        if self._clickup_on and self._clickup_ready:
            out['clickup'] = True
        return out

    def build_asana_menu(self) -> QMenu:
        """Dónde crearla en Asana: «Mis tareas» y los proyectos marcados en
        Ajustes, por workspace (con su cabecera si hay varios)."""
        t = tokens()
        menu = _styled_menu(self)
        groups: dict = {}
        for dest in self._asana_dests or []:
            groups.setdefault(dest['workspace_gid'], []).append(dest)
        for ws, dests in groups.items():
            if menu.actions():
                menu.addSeparator()
            if len(groups) > 1:
                menu.add_option(None, f"Workspace {ws}").setEnabled(False)
            for dest in dests:
                current = self._same_dest(dest, self._asana_target)
                icon = "folder" if dest['project_gid'] else "user-round"
                action = menu.add_option(
                    tinted_icon(icon, t.accent if current else t.fg_dim, 16),
                    self._dest_label(dest), current=current,
                    data=f"{dest['workspace_gid']}/{dest['project_gid']}")
                action.triggered.connect(lambda _=False, d=dest: self.set_asana_target(d))
        return menu

    def submit(self) -> bool:
        """Emite la tarea. Las «#etiquetas» que queden en el nombre pasan a
        etiquetas. Sin nombre no emite: avisa y sigue en el campo."""
        name, inline = extract_tags(self.name_edit.text())
        for tag in inline:
            self.add_tag(tag)
        if not name:
            if inline:
                self.name_edit.clear()
            self._warn_empty()
            return False
        self.task_submitted.emit(name, list(self._tags), self._cycle,
                                 self._due.isoformat() if self._due else '',
                                 self.targets)
        self._reset_draft()
        self.name_edit.setFocus(Qt.OtherFocusReason)
        self._confirm()
        return True

    _submit = submit  # compatibilidad

    def discard(self) -> None:
        """Escape: borra el borrador, pliega y suelta el foco."""
        # Antes de plegar: si el foco está en un chip y se oculta con él, Qt
        # lo pasa al siguiente widget de la ventana (el que sea)
        focus = QApplication.focusWidget()
        if focus is not None and self.isAncestorOf(focus):
            focus.clearFocus()
        self._reset_draft()
        self._end_warning()
        self._set_expanded(False)
        self.card.set_focused(False)
        self.name_edit.clearFocus()

    def build_date_menu(self) -> QMenu:
        """Menú de fecha (atajos, calendario y quitar). Público para los tests."""
        t = tokens()
        today = date.today()
        menu = _styled_menu(self)
        shortcut_dates = set()
        for key, label, icon in DUE_SHORTCUTS:
            when = shortcut_date(key, today)
            shortcut_dates.add(when)
            hint = (f"{_WEEKDAYS_SHORT[when.weekday()]} {when.day} {_MONTHS[when.month - 1]}"
                    if key == 'next_monday' else _WEEKDAYS_SHORT[when.weekday()])
            current = self._due == when
            action = menu.add_option(tinted_icon(icon, t.accent if current else t.fg_dim, 16),
                                     label, hint, current=current, data=key)
            action.triggered.connect(lambda _=False, when=when: self.set_due(when))
        menu.addSeparator()
        # Una fecha del calendario se ve aquí, marcada como la vigente
        custom = self._due is not None and self._due not in shortcut_dates
        pick = menu.add_option(
            tinted_icon("calendar-days", t.accent if custom else t.fg_dim, 16),
            "Elegir fecha…", due_text(self._due, today) if custom else '',
            current=custom, data="pick")
        # Tras cerrar el menú: abrir otra emergente desde su señal la anidaría
        pick.triggered.connect(lambda: QTimer.singleShot(0, self.open_calendar))
        if self._due is not None:
            clear = menu.add_option(tinted_icon("calendar-x", t.fg_dim, 16),
                                    "Quitar fecha", data="clear")
            clear.triggered.connect(lambda: self.set_due(None))
        return menu

    def build_repeat_menu(self) -> QMenu:
        """Menú de repetición con el ciclo actual marcado."""
        t = tokens()
        menu = _styled_menu(self)
        for text, cycle in RECURRING_OPTIONS:
            current = cycle == self._cycle
            icon = tinted_icon("check", t.accent, 16) if current else None
            action = menu.add_option(icon, text, current=current, data=cycle)
            action.triggered.connect(lambda _=False, c=cycle: self.set_cycle(c))
        return menu

    def open_calendar(self) -> '_DatePopup':
        popup = _DatePopup(self, self._due)
        popup.picked.connect(self.set_due)
        self._show_popup(popup, self.due_chip)
        return popup

    def open_tag_popup(self) -> '_TagPopup':
        popup = _TagPopup(self, self._known_tags, self._tags)
        popup.chosen.connect(self.add_tag)
        self._show_popup(popup, self.tags_chip)
        return popup

    # ------------------------------------------------------------ internos
    def _open_date_menu(self) -> None:
        self._show_menu(self.build_date_menu(), self.due_chip)

    def _open_repeat_menu(self) -> None:
        self._show_menu(self.build_repeat_menu(), self.repeat_chip)

    def _on_asana_clicked(self) -> None:
        """Sin configurar abre Ajustes; encendido se apaga; apagado pregunta
        dónde crearla (elegir un destino lo enciende)."""
        if self._asana_dests is None:
            self.setup_requested.emit('asana')
        elif self._asana_target is not None:
            self.set_asana_target(None)
        else:
            self._show_menu(self.build_asana_menu(), self.asana_btn)

    def _on_clickup_clicked(self) -> None:
        if not self._clickup_ready:
            self.setup_requested.emit('clickup')
        else:
            self.set_clickup(not self._clickup_on)

    @staticmethod
    def _clean_dest(dest) -> Optional[dict]:
        if not isinstance(dest, dict) or not str(dest.get('workspace_gid') or '').strip():
            return None
        return {'workspace_gid': str(dest['workspace_gid']).strip(),
                'project_gid': str(dest.get('project_gid') or '').strip(),
                'project_name': str(dest.get('project_name') or '').strip()}

    @staticmethod
    def _same_dest(a: Optional[dict], b: Optional[dict]) -> bool:
        return bool(a and b and a['workspace_gid'] == b['workspace_gid']
                    and a['project_gid'] == b['project_gid'])

    @staticmethod
    def _dest_label(dest: dict) -> str:
        if not dest['project_gid']:
            return NO_PROJECT_TEXT
        return dest['project_name'] or f"Proyecto {dest['project_gid']}"

    def _reconcile_asana_target(self) -> bool:
        """Con los destinos conocidos, el elegido toma su nombre al día o se
        apaga si ya no está. Sin configurar se conserva (vuelve al
        configurar). True si se apagó."""
        if self._asana_target is None or self._asana_dests is None:
            return False
        match = next((d for d in self._asana_dests
                      if self._same_dest(d, self._asana_target)), None)
        self._asana_target = dict(match) if match else None
        return match is None

    def _emit_targets(self) -> None:
        self.targets_changed.emit({'asana': self.asana_target, 'clickup': self._clickup_on})

    def _update_target_buttons(self) -> None:
        asana = self.asana_btn
        asana.set_dimmed(self._asana_dests is None)
        asana.setChecked(self._asana_target is not None)
        # Con un ciclo, Asana no: su API no admite la repetición
        asana.setEnabled(not self._cycle)
        if self._cycle:
            asana.setToolTip(_ASANA_REPEAT_TIP)
        elif self._asana_dests is None:
            asana.setToolTip("Configura Asana en Ajustes")
        elif self._asana_target is not None:
            asana.setToolTip(f"Crear también en Asana · {self._dest_label(self._asana_target)}")
        else:
            asana.setToolTip("Crear también en Asana (elige dónde)")
        clickup = self.clickup_btn
        clickup.set_dimmed(not self._clickup_ready)
        clickup.setChecked(self._clickup_on)
        if not self._clickup_ready:
            clickup.setToolTip("Configura ClickUp en Ajustes")
        elif self._clickup_on:
            clickup.setToolTip("Crear también en ClickUp · activado")
        else:
            clickup.setToolTip("Crear también en ClickUp")

    def _chip_clicked(self, chip: QWidget, opener) -> None:
        """Clic en un chip: abre su menú. En Windows, el clic que cierra un
        menú se repite sobre el widget de debajo: si es el mismo chip, ese
        clic cierra (como un desplegable) en vez de volver a abrirlo."""
        if chip is self._closed_from and time.monotonic() - self._closed_at < _REOPEN_GUARD:
            self._closed_from = None
            return
        opener()

    def _remember_close(self, anchor: QWidget) -> None:
        self._closed_from, self._closed_at = anchor, time.monotonic()

    def _show_menu(self, menu: QMenu, anchor: QWidget) -> None:
        menu.aboutToHide.connect(lambda: self._remember_close(anchor))
        menu.aboutToHide.connect(self._on_popup_closed)
        menu.aboutToHide.connect(menu.deleteLater)
        x, y = _place_above(anchor, self.card, menu.sizeHint())
        menu.popup(QPoint(x, y))

    def _show_popup(self, popup: _Popup, anchor: QWidget) -> None:
        if self._popup is not None:
            self._popup.close()
        self._popup = popup
        popup.closed.connect(lambda: self._remember_close(anchor))
        popup.closed.connect(self._on_popup_closed)
        popup.show_near(anchor, self.card)

    def _on_popup_closed(self) -> None:
        sender = self.sender()
        if sender is self._popup:
            self._popup = None
        # De vuelta al campo para seguir escribiendo
        if self.isVisible():
            QTimer.singleShot(0, lambda: self.isVisible()
                              and self.name_edit.setFocus(Qt.PopupFocusReason))

    def _draft_empty(self) -> bool:
        return (not self.name_edit.text().strip() and self._due is None
                and not self._cycle and not self._tags)

    def _reset_draft(self) -> None:
        self.name_edit.clear()
        for tag in list(self._tags):
            self.remove_tag(tag)
        self.set_due(None)
        self.set_cycle('')

    def _after_option_change(self) -> None:
        if not self._draft_empty():
            self._set_expanded(True)

    def _refresh_tag_model(self) -> None:
        self._tag_model.clear()
        for name in self._known_tags:
            if name not in self._tags:
                item = QStandardItem(_dot_icon(_tag_color(name)), name)
                item.setEditable(False)
                self._tag_model.appendRow(item)

    def _options_height(self) -> int:
        lay = self.options.layout()
        width = self.options.width() or self.card.width()
        height = lay.totalHeightForWidth(width) if lay.hasHeightForWidth() else -1
        return max(height, lay.totalSizeHint().height())

    def _set_expanded(self, on: bool, animate: bool = True) -> None:
        if on == self._expanded:
            return
        self._expanded = on
        self._update_placeholder()
        self._update_lead()
        self._expand_group.stop()
        self._opacity.setEnabled(True)
        if on:
            self.options.setVisible(True)
        start_h = self.options.maximumHeight()
        if start_h >= _QWIDGETSIZE_MAX:
            start_h = self.options.height()
        end_h = self._options_height() if on else 0
        if not animate or not self.isVisible():
            self.options.setMaximumHeight(end_h)
            self._opacity.setOpacity(1.0 if on else 0.0)
            self._on_expand_finished()
            return
        self._height_anim.setStartValue(start_h)
        self._height_anim.setEndValue(end_h)
        self._fade_anim.setStartValue(self._opacity.opacity())
        self._fade_anim.setEndValue(1.0 if on else 0.0)
        self._expand_group.start()

    def _on_expand_finished(self) -> None:
        if self._expanded:
            # Abierta, el alto lo decide el layout (los chips pueden saltar
            # de línea al añadir etiquetas o al estrechar el panel)
            self.options.setMaximumHeight(_QWIDGETSIZE_MAX)
            self._opacity.setOpacity(1.0)
            self._opacity.setEnabled(False)  # sin render fuera de pantalla
        else:
            self.options.setVisible(False)

    def _update_placeholder(self) -> None:
        """Texto y color del marcador: en reposo es la invitación (legible,
        fg_dim); abierta, una pista (fg_muted); sin nombre, el aviso."""
        t = tokens()
        if self._warning:
            text, color = "Escribe un nombre para la tarea", _mix(t.fg_muted, t.danger, 0.75)
        elif self._expanded:
            text, color = "Nombre de la tarea · usa #etiqueta para etiquetar", QColor(t.fg_muted)
        elif self._plan_hint:
            text, color = "Añadir una tarea para hoy…", QColor(t.fg_dim)
        else:
            text, color = "Añadir una tarea…", QColor(t.fg_dim)
        self.name_edit.setPlaceholderText(text)
        if color.name() != self._placeholder_color:
            self._placeholder_color = color.name()
            self._style_name_edit()

    def _style_name_edit(self) -> None:
        t = tokens()
        self.name_edit.setStyleSheet(
            f"QLineEdit {{ background: transparent; border: none; padding: 0px;"
            f" color: {t.fg}; placeholder-text-color: {self._placeholder_color};"
            f" selection-background-color: {t.accent}; selection-color: {t.on_accent}; }}")

    def _update_lead(self) -> None:
        if self._lead_timer.isActive():
            return
        self.lead.set_mode('draft' if self._expanded else 'add')

    def _confirm(self) -> None:
        """Confirmación sutil: destello del borde y un «✓» momentáneo."""
        self.card.flash()
        self.lead.set_mode('saved')
        self._lead_timer.start(900)

    def _warn_empty(self) -> None:
        self._warning = True
        self._update_placeholder()
        self.card.warn()
        self._warn_timer.start(1600)
        self.name_edit.setFocus(Qt.OtherFocusReason)

    def _end_warning(self) -> None:
        self._warn_timer.stop()
        if self._warning:
            self._warning = False
            self._update_placeholder()

    def _sync_submit(self, text: str) -> None:
        self.submit_btn.setEnabled(bool(extract_tags(text)[0]))
        if text.strip() and self._warning:
            self._end_warning()

    def _on_text_edited(self, text: str) -> None:
        self._convert_typed_tags()
        self._update_inline_completion()
        if self.name_edit.text() and not self._expanded:
            self._set_expanded(True)

    def _convert_typed_tags(self) -> None:
        """«#etiqueta » (ya cerrada con un espacio) pasa a chip y sale del
        nombre mientras se escribe."""
        text = self.name_edit.text()
        cursor = self.name_edit.cursorPosition()
        parts, found, pos, new_cursor = [], [], 0, cursor
        for match in _TYPED_TAG_RE.finditer(text):
            if not _is_tag(match.group(1)):
                continue
            start, end = match.span()
            if end < len(text) and text[end].isspace():
                end += 1  # el espacio que la cerró
            parts.append(text[pos:start])
            pos = end
            if cursor >= end:
                new_cursor -= end - start
            elif cursor > start:
                new_cursor -= cursor - start
            found.append(match.group(1).lower())
        if not found:
            return
        parts.append(text[pos:])
        self.name_edit.setText(''.join(parts))
        self.name_edit.setCursorPosition(max(0, new_cursor))
        for tag in found:
            self.add_tag(tag)

    def _update_inline_completion(self) -> None:
        popup = self._completer.popup()
        before = self.name_edit.text()[:self.name_edit.cursorPosition()]
        match = _PARTIAL_TAG_RE.search(before)
        if not match or not self._tag_model.rowCount():
            popup.hide()
            return
        self._completer.setCompletionPrefix(match.group(1))
        if self._completer.completionCount() == 0:
            popup.hide()
            return
        rect = self.name_edit.tag_anchor()
        rect.setWidth(max(180, popup.sizeHintForColumn(0) + 36))
        self._completer.complete(rect)
        # QCompleter calcula el alto sin el relleno de la lista: sin esto
        # aparece una scrollbar con tres sugerencias
        rows = min(self._completer.completionCount(), self._completer.maxVisibleItems())
        popup.resize(rect.width(), popup.sizeHintForRow(0) * rows + 2 * _COMPLETER_PAD)
        popup.setCurrentIndex(self._completer.completionModel().index(0, 0))
        # Encima de la tarjeta (la entrada vive al pie de la ventana) y con
        # el texto de la lista alineado bajo la «#»
        left = self.name_edit.mapToGlobal(QPoint(rect.x(), 0)).x() - 30
        top = self.card.mapToGlobal(QPoint(0, 0)).y() - popup.height() - 6
        popup.move(left, top)

    def _complete_inline_tag(self, tag: str) -> None:
        """Elegida en el autocompletado: «#co|» → chip «cocina»."""
        text = self.name_edit.text()
        cursor = self.name_edit.cursorPosition()
        match = _PARTIAL_TAG_RE.search(text[:cursor])
        if match:
            before, after = text[:match.start()], text[cursor:]
            if before.endswith(' ') and after.startswith(' '):
                after = after[1:]
            self.name_edit.setText(before + after)
            self.name_edit.setCursorPosition(len(before))
        self.add_tag(tag)

    # ----------------------------------------- alineación con las tarjetas
    def showEvent(self, event):
        super().showEvent(event)
        self._track_list_scrollbar()

    def _track_list_scrollbar(self) -> None:
        """El borde derecho de la entrada sigue al de las tarjetas: margen de
        la lista + la scrollbar cuando se ve. Solo lee el panel padre; si no
        es el de la lista (otro contenedor), queda el margen por defecto."""
        if self._list_bar is not None:
            return
        scroll = getattr(self.parentWidget(), 'scroll', None)
        if not isinstance(scroll, QScrollArea):
            return
        self._list_bar = scroll.verticalScrollBar()
        self._list_bar.installEventFilter(self)
        self._sync_right_margin()

    def _sync_right_margin(self) -> None:
        bar = self._list_bar
        if bar is None:
            return
        list_layout = getattr(self.parentWidget(), 'list_layout', None)
        inner = list_layout.contentsMargins().right() if list_layout is not None else 6
        right = inner + (bar.width() if bar.isVisible() else 0)
        lay = self.layout()
        m = lay.contentsMargins()
        if m.right() != right:
            lay.setContentsMargins(m.left(), m.top(), right, m.bottom())

    def _paint_completer_card(self, view: QWidget) -> None:
        """Fondo redondeado del autocompletado (ventana translúcida: el QSS
        de una lista top-level no pinta su fondo)."""
        t = tokens()
        p = QPainter(view)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor(t.border), 1))
        p.setBrush(QColor(t.surface))
        radius = t.radius_small + 2
        p.drawRoundedRect(QRectF(view.rect()).adjusted(0.5, 0.5, -0.5, -0.5), radius, radius)
        p.end()

    def eventFilter(self, obj, event):
        if obj is self._list_bar and event.type() in (QEvent.Show, QEvent.Hide):
            QTimer.singleShot(0, self._sync_right_margin)
        if obj is self._completer.popup() and event.type() == QEvent.Paint:
            self._paint_completer_card(obj)
            return False  # después pinta la lista encima
        if obj is self.name_edit and event.type() == QEvent.FocusIn:
            self.card.set_focused(True)
            # Al activarse la ventana el foco puede caer aquí solo: eso
            # no abre las opciones (escribir o hacer clic, sí)
            if event.reason() != Qt.ActiveWindowFocusReason:
                self._set_expanded(True)
        elif (event.type() == QEvent.FocusOut and obj in self._focusables()
              and event.reason() not in (Qt.PopupFocusReason, Qt.ActiveWindowFocusReason)):
            # Del nombre o de un chip/«Añadir» (con Tab se sale desde ahí).
            # Un menú o calendario propio no cuenta como salir, ni cambiar a
            # otra app un momento (el foco vuelve al regresar)
            QTimer.singleShot(0, self._after_focus_out)
        return super().eventFilter(obj, event)

    def _focusables(self) -> tuple:
        return (self.name_edit, self.due_chip, self.repeat_chip, self.tags_chip,
                self.asana_btn, self.clickup_btn, self.submit_btn,
                *self._tag_chips.values())

    def _after_focus_out(self) -> None:
        focus = QApplication.focusWidget()
        if focus is self.name_edit or self._popup is not None:
            return
        inside = focus is not None and self.isAncestorOf(focus)
        self.card.set_focused(inside)
        if not inside and self._draft_empty():
            self._end_warning()
            self._set_expanded(False)

    def keyPressEvent(self, event):
        # Escape con el foco en un chip o en «Añadir» (el campo ya lo
        # resuelve él): mismo efecto que desde el nombre
        if event.key() == Qt.Key_Escape:
            self.discard()
            event.accept()
            return
        super().keyPressEvent(event)

    def _retheme(self) -> None:
        t = tokens()
        self._placeholder_color = ''  # fuerza la hoja de estilo del campo
        self._update_placeholder()
        icon = QIcon()
        icon.addPixmap(tinted_icon("corner-down-left", t.on_accent, 14).pixmap(14, 14),
                       QIcon.Normal)
        icon.addPixmap(tinted_icon("corner-down-left", t.fg_muted, 14).pixmap(14, 14),
                       QIcon.Disabled)
        self.submit_btn.setIcon(icon)
        self._completer.popup().setStyleSheet(
            "QListView { background: transparent; border: none;"
            f" padding: {_COMPLETER_PAD}px; }}"
            f"QListView::item {{ padding: 5px 8px; border-radius: 6px; color: {t.fg}; }}"
            f"QListView::item:selected {{ background: {t.surface_active}; color: {t.fg}; }}")
        for tag, chip in self._tag_chips.items():
            chip.set_value(tag, True, color=_tag_color(tag))
        for chip in (self.due_chip, self.repeat_chip, self.tags_chip, self.plan_chip,
                     self.asana_btn, self.clickup_btn, *self._tag_chips.values()):
            chip.retheme()
        self.card.update()
        self.lead.update()
