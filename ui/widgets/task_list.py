"""Panel de tareas: filtros, orden, lista scrollable de tarjetas y añadir.

Mantiene un registro de tarjetas por id para actualizaciones in-place
(selección, completado, tiempo) sin reconstruir la lista — la lección
aprendida en v8.
"""
from typing import Dict, List, Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, Qt, Signal
from PySide6.QtGui import QGuiApplication
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (QButtonGroup, QFrame, QHBoxLayout, QLabel,
                               QPushButton, QScrollArea, QSizePolicy, QVBoxLayout,
                               QWidget)

from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.task_add_panel import TaskAddPanel
from ui.widgets.task_card import TaskCard

FILTERS = [("today", "Hoy"), ("pending", "Pendientes"), ("completed", "Completadas"),
           ("recurring", "Fijas"), ("all", "Todas")]
SORTS = [("manual", "Manual"), ("project", "Proyecto"),
         ("due", "Fecha"), ("name", "Nombre"), ("created", "Creada")]
# Iconos de los segmentos de orden (antes, emojis)
SORT_ICONS = {"manual": "hand", "project": "folder", "due": "calendar",
              "name": "sort-az", "created": "clock"}


def _segmented() -> tuple:
    """Carril de un control segmentado: (frame, layout)."""
    frame = QFrame()
    frame.setProperty("segmented", True)
    lay = QHBoxLayout(frame)
    lay.setContentsMargins(3, 3, 3, 3)
    lay.setSpacing(2)
    return frame, lay


