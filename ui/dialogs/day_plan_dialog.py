"""Plan de hoy: al empezar el día, elegir en qué tareas se va a trabajar.

Diálogo autocontenido con el sistema de diseño (no depende de
branded_dialogs): tarjeta flotante sin marco, con sombra, sobre un velo que
oscurece la ventana. Arriba «Sugeridas» (urgentes primero, con el motivo en
un chip de color); debajo «Otras pendientes», plegable y con buscador.

Uso: pick_day_plan(parent, tasks, suggestions, preselected_ids) → lista de
ids, o None si se elige «Ahora no». Las sugerencias salen de
core.day_plan.suggest() y la preselección de default_selection() (o del plan
ya guardado hoy).
"""
import unicodedata
from datetime import date, datetime
from typing import Any, Dict, Iterable, List, Optional

from PySide6.QtCore import QEasingCurve, QPropertyAnimation, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (QCheckBox, QDialog, QFrame,
                               QGraphicsDropShadowEffect, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QScrollArea, QSizePolicy,
                               QVBoxLayout, QWidget)

from core import day_plan
from ui.greeting import clean_name, greeting_text, valid_streak
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon

_SHORT_MONTHS = ('ene', 'feb', 'mar', 'abr', 'may', 'jun',
                 'jul', 'ago', 'sep', 'oct', 'nov', 'dic')

# Motivo → (token de color del chip, icono)
_REASON_STYLE = {
    day_plan.OVERDUE: ('danger', 'calendar'),
    day_plan.OVERDUE_STALE: ('danger', 'calendar'),
    day_plan.DUE_TODAY: ('warning', 'calendar'),
    day_plan.DUE_TOMORROW: ('warning', 'calendar'),
    day_plan.DUE_SOON: ('fg_dim', 'calendar'),
    day_plan.RECURRING: ('accent', 'repeat'),
    day_plan.IN_PROGRESS: ('accent', 'timer'),
}

CARD_WIDTH = 520
MAX_SUGGESTED_VISIBLE = 6   # el resto de sugeridas, tras «Ver N más»
SHADOW_MARGIN = 24   # hueco transparente alrededor de la tarjeta para la sombra


def _rgba(color: str, alpha: int) -> str:
    c = QColor(color)
    return f"rgba({c.red()}, {c.green()}, {c.blue()}, {alpha})"


def _normalize(text: str) -> str:
    """Minúsculas y sin tildes: «reunion» encuentra «Reunión»."""
    decomposed = unicodedata.normalize('NFKD', text or '')
    return ''.join(ch for ch in decomposed if not unicodedata.combining(ch)).lower()


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _join_es(parts: List[str]) -> str:
    """'a', 'a y b', 'a, b y c'."""
    if len(parts) <= 1:
        return ''.join(parts)
    return ', '.join(parts[:-1]) + ' y ' + parts[-1]


def summary_text(pending: int, suggestions: Iterable[day_plan.Suggestion]) -> str:
    """Resumen breve bajo el título: cuántas pendientes y cuántas urgen."""
    if pending == 0:
        return "No tienes tareas pendientes. Añade alguna y vuelve a planificar."
    counts = day_plan.summary_counts(suggestions)
    parts = []
    overdue = counts.get(day_plan.OVERDUE, 0) + counts.get(day_plan.OVERDUE_STALE, 0)
    if overdue:
        n = overdue
        parts.append(_plural(n, "vencida", "vencidas"))
    for code, when in ((day_plan.DUE_TODAY, "hoy"), (day_plan.DUE_TOMORROW, "mañana")):
        n = counts.get(code, 0)
        if n:
            parts.append(f"{n} {'vence' if n == 1 else 'vencen'} {when}")
    head = f"Tienes {_plural(pending, 'tarea pendiente', 'tareas pendientes')}"
    if parts:
        head += f": {_join_es(parts)}"
    return head + ". Marca en cuáles trabajarás hoy."


class _Overlay(QWidget):
    """Velo semitransparente sobre la ventana mientras el diálogo está abierto."""

    def __init__(self, window):
        super().__init__(window)
        self.setAttribute(Qt.WA_StyledBackground, True)
        self.setStyleSheet("background: rgba(0, 0, 0, 140);")
        self.setGeometry(window.rect())


