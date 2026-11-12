"""Tarjeta de tarea: check, nombre, marcas (Asana, fija), tiempo, tags, fechas
y acciones.

El nombre es el protagonista: hasta dos líneas, recortado con «…» al final
si no cabe. Debajo, discretos, los metadatos (Asana y proyecto, fija, tiempo,
tags). Las fechas viven en una columna de ancho fijo a la derecha
(vencimiento o completado arriba, creación debajo); al pasar el ratón esa
misma columna muestra editar/notas/eliminar (y subir/bajar en orden manual),
así el nombre no salta. Las fechas completas quedan en el tooltip.

El fondo se pinta en paintEvent: el hover anima un color sin regenerar la
hoja de estilo en cada frame.

Animaciones: hover (color interpolado), selección (barra de acento),
completar (pop del check) y colapso de altura al desaparecer.
"""
from datetime import date, datetime
from functools import lru_cache
from typing import Dict, List, Optional

from PySide6.QtCore import (QEasingCurve, QEvent, QPropertyAnimation, QRectF, QSize,
                            Qt, QVariantAnimation, Signal)
from PySide6.QtGui import (QColor, QFont, QFontMetrics, QPainter, QPainterPath, QPen,
                           QTextLayout, QTextOption)
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QSizePolicy,
                               QStackedLayout, QVBoxLayout, QWidget)

from ui.theme import fonts
from ui.theme.tokens import tokens
from ui.widgets.icon_button import IconButton, tinted_icon

_MONTHS = ('ene', 'feb', 'mar', 'abr', 'may', 'jun',
           'jul', 'ago', 'sep', 'oct', 'nov', 'dic')
# Tags internos que no aportan nada a la vista
_HIDDEN_TAGS = {'asana', 'asana_deleted'}
_MAX_TAGS = 3
_PROJECT_MAX_W = 150  # px: un proyecto largo no se come la fila de metadatos

# Márgenes de la tarjeta, separación nombre → metadatos y extra entre grupos
# de metadatos (Asana, fija, tiempo, tags)
_PAD_H, _PAD_V = 12, 12
_META_GAP = 4
_GROUP_GAP = 6


def _short_date(d) -> str:
    """'7 oct' — en español y sin depender del locale del sistema."""
    return f"{d.day} {_MONTHS[d.month - 1]}"


def _tag_color(tag: str) -> str:
    palette = tokens().tag_colors
    return palette[sum(ord(c) for c in tag) % len(palette)]


def _local_datetime(raw) -> Optional[datetime]:
    """ISO → datetime en hora local (Asana envía UTC '…Z'), o None."""
    try:
        d = datetime.fromisoformat(str(raw).replace('Z', '+00:00'))
    except ValueError:
        return None
    return d.astimezone() if d.tzinfo is not None else d


def _created_info(task: Dict) -> Optional[tuple]:
    """(texto corto, fecha y hora completas) de la creación, o None.
    Fallback: el id es un timestamp en ms (así se generan desde siempre)."""
    raw = task.get('created_at')
    if not raw:
        try:
            raw = datetime.fromtimestamp(task['id'] / 1000).isoformat()
        except (KeyError, TypeError, ValueError, OSError, OverflowError):
            return None
    d = _local_datetime(raw)
    if d is None:
        return None
    return _short_date(d), d.strftime("%d/%m/%Y %H:%M")


def _completed_info(task: Dict) -> Optional[tuple]:
    """(texto corto, fecha y hora completas) del completado, o None (datos
    viejos o tareas completadas en Asana sin completed_at)."""
    raw = task.get('completed_at')
    d = _local_datetime(raw) if raw else None
    if d is None:
        return None
    return _short_date(d), d.strftime("%d/%m/%Y %H:%M")