class TaskListPanel(QWidget):
    filter_changed = Signal(str)
    sort_changed = Signal(str)
    # Signal(object) y no Signal(int): los task_id superan int32 (ver TaskCard)
    task_selected = Signal(object)
    task_toggle = Signal(object)
    task_edit = Signal(object)
    task_note = Signal(object)
    task_delete = Signal(object)
    task_move_up = Signal(object)
    task_move_down = Signal(object)
    order_changed = Signal(list)
    # (nombre, tags, ciclo de repetición o '', vencimiento 'YYYY-MM-DD' o '',
    # destinos {'asana': {…}, 'clickup': True} o {}: ver TaskAddPanel)
    task_added = Signal(str, list, str, str, dict)
    position_cycle_requested = Signal()
    plan_requested = Signal()  # abrir el «Plan de hoy»
    advanced_toggled = Signal(bool)  # filtros avanzados (orden) abiertos/cerrados

    def __init__(self, parent=None):
        super().__init__(parent)
        t = tokens()
        self._cards: Dict[int, TaskCard] = {}
        self._drag_card: Optional[TaskCard] = None
        self._manual_mode = True

        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 14, 10, 14)
        lay.setSpacing(10)

        # Título + contador (el sync vive ahora junto al estado de conexión,
        # en la parte superior del panel del timer)
        head = QHBoxLayout()
        head.setSpacing(8)
        title = QLabel("Tareas")
        title.setFont(fonts.display(fonts.MEDIUM, weight=QFont.Weight.DemiBold,
                                    tabular=False))
        title.setStyleSheet(f"color: {t.fg};")
        head.addWidget(title, 0, Qt.AlignBottom)
        self.counter = QLabel("")
        self.counter.setFont(fonts.font(fonts.XS))
        self.counter.setProperty("role", "muted")
        head.addWidget(self.counter, 0, Qt.AlignBottom)
        head.addStretch(1)
        # Orden de la lista (antes «Filtros avanzados» junto a los filtros,
        # donde a 760 px recortaba las píldoras): compacto en la cabecera
        self._advanced_open = False
        self.advanced_btn = QPushButton("Orden")
        self.advanced_btn.setProperty("pill", True)
        self.advanced_btn.setCursor(Qt.PointingHandCursor)
        self.advanced_btn.setToolTip("Mostrar u ocultar las opciones de orden "
                                     "(manual, proyecto, fecha, nombre, creación)")
        self.advanced_btn.clicked.connect(self._toggle_advanced)
        head.addWidget(self.advanced_btn, 0, Qt.AlignVCenter)
        from ui.widgets.icon_button import IconButton
        self.plan_btn = IconButton("calendar-check", "Planificar el día",
                                   color=t.fg_muted, hover_color=t.accent, size=16)
        self.plan_btn.clicked.connect(self.plan_requested.emit)
        head.addWidget(self.plan_btn)
        self.position_btn = IconButton("panel-left", "Cambiar el panel de tareas de lado",
                                       color=t.fg_muted, hover_color=t.fg, size=15)
        self.position_btn.clicked.connect(self.position_cycle_requested.emit)
        head.addWidget(self.position_btn)
        lay.addLayout(head)

        # Filtros de estado + botón de filtros avanzados (orden, colapsable)
        self._filter_group = QButtonGroup(self)
        self._filter_group.setExclusive(True)
        filters_row = QHBoxLayout()
        filters_row.setSpacing(8)
        segment, segment_lay = _segmented()
        self._filter_buttons = {}
        for key, label in FILTERS:
            b = QPushButton(label)
            b.setProperty("pill", True)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, k=key: self.filter_changed.emit(k))
            self._filter_group.addButton(b)
            self._filter_buttons[key] = b
            segment_lay.addWidget(b)
        # Ancho fijo: si falta sitio, el divisor cede (antes se recortaban
        # las píldoras: «lo», «die»…)
        segment.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        filters_row.addWidget(segment)
        filters_row.addStretch(1)
        lay.addLayout(filters_row)

        # Orden (dentro del contenedor colapsable de filtros avanzados)
        self._sort_group = QButtonGroup(self)
        self._sort_group.setExclusive(True)
        self.sort_box = QWidget()
        sort_row = QHBoxLayout(self.sort_box)
        sort_row.setContentsMargins(0, 0, 0, 0)
        sort_row.setSpacing(8)
        sort_caption = QLabel("Ordenar")
        sort_caption.setFont(fonts.font(fonts.XS))
        sort_caption.setProperty("role", "muted")
        sort_row.addWidget(sort_caption)
        sort_segment, sort_segment_lay = _segmented()
        from ui.widgets.icon_button import tinted_icon
        self._sort_buttons = {}
        for key, label in SORTS:
            b = QPushButton(label)
            b.setProperty("pill", True)
            b.setCheckable(True)
            b.setCursor(Qt.PointingHandCursor)
            b.setIcon(tinted_icon(SORT_ICONS[key], t.fg_dim, 14))
            b.clicked.connect(lambda _=False, k=key: self.sort_changed.emit(k))
            self._sort_group.addButton(b)
            self._sort_buttons[key] = b
            sort_segment_lay.addWidget(b)
        sort_row.addWidget(sort_segment)
        sort_row.addStretch(1)
        self._sort_caption = sort_caption
        self._sort_compact = False
        self._sort_full_w = 0
        self._sort_group.buttonToggled.connect(lambda *_: self._apply_sort_labels())
        self.sort_box.setMaximumHeight(0)  # colapsado por defecto
        lay.addWidget(self.sort_box)
        self._advanced_anim = QPropertyAnimation(self.sort_box, b"maximumHeight", self)
        self._advanced_anim.setDuration(220)
        self._advanced_anim.setEasingCurve(QEasingCurve.OutCubic)
        self._update_advanced_button()

        # Lista scrollable
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.list_container = QWidget()
        self.list_container.setMouseTracking(True)
        self.list_layout = QVBoxLayout(self.list_container)
        self.list_layout.setContentsMargins(0, 2, 6, 2)
        self.list_layout.setSpacing(8)
        self.list_layout.addStretch(1)
        self.scroll.setWidget(self.list_container)
        lay.addWidget(self.scroll, 1)

        self.empty_label = QLabel("No hay tareas para mostrar.")
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setProperty("role", "muted")
        self.empty_label.hide()
        # Sustituye a la lista: se queda con el alto sobrante (sin factor, el
        # campo de añadir crecía hasta media columna con la lista vacía)
        lay.addWidget(self.empty_label, 1)

        # Añadir tarea
        self.add_panel = TaskAddPanel()
        self.add_panel.task_submitted.connect(self.task_added.emit)
        lay.addWidget(self.add_panel)

        self._title = title
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _retheme(self) -> None:
        from ui.widgets.icon_button import tinted_icon
        t = tokens()
        self._title.setStyleSheet(f"color: {t.fg};")
        for key, b in self._sort_buttons.items():
            b.setIcon(tinted_icon(SORT_ICONS[key], t.fg_dim, 14))
        self.position_btn.set_colors(t.fg_muted, t.fg)
        self.plan_btn.set_colors(t.fg_muted, t.accent)
        self._update_advanced_button()
        self._fit_sort()

    # ---------- orden compacto ----------
    def _apply_sort_labels(self) -> None:
        """Compacto, solo el orden elegido conserva el texto; el resto queda
        en icono con tooltip."""
        for key, label in SORTS:
            b = self._sort_buttons[key]
            b.setText("" if self._sort_compact and not b.isChecked() else label)
            b.setToolTip(label if self._sort_compact else "")

    def _fit_sort(self) -> None:
        """Con poco ancho (760 px con el panel a un lado) los cinco órdenes no
        caben con su texto y se recortaban («Manu», «Proyec»): se quita
        «Ordenar» y se pasa a iconos."""
        if not self._sort_compact:
            self._sort_full_w = self.sort_box.layout().sizeHint().width()
        m = self.layout().contentsMargins()
        compact = self.width() - m.left() - m.right() < self._sort_full_w
        if compact != self._sort_compact:
            self._sort_compact = compact
            self._sort_caption.setVisible(not compact)
            self._apply_sort_labels()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_sort()

    # ---------- filtros avanzados (orden colapsable) ----------
    def _update_advanced_button(self) -> None:
        """Refresca icono (flecha) y texto del botón; colapsado, muestra el
        orden activo para que no quede invisible."""
        from ui.widgets.icon_button import tinted_icon
        t = tokens()
        self.advanced_btn.setIcon(tinted_icon(
            "chevron-up" if self._advanced_open else "chevron-down", t.fg_dim, 13))
        text = "Orden"
        if not self._advanced_open:
            label = dict(SORTS).get(getattr(self, '_sort_mode', ''), '')
            if label:
                text += " · " + label
        self.advanced_btn.setText(text)

    def _toggle_advanced(self) -> None:
        self.set_advanced_open(not self._advanced_open)
        self.advanced_toggled.emit(self._advanced_open)

    def set_advanced_open(self, open_: bool, animate: bool = True) -> None:
        self._advanced_open = bool(open_)
        target = self.sort_box.sizeHint().height() + 2 if self._advanced_open else 0
        self._advanced_anim.stop()
        if animate:
            self._advanced_anim.setStartValue(self.sort_box.maximumHeight())
            self._advanced_anim.setEndValue(target)
            self._advanced_anim.start()
        else:
            self.sort_box.setMaximumHeight(target)
        self._update_advanced_button()

    # ---------- construcción ----------
    def set_tasks(self, tasks: List[dict], active_id: Optional[int],
                  times: Dict[int, float], filter_key: str, sort_mode: str) -> None:
        # Un refresh (webhook, sync, cambio de día) puede llegar en pleno
        # arrastre: soltar el drag antes de destruir las tarjetas o el panel
        # queda con un _drag_card muerto y el mouse secuestrado
        self._cancel_drag()
        self._manual_mode = (sort_mode == 'manual')
        self._update_pills(filter_key, sort_mode)
        # Preservar scroll
        sb = self.scroll.verticalScrollBar()
        saved_scroll = sb.value()

        for card in self._cards.values():
            card.deleteLater()
        self._cards.clear()
        while self.list_layout.count() > 1:  # deja el stretch final
            item = self.list_layout.takeAt(0)
            if item.widget():
                item.widget().deleteLater()

        for task in tasks:
            card = self._make_card(task, times.get(task['id'], 0),
                                   task['id'] == active_id)
            self.list_layout.insertWidget(self.list_layout.count() - 1, card)
            self._cards[task['id']] = card

        self.empty_label.setText(
            "Aún no hay tareas en el plan de hoy.\nPulsa el calendario para elegirlas."
            if filter_key == 'today' else "No hay tareas para mostrar.")
        self.empty_label.setVisible(not tasks)
        self.scroll.setVisible(bool(tasks))
        sb.setValue(saved_scroll)

    def _make_card(self, task: dict, seconds: float, is_active: bool) -> TaskCard:
        card = TaskCard(task, seconds, is_active, self._manual_mode)
        card.selected.connect(self.task_selected.emit)
        card.toggle_completed.connect(self.task_toggle.emit)
        card.edit_requested.connect(self.task_edit.emit)
        card.note_requested.connect(self.task_note.emit)
        card.delete_requested.connect(self.task_delete.emit)
        card.move_up.connect(self.task_move_up.emit)
        card.move_down.connect(self.task_move_down.emit)
        card.drag_started.connect(self._start_drag)
        return card

    def _update_pills(self, filter_key: str, sort_mode: str) -> None:
        for key, b in self._filter_buttons.items():
            b.setChecked(key == filter_key)
        for key, b in self._sort_buttons.items():
            b.setChecked(key == sort_mode)
        self._sort_mode = sort_mode
        self._update_advanced_button()

    def set_counter(self, pending: int, total: int) -> None:
        self.counter.setText(f"{pending} pendientes de {total}")

    # ---------- actualizaciones in-place ----------
    def set_active_card(self, old_id: Optional[int], new_id: Optional[int]) -> None:
        if old_id is not None and old_id in self._cards:
            self._cards[old_id].set_active(False)
        if new_id is not None and new_id in self._cards:
            self._cards[new_id].set_active(True)

    def update_card_completion(self, task_id: int, completed: bool) -> bool:
        card = self._cards.get(task_id)
        if card is None:
            return False
        card.set_completed(completed)
        return True

    def update_card_effort(self, task_id: int, effort: Optional[int]) -> bool:
        card = self._cards.get(task_id)
        if card is None:
            return False
        card.set_effort(effort)
        return True

    def remove_card(self, task_id: int, on_empty=None) -> bool:
        card = self._cards.pop(task_id, None)
        if card is None:
            return False

        def done():
            self.list_layout.removeWidget(card)
            card.deleteLater()
            if not self._cards:
                self.empty_label.show()
                self.scroll.hide()
                if on_empty:
                    on_empty()
        card.animate_remove(done)
        return True

    def update_card_time(self, task_id: int, seconds: float) -> None:
        card = self._cards.get(task_id)
        if card is not None:
            card.set_time(seconds)

    def scroll_to_task(self, task_id: int) -> None:
        """Trae la tarjeta a la vista (p. ej. una tarea recién añadida que el
        orden por fecha manda al fondo de la lista)."""
        card = self._cards.get(task_id)
        if card is not None:
            from PySide6.QtCore import QTimer
            QTimer.singleShot(0, lambda: self.scroll.ensureWidgetVisible(card))

    def visible_order(self) -> List[int]:
        order = []
        for i in range(self.list_layout.count() - 1):
            w = self.list_layout.itemAt(i).widget()
            if isinstance(w, TaskCard):
                order.append(w.task_id)
        return order

    def move_card(self, task_id: int, direction: int) -> bool:
        """Mueve la tarjeta una posición (repack). direction: -1 arriba, +1 abajo."""
        card = self._cards.get(task_id)
        if card is None:
            return False
        idx = self.list_layout.indexOf(card)
        new_idx = idx + direction
        if new_idx < 0 or new_idx >= self.list_layout.count() - 1:
            return False
        self.list_layout.removeWidget(card)
        self.list_layout.insertWidget(new_idx, card)
        return True

    # ---------- drag & drop (repack en vivo) ----------
    def _cancel_drag(self) -> None:
        if self._drag_card is None:
            return
        self.releaseMouse()
        QGuiApplication.restoreOverrideCursor()
        try:
            self._drag_card.setWindowOpacity(1.0)
        except RuntimeError:
            pass  # la tarjeta ya fue destruida
        self._drag_card = None

    def _start_drag(self, card: TaskCard, _pos) -> None:
        self._drag_card = card
        card.setWindowOpacity(0.7)
        QGuiApplication.setOverrideCursor(Qt.ClosedHandCursor)
        self.grabMouse()

    def mouseMoveEvent(self, event):
        if self._drag_card is None:
            super().mouseMoveEvent(event)
            return
        pos = self.list_container.mapFromGlobal(event.globalPosition().toPoint())
        current = self.list_layout.indexOf(self._drag_card)
        target = current
        for i in range(self.list_layout.count() - 1):
            w = self.list_layout.itemAt(i).widget()
            if not isinstance(w, TaskCard):
                continue
            center_y = w.geometry().center().y()
            if pos.y() < center_y:
                target = i
                break
            target = i
        if target != current:
            self.list_layout.removeWidget(self._drag_card)
            self.list_layout.insertWidget(target, self._drag_card)

    def mouseReleaseEvent(self, event):
        if self._drag_card is not None:
            self.releaseMouse()
            QGuiApplication.restoreOverrideCursor()
            self._drag_card.setWindowOpacity(1.0)
            self._drag_card = None
            self.order_changed.emit(self.visible_order())
            return
        super().mouseReleaseEvent(event)