class _ElidedLabel(QLabel):
    """Una línea recortada con «…» al ancho disponible (texto completo en el
    tooltip). No impone ancho mínimo a la fila."""

    def __init__(self, text: str = "", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text: str) -> None:  # noqa: N802 (API de Qt)
        self._full = text or ""
        super().setText(self._full)
        self._refresh()

    def full_text(self) -> str:
        return self._full

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh()

    def _refresh(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, max(0, self.width()))
        if shown != super().text():
            super().setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class _ReasonChip(QFrame):
    """Chip con icono y texto del motivo de la sugerencia."""

    def __init__(self, reason_code: str, text: str, tooltip: str = "", parent=None):
        super().__init__(parent)
        self._code = reason_code
        self.setObjectName("dpChip")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(7, 2, 8, 2)
        lay.setSpacing(4)
        self._icon = QLabel(self)
        self._icon.setFixedSize(12, 12)
        lay.addWidget(self._icon)
        self.label = QLabel(text, self)
        self.label.setFont(fonts.font(fonts.XS, weight=QFont.Weight.Medium))
        lay.addWidget(self.label)
        if tooltip:
            self.setToolTip(tooltip)
        self.apply_theme()

    def apply_theme(self) -> None:
        t = tokens()
        color_key, icon = _REASON_STYLE.get(self._code, ('fg_dim', 'calendar'))
        color = getattr(t, color_key)
        self._icon.setPixmap(tinted_icon(icon, color, 12).pixmap(12, 12))
        self.label.setStyleSheet(f"color: {color};")
        self.setStyleSheet(f"#dpChip {{ background: {_rgba(color, 32)};"
                           f" border: none; border-radius: 6px; }}")