def _due_info(due: Optional[str]) -> Optional[tuple]:
    """(texto, color, tooltip) según vencimiento, o None."""
    if not due:
        return None
    t = tokens()
    # Fecha LOCAL: un due_at a las 22:00 de Lima ya es «mañana» en UTC
    from core.day_plan import due_local_date
    d = due_local_date({'due': due})
    if d is None:
        return f"Vence {due}", t.fg_muted, f"Vence: {due}"
    delta = (d - date.today()).days
    full = d.strftime("%d/%m/%Y")
    if delta < 0:
        return f"Vencida · {_short_date(d)}", t.danger, f"Venció el {full}"
    if delta == 0:
        return "Vence hoy", t.warning, f"Vence hoy, {full}"
    if delta == 1:
        return "Vence mañana", t.warning, f"Vence mañana, {full}"
    return f"Vence {_short_date(d)}", t.fg_dim, f"Vence el {full}"


def _due_font() -> QFont:
    return fonts.font(fonts.XS, weight=QFont.Weight.Medium)


def _created_font() -> QFont:
    return fonts.font(fonts.XS - 0.5)


@lru_cache(maxsize=4)
def _dates_width(family: str) -> int:
    """Ancho de la columna de fechas: el del texto más largo posible con
    cualquier mes, para que todas las tarjetas alineen la columna (la
    familia es la clave de caché: init_fonts puede cambiarla)."""
    due_fm, created_fm = QFontMetrics(_due_font()), QFontMetrics(_created_font())
    widths = [due_fm.horizontalAdvance("Vence mañana")]
    for month in _MONTHS:
        day = f"28 {month}"
        widths += [due_fm.horizontalAdvance(f"Vencida · {day}"),
                   due_fm.horizontalAdvance(f"Completada {day}"),
                   created_fm.horizontalAdvance(f"Creada {day}")]
    return max(widths) + 2


class _ClampedLabel(QLabel):
    """QLabel de hasta `max_lines` líneas ajustadas por palabras, con «…» al
    final de la última si el texto no cabe (el completo queda en el
    tooltip). No impone ancho mínimo; su alto sigue a las líneas reales."""

    def __init__(self, text: str = "", max_lines: int = 2, parent=None):
        super().__init__(parent)
        self._full = ""
        self._max_lines = max_lines
        self.setTextFormat(Qt.PlainText)
        self.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (API de Qt)
        self._full = text or ""
        self._refresh()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, '_max_lines'):
            self._refresh()

    def _lines(self, width: int) -> tuple:
        """(líneas a mostrar, ¿se recortó?) para el ancho dado."""
        text = " ".join(self._full.split())  # saltos y espacios dobles fuera
        if width <= 0 or not text:
            return [text], False  # aún sin geometría: una línea provisional
        layout = QTextLayout(text, self.font())
        option = QTextOption()
        option.setWrapMode(QTextOption.WrapAtWordBoundaryOrAnywhere)
        layout.setTextOption(option)
        spans = []
        layout.beginLayout()
        while True:
            line = layout.createLine()
            if not line.isValid():
                break
            line.setLineWidth(width)
            spans.append((line.textStart(), line.textLength()))
        layout.endLayout()
        if len(spans) <= self._max_lines:
            return [text[s:s + n].strip() for s, n in spans], False
        lines = [text[s:s + n].strip() for s, n in spans[:self._max_lines - 1]]
        rest = text[spans[self._max_lines - 1][0]:]
        last = self.fontMetrics().elidedText(rest, Qt.ElideRight, width)
        # «clientes …» → «clientes…» (el corte suele caer tras un espacio)
        lines.append(last[:-1].rstrip() + "…" if last.endswith("…") else last)
        return lines, True

    def _refresh(self) -> None:
        lines, elided = self._lines(self.width())
        shown = "\n".join(lines)
        if shown != super().text():
            super().setText(shown)  # QLabel ya llama updateGeometry: el alto sigue
        self.setToolTip(self._full if elided else "")


