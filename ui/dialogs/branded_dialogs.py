"""Diálogos con la identidad de PomoGrowth: frameless, tarjeta flotante con
sombra suave, velo tenue sobre la ventana y entrada con fade + leve subida.
Reemplazo de branded_dialogs/modern_modal de v8."""
from typing import List, Optional, Tuple

from PySide6.QtCore import (QEasingCurve, QParallelAnimationGroup, QPoint,
                            QPropertyAnimation, Qt, Signal)
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QDialog, QFrame, QGraphicsDropShadowEffect,
                               QGridLayout, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QVBoxLayout, QWidget)

from ui.theme import brand, fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon

# Margen transparente alrededor de la tarjeta donde cae la sombra (la sombra
# baja unos px: más hueco abajo que arriba para que no se corte)
SHADOW_MARGINS = (28, 20, 28, 36)


def soft_shadow(widget: QWidget, blur: int = 40, dy: int = 8,
                alpha: int = 110) -> QGraphicsDropShadowEffect:
    """Sombra suave para UN contenedor flotante (diálogo, toast, splash)."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(blur)
    effect.setOffset(0, dy)
    effect.setColor(QColor(0, 0, 0, alpha))
    widget.setGraphicsEffect(effect)
    return effect


def style_icon_chip(chip: QLabel, icon: str, color: Optional[str] = None) -> None:
    """(Re)pinta una ficha de icono: con `color`, icono de ese color sobre el
    mismo color al 15 %; sin él, neutra (icono atenuado sobre superficie).
    Con `brand.MASCOT_ICON`, la mascota de ese color (con su cara y ojos)."""
    t = tokens()
    if color:
        c = QColor(color)
        bg = f"rgba({c.red()}, {c.green()}, {c.blue()}, 38)"
        fg = color
    else:
        bg, fg = t.surface_active, t.fg_dim
    size = chip.property("iconSize") or 18
    if icon == brand.MASCOT_ICON:
        chip.setPixmap(brand.mascot_pixmap(fg, size))
    else:
        chip.setPixmap(tinted_icon(icon, fg, size).pixmap(size, size))
    chip.setStyleSheet(f"background: {bg}; border: none;"
                       f" border-radius: {t.radius_small}px;")


def icon_chip(icon: str, color: Optional[str] = None, size: int = 32,
              icon_size: int = 18, parent: Optional[QWidget] = None) -> QLabel:
    """Icono dentro de una ficha cuadrada de esquinas redondeadas."""
    chip = QLabel(parent)
    chip.setFixedSize(size, size)
    chip.setAlignment(Qt.AlignCenter)
    chip.setProperty("iconSize", icon_size)
    style_icon_chip(chip, icon, color)
    return chip


class _Overlay(QWidget):
    """Velo muy tenue sobre la ventana (o sobre la tarjeta del diálogo padre)
    mientras el diálogo está abierto: separa planos sin apagar la app."""
    clicked = Signal()
    ALPHA = 46  # ≈ 18 % de negro

    def __init__(self, host: QWidget, radius: int = 0):
        super().__init__(host)
        self._radius = radius
        self.setGeometry(host.rect())
        self.show()
        self.raise_()

    @classmethod
    def over(cls, window: Optional[QWidget]) -> Optional['_Overlay']:
        if window is None:
            return None
        card = getattr(window, "_card", None)
        if isinstance(card, QWidget):  # diálogo sobre diálogo: solo su tarjeta
            return cls(card, tokens().radius)
        if window.testAttribute(Qt.WA_TranslucentBackground):
            return None  # ventana con márgenes transparentes (p. ej. compacta)
        return cls(window)

    def mousePressEvent(self, event):
        self.clicked.emit()
        super().mousePressEvent(event)

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0, 0, 0, self.ALPHA))
        p.drawRoundedRect(self.rect(), self._radius, self._radius)
        p.end()


class BrandedDialog(QDialog):
    """Base de los diálogos: tarjeta (radio 12, borde de 1 px, sombra) con
    cabecera de icono opcional + título y 24 px de padding. Los hijos añaden
    su contenido a `self._lay` y cierran con `add_buttons`."""

    def __init__(self, parent_window, title: str, accent: Optional[str] = None,
                 icon: Optional[str] = None, subtitle: Optional[str] = None):
        super().__init__(parent_window)
        t = tokens()
        # `accent` tiñe la ficha del icono (danger, info…); sin él, neutra
        self._accent_override = accent
        self._accent = accent or t.accent
        self._icon_name = icon
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Dialog)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setModal(True)
        self._overlay = _Overlay.over(parent_window)

        self.setObjectName("brandedDialog")
        self.setStyleSheet("#brandedDialog { background: transparent; }")
        outer = QGridLayout(self)
        outer.setContentsMargins(*SHADOW_MARGINS)
        # La sombra va en un marco hermano vacío, detrás de la tarjeta: así el
        # efecto no re-renderiza todo el contenido en cada repintado
        self._shadow = QFrame(self)
        self._shadow.setObjectName("dialogShadow")
        soft_shadow(self._shadow)
        outer.addWidget(self._shadow, 0, 0)
        self._card = QFrame(self)
        self._card.setObjectName("dialogCard")
        self._card.setMinimumWidth(400)
        outer.addWidget(self._card, 0, 0)

        self._lay = QVBoxLayout(self._card)
        self._lay.setContentsMargins(24, 24, 24, 24)
        self._lay.setSpacing(12)

        # Cabecera: ficha de icono (opcional) + título display (+ subtítulo)
        self._header = QHBoxLayout()
        self._header.setSpacing(12)
        self._icon_chip = None
        if icon:
            self._icon_chip = icon_chip(icon, accent, parent=self._card)
            self._header.addWidget(self._icon_chip, 0, Qt.AlignVCenter)
        titles = QVBoxLayout()
        titles.setSpacing(0)
        self._title_label = QLabel(title, self._card)
        self._title_label.setFont(fonts.display(fonts.MEDIUM, tabular=False))
        titles.addWidget(self._title_label)
        self._subtitle_label = None
        if subtitle:
            self._subtitle_label = QLabel(subtitle, self._card)
            self._subtitle_label.setFont(fonts.font(fonts.XS))
            self._subtitle_label.setProperty("role", "muted")
            titles.addWidget(self._subtitle_label)
        self._header.addLayout(titles, 1)
        self._lay.addLayout(self._header)
        self._lay.addSpacing(4)  # título → contenido: 16 px
        self._buttons_row = None  # fila de add_buttons

        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _retheme(self) -> None:
        t = tokens()
        self._accent = self._accent_override or t.accent
        surface = (f"background: {t.surface}; border: 1px solid {t.border};"
                   f" border-radius: {t.radius}px;")
        self._shadow.setStyleSheet(f"#dialogShadow {{ {surface} }}")
        self._card.setStyleSheet(f"#dialogCard {{ {surface} }}")
        if self._icon_chip is not None:
            style_icon_chip(self._icon_chip, self._icon_name, self._accent_override)

    def add_message(self, message: str) -> QLabel:
        label = QLabel(message, self._card)
        label.setWordWrap(True)
        label.setMaximumWidth(440)
        label.setFont(fonts.font(fonts.SMALL))
        label.setProperty("role", "dim")
        self._lay.addWidget(label)
        return label

    def add_buttons(self, buttons: List[Tuple[str, str, int]],
                    leading: Optional[QWidget] = None) -> List[QPushButton]:
        """buttons: lista de (texto, variant, result_code), de secundaria a
        primaria; quedan alineados a la derecha. `leading`: un widget en la
        misma fila, a la izquierda (p. ej. un enlace discreto), centrado en
        vertical con los botones. La fila queda en `self._buttons_row`."""
        row = QHBoxLayout()
        row.setSpacing(8)
        if leading is not None:
            row.addWidget(leading, 0, Qt.AlignVCenter)
        row.addStretch(1)
        created = []
        for text, variant, code in buttons:
            b = QPushButton(text, self._card)
            b.setProperty("variant", variant)
            b.setCursor(Qt.PointingHandCursor)
            b.setMinimumWidth(88)
            b.clicked.connect(lambda _=False, c=code: self.done(c))
            row.addWidget(b)
            created.append(b)
        self._lay.addSpacing(8)
        self._lay.addLayout(row)
        self._buttons_row = row
        return created

    def _animate_in(self) -> None:
        """Centra el diálogo sobre su ventana y lo hace entrar (fade + leve
        subida). Lo usan exec() (modal) y popup() (no modal)."""
        self.adjustSize()
        target = self.pos()
        parent = self.parentWidget()
        if parent:
            screen = parent.screen()
            area = screen.availableGeometry() if screen else None
            if area is not None and self.height() > area.height():
                self.resize(self.width(), area.height())  # nunca más alto que la pantalla
            geo = parent.window().geometry()
            target = QPoint(geo.center().x() - self.width() // 2,
                            geo.center().y() - self.height() // 2)
            if area is not None:  # que no se salga de la pantalla (p. ej. sobre la compacta)
                target.setX(max(area.left(), min(target.x(), area.right() - self.width())))
                target.setY(max(area.top(), min(target.y(), area.bottom() - self.height())))
        self.move(target + QPoint(0, 8))
        self.setWindowOpacity(0.0)
        group = QParallelAnimationGroup(self)
        fade = QPropertyAnimation(self, b"windowOpacity", self)
        fade.setDuration(180)
        fade.setStartValue(0.0)
        fade.setEndValue(1.0)
        fade.setEasingCurve(QEasingCurve.OutCubic)
        rise = QPropertyAnimation(self, b"pos", self)
        rise.setDuration(220)
        rise.setStartValue(target + QPoint(0, 8))
        rise.setEndValue(target)
        rise.setEasingCurve(QEasingCurve.OutCubic)
        group.addAnimation(fade)
        group.addAnimation(rise)
        group.start(QParallelAnimationGroup.DeleteWhenStopped)

    def exec(self) -> int:
        self._animate_in()
        result = super().exec()
        self._remove_overlay()
        return result

    def popup(self) -> None:
        """Muestra el diálogo SIN bloquear (sin exec()): el resultado llega
        por la señal finished(int). El velo se retira en done()."""
        self.setModal(False)
        self._animate_in()
        self.show()
        self.raise_()
        self.activateWindow()

    def done(self, result: int) -> None:
        # Aquí y no solo tras exec(): un diálogo abierto con popup() no
        # vuelve de exec() y el velo quedaba colgado sobre la ventana
        self._remove_overlay()
        super().done(result)

    def _remove_overlay(self) -> None:
        if self._overlay:
            self._overlay.hide()
            self._overlay.deleteLater()
            self._overlay = None

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.reject()
            return
        super().keyPressEvent(event)


def confirm(parent_window, title: str, message: str,
            ok_text: str = "Confirmar", danger: bool = False,
            icon: Optional[str] = None) -> bool:
    t = tokens()
    dlg = BrandedDialog(parent_window, title,
                        accent=t.danger if danger else None,
                        icon=icon or ("triangle-alert" if danger else "circle-help"))
    dlg.add_message(message)
    _cancel, ok = dlg.add_buttons([("Cancelar", "ghost", 0),
                                   (ok_text, "danger" if danger else "accent", 1)])
    if not danger:  # Enter confirma; en las destructivas solo el clic
        ok.setDefault(True)
    result = dlg.exec() == 1
    dlg.deleteLater()
    return result


def info(parent_window, title: str, message: str, icon: str = "info") -> None:
    dlg = BrandedDialog(parent_window, title, accent=tokens().info, icon=icon)
    dlg.add_message(message)
    dlg.add_buttons([("Entendido", "accent", 1)])[0].setDefault(True)
    dlg.exec()
    dlg.deleteLater()


def _field(dlg: BrandedDialog, label_text: str, widget: QWidget,
           full_width: bool = True) -> None:
    """Campo de formulario: etiqueta atenuada encima del control."""
    box = QVBoxLayout()
    box.setSpacing(6)
    label = QLabel(label_text, dlg._card)
    label.setFont(fonts.font(fonts.SMALL))
    label.setProperty("role", "dim")
    box.addWidget(label)
    box.addWidget(widget, 0, Qt.Alignment() if full_width else Qt.AlignLeft)
    dlg._lay.addLayout(box)


def edit_task(parent_window, name: str, tags: List[str],
              recurring: Optional[str] = None, hint: Optional[str] = None
              ) -> Optional[Tuple[str, List[str], Optional[str]]]:
    """Diálogo de edición de tarea. Devuelve (nombre, tags, ciclo) o None si
    se cancela. `recurring`: None oculta el selector de repetición (tareas de
    Asana); '' o un ciclo ('daily'/'weekly'/'monthly'/'quarterly') lo muestra
    con esa selección. En el retorno, ciclo es None si el selector no se
    mostró y '' si se eligió «No repetir». `hint`: nota breve bajo los campos."""
    dlg = BrandedDialog(parent_window, "Editar tarea", icon="edit")
    name_edit = QLineEdit(name, dlg._card)
    name_edit.setMinimumWidth(380)
    _field(dlg, "Nombre", name_edit)
    tags_edit = QLineEdit(", ".join(tags), dlg._card)
    tags_edit.setPlaceholderText("Separadas por coma")
    _field(dlg, "Etiquetas", tags_edit)
    recurring_combo = None
    if recurring is not None:
        from PySide6.QtWidgets import QComboBox
        from ui.widgets.task_add_panel import RECURRING_OPTIONS
        recurring_combo = QComboBox(dlg._card)
        for text, cycle in RECURRING_OPTIONS:
            recurring_combo.addItem(text, cycle)
        idx = recurring_combo.findData(recurring)
        recurring_combo.setCurrentIndex(idx if idx >= 0 else 0)
        recurring_combo.setMinimumWidth(200)
        _field(dlg, "Repetición", recurring_combo, full_width=False)
    if hint:
        t = tokens()
        hint_row = QHBoxLayout()
        hint_row.setSpacing(8)
        hint_icon = QLabel(dlg._card)
        hint_icon.setPixmap(tinted_icon("info", t.fg_muted, 14).pixmap(14, 14))
        hint_row.addWidget(hint_icon, 0, Qt.AlignTop)
        hint_label = QLabel(hint, dlg._card)
        hint_label.setWordWrap(True)
        hint_label.setFont(fonts.font(fonts.XS))
        hint_label.setProperty("role", "muted")
        hint_row.addWidget(hint_label, 1)
        dlg._lay.addSpacing(2)
        dlg._lay.addLayout(hint_row)
    dlg.add_buttons([("Cancelar", "ghost", 0), ("Guardar", "accent", 1)])
    name_edit.returnPressed.connect(lambda: dlg.done(1))
    tags_edit.returnPressed.connect(lambda: dlg.done(1))
    name_edit.setFocus()
    accepted = dlg.exec() == 1
    new_name = name_edit.text().strip()
    new_tags = [x.strip().lower() for x in tags_edit.text().split(',') if x.strip()]
    new_cycle = (recurring_combo.currentData() or '') if recurring_combo else None
    dlg.deleteLater()
    if not accepted or not new_name:
        return None
    return new_name, new_tags, new_cycle


def note_task(parent_window, task_name: str, current_note: str) -> Optional[str]:
    """Nota rápida de una tarea. Devuelve la nota o None si se cancela."""
    from PySide6.QtWidgets import QPlainTextEdit
    shown = task_name if len(task_name) <= 60 else task_name[:59].rstrip() + "…"
    dlg = BrandedDialog(parent_window, "Nota", icon="note", subtitle=shown)
    editor = QPlainTextEdit(current_note, dlg._card)
    editor.setPlaceholderText("Apuntes, enlaces o el siguiente paso…")
    editor.setMinimumSize(400, 140)
    dlg._lay.addWidget(editor)
    dlg.add_buttons([("Cancelar", "ghost", 0), ("Guardar", "accent", 1)])
    editor.setFocus()
    accepted = dlg.exec() == 1
    note = editor.toPlainText().strip()
    dlg.deleteLater()
    return note if accepted else None


def ask_user_name(parent_window, current: str = "") -> str:
    """«¿Cómo te llamas?» del primer arranque: un campo, «Omitir» y
    «Guardar» (Enter guarda; vacío no guarda). Devuelve el nombre limpio o
    '' si se omite (Escape también omite)."""
    from ui.greeting import NAME_MAX_LEN, clean_name
    dlg = BrandedDialog(parent_window, "¿Cómo te llamas?", icon="user-round")
    dlg.add_message("Lo usaremos para saludarte al empezar el día."
                    " Puedes cambiarlo en Configuración.")
    name_edit = QLineEdit(current, dlg._card)
    name_edit.setPlaceholderText("Tu nombre")
    name_edit.setMaxLength(NAME_MAX_LEN)
    name_edit.setMinimumWidth(320)
    dlg._lay.addWidget(name_edit)
    _skip, save = dlg.add_buttons([("Omitir", "ghost", 0), ("Guardar", "accent", 1)])
    save.setDefault(True)

    def sync(text: str) -> None:
        save.setEnabled(bool(clean_name(text)))
    sync(name_edit.text())
    name_edit.textChanged.connect(sync)
    name_edit.returnPressed.connect(lambda: clean_name(name_edit.text()) and dlg.done(1))
    name_edit.setFocus()
    accepted = dlg.exec() == 1
    name = clean_name(name_edit.text())
    dlg.deleteLater()
    return name if accepted else ""