class _TaskRow(QFrame):
    """Fila checkable: casilla, nombre recortado y, a la derecha, el chip del
    motivo (sugeridas) o la fecha de vencimiento (otras). Toda la fila es
    clicable; la casilla también responde al espacio con el foco."""
    toggled = Signal()

    def __init__(self, task: Dict[str, Any], suggestion: Optional[day_plan.Suggestion],
                 checked: bool, parent=None):
        super().__init__(parent)
        self.task_id = task.get('id')
        self._task = task
        self._suggestion = suggestion
        self.setObjectName("dpRow")
        self.setAttribute(Qt.WA_Hover, True)
        self.setCursor(Qt.PointingHandCursor)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._search_key = _normalize(task.get('name', ''))

        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 8, 10, 8)
        lay.setSpacing(10)
        self.check = QCheckBox(self)
        self.check.setCursor(Qt.PointingHandCursor)
        self.check.setChecked(checked)
        self.check.setToolTip("Incluir en el plan de hoy")
        self.check.toggled.connect(self._on_toggled)
        lay.addWidget(self.check)

        self.name_label = _ElidedLabel(task.get('name') or 'Sin nombre', self)
        self.name_label.setFont(fonts.font(fonts.SMALL + 0.5, weight=QFont.Weight.Medium))
        lay.addWidget(self.name_label, 1)

        self.chip: Optional[_ReasonChip] = None
        self.meta: Optional[QLabel] = None
        if suggestion is not None:
            self.chip = _ReasonChip(suggestion.reason_code, suggestion.reason_text,
                                    self._chip_tooltip(), self)
            lay.addWidget(self.chip, 0, Qt.AlignVCenter)
        else:
            meta = self._meta_text()
            if meta:
                self.meta = QLabel(meta, self)
                self.meta.setFont(fonts.font(fonts.XS))
                lay.addWidget(self.meta, 0, Qt.AlignVCenter)
        self.apply_theme()
        self._sync_checked_property()

    # ---- textos ----
    def _chip_tooltip(self) -> str:
        code = self._suggestion.reason_code
        due = day_plan.due_local_date(self._task)
        if code in (day_plan.OVERDUE, day_plan.OVERDUE_STALE, day_plan.DUE_TODAY,
                    day_plan.DUE_TOMORROW, day_plan.DUE_SOON) and due:
            verb = "Venció" if code in (day_plan.OVERDUE, day_plan.OVERDUE_STALE) else "Vence"
            return f"{verb} el {due.strftime('%d/%m/%Y')}"
        if code == day_plan.RECURRING:
            return "Tarea fija: vuelve a Pendientes al cumplirse su ciclo"
        if code == day_plan.IN_PROGRESS:
            return "Trabajaste en ella en los últimos días"
        return ""

    def _meta_text(self) -> str:
        if self._task.get('asana_deleted'):
            return "Eliminada en Asana"
        due = day_plan.due_local_date(self._task)
        if due:
            return f"{due.day} {_SHORT_MONTHS[due.month - 1]}"
        return ""

    # ---- estilo ----
    def apply_theme(self) -> None:
        t = tokens()
        self.name_label.setStyleSheet(f"color: {t.fg};")
        if self.chip is not None:
            self.chip.apply_theme()
        if self.meta is not None:
            danger = bool(self._task.get('asana_deleted'))
            self.meta.setStyleSheet(f"color: {t.danger if danger else t.fg_muted};")

    def _sync_checked_property(self) -> None:
        self.setProperty("checked", self.check.isChecked())
        self.style().unpolish(self)
        self.style().polish(self)

    # ---- API ----
    def is_checked(self) -> bool:
        return self.check.isChecked()

    def set_checked(self, checked: bool) -> None:
        self.check.setChecked(checked)

    def matches(self, query: str) -> bool:
        return not query or query in self._search_key

    # ---- eventos ----
    def _on_toggled(self, _checked: bool) -> None:
        self._sync_checked_property()
        self.toggled.emit()

    def mousePressEvent(self, event):
        # Aceptar la pulsación: la fila se queda el clic y alterna al soltar
        if event.button() == Qt.LeftButton:
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton and self.rect().contains(event.position().toPoint()):
            self.check.toggle()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class DayPlanDialog(QDialog):
    """«Planifica tu día». exec() devuelve QDialog.Accepted («Empezar el día»,
    Enter) o Rejected («Ahora no», Escape); selected_ids() da los ids marcados
    en el orden en que se muestran (sugeridas primero). Con `user_name`, el
    título es el saludo («Buenos días, Fabricio») y la fecha va tras
    «Planifica tu día»; con `streak` (actual, mejor), el contador de la racha
    de uso a la derecha de la cabecera."""

    def __init__(self, parent, tasks: Iterable[Dict[str, Any]],
                 suggestions: Iterable[day_plan.Suggestion],
                 preselected_ids: Iterable[Any], today: Optional[date] = None,
                 user_name: str = "", streak=None):
        super().__init__(parent)
        self.setWindowFlags(Qt.Dialog | Qt.FramelessWindowHint)
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setModal(True)
        self.setObjectName("dayPlanDialog")
        self.setWindowTitle("Planifica tu día")
        self._overlay: Optional[_Overlay] = None
        self._today = today or date.today()

        pending = [t for t in (tasks or []) if isinstance(t, dict)
                   and not t.get('completed') and t.get('id') is not None]
        by_id = {t['id']: t for t in pending}
        preselected = set(preselected_ids or [])
        self._suggestions = [s for s in (suggestions or []) if s.task_id in by_id]
        suggested_ids = {s.task_id for s in self._suggestions}
        # Otras: lo que vence antes, arriba; sin fecha, al final (orden estable)
        others = [t for t in pending if t['id'] not in suggested_ids]
        others.sort(key=lambda t: (day_plan.due_local_date(t) or date.max))

        outer = QVBoxLayout(self)
        outer.setContentsMargins(SHADOW_MARGIN, SHADOW_MARGIN - 8,
                                 SHADOW_MARGIN, SHADOW_MARGIN + 8)
        self.card = QFrame(self)
        self.card.setObjectName("dayPlanCard")
        self.card.setFixedWidth(CARD_WIDTH)
        outer.addWidget(self.card)
        # Sombra en UN solo contenedor (la tarjeta), nunca en cada fila
        self._shadow = QGraphicsDropShadowEffect(self.card)
        self._shadow.setBlurRadius(44)
        self._shadow.setOffset(0, 10)
        self._shadow.setColor(QColor(0, 0, 0, 150))
        self.card.setGraphicsEffect(self._shadow)

        lay = QVBoxLayout(self.card)
        lay.setContentsMargins(24, 22, 24, 20)
        lay.setSpacing(0)

        # --- cabecera: icono + título + fecha ---
        head = QHBoxLayout()
        head.setSpacing(12)
        self._badge = QLabel(self.card)
        self._badge.setFixedSize(40, 40)
        self._badge.setAlignment(Qt.AlignCenter)
        head.addWidget(self._badge, 0, Qt.AlignTop)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._title = QLabel("Planifica tu día", self.card)
        self._title.setFont(fonts.display(fonts.MEDIUM, tabular=False))
        titles.addWidget(self._title)
        long_date = day_plan.format_long_date(self._today)
        self._date_label = QLabel(long_date[:1].upper() + long_date[1:], self.card)
        self._date_label.setFont(fonts.font(fonts.SMALL))
        titles.addWidget(self._date_label)
        head.addLayout(titles, 1)
        self.streak_badge = self._personalize(head, user_name, streak)
        lay.addLayout(head)
        lay.addSpacing(14)

        self._summary = QLabel(summary_text(len(pending), self._suggestions), self.card)
        self._summary.setWordWrap(True)
        self._summary.setFont(fonts.font(fonts.SMALL))
        lay.addWidget(self._summary)
        lay.addSpacing(16)

        # --- lista con scroll: sugeridas + otras pendientes ---
        self.scroll = QScrollArea(self.card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self._content = QWidget(self.scroll)
        self._content.setObjectName("dpContent")
        body = QVBoxLayout(self._content)
        body.setContentsMargins(0, 0, 0, 0)
        body.setSpacing(6)

        # Sugeridas
        sug_head = QHBoxLayout()
        sug_head.setContentsMargins(0, 0, 0, 2)
        sug_head.setSpacing(6)
        self._sug_title = QLabel("Sugeridas", self._content)
        self._sug_title.setFont(fonts.font(fonts.SMALL, bold=True))
        sug_head.addWidget(self._sug_title)
        self._sug_count = QLabel(str(len(self._suggestions)), self._content)
        self._sug_count.setFont(fonts.font(fonts.XS))
        sug_head.addWidget(self._sug_count)
        sug_head.addStretch(1)
        self.toggle_all_btn = QPushButton(self._content)
        self.toggle_all_btn.setProperty("variant", "ghost")
        self.toggle_all_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_all_btn.setAutoDefault(False)
        self.toggle_all_btn.setFont(fonts.font(fonts.XS))
        self.toggle_all_btn.setStyleSheet("padding: 2px 8px;")
        self.toggle_all_btn.clicked.connect(self._toggle_all_suggested)
        sug_head.addWidget(self.toggle_all_btn)
        body.addLayout(sug_head)
        self.toggle_all_btn.setVisible(bool(self._suggestions))
        self._sug_count.setVisible(bool(self._suggestions))

        self.suggested_rows: List[_TaskRow] = []
        for s in self._suggestions:
            row = _TaskRow(by_id[s.task_id], s, s.task_id in preselected, self._content)
            row.toggled.connect(self._on_selection_changed)
            body.addWidget(row)
            self.suggested_rows.append(row)
        # Con muchas sugerencias, solo las primeras: así «Otras pendientes»
        # queda a la vista sin tener que desplazarse
        self._sug_expanded = any(r.is_checked() for r in
                                 self.suggested_rows[MAX_SUGGESTED_VISIBLE:])
        self.more_btn = QPushButton(self._content)
        self.more_btn.setObjectName("dpMore")
        self.more_btn.setCursor(Qt.PointingHandCursor)
        self.more_btn.setAutoDefault(False)
        self.more_btn.setFont(fonts.font(fonts.XS, weight=QFont.Weight.Medium))
        self.more_btn.setIconSize(QSize(13, 13))
        self.more_btn.clicked.connect(lambda: self.set_suggestions_expanded(not self._sug_expanded))
        body.addWidget(self.more_btn, 0, Qt.AlignLeft)
        self._no_sug = QLabel("Nada urgente por hoy: elige entre tus pendientes.",
                              self._content)
        self._no_sug.setFont(fonts.font(fonts.SMALL))
        body.addWidget(self._no_sug)
        self._no_sug.setVisible(not self._suggestions)

        # Otras pendientes (plegable, con buscador)
        body.addSpacing(12)
        other_head = QHBoxLayout()
        other_head.setSpacing(6)
        self.others_btn = QPushButton("Otras pendientes", self._content)
        self.others_btn.setObjectName("dpSectionToggle")
        self.others_btn.setCursor(Qt.PointingHandCursor)
        self.others_btn.setAutoDefault(False)
        self.others_btn.setFont(fonts.font(fonts.SMALL, bold=True))
        self.others_btn.setIconSize(QSize(14, 14))
        self.others_btn.clicked.connect(lambda: self.set_others_expanded(not self._others_open))
        other_head.addWidget(self.others_btn)
        self._others_count = QLabel(str(len(others)), self._content)
        self._others_count.setFont(fonts.font(fonts.XS))
        other_head.addWidget(self._others_count)
        other_head.addStretch(1)
        body.addLayout(other_head)

        self._others_box = QWidget(self._content)
        others_lay = QVBoxLayout(self._others_box)
        others_lay.setContentsMargins(0, 4, 0, 0)
        others_lay.setSpacing(6)
        self.search = QLineEdit(self._others_box)
        self.search.setPlaceholderText("Buscar tarea…")
        self.search.setClearButtonEnabled(True)
        self.search.setFont(fonts.font(fonts.SMALL))
        self._search_action = self.search.addAction(
            tinted_icon("search", tokens().fg_muted, 14), QLineEdit.LeadingPosition)
        self.search.textChanged.connect(self._apply_search)
        others_lay.addWidget(self.search)
        self.other_rows: List[_TaskRow] = []
        for task in others:
            row = _TaskRow(task, None, task['id'] in preselected, self._others_box)
            row.toggled.connect(self._on_selection_changed)
            others_lay.addWidget(row)
            self.other_rows.append(row)
        self._no_results = QLabel("Sin resultados.", self._others_box)
        self._no_results.setFont(fonts.font(fonts.XS))
        self._no_results.setAlignment(Qt.AlignCenter)
        others_lay.addWidget(self._no_results)
        self._no_results.setVisible(False)
        self.search.setVisible(len(others) > 4)
        body.addWidget(self._others_box)
        body.addStretch(1)
        self.scroll.setWidget(self._content)
        lay.addWidget(self.scroll, 1)
        other_head_visible = bool(others)
        self.others_btn.setVisible(other_head_visible)
        self._others_count.setVisible(other_head_visible)

        # --- pie: contador + botones ---
        lay.addSpacing(16)
        self._separator = QFrame(self.card)
        self._separator.setFixedHeight(1)
        lay.addWidget(self._separator)
        lay.addSpacing(16)
        foot = QHBoxLayout()
        foot.setSpacing(8)
        self.count_label = QLabel(self.card)
        self.count_label.setFont(fonts.font(fonts.SMALL))
        self.count_label.setTextFormat(Qt.RichText)
        foot.addWidget(self.count_label)
        foot.addStretch(1)
        self.later_btn = QPushButton("Ahora no", self.card)
        self.later_btn.setProperty("variant", "ghost")
        self.later_btn.setCursor(Qt.PointingHandCursor)
        self.later_btn.setAutoDefault(False)
        self.later_btn.clicked.connect(self.reject)
        foot.addWidget(self.later_btn)
        self.start_btn = QPushButton("Empezar el día", self.card)
        self.start_btn.setProperty("variant", "accent")
        self.start_btn.setCursor(Qt.PointingHandCursor)
        self.start_btn.setAutoDefault(False)
        self.start_btn.setDefault(True)
        self.start_btn.setIconSize(QSize(14, 14))
        self.start_btn.clicked.connect(self.accept)
        foot.addWidget(self.start_btn)
        lay.addLayout(foot)

        # Plegada salvo que no haya sugeridas o que traiga algo ya marcado
        self._others_open = False
        self._apply_suggestion_limit()
        self.set_others_expanded(not self._suggestions
                                 or any(r.is_checked() for r in self.other_rows))
        self._retheme()
        self._on_selection_changed()
        self.start_btn.setFocus()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _personalize(self, head: QHBoxLayout, user_name: str, streak):
        """Saludo con nombre en el título y la racha de uso en la cabecera.
        Devuelve el contador (o None si no hay racha que mostrar)."""
        if clean_name(user_name):
            self._title.setTextFormat(Qt.RichText)
            self._title.setWordWrap(True)  # tarjeta de ancho fijo
            self._title.setText(greeting_text(datetime.now(), user_name, tokens().accent))
            self._date_label.setText(f"Planifica tu día · {self._date_label.text()}")
        streak = valid_streak(streak)
        if streak is None:
            return None
        from ui.widgets.streak_badge import StreakBadge
        badge = StreakBadge(*streak, parent=self.card)
        head.addWidget(badge, 0, Qt.AlignTop)
        return badge

    # ---------- estilo ----------
    def _retheme(self) -> None:
        t = tokens()
        self.setStyleSheet(f"""
            QDialog#dayPlanDialog {{ background: transparent; }}
            #dayPlanCard {{
                background: {t.bg};
                border: 1px solid {t.border};
                border-radius: {t.radius}px;
            }}
            #dpContent {{ background: transparent; }}
            QFrame#dpRow {{
                background: {t.surface};
                border: 1px solid {t.border};
                border-radius: {t.radius_small + 2}px;
            }}
            QFrame#dpRow:hover {{ background: {t.surface_hover}; }}
            QFrame#dpRow[checked="true"] {{ border-color: {_rgba(t.accent, 120)}; }}
            QPushButton#dpSectionToggle {{
                background: transparent;
                border: none;
                padding: 2px 4px 2px 0px;
                color: {t.fg};
                text-align: left;
            }}
            QPushButton#dpSectionToggle:hover {{ color: {t.accent}; }}
            QPushButton#dpMore {{
                background: transparent;
                border: none;
                padding: 2px 6px 2px 12px;
                color: {t.fg_dim};
                text-align: left;
            }}
            QPushButton#dpMore:hover {{ color: {t.fg}; }}
            QCheckBox:focus::indicator {{ border-color: {t.accent}; }}
        """)
        self._badge.setPixmap(tinted_icon("calendar-check", t.accent, 22).pixmap(22, 22))
        self._badge.setStyleSheet(f"background: {_rgba(t.accent, 34)};"
                                  f" border-radius: {t.radius_small + 2}px;")
        self._title.setStyleSheet(f"color: {t.fg};")
        self._date_label.setStyleSheet(f"color: {t.fg_dim};")
        self._summary.setStyleSheet(f"color: {t.fg_dim};")
        self._sug_title.setStyleSheet(f"color: {t.fg};")
        for lbl in (self._sug_count, self._others_count, self._no_results):
            lbl.setStyleSheet(f"color: {t.fg_muted};")
        self._no_sug.setStyleSheet(f"color: {t.fg_dim};")
        self._separator.setStyleSheet(f"background: {t.border};")
        self._search_action.setIcon(tinted_icon("search", t.fg_muted, 14))
        self.start_btn.setIcon(tinted_icon("play", t.on_accent, 14))
        self._update_others_icon()
        self._update_more_btn()
        for row in self.suggested_rows + self.other_rows:
            row.apply_theme()
        self._update_count()

    def _update_others_icon(self) -> None:
        self.others_btn.setIcon(tinted_icon(
            "chevron-down" if self._others_open else "chevron-right", tokens().fg_dim, 14))

    def _update_more_btn(self) -> None:
        extra = len(self.suggested_rows) - MAX_SUGGESTED_VISIBLE
        self.more_btn.setText("Ver menos" if self._sug_expanded else f"Ver {extra} más")
        self.more_btn.setIcon(tinted_icon(
            "chevron-up" if self._sug_expanded else "chevron-down", tokens().fg_dim, 13))

    # ---------- comportamiento ----------
    def set_others_expanded(self, expanded: bool) -> None:
        self._others_open = bool(expanded) and bool(self.other_rows)
        self._others_box.setVisible(self._others_open)
        self._update_others_icon()
        self._fit_height()
        if self._others_open and self.isVisible():
            # La sección queda bajo las sugeridas: subirla a la vista
            QTimer.singleShot(0, self._scroll_to_others)
            if self.search.isVisible():
                self.search.setFocus()

    def _scroll_to_others(self) -> None:
        self.scroll.verticalScrollBar().setValue(max(0, self.others_btn.y() - 4))

    def others_expanded(self) -> bool:
        return self._others_open

    def set_suggestions_expanded(self, expanded: bool) -> None:
        self._sug_expanded = bool(expanded)
        self._apply_suggestion_limit()
        self._fit_height()

    def _apply_suggestion_limit(self) -> None:
        for i, row in enumerate(self.suggested_rows):
            row.setVisible(self._sug_expanded or i < MAX_SUGGESTED_VISIBLE)
        self.more_btn.setVisible(len(self.suggested_rows) > MAX_SUGGESTED_VISIBLE)
        self._update_more_btn()

    def _apply_search(self, text: str) -> None:
        query = _normalize(text.strip())
        shown = 0
        for row in self.other_rows:
            visible = row.matches(query)
            row.setVisible(visible)
            shown += visible
        self._no_results.setVisible(bool(self.other_rows) and shown == 0)

    def _toggle_all_suggested(self) -> None:
        target = not all(r.is_checked() for r in self.suggested_rows)
        for row in self.suggested_rows:
            row.set_checked(target)
        # Que no se marquen tareas que no se ven
        if target and not self._sug_expanded and len(self.suggested_rows) > MAX_SUGGESTED_VISIBLE:
            self.set_suggestions_expanded(True)

    def _on_selection_changed(self) -> None:
        self._update_count()
        all_on = bool(self.suggested_rows) and all(r.is_checked() for r in self.suggested_rows)
        self.toggle_all_btn.setText("Quitar todas" if all_on else "Elegir todas")

    def _update_count(self) -> None:
        t = tokens()
        n = len(self.selected_ids())
        if n == 0:
            text = "Ninguna tarea para hoy"
        else:
            text = (f"<span style='color:{t.fg}; font-weight:600'>{n}</span> "
                    f"{'tarea' if n == 1 else 'tareas'} para hoy")
        self.count_label.setText(text)
        self.count_label.setStyleSheet(f"color: {t.fg_dim};")

    def _fit_height(self) -> None:
        """Alto de la lista = su contenido, con tope para que el diálogo quepa
        en la ventana (o en la pantalla). Al buscar no se recalcula: el
        diálogo no da saltos mientras se escribe."""
        self._content.adjustSize()
        natural = self._content.sizeHint().height()
        parent = self.parentWidget()
        if parent is not None and parent.window().isVisible():
            room = parent.window().height()
        else:
            screen = self.screen()
            room = screen.availableGeometry().height() if screen else 800
        # Cabecera + resumen + pie + márgenes de la tarjeta y de la sombra
        chrome = 260 + 2 * SHADOW_MARGIN
        cap = max(160, min(room - 32, 760) - chrome)
        self.scroll.setFixedHeight(min(natural, cap))
        self.adjustSize()

    def selected_ids(self) -> List[Any]:
        """Ids marcados, sugeridas primero y luego las otras, en su orden."""
        return [r.task_id for r in self.suggested_rows + self.other_rows if r.is_checked()]

    # ---------- ventana ----------
    def showEvent(self, event):
        super().showEvent(event)
        parent = self.parentWidget()
        window = parent.window() if parent is not None else None
        if window is not None and window.isVisible() and self._overlay is None:
            self._overlay = _Overlay(window)
            self._overlay.show()
            self._overlay.raise_()

    def hideEvent(self, event):
        self._drop_overlay()
        super().hideEvent(event)

    def _drop_overlay(self) -> None:
        if self._overlay is not None:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None

    def exec(self) -> int:
        self._fit_height()
        parent = self.parentWidget()
        if parent is not None and parent.window().isVisible():
            geo = parent.window().frameGeometry()
        else:
            screen = self.screen()
            geo = screen.availableGeometry() if screen else None
        if geo is not None:
            self.move(geo.center().x() - self.width() // 2,
                      geo.center().y() - self.height() // 2)
        self.setWindowOpacity(0.0)
        anim = QPropertyAnimation(self, b"windowOpacity", self)
        anim.setDuration(180)
        anim.setStartValue(0.0)
        anim.setEndValue(1.0)
        anim.setEasingCurve(QEasingCurve.OutCubic)
        anim.start(QPropertyAnimation.DeleteWhenStopped)
        try:
            return super().exec()
        finally:
            self._drop_overlay()

    def keyPressEvent(self, event):
        # Enter confirma desde cualquier sitio (casillas, buscador); Escape
        # equivale a «Ahora no». Los botones no son autoDefault, así que un
        # Enter con el foco en «Ahora no» también llega aquí
        if event.key() in (Qt.Key_Return, Qt.Key_Enter):
            self.accept()
            return
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


def pick_day_plan(parent, tasks: Iterable[Dict[str, Any]],
                  suggestions: Iterable[day_plan.Suggestion],
                  preselected_ids: Iterable[Any], user_name: str = "",
                  streak=None) -> Optional[List[Any]]:
    """Abre «Planifica tu día» y devuelve los ids elegidos (puede ser una
    lista vacía) o None si el usuario eligió «Ahora no» / Escape."""
    dlg = DayPlanDialog(parent, tasks, suggestions, preselected_ids,
                        user_name=user_name, streak=streak)
    try:
        if dlg.exec() != QDialog.Accepted:
            return None
        return dlg.selected_ids()
    finally:
        dlg.deleteLater()