class _MetaRow(QWidget):
    """Fila de metadatos que NO impone ancho mínimo a la tarjeta. Si no cabe,
    oculta enteros los últimos elementos (los menos importantes: tags) en vez
    de encoger todas las etiquetas y cortarlas a mitad de palabra, o de
    ensanchar la tarjeta y empujar la columna de fechas.

    Coloca a mano a sus hijos (parent=self): así la separación extra entre
    grupos solo aparece si hay algo visible antes, sin espaciadores."""

    SPACING = 6

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self._items: List[list] = []  # [widget, gap, wanted]

    def add(self, widget: QWidget, *, gap: int = 0, wanted: bool = True) -> QWidget:
        """Añade un elemento (hijo de esta fila) con `gap` px extra a su
        izquierda para separarlo del grupo anterior. `wanted=False` lo deja
        fuera hasta set_wanted (p. ej. el tiempo mientras sea 0)."""
        self._items.append([widget, gap, wanted])
        if not wanted:
            widget.setVisible(False)
        self.updateGeometry()
        return widget

    def set_wanted(self, widget: QWidget, wanted: bool) -> None:
        for item in self._items:
            if item[0] is widget:
                item[2] = wanted
        self._fit()

    def has_wanted(self) -> bool:
        return any(wanted for _, _, wanted in self._items)

    def sizeHint(self):  # noqa: N802 (API de Qt)
        # Alto estable: el del elemento más alto, visible o no
        h = max((w.sizeHint().height() for w, _, _ in self._items), default=0)
        return QSize(0, h)

    def minimumSizeHint(self):  # noqa: N802 (API de Qt)
        return self.sizeHint()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit()

    def _fit(self) -> None:
        available, h = self.width(), self.height()
        x = 0
        overflow = False
        for w, gap, wanted in self._items:
            if not wanted:
                w.setVisible(False)
                continue
            hint = w.sizeHint()
            lead = self.SPACING + gap if x else 0
            overflow = overflow or x + lead + hint.width() > available
            if overflow:
                w.setVisible(False)
                continue
            x += lead
            w.setGeometry(x, (h - hint.height()) // 2, hint.width(), hint.height())
            w.setVisible(True)
            x += hint.width()


def _meta_label(text: str, color: str, *, bold: bool = False, parent=None) -> QLabel:
    lbl = QLabel(text, parent)
    lbl.setFont(fonts.font(fonts.XS, bold=bold))
    lbl.setStyleSheet(f"color: {color}; background: transparent;")
    return lbl


def _meta_icon(name: str, color: str, size: int = 12, parent=None) -> QLabel:
    lbl = QLabel(parent)
    lbl.setPixmap(tinted_icon(name, color, size).pixmap(size, size))
    lbl.setFixedSize(size, size)
    return lbl


def _meta_group(parent: QWidget, icon: str, icon_color: str, label: QLabel,
                tooltip: str = "", icon_size: int = 12) -> QWidget:
    """Icono + etiqueta como una sola pieza (se oculta entera)."""
    box = QWidget(parent)
    lay = QHBoxLayout(box)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(4)
    lay.addWidget(_meta_icon(icon, icon_color, icon_size, box), 0, Qt.AlignVCenter)
    lay.addWidget(label, 0, Qt.AlignVCenter)
    if tooltip:
        box.setToolTip(tooltip)
    return box


class TaskCard(QFrame):
    # ⚠ Los task_id son timestamps en ms (>2^31): Signal(int) es int32 en C++
    # y DESBORDA — la señal muere con OverflowError y el clic "no hace nada".
    # Signal(object) transporta el int de Python intacto.
    selected = Signal(object)
    toggle_completed = Signal(object)
    edit_requested = Signal(object)
    note_requested = Signal(object)
    delete_requested = Signal(object)
    move_up = Signal(object)
    move_down = Signal(object)
    drag_started = Signal(object, object)  # (card, global_pos)

    def __init__(self, task: Dict, time_seconds: float, is_active: bool,
                 manual_mode: bool, parent=None):
        super().__init__(parent)
        t = tokens()
        self.task_id = task['id']
        self._completed = bool(task.get('completed'))
        self._active = is_active
        self._hover = 0.0
        self._due = _due_info(task.get('due'))
        self._created = _created_info(task)
        self._done = _completed_info(task)
        self._effort = task.get('effort')  # valoración 1-5 al completarla
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setCursor(Qt.PointingHandCursor)
        self.setObjectName("taskCard")
        self.setAttribute(Qt.WA_Hover, True)

        self._hover_anim = QVariantAnimation(self)
        self._hover_anim.setDuration(140)
        self._hover_anim.valueChanged.connect(self._on_hover_value)

        lay = QHBoxLayout(self)
        # Margen superior ≥ 11: el test de clic real pulsa en y=10 y espera
        # SELECCIONAR — ahí no puede haber ningún hijo clicable
        lay.setContentsMargins(_PAD_H, _PAD_V, _PAD_H, _PAD_V)
        lay.setSpacing(12)

        # Asa de arrastre (solo orden manual).
        # ⚠ SIEMPRE crear los botones con parent=self y llamar setVisible
        # DESPUÉS de addWidget: setVisible(True) sobre un widget sin padre lo
        # muestra como VENTANA top-level (parpadeo de ventanitas + ~280 ms de
        # HWND nativo por botón; el arranque tardaba ~57 s por esto).
        self.grip = IconButton("grip", "Arrastrar para reordenar",
                               color=t.fg_muted, hover_color=t.fg, size=15,
                               parent=self)
        self.grip.setCursor(Qt.OpenHandCursor)
        self.grip.pressed_pos = None
        self.grip.mousePressEvent = self._grip_press
        lay.addWidget(self.grip, 0, Qt.AlignVCenter)
        self.grip.setVisible(manual_mode)

        # Check
        self.check = IconButton("check-circle" if self._completed else "circle", "",
                                color=t.accent if self._completed else t.fg_muted,
                                hover_color=t.accent, size=20, parent=self)
        self.check.setToolTip("Marcar como pendiente" if self._completed
                              else "Completar tarea")
        self.check.clicked.connect(lambda: self.toggle_completed.emit(self.task_id))
        lay.addWidget(self.check, 0, Qt.AlignVCenter)

        # Nombre (protagonista) + metadatos, centrados en vertical
        text_col = QVBoxLayout()
        text_col.setContentsMargins(0, 0, 0, 0)
        text_col.setSpacing(_META_GAP)
        text_col.addStretch(1)
        self.name_label = _ClampedLabel(task.get('name', ''), 2, self)
        self.name_label.setFont(self._name_font())
        self.name_label.setStyleSheet(self._name_style())
        text_col.addWidget(self.name_label)
        self._meta_row = _MetaRow(self)
        text_col.addWidget(self._meta_row)
        text_col.addStretch(1)
        lay.addLayout(text_col, 1)
        self._build_meta(task, time_seconds)
        self._meta_row.setVisible(self._meta_row.has_wanted())

        # Columna derecha: fechas en reposo, acciones al pasar el ratón
        self._right = QWidget(self)
        self._right_stack = QStackedLayout(self._right)
        self._right_stack.setContentsMargins(0, 0, 0, 0)

        info = QWidget(self._right)
        info_lay = QVBoxLayout(info)
        info_lay.setContentsMargins(0, 0, 0, 0)
        info_lay.setSpacing(2)
        info_lay.addStretch(1)
        self.due_label = QLabel(info)
        self.due_label.setFont(_due_font())
        self.due_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        info_lay.addWidget(self.due_label)
        self.created_label = QLabel(info)
        self.created_label.setFont(_created_font())
        self.created_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.created_label.setStyleSheet(f"color: {t.fg_muted}; background: transparent;")
        info_lay.addWidget(self.created_label)
        info_lay.addStretch(1)
        self._right_stack.addWidget(info)
        if self._created:
            self.created_label.setText(f"Creada {self._created[0]}")
        self.created_label.setVisible(bool(self._created))
        self._update_dates()

        actions = QWidget(self._right)
        act_lay = QHBoxLayout(actions)
        act_lay.setContentsMargins(0, 0, 0, 0)
        act_lay.setSpacing(0)
        act_lay.addStretch(1)
        # Fondo del hover de cada acción: un escalón sobre el hover de la
        # tarjeta (con el del QSS global, surface_hover, no se distinguía)
        action_qss = f"QToolButton:hover {{ background: {t.surface_active}; }}"
        # Subir/bajar apilados: ocupan una sola columna (mismas reglas:
        # parent, setVisible tras addWidget)
        self._order_box = QWidget(actions)
        order_lay = QVBoxLayout(self._order_box)
        order_lay.setContentsMargins(0, 0, 2, 0)
        order_lay.setSpacing(0)
        self.up_btn = IconButton("chevron-up", "Subir", color=t.fg_dim,
                                 hover_color=t.fg, size=13, parent=self._order_box)
        self.down_btn = IconButton("chevron-down", "Bajar", color=t.fg_dim,
                                   hover_color=t.fg, size=13, parent=self._order_box)
        for b in (self.up_btn, self.down_btn):
            b.setStyleSheet(f"QToolButton {{ padding: 1px 4px; border-radius: 5px; }}"
                            f"{action_qss}")
            order_lay.addWidget(b)
        self.up_btn.clicked.connect(lambda: self.move_up.emit(self.task_id))
        self.down_btn.clicked.connect(lambda: self.move_down.emit(self.task_id))
        act_lay.addWidget(self._order_box, 0, Qt.AlignVCenter)
        self.up_btn.setVisible(manual_mode)
        self.down_btn.setVisible(manual_mode)
        self._order_box.setVisible(manual_mode)
        for icon, tip, sig in (("edit", "Editar", self.edit_requested),
                               ("note", "Notas", self.note_requested),
                               ("delete", "Eliminar", self.delete_requested)):
            b = IconButton(icon, tip, color=t.fg_dim,
                           hover_color=t.danger if icon == "delete" else t.fg,
                           size=15, parent=actions)
            b.setStyleSheet(action_qss)
            b.clicked.connect(lambda _=False, s=sig: s.emit(self.task_id))
            act_lay.addWidget(b, 0, Qt.AlignVCenter)
        self._right_stack.addWidget(actions)
        # Ancho fijo (el mayor entre fechas y acciones) y común a todas las
        # tarjetas: la columna alinea y el nombre no salta al hacer hover
        self._right.setFixedWidth(max(_dates_width(fonts.ui_family()),
                                      actions.sizeHint().width()))
        lay.addWidget(self._right)

        # Alto mínimo = una línea de nombre + una de metadatos: las tarjetas
        # sin metadatos (o con nombre de dos líneas) no quedan más bajas.
        # ⚠ Va en sizeHint y no en setMinimumHeight: un mínimo explícito
        # reemplaza al sizeHint como mínimo de la política Fixed, y en la
        # lista con scroll (alto = suma de mínimos) los nombres de dos
        # líneas quedaban cortados
        name_h = QFontMetrics(self.name_label.font()).height()
        meta_h = QFontMetrics(fonts.font(fonts.XS)).height() + 2
        self._floor_h = 2 * _PAD_V + name_h + _META_GAP + meta_h

    # ---- construcción ----
    def _build_meta(self, task: Dict, time_seconds: float) -> None:
        """Metadatos bajo el nombre, por importancia (los últimos se ocultan
        primero si no caben): Asana + proyecto, fija, tiempo y tags."""
        t = tokens()
        row = self._meta_row
        projects = [p.get('name') for p in task.get('projects') or []
                    if isinstance(p, dict) and p.get('name')]
        if task.get('source') == 'asana' or task.get('asana_deleted'):
            if task.get('asana_deleted'):
                label = _meta_label("Eliminada en Asana", t.danger, bold=True)
                tip = "La tarea ya no existe en Asana"
            else:
                text = projects[0] if projects else "Asana"
                fm = QFontMetrics(fonts.font(fonts.XS))
                label = _meta_label(fm.elidedText(text, Qt.ElideRight, _PROJECT_MAX_W),
                                    t.fg_muted)
                tip = "Tarea de Asana" + (f" · {', '.join(projects)}" if projects else "")
            row.add(_meta_group(row, "asana-mark", t.asana, label, tip, icon_size=11))
        if task.get('recurring'):
            from core.data_handler import recurring_cycle
            cycle = recurring_cycle(task)
            cycle_labels = {'daily': ("Diaria", "cada día"),
                            'weekly': ("Semanal", "cada semana"),
                            'monthly': ("Mensual", "cada mes"),
                            'quarterly': ("Trimestral", "cada trimestre")}
            label, freq = cycle_labels.get(cycle, cycle_labels['daily'])
            tip = f"Tarea fija: al completarla vuelve a Pendientes {freq}"
            row.add(_meta_group(row, "repeat", t.fg_dim, _meta_label(label, t.fg_muted),
                                tip), gap=_GROUP_GAP)
        self.time_label = _meta_label(self._fmt_time(time_seconds), t.fg_muted)
        self._time_box = _meta_group(row, "timer", t.fg_dim, self.time_label,
                                     "Tiempo enfocado en esta tarea")
        row.add(self._time_box, gap=_GROUP_GAP, wanted=bool(self.time_label.text()))
        # Esfuerzo valorado al completarla: «★ n», solo en las completadas
        self.effort_label = _meta_label("", t.fg_muted)
        self._effort_box = _meta_group(row, "star", t.fg_dim, self.effort_label)
        row.add(self._effort_box, gap=_GROUP_GAP, wanted=False)
        self._sync_effort()
        # Los proyectos de Asana también llegan como tags: no repetirlos
        skip = _HIDDEN_TAGS | {p.lower() for p in projects}
        tags = [tag for tag in (task.get('tags') or [])
                if str(tag).lower() not in skip]
        for i, tag in enumerate(tags[:_MAX_TAGS]):
            c = _tag_color(tag)
            qc = QColor(c)
            chip = QLabel(tag, row)
            chip.setFont(fonts.font(fonts.XS - 0.5))
            chip.setStyleSheet(
                f"color: {c}; background: rgba({qc.red()},{qc.green()},{qc.blue()},30);"
                f" border-radius: 6px; padding: 1px 6px;")
            row.add(chip, gap=_GROUP_GAP if i == 0 else 0)
        if len(tags) > _MAX_TAGS:
            more = _meta_label(f"+{len(tags) - _MAX_TAGS}", t.fg_muted, parent=row)
            more.setToolTip(", ".join(tags[_MAX_TAGS:]))
            row.add(more)

    def sizeHint(self):  # noqa: N802 (API de Qt)
        hint = super().sizeHint()
        hint.setHeight(max(hint.height(), getattr(self, '_floor_h', 0)))
        return hint

    # ---- estilo ----
    def _name_font(self) -> QFont:
        f = fonts.font(fonts.NORMAL - 0.5, weight=QFont.Weight.Medium)
        f.setStrikeOut(self._completed)
        return f

    def _name_style(self) -> str:
        t = tokens()
        color = t.fg_muted if self._completed else t.fg
        return f"color: {color}; background: transparent;"

    def _update_dates(self) -> None:
        """Línea superior de la columna derecha (vencimiento, o completado si
        la tarea está hecha) y tooltip de la tarjeta con las fechas completas
        (al pasar el ratón la columna muestra las acciones)."""
        t = tokens()
        tips = []
        text, color = "", t.fg_dim
        if self._completed:
            if self._done:
                text = f"Completada {self._done[0]}"
                tips.append(f"Completada el {self._done[1]}")
            else:
                text = "Completada"
            if self._effort:
                tips.append(f"Esfuerzo {self._effort}/5")
        elif self._due:
            text, color, _ = self._due
        if self._due:
            tips.append(self._due[2])
        if self._created:
            tips.append(f"Creada el {self._created[1]}")
        self.due_label.setText(text)
        self.due_label.setStyleSheet(f"color: {color}; background: transparent;")
        self.due_label.setVisible(bool(text))
        self.setToolTip("\n".join(tips))

    def _bg_color(self) -> QColor:
        t = tokens()
        if self._active:
            return QColor(t.surface_active)
        a, b = QColor(t.surface), QColor(t.surface_hover)
        f = self._hover
        return QColor(round(a.red() + (b.red() - a.red()) * f),
                      round(a.green() + (b.green() - a.green()) * f),
                      round(a.blue() + (b.blue() - a.blue()) * f))

    def paintEvent(self, event):
        t = tokens()
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(r, t.radius, t.radius)
        p.fillPath(path, self._bg_color())
        border = QColor(t.accent if self._active else t.border)
        if self._active:
            border.setAlpha(110)
        p.setPen(QPen(border, 1))
        p.drawPath(path)
        if self._active:
            # Barra de acento a la izquierda: «esta es la tarea del timer»
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(t.accent))
            p.drawRoundedRect(QRectF(0, 10, 3, self.height() - 20), 1.5, 1.5)
        p.end()

    # ---- API ----
    def set_active(self, active: bool) -> None:
        if active != self._active:
            self._active = active
            self.update()

    def set_completed(self, completed: bool) -> None:
        t = tokens()
        self._completed = completed
        if completed:
            now = datetime.now()
            self._done = (_short_date(now), now.strftime("%d/%m/%Y %H:%M"))
        else:
            self._effort = None  # reabrir quita la valoración de la tarjeta
        self._sync_effort()
        self.check.set_icon_name("check-circle" if completed else "circle")
        self.check.set_colors(t.accent if completed else t.fg_muted, t.accent)
        self.check.setToolTip("Marcar como pendiente" if completed else "Completar tarea")
        self.name_label.setFont(self._name_font())  # re-recorta (FontChange)
        self.name_label.setStyleSheet(self._name_style())
        self._update_dates()
        # Pop del check
        pop = QVariantAnimation(self)
        pop.setDuration(320)
        pop.setStartValue(27)
        pop.setEndValue(20)
        pop.setEasingCurve(QEasingCurve.OutBack)
        pop.valueChanged.connect(lambda v: self.check.setIconSize(QSize(int(v), int(v))))
        pop.start(QVariantAnimation.DeleteWhenStopped)

    def set_time(self, seconds: float) -> None:
        text = self._fmt_time(seconds)
        if text == self.time_label.text():
            return  # el tick llega cada segundo: solo cambia por minuto
        self.time_label.setText(text)
        self._meta_row.set_wanted(self._time_box, bool(text))
        self._meta_row.setVisible(self._meta_row.has_wanted())

    def set_effort(self, effort: Optional[int]) -> None:
        """Valoración de esfuerzo (1-5) recién guardada: «★ n» en los
        metadatos y «Esfuerzo n/5» en el tooltip."""
        self._effort = effort
        self._sync_effort()
        self._update_dates()

    def _sync_effort(self) -> None:
        shown = bool(self._completed and self._effort)
        if shown:
            self.effort_label.setText(str(self._effort))
            self._effort_box.setToolTip(f"Esfuerzo {self._effort}/5")
        self._meta_row.set_wanted(self._effort_box, shown)
        self._meta_row.setVisible(self._meta_row.has_wanted())

    def animate_remove(self, on_done) -> None:
        """Colapsa la tarjeta y llama on_done al terminar."""
        self.setMinimumHeight(0)
        anim = QPropertyAnimation(self, b"maximumHeight", self)
        anim.setDuration(240)
        anim.setStartValue(self.height())
        anim.setEndValue(0)
        anim.setEasingCurve(QEasingCurve.InCubic)
        anim.finished.connect(on_done)
        anim.start(QPropertyAnimation.DeleteWhenStopped)

    @staticmethod
    def _fmt_time(seconds: float) -> str:
        m = int(seconds // 60)
        if m <= 0:
            return ""
        if m >= 60:
            return f"{m // 60} h {m % 60} min"
        return f"{m} min"

    # ---- eventos ----
    def _grip_press(self, event):
        if event.button() == Qt.LeftButton:
            self.drag_started.emit(self, event.globalPosition().toPoint())

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.selected.emit(self.task_id)
        super().mousePressEvent(event)

    def _on_hover_value(self, v):
        self._hover = float(v)
        self.update()

    def enterEvent(self, event):
        self._right_stack.setCurrentIndex(1)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(1.0)
        self._hover_anim.start()
        super().enterEvent(event)

    def leaveEvent(self, event):
        self._right_stack.setCurrentIndex(0)
        self._hover_anim.stop()
        self._hover_anim.setStartValue(self._hover)
        self._hover_anim.setEndValue(0.0)
        self._hover_anim.start()
        super().leaveEvent(event)
