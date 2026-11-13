"""Configuración de PomoGrowth v9: timer, Asana multi-conexión, ClickUp,
webhooks (n8n y recibir tareas), mascota IA y datos. Port de
settings_view_ultra (v8) sobre Qt.

Diseño «setting cards» de Windows 11: cada pestaña agrupa los ajustes en
secciones con título; cada ajuste es una fila con título y ayuda a la
izquierda y un control de ancho fijo a la derecha (los on/off, con
ToggleSwitch).

Toda la red (test de conexión, proyectos, jerarquía ClickUp) va por
services.workers.run_async — nunca en el hilo de UI.
"""
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainter
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QLabel, QLineEdit, QPushButton,
                               QScrollArea, QSlider, QSpinBox, QTabWidget,
                               QVBoxLayout, QWidget)

from core.config import Config
from services.workers import run_async
from ui.dialogs.branded_dialogs import BrandedDialog, confirm, info
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon
from ui.widgets.link_button import LinkButton
from ui.widgets.toggle_switch import ToggleSwitch

# Anchos fijos de los controles (columna derecha alineada en todas las filas)
SPIN_W, COMBO_W, EDIT_W, SLIDER_W = 120, 240, 280, 160
WEBHOOK_KEY_W = 220  # la clave del webhook, con «Copiar» y «Regenerar» al lado
ROW_MIN_H = 56
# Ayudas de la sección «Sonidos» (los de Windows suenan al volumen del sistema)
SOUND_VOLUME_HELP = "Solo el de los avisos: la música va aparte"
SOUND_VOLUME_WINDOWS_HELP = "Los de Windows suenan al volumen del sistema"
SOUND_PREVIEW_HELP = "El fin de un pomodoro, con el estilo y el volumen elegidos"
SOUND_PREVIEW_WINDOWS_HELP = "El fin de un pomodoro, con el sonido de Windows"
# Ayuda de «Recibir tareas» según el modo (services/webhook_server.MODES)
WEBHOOK_MODE_HELP = {
    'local': "Solo los programas de este equipo (n8n, scripts…) pueden enviarte tareas",
    'lan': "Otros equipos de tu red pueden enviarte tareas si llevan la clave",
    'off': "No se escucha en ningún puerto: no llegan tareas por webhook"}
# Alto de las pestañas (la de Timer cabe sin scroll) y lo que suman cabecera,
# botonera, padding y sombra del diálogo alrededor
PANE_MIN_H, DIALOG_CHROME_H = 524, 240

# Pestañas: (icono Lucide, texto). El orden es parte de la API (smoke test)
TABS = [("timer", "Timer y ventana"), ("asana-mark", "Asana"),
        ("list-checks", "ClickUp"), ("webhook", "Webhook"),
        ("paw-print", "Mascota IA"), ("database", "Datos")]

# Estados de _StatusLine: (icono, color del icono, color del texto) como
# tokens. Sin emojis; el color fuerte en el texto solo para errores y avisos
STATUS_KINDS = {'ok': ('check-circle', 'accent', 'fg_dim'),
                'error': ('circle-alert', 'error', 'error'),
                'warn': ('triangle-alert', 'warning', 'warning'),
                'busy': ('sync', 'fg_muted', 'fg_muted'),
                'info': (None, 'fg_muted', 'fg_muted')}


class _Divider(QWidget):
    """Separador de 1 px entre las filas de una tarjeta (pintado: sigue al tema)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(tokens().border))
        p.end()


class _SettingRow(QWidget):
    """Fila de ajuste: título y ayuda a la izquierda, control a la derecha."""

    def __init__(self, parent, title: str, help_text: str = ""):
        super().__init__(parent)
        self.setMinimumHeight(ROW_MIN_H)
        self._lay = QHBoxLayout(self)
        self._lay.setContentsMargins(16, 8, 16, 8)
        self._lay.setSpacing(16)
        # Columna de texto centrada en vertical (los estirables la encuadran)
        self._text = QVBoxLayout()
        self._text.setSpacing(2)
        self._text.addStretch(1)
        self.title = QLabel(title, self)
        self.title.setFont(fonts.font(fonts.SMALL))
        self._text.addWidget(self.title)
        if help_text:
            self.help = QLabel(help_text, self)
            self.help.setFont(fonts.font(fonts.XS))
            self.help.setProperty("role", "muted")
            self.help.setWordWrap(True)
            self._text.addWidget(self.help)
        self._text.addStretch(1)
        self._lay.addLayout(self._text, 1)

    def add_below(self, widget: QWidget) -> QWidget:
        """Añade un widget bajo el título (p. ej. una línea de estado)."""
        self._text.insertWidget(self._text.count() - 1, widget)
        return widget

    def control(self, widget: QWidget) -> QWidget:
        self._lay.addWidget(widget, 0, Qt.AlignVCenter)
        return widget


class _Card(QFrame):
    """Tarjeta de una sección: filas apiladas con separadores entre ellas."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty("card", True)
        self._lay = QVBoxLayout(self)
        self._lay.setContentsMargins(0, 0, 0, 0)
        self._lay.setSpacing(0)

    def add(self, widget: QWidget) -> QWidget:
        if self._lay.count():
            self._lay.addWidget(_Divider(self))
        self._lay.addWidget(widget)
        return widget

    def row(self, title: str, help_text: str = "") -> _SettingRow:
        return self.add(_SettingRow(self, title, help_text))


class _ToggleControl(QWidget):
    """«Activado/Desactivado» + ToggleSwitch, como en Configuración de Windows 11."""

    def __init__(self, parent, checked: bool):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.state = QLabel(self)
        self.state.setFont(fonts.font(fonts.SMALL))
        self.state.setProperty("role", "dim")
        self.switch = ToggleSwitch(parent=self)
        self.switch.setChecked(checked)
        lay.addWidget(self.state)
        lay.addWidget(self.switch)
        self.switch.toggled.connect(self._sync)
        self._sync(checked)

    def _sync(self, on: bool) -> None:
        self.state.setText("Activado" if on else "Desactivado")


class _StatusLine(QWidget):
    """Mensaje de estado con icono y color semántico. Sustituye a los
    prefijos ✅/❌/⚠ de antes; setText() se mantiene para mensajes neutros."""

    def __init__(self, parent, text: str = "", kind: str = 'info'):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        self._icon = QLabel(self)
        self._icon.setFixedSize(14, 16)
        self._label = QLabel(self)
        self._label.setFont(fonts.font(fonts.XS))
        self._label.setWordWrap(True)
        self._label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(self._label, 1)
        self._kind = kind
        self.set_status(text, kind)
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def set_status(self, text: str, kind: str = 'info') -> None:
        self._kind = kind if kind in STATUS_KINDS else 'info'
        self._label.setText(text)
        self._retheme()
        if self.parentWidget() is not None:
            self.setVisible(bool(text))

    def setText(self, text: str) -> None:
        self.set_status(text)

    def text(self) -> str:
        return self._label.text()

    def _retheme(self) -> None:
        t = tokens()
        icon, icon_key, text_key = STATUS_KINDS[self._kind]
        self._label.setStyleSheet(f"color: {getattr(t, text_key)};")
        if icon:
            self._icon.setPixmap(tinted_icon(icon, getattr(t, icon_key), 14).pixmap(14, 14))
        self._icon.setVisible(bool(icon))


class _EmptyState(QWidget):
    """Estado vacío centrado: icono, título y una línea de ayuda."""

    def __init__(self, parent, icon: str, title: str, text: str):
        super().__init__(parent)
        self._icon_name = icon
        lay = QVBoxLayout(self)
        lay.setContentsMargins(24, 20, 24, 22)
        lay.setSpacing(4)
        self._icon = QLabel(self)
        self._icon.setAlignment(Qt.AlignCenter)
        self._title = QLabel(self)
        self._title.setFont(fonts.font(fonts.SMALL, bold=True))
        self._title.setProperty("role", "dim")
        self._title.setAlignment(Qt.AlignCenter)
        self._text = QLabel(self)
        self._text.setFont(fonts.font(fonts.XS))
        self._text.setProperty("role", "muted")
        self._text.setAlignment(Qt.AlignCenter)
        self._text.setWordWrap(True)
        lay.addWidget(self._icon)
        lay.addSpacing(4)
        lay.addWidget(self._title)
        lay.addWidget(self._text)
        self.set_text(title, text)
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def set_text(self, title: str, text: str) -> None:
        self._title.setText(title)
        self._text.setText(text)

    def _retheme(self) -> None:
        self._icon.setPixmap(tinted_icon(self._icon_name, tokens().fg_muted, 28).pixmap(28, 28))


class _Footnote(QWidget):
    """Nota al pie de una pestaña: icono de información y texto pequeño."""

    def __init__(self, parent, text: str):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(4, 0, 4, 0)
        lay.setSpacing(8)
        self._icon = QLabel(self)
        self._icon.setFixedSize(14, 16)
        label = QLabel(text, self)
        label.setFont(fonts.font(fonts.XS))
        label.setProperty("role", "muted")
        label.setWordWrap(True)
        lay.addWidget(self._icon, 0, Qt.AlignTop)
        lay.addWidget(label, 1)
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _retheme(self) -> None:
        self._icon.setPixmap(tinted_icon("info", tokens().fg_muted, 14).pixmap(14, 14))


class _SecretEdit(QLineEdit):
    """Campo de token/key oculto, con un ojo para comprobar lo pegado."""

    def __init__(self, parent, text: str = "", placeholder: str = ""):
        super().__init__(text, parent)
        self.setEchoMode(QLineEdit.Password)
        if placeholder:
            self.setPlaceholderText(placeholder)
        self.setFixedWidth(EDIT_W)
        self.setCursorPosition(0)
        self._reveal = self.addAction(tinted_icon("eye", tokens().fg_muted, 16),
                                      QLineEdit.TrailingPosition)
        self._reveal.triggered.connect(self._toggle_echo)
        self._retheme()
        ThemeManager.instance().theme_changed.connect(self._retheme)

    def _toggle_echo(self) -> None:
        hidden = self.echoMode() == QLineEdit.Password
        self.setEchoMode(QLineEdit.Normal if hidden else QLineEdit.Password)
        self._retheme()

    def _retheme(self) -> None:
        hidden = self.echoMode() == QLineEdit.Password
        self._reveal.setIcon(tinted_icon("eye" if hidden else "eye-off",
                                         tokens().fg_muted, 16))
        self._reveal.setToolTip("Mostrar" if hidden else "Ocultar")


class SettingsDialog(BrandedDialog):
    def __init__(self, parent_window, controller):
        super().__init__(parent_window, "Configuración", icon="configurar")
        self.controller = controller
        self.data_handler = controller.data_handler
        self.settings = dict(self.data_handler.load_settings())
        self._project_checks: Dict[str, tuple] = {}  # gid -> (QCheckBox, name)
        self._cu_workspaces: List[Dict] = []
        self._cu_spaces: List[Dict] = []
        self._cu_lists: List[Dict] = []
        self._icon_buttons: List[tuple] = []  # (QPushButton, icono) a re-teñir

        # El tamaño mínimo va en las pestañas (el diálogo suma la tarjeta y el
        # margen de la sombra). Alto acotado a la pantalla: con escalado alto
        # la botonera no debe quedar fuera; las pestañas ya hacen scroll
        self.tabs = QTabWidget(self._lay.parentWidget())
        screen = parent_window.screen() if parent_window else QGuiApplication.primaryScreen()
        avail_h = screen.availableGeometry().height() if screen else 900
        self.tabs.setMinimumSize(712, max(360, min(PANE_MIN_H, avail_h - DIALOG_CHROME_H)))
        self.tabs.setIconSize(QSize(16, 16))
        self.tabs.setUsesScrollButtons(False)
        self._lay.addWidget(self.tabs, 1)
        builders = (self._build_timer_tab, self._build_asana_tab,
                    self._build_clickup_tab, self._build_webhook_tab,
                    self._build_pet_tab, self._build_data_tab)
        for (_icon, label), build in zip(TABS, builders):
            self.tabs.addTab(build(), label)
        self.tabs.currentChanged.connect(self._retint_tabs)
        # Firma discreta en el hueco de la izquierda del pie, en la fila de
        # «Cancelar» y «Guardar»: lleva al repositorio del proyecto
        self.github_link = LinkButton("Fabricioplus", Config.GITHUB_URL,
                                      icon="brand-github", parent=self._card)
        self.add_buttons([("Cancelar", "ghost", 0), ("Guardar", "accent", 1)],
                         leading=self.github_link)
        self._restyle()
        ThemeManager.instance().theme_changed.connect(self._restyle)

    # ================== Piezas de la maqueta ==================
    def _page(self):
        """Pestaña desplazable: (página, contenedor, layout de secciones)."""
        scroll = QScrollArea(self.tabs)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        inner = QWidget(scroll)
        lay = QVBoxLayout(inner)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(8)
        scroll.setWidget(inner)
        # Con barra de scroll (10 px), el margen derecho le cede su hueco: las
        # tarjetas acaban en el mismo sitio en todas las pestañas
        scroll.verticalScrollBar().rangeChanged.connect(
            lambda _lo, hi: lay.setContentsMargins(16, 16, 6 if hi > 0 else 16, 16))
        return scroll, inner, lay

    @staticmethod
    def _section(inner: QWidget, lay: QVBoxLayout, title: str) -> QHBoxLayout:
        """Encabezado de sección; devuelve su layout para acciones a la derecha."""
        if lay.count():
            lay.addSpacing(8)
        head = QWidget(inner)
        hl = QHBoxLayout(head)
        hl.setContentsMargins(4, 0, 0, 0)
        hl.setSpacing(4)
        label = QLabel(title, head)
        label.setFont(fonts.font(fonts.SMALL, bold=True))
        hl.addWidget(label)
        hl.addStretch(1)
        lay.addWidget(head)
        return hl

    @staticmethod
    def _add_card(inner: QWidget, lay: QVBoxLayout) -> _Card:
        card = _Card(inner)
        lay.addWidget(card)
        return card

    @staticmethod
    def _spin(parent, minimum: int, maximum: int, value, suffix: str = "") -> QSpinBox:
        sp = QSpinBox(parent, minimum=minimum, maximum=maximum, value=int(value))
        if suffix:
            sp.setSuffix(suffix)
        sp.setFixedWidth(SPIN_W)
        return sp

    @staticmethod
    def _edit(parent, text: str = "", placeholder: str = "") -> QLineEdit:
        ed = QLineEdit(text, parent)
        if placeholder:
            ed.setPlaceholderText(placeholder)
        ed.setFixedWidth(EDIT_W)
        ed.setCursorPosition(0)  # en URLs largas, que se vea el principio
        return ed

    def _button(self, parent, text: str, handler, icon: Optional[str] = None,
                variant: Optional[str] = None) -> QPushButton:
        # Con icono, un espacio delante: Fusion los pega al texto
        b = QPushButton(f" {text}" if icon else text, parent)
        if variant:
            b.setProperty("variant", variant)
        if icon:
            b.setIconSize(QSize(16, 16))
            self._icon_buttons.append((b, icon))
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(handler)
        return b

    def _restyle(self) -> None:
        # Ojo: no llamarlo _retheme, que es el de la tarjeta de BrandedDialog
        t = tokens()
        # Área de contenido hundida (fondo de ventana) para que las tarjetas
        # en superficie se lean igual que las tareas de la pantalla principal
        self.tabs.setStyleSheet(
            f"QTabWidget::pane {{ background: {t.bg}; border: 1px solid {t.border};"
            f" border-radius: {t.radius}px; top: -1px; }}"
            f" QTabBar::tab {{ padding: 8px 14px 9px 12px; }}"
            # Placeholders («Sin cargar», «(opcional)»…) atenuados, no como valor.
            # El del combo lo pinta el QSS con su `color`: va por propiedad
            f" QComboBox, QLineEdit {{ placeholder-text-color: {t.fg_muted}; }}"
            f" QComboBox[empty=\"true\"] {{ color: {t.fg_muted}; }}")
        for b, icon in self._icon_buttons:
            b.setIcon(tinted_icon(icon, t.fg, 16))
        self._retint_tabs()

    @staticmethod
    def _mark_empty(cb: QComboBox) -> None:
        """Propiedad empty para atenuar el placeholder del combo vacío."""
        empty = cb.count() == 0
        if cb.property("empty") != empty:
            cb.setProperty("empty", empty)
            cb.style().unpolish(cb)
            cb.style().polish(cb)

    def _retint_tabs(self, *_args) -> None:
        t = tokens()
        current = self.tabs.currentIndex()
        for i, (icon, _label) in enumerate(TABS[:self.tabs.count()]):
            self.tabs.setTabIcon(i, tinted_icon(icon, t.fg if i == current else t.fg_dim, 16))

    # ================== Tabs ==================
    def _build_timer_tab(self) -> QWidget:
        s = self.settings
        page, inner, lay = self._page()
        self._build_profile_section(inner, lay)

        self._section(inner, lay, "Ciclo pomodoro")
        card = self._add_card(inner, lay)
        row = card.row("Duración del pomodoro", "Minutos de enfoque de cada sesión")
        self.sp_pomodoro = row.control(self._spin(row, 1, 120, s.get('pomodoro_min', 25), " min"))
        row = card.row("Descanso corto", "Pausa después de cada pomodoro")
        self.sp_break = row.control(self._spin(row, 1, 60, s.get('break_min', 5), " min"))
        row = card.row("Descanso largo", "Pausa al completar un ciclo")
        self.sp_long = row.control(self._spin(row, 1, 90, s.get('long_break_min', 15), " min"))
        row = card.row("Pomodoros por ciclo", "Cuántos pomodoros hay antes del descanso largo")
        self.sp_cycle = row.control(self._spin(row, 2, 8, s.get('pomodoros_per_cycle', 4)))

        self._section(inner, lay, "Ventana")
        card = self._add_card(inner, lay)
        row = card.row("Siempre visible", "Mantiene PomoGrowth encima de las demás aplicaciones")
        self.chk_topmost = row.control(
            _ToggleControl(row, bool(s.get('always_on_top', True)))).switch
        row = card.row("Opacidad", "Transparencia de la ventana principal")
        op_box = QWidget(row)
        op_lay = QHBoxLayout(op_box)
        op_lay.setContentsMargins(0, 0, 0, 0)
        op_lay.setSpacing(10)
        self.sl_opacity = QSlider(Qt.Horizontal, op_box)
        self.sl_opacity.setRange(50, 100)
        self.sl_opacity.setValue(int(float(s.get('window_opacity', 0.98)) * 100))
        self.sl_opacity.setFixedWidth(SLIDER_W)
        self.opacity_label = QLabel(f"{self.sl_opacity.value()} %", op_box)
        self.opacity_label.setFont(fonts.font(fonts.SMALL))
        self.opacity_label.setProperty("role", "dim")
        self.opacity_label.setFixedWidth(44)
        self.opacity_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sl_opacity.valueChanged.connect(
            lambda v: self.opacity_label.setText(f"{v} %"))
        op_lay.addWidget(self.sl_opacity)
        op_lay.addWidget(self.opacity_label)
        row.control(op_box)
        self._build_sounds_section(inner, lay)  # Sonidos del temporizador
        self._build_tasks_section(inner, lay)
        lay.addStretch(1)
        return page

    def _build_profile_section(self, inner: QWidget, lay: QVBoxLayout) -> None:
        """«Perfil»: el nombre del saludo (el que se pide al primer arranque)."""
        from ui.greeting import NAME_MAX_LEN
        head = self._section(inner, lay, "Perfil")
        self._profile_icon = QLabel(inner)
        self._profile_icon.setFixedSize(14, 14)
        head.insertWidget(0, self._profile_icon, 0, Qt.AlignVCenter)
        self._retint_profile_icon()
        ThemeManager.instance().theme_changed.connect(self._retint_profile_icon)
        card = self._add_card(inner, lay)
        row = card.row("Tu nombre", "Para saludarte al empezar el día y en las estadísticas")
        self.ed_user_name = row.control(self._edit(
            row, self.settings.get('user_name', ''), "Sin nombre"))
        self.ed_user_name.setMaxLength(NAME_MAX_LEN)
        # La presentación del primer arranque, de nuevo cuando se quiera
        row = card.row("Presentación", "Tres pasos para ver cómo te ayuda PomoGrowth")
        self.btn_onboarding = row.control(self._button(
            row, "Ver la presentación", self._open_onboarding, icon="presentation"))

    def _open_onboarding(self) -> None:
        """Encima de Ajustes (su velo cubre la tarjeta); al cerrarla, sigue aquí."""
        self.controller.show_onboarding(parent=self)

    def _retint_profile_icon(self, *_args) -> None:
        self._profile_icon.setPixmap(
            tinted_icon("user-round", tokens().fg_dim, 14).pixmap(14, 14))

    def _save_profile(self, s: Dict) -> None:
        """Nombre del campo «Tu nombre». Tocarlo cuenta como respuesta: el
        «¿Cómo te llamas?» del arranque ya no se muestra."""
        from ui.greeting import clean_name
        name = clean_name(self.ed_user_name.text())
        if name or name != clean_name(s.get('user_name', '')):
            s['user_name_asked'] = True
        s['user_name'] = name

    def _build_tasks_section(self, inner: QWidget, lay: QVBoxLayout) -> None:
        """Sección «Tareas» (al final de «Timer y ventana»): la pregunta de
        esfuerzo al completar."""
        self._section(inner, lay, "Tareas")
        card = self._add_card(inner, lay)
        row = card.row("Preguntar el esfuerzo al completar una tarea",
                       "Valóralo de 1 a 5 estrellas; la media sale en Estadísticas")
        self.chk_ask_effort = row.control(
            _ToggleControl(row, self.controller.effort_prompt_enabled())).switch

    # --- Sonidos del temporizador (services/sound_service.py) ---
    def _build_sounds_section(self, inner: QWidget, lay: QVBoxLayout) -> None:
        """Sección «Sonidos» de «Timer y ventana»: avisos al acabar pomodoro y
        descanso (y al iniciar/pausar), su estilo, su volumen y «Probar»."""
        from services import sound_service
        s = self.settings
        self._section(inner, lay, "Sonidos")
        card = self._add_card(inner, lay)
        row = card.row("Sonidos del temporizador",
                       "Avisan al acabar cada pomodoro y cada descanso, y al iniciar o pausar")
        self.chk_timer_sounds = row.control(_ToggleControl(
            row, bool(s.get('timer_sounds_enabled', sound_service.DEFAULT_ENABLED)))).switch

        row = card.row("Estilo", "Campana, marimba o tonos suaves; o los sonidos de Windows")
        self.cb_sound_style = row.control(QComboBox(row))
        self.cb_sound_style.setFixedWidth(COMBO_W)
        for key, label in sound_service.STYLES:
            self.cb_sound_style.addItem(label, key)
        idx = self.cb_sound_style.findData(
            sound_service.normalize_style(s.get('timer_sound_style')))
        self.cb_sound_style.setCurrentIndex(max(0, idx))

        row = card.row("Volumen", SOUND_VOLUME_HELP)
        volume_row = row
        vol_box = QWidget(row)
        vol_lay = QHBoxLayout(vol_box)
        vol_lay.setContentsMargins(0, 0, 0, 0)
        vol_lay.setSpacing(10)
        self.sl_sound_volume = QSlider(Qt.Horizontal, vol_box)
        self.sl_sound_volume.setRange(0, 100)
        self.sl_sound_volume.setSingleStep(5)
        self.sl_sound_volume.setPageStep(10)
        self.sl_sound_volume.setValue(sound_service.normalize_volume(
            s.get('timer_sound_volume', sound_service.DEFAULT_VOLUME)))
        self.sl_sound_volume.setFixedWidth(SLIDER_W)
        self.sound_volume_label = QLabel(f"{self.sl_sound_volume.value()} %", vol_box)
        self.sound_volume_label.setFont(fonts.font(fonts.SMALL))
        self.sound_volume_label.setProperty("role", "dim")
        self.sound_volume_label.setFixedWidth(44)
        self.sound_volume_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.sl_sound_volume.valueChanged.connect(
            lambda v: self.sound_volume_label.setText(f"{v} %"))
        vol_lay.addWidget(self.sl_sound_volume)
        vol_lay.addWidget(self.sound_volume_label)
        row.control(vol_box)

        row = card.row("Probar el aviso", SOUND_PREVIEW_HELP)
        preview_row = row
        self.btn_sound_preview = row.control(self._button(
            row, "Probar", self._preview_timer_sound, icon="bell-ring"))
        self.btn_sound_preview.setMinimumWidth(136)

        # Apagados, el estilo, el volumen y «Probar» no aplican; con los de
        # Windows, el volumen tampoco (suenan al volumen del sistema)
        def _sync_sound_controls(*_args) -> None:
            on = self.chk_timer_sounds.isChecked()
            windows = self.cb_sound_style.currentData() == 'windows'
            for w in (self.cb_sound_style, self.btn_sound_preview):
                w.setEnabled(on)
            for w in (self.sl_sound_volume, self.sound_volume_label):
                w.setEnabled(on and not windows)
            volume_row.help.setText(SOUND_VOLUME_WINDOWS_HELP if windows
                                    else SOUND_VOLUME_HELP)
            preview_row.help.setText(SOUND_PREVIEW_WINDOWS_HELP if windows
                                     else SOUND_PREVIEW_HELP)
        self.chk_timer_sounds.toggled.connect(_sync_sound_controls)
        self.cb_sound_style.currentIndexChanged.connect(_sync_sound_controls)
        _sync_sound_controls()

    def _preview_timer_sound(self) -> None:
        """«Probar»: lo elegido en el diálogo, aunque aún no se haya guardado."""
        from services import sound_service
        sound_service.preview(self.cb_sound_style.currentData() or sound_service.DEFAULT_STYLE,
                              'complete', self.sl_sound_volume.value())

    def _save_timer_sounds(self, s: Dict) -> None:
        from services import sound_service
        s['timer_sounds_enabled'] = self.chk_timer_sounds.isChecked()
        s['timer_sound_style'] = sound_service.normalize_style(self.cb_sound_style.currentData())
        s['timer_sound_volume'] = self.sl_sound_volume.value()

    def _build_asana_tab(self) -> QWidget:
        s = self.settings
        page, inner, lay = self._page()
        active = s.get('active_integrations', ['asana']) or []

        self._section(inner, lay, "Conexión")
        card = self._add_card(inner, lay)
        row = card.row("Integración con Asana", "Sincroniza tus tareas de Asana con PomoGrowth")
        self.chk_asana = row.control(_ToggleControl(row, 'asana' in active)).switch
        row = card.row("Token de acceso personal",
                       "Se crea en la consola para desarrolladores de Asana")
        self.ed_pat = row.control(_SecretEdit(row, s.get('asana_pat', ''),
                                              "Personal Access Token"))
        row = card.row("Assignee GID", "Tu usuario; se rellena al probar la conexión")
        self.ed_assignee = row.control(self._edit(row, s.get('asana_assignee_gid', '')))
        row = card.row("Workspace GID", "Tu espacio de trabajo; se rellena al probar la conexión")
        self.ed_workspace = row.control(self._edit(row, s.get('asana_workspace_gid', '')))
        row = card.row("Estado de la conexión")
        self.asana_status = row.add_below(_StatusLine(row, "Sin comprobar"))
        self.btn_test_asana = row.control(self._button(row, "Probar conexión", self._test_asana))

        # Lista de proyectos con checkboxes (multi-conexión)
        head = self._section(inner, lay, "Proyectos a sincronizar")
        self.btn_all = QPushButton("Todos", inner)
        self.btn_none = QPushButton("Ninguno", inner)
        for b, v in ((self.btn_all, True), (self.btn_none, False)):
            b.setProperty("pill", True)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, val=v: self._set_all_projects(val))
            head.addWidget(b)
            b.hide()
        card = self._add_card(inner, lay)
        # Mostrar la selección guardada aunque aún no se carguen proyectos
        saved = s.get('asana_connections') or []
        names = [c.get('project_name') for c in saved if c.get('project_name')]
        row = card.row("Proyectos del workspace")
        self.projects_status = row.add_below(_StatusLine(row))
        if names:
            self.projects_status.setText(
                "Selección guardada: " + ", ".join(names[:6])
                + ("…" if len(names) > 6 else ""))
        else:
            self.projects_status.setText("Necesita el token y el Workspace GID")
        self.btn_load_projects = row.control(
            self._button(row, "Cargar proyectos", self._load_projects, icon="sync"))
        # Cuerpo: estado vacío hasta cargar; luego, la lista de proyectos
        body = QWidget(card)
        body_lay = QVBoxLayout(body)
        body_lay.setContentsMargins(0, 0, 0, 0)
        body_lay.setSpacing(0)
        self.projects_empty = _EmptyState(
            body, "folder-open", "Aún no hay proyectos cargados",
            "Prueba la conexión y pulsa «Cargar proyectos» para elegir cuáles sincronizar.")
        body_lay.addWidget(self.projects_empty)
        self.projects_box = QWidget(body)
        self.projects_lay = QVBoxLayout(self.projects_box)
        self.projects_lay.setContentsMargins(16, 10, 16, 12)
        self.projects_lay.setSpacing(2)
        body_lay.addWidget(self.projects_box)
        self.projects_box.hide()
        card.add(body)

        self._section(inner, lay, "Sincronización")
        card = self._add_card(inner, lay)
        row = card.row("Incluir tareas completadas",
                       "Trae también las tareas que ya cerraste en Asana")
        self.chk_include_completed = row.control(
            _ToggleControl(row, bool(s.get('asana_include_completed', False)))).switch
        self._build_no_project_row(card)
        row = card.row("Sincronizar al abrir la app",
                       "Actualiza tus tareas cada vez que inicias PomoGrowth")
        self.chk_auto_sync = row.control(
            _ToggleControl(row, bool(s.get('asana_auto_sync_on_start', False)))).switch
        lay.addStretch(1)
        return page

    def _build_no_project_row(self, card: _Card) -> None:
        """«Incluir mis tareas sin proyecto»: con proyectos marcados, el sync
        trae también las tareas sueltas asignadas a ti (no las subtareas).
        Sin ninguno marcado ya llegan todas, y la fila lo dice."""
        row = card.row("Incluir mis tareas sin proyecto",
                       "Trae también las tareas asignadas a ti que no están en "
                       "ningún proyecto")
        self.chk_no_project = row.control(_ToggleControl(
            row, bool(self.settings.get('asana_include_no_project', False)))).switch
        self.no_project_hint = row.add_below(_StatusLine(row))
        self._sync_no_project_hint()

    def _sync_no_project_hint(self, *_args) -> None:
        if self._project_checks:
            marked = any(chk.isChecked() for chk, _name in self._project_checks.values())
        else:
            s = self.settings
            marked = bool(s.get('asana_project_gid')) or any(
                isinstance(c, dict) and c.get('project_gid')
                for c in s.get('asana_connections') or [])
        self.no_project_hint.setText(
            "" if marked else "Sin proyectos marcados ya llegan todas tus tareas, "
                              "también las que no tienen proyecto.")

    def _build_clickup_tab(self) -> QWidget:
        """Pestaña de ClickUp. En la 1.0 ClickUp solo crea tareas desde el
        alta (ui/controllers/task_destinations.py): no se sincroniza, así que
        aquí no hay interruptores de sincronización ni textos que la prometan."""
        s = self.settings
        page, inner, lay = self._page()
        active = s.get('active_integrations', []) or []

        self._section(inner, lay, "Conexión")
        card = self._add_card(inner, lay)
        row = card.row("Integración con ClickUp",
                       "Crea en ClickUp las tareas que añadas, en la lista que elijas")
        self.chk_clickup = row.control(_ToggleControl(row, 'clickup' in active)).switch
        row = card.row("Token de API", "Token personal de ClickUp (empieza por pk_)")
        self.ed_cu_token = row.control(_SecretEdit(row, s.get('clickup_api_token', '')))
        row = card.row("Estado de la conexión")
        self.cu_status = row.add_below(_StatusLine(row, "Sin comprobar"))
        btns = QWidget(row)
        btn_lay = QHBoxLayout(btns)
        btn_lay.setContentsMargins(0, 0, 0, 0)
        btn_lay.setSpacing(8)
        self.btn_test_cu = self._button(btns, "Probar conexión", self._test_clickup)
        self.btn_load_cu = self._button(btns, "Cargar jerarquía", self._load_cu_workspaces,
                                        icon="sync")
        btn_lay.addWidget(self.btn_test_cu)
        btn_lay.addWidget(self.btn_load_cu)
        row.control(btns)

        self._section(inner, lay, "Lista donde crear")
        card = self._add_card(inner, lay)
        combos = []
        for title, help_text, key in (
                ("Workspace", "Tu equipo de ClickUp", 'clickup_workspace_id'),
                ("Space", "Espacio dentro del workspace", 'clickup_space_id'),
                ("Lista", "Donde se crean las tareas nuevas", 'clickup_list_id')):
            row = card.row(title, help_text)
            cb = QComboBox(row)
            cb.setFixedWidth(COMBO_W)
            # Vacío hasta cargar la jerarquía; si hay uno guardado, se respeta
            cb.setPlaceholderText("Guardado (sin cargar)" if s.get(key) else "Sin cargar")
            model = cb.model()
            for sig in (model.rowsInserted, model.rowsRemoved, model.modelReset):
                sig.connect(lambda *_a, c=cb: self._mark_empty(c))
            self._mark_empty(cb)
            combos.append(row.control(cb))
        self.cb_cu_workspace, self.cb_cu_space, self.cb_cu_list = combos
        self.cb_cu_workspace.currentIndexChanged.connect(self._on_cu_workspace)
        self.cb_cu_space.currentIndexChanged.connect(self._on_cu_space)
        lay.addStretch(1)
        return page

    def _build_webhook_tab(self) -> QWidget:
        s = self.settings
        page, inner, lay = self._page()

        self._section(inner, lay, "n8n")
        card = self._add_card(inner, lay)
        row = card.row("URL del webhook", "Recibe un evento al completar cada pomodoro")
        self.ed_n8n = row.control(self._edit(row, s.get('n8n_webhook_url', ''),
                                             "https://…/webhook/…"))

        self._build_webhook_inbox_section(inner, lay)
        lay.addStretch(1)
        return page

    # --- Recibir tareas (services/webhook_server.py) ---
    def _build_webhook_inbox_section(self, inner: QWidget, lay: QVBoxLayout) -> None:
        """«Recibir tareas»: el servidor de webhooks entrantes, solo para este
        equipo, para la red local con clave o desactivado. Lo recibido se
        crea siempre como tarea local (ver AppController._process_webhook_queue).
        La clave, generada, se ve solo en «Red local»."""
        from services.webhook_server import (KEY_HEADER, MODE_LABELS, WEBHOOK_PATH,
                                             normalize_key, normalize_mode)
        s = self.settings
        self._section(inner, lay, "Recibir tareas")
        card = self._add_card(inner, lay)
        self._webhook_mode_row = card.row("Servidor de webhooks", WEBHOOK_MODE_HELP['local'])
        self.cb_webhook_mode = self._webhook_mode_row.control(QComboBox(self._webhook_mode_row))
        self.cb_webhook_mode.setFixedWidth(COMBO_W)
        for key, label in MODE_LABELS:
            self.cb_webhook_mode.addItem(label, key)
        self.cb_webhook_mode.setCurrentIndex(max(0, self.cb_webhook_mode.findData(
            normalize_mode(s.get('webhook_mode')))))

        row = card.row("Clave", f"Va en la cabecera {KEY_HEADER} de cada envío")
        self.webhook_key_row = row
        # El separador que la tarjeta puso encima de la fila se oculta con ella
        self._webhook_key_divider = card._lay.itemAt(card._lay.count() - 2).widget()
        box = QWidget(row)
        box_lay = QHBoxLayout(box)
        box_lay.setContentsMargins(0, 0, 0, 0)
        box_lay.setSpacing(8)
        self.ed_webhook_key = _SecretEdit(box, normalize_key(s.get('webhook_key')))
        self.ed_webhook_key.setReadOnly(True)  # se genera: «Regenerar» la cambia
        self.ed_webhook_key.setFixedWidth(WEBHOOK_KEY_W)
        self.btn_copy_webhook_key = self._button(box, "Copiar", self._copy_webhook_key)
        self.btn_new_webhook_key = self._button(box, "Regenerar", self._regenerate_webhook_key)
        for w in (self.ed_webhook_key, self.btn_copy_webhook_key, self.btn_new_webhook_key):
            box_lay.addWidget(w)
        row.control(box)
        self.webhook_key_status = row.add_below(_StatusLine(row))

        self.cb_webhook_mode.currentIndexChanged.connect(self._sync_webhook_mode)
        self._sync_webhook_mode()

        lay.addSpacing(8)
        lay.addWidget(_Footnote(
            inner,
            f"Envío: POST http://127.0.0.1:{Config.WEBHOOK_PORT}{WEBHOOK_PATH} (en red "
            "local, con la IP de este equipo) con la cabecera Content-Type: "
            f"application/json y, en red local, {KEY_HEADER} con la clave.\n"
            'Cuerpo: {"name": "…", "tags": ["…"]}. Se crea siempre como tarea local '
            "y llega con la sincronización activada.\n"
            "En red local, Windows pedirá permiso del cortafuegos la primera vez."))

    def _sync_webhook_mode(self, *_args) -> None:
        """La fila de la clave, solo en «Red local» (al elegirlo sin clave se
        crea una; se guarda con «Guardar»), y la ayuda del modo elegido."""
        from services.webhook_server import new_key
        mode = self.cb_webhook_mode.currentData()
        lan = mode == 'lan'
        if lan and not self.ed_webhook_key.text():
            self.ed_webhook_key.setText(new_key())
            self.ed_webhook_key.setCursorPosition(0)
        self.webhook_key_row.setVisible(lan)
        self._webhook_key_divider.setVisible(lan)
        self._webhook_mode_row.help.setText(WEBHOOK_MODE_HELP.get(mode, ''))

    def _copy_webhook_key(self) -> None:
        QGuiApplication.clipboard().setText(self.ed_webhook_key.text())
        self.webhook_key_status.set_status("Clave copiada al portapapeles", 'ok')

    def _regenerate_webhook_key(self) -> None:
        from services.webhook_server import new_key
        self.ed_webhook_key.setText(new_key())
        self.ed_webhook_key.setCursorPosition(0)
        self.webhook_key_status.set_status(
            "Clave nueva: vale desde que guardes. Cámbiala también donde usabas la anterior",
            'warn')

    def _save_webhook_inbox(self, s: Dict) -> None:
        from services.webhook_server import new_key, normalize_mode
        mode = normalize_mode(self.cb_webhook_mode.currentData())
        key = self.ed_webhook_key.text().strip()
        if mode == 'lan' and not key:
            key = new_key()
        s['webhook_mode'] = mode
        s['webhook_key'] = key

    def _build_pet_tab(self) -> QWidget:
        s = self.settings
        page, inner, lay = self._page()

        intro = QLabel("Tu mascota pixel (se compra en Recompensas → Tienda) puede "
                       "darte consejos personalizados analizando tu progreso. Sin "
                       "configurar, funciona igual con frases locales.", inner)
        intro.setWordWrap(True)
        intro.setFont(fonts.font(fonts.SMALL))
        intro.setProperty("role", "dim")
        intro.setContentsMargins(4, 0, 4, 0)
        lay.addWidget(intro)

        from services.pet_service import DEFAULT_MODEL, PROVIDERS, SUGGESTED_MODELS
        self._section(inner, lay, "Proveedor")
        card = self._add_card(inner, lay)
        row = card.row("Proveedor de IA", "Cada proveedor recuerda su propia key y modelo")
        self.cb_pet_provider = row.control(QComboBox(row))
        self.cb_pet_provider.setFixedWidth(COMBO_W)
        for key, label in PROVIDERS:
            self.cb_pet_provider.addItem(label, key)
        idx = self.cb_pet_provider.findData(s.get('pet_provider') or 'claude')
        self.cb_pet_provider.setCurrentIndex(idx if idx >= 0 else 0)

        def _model_combo(parent, suggestions, saved):
            cb = QComboBox(parent)
            cb.setEditable(True)  # cualquier id de modelo futuro sirve
            cb.setFixedWidth(COMBO_W)
            for m in suggestions:
                cb.addItem(m)
            cb.setCurrentText(saved or suggestions[0])
            return cb

        self._section(inner, lay, "Credenciales y modelo")

        # --- Claude (Anthropic) ---
        self.pet_claude_box = self._add_card(inner, lay)
        row = self.pet_claude_box.row("API key de Claude", "Créala en platform.claude.com")
        self.ed_pet_key = row.control(_SecretEdit(row, s.get('pet_api_key', ''), "sk-ant-…"))
        row = self.pet_claude_box.row("Modelo", "claude-opus-4-8 da los mejores consejos; "
                                                "claude-haiku-4-5 es el más económico")
        self.cb_pet_model = row.control(_model_combo(row, SUGGESTED_MODELS['claude'],
                                                     s.get('pet_model') or DEFAULT_MODEL))

        # --- ChatGPT (OpenAI) ---
        self.pet_openai_box = self._add_card(inner, lay)
        row = self.pet_openai_box.row("API key de OpenAI", "Créala en platform.openai.com")
        self.ed_openai_key = row.control(_SecretEdit(row, s.get('pet_openai_key', ''), "sk-…"))
        row = self.pet_openai_box.row("Modelo", "gpt-5-mini es rápido y económico; puedes "
                                                "escribir cualquier id de modelo")
        self.cb_openai_model = row.control(_model_combo(row, SUGGESTED_MODELS['openai'],
                                                        s.get('pet_openai_model')))

        # --- Endpoint propio (ngrok / local) ---
        self.pet_custom_box = self._add_card(inner, lay)
        row = self.pet_custom_box.row(
            "URL del endpoint", "Servidor compatible con la API de OpenAI (Ollama, LM Studio, "
                                "vLLM, n8n…), local o expuesto con ngrok. Se le añade "
                                "/v1/chat/completions si hace falta")
        self.ed_custom_url = row.control(self._edit(
            row, s.get('pet_custom_url', ''),
            "https://xxxx.ngrok-free.app  ·  http://localhost:11434"))
        row = self.pet_custom_box.row("API key o token",
                                      "Solo si tu servidor la exige; muchos locales van sin key")
        self.ed_custom_key = row.control(_SecretEdit(row, s.get('pet_custom_key', ''),
                                                     "(opcional)"))
        row = self.pet_custom_box.row("Modelo", "El nombre del modelo cargado en tu servidor")
        self.cb_custom_model = row.control(_model_combo(row, SUGGESTED_MODELS['custom'],
                                                        s.get('pet_custom_model')))

        def _show_provider_fields():
            provider = self.cb_pet_provider.currentData()
            self.pet_claude_box.setVisible(provider == 'claude')
            self.pet_openai_box.setVisible(provider == 'openai')
            self.pet_custom_box.setVisible(provider == 'custom')
        self.cb_pet_provider.currentIndexChanged.connect(_show_provider_fields)
        _show_provider_fields()

        lay.addSpacing(8)
        lay.addWidget(_Footnote(inner, "La mascota consulta la IA solo cuando le haces clic "
                                       "para pedirle un consejo. Las keys se guardan en "
                                       "pomogrowth_data.json (texto plano, como el resto de "
                                       "tokens de la app)."))
        lay.addStretch(1)
        return page

    def _build_data_tab(self) -> QWidget:
        page, inner, lay = self._page()

        self._section(inner, lay, "Copia de seguridad")
        card = self._add_card(inner, lay)
        for title, help_text, text, icon, handler in (
                ("Carpeta de datos", "Abre la carpeta donde PomoGrowth guarda tus datos",
                 "Abrir carpeta", "folder-open", self._open_folder),
                ("Exportar datos", "Guarda una copia de seguridad en un archivo JSON",
                 "Exportar…", "download", self._export_data),
                ("Importar datos", "Reemplaza tus datos por los de una copia; antes se "
                                   "crea un backup", "Importar…", "upload", self._import_data)):
            row = card.row(title, help_text)
            b = row.control(self._button(row, text, handler, icon=icon))
            b.setMinimumWidth(136)

        self._section(inner, lay, "Restablecer")
        card = self._add_card(inner, lay)
        row = card.row("Borrar todos los datos",
                       "Elimina tareas, tiempos e historial. Se crea un backup previo")
        row.control(self._button(row, "Borrar todo…", self._clear_all,
                                 variant="danger")).setMinimumWidth(136)

        lay.addSpacing(4)
        self.data_status = _StatusLine(inner)
        self.data_status.setContentsMargins(4, 0, 4, 0)
        lay.addWidget(self.data_status)
        self.data_status.hide()
        lay.addStretch(1)
        return page

    # ================== Red (Asana) ==================
    def _test_asana(self) -> None:
        pat = self.ed_pat.text().strip()
        if not pat:
            self.asana_status.set_status("Ingresa primero el token de acceso", 'warn')
            return
        self.asana_status.set_status("Probando conexión…", 'busy')
        self.btn_test_asana.setEnabled(False)

        def check():
            import requests
            r = requests.get("https://app.asana.com/api/1.0/users/me",
                             headers={"Authorization": f"Bearer {pat}"}, timeout=12)
            r.raise_for_status()
            data = r.json().get('data', {})
            workspaces = data.get('workspaces') or []
            return data.get('name', '¿?'), data.get('gid', ''), workspaces

        def ok(result):
            name, gid, workspaces = result
            self.btn_test_asana.setEnabled(True)
            ws_txt = ", ".join(f"{w.get('name')} ({w.get('gid')})" for w in workspaces[:4])
            self.asana_status.set_status(f"Conectado como {name} (GID {gid}). "
                                         f"Workspaces: {ws_txt}", 'ok')
            if not self.ed_assignee.text().strip():
                self.ed_assignee.setText(gid)
            if not self.ed_workspace.text().strip() and workspaces:
                self.ed_workspace.setText(workspaces[0].get('gid', ''))

        def fail(e):
            self.btn_test_asana.setEnabled(True)
            self.asana_status.set_status(f"Error de conexión: {e}", 'error')

        run_async(check, ok, fail)

    def _load_projects(self) -> None:
        pat = self.ed_pat.text().strip()
        ws = self.ed_workspace.text().strip()
        if not pat or not ws:
            self.projects_status.set_status(
                "Ingresa primero el token de acceso y el Workspace GID", 'warn')
            return
        self.projects_status.set_status("Cargando proyectos…", 'busy')
        self.btn_load_projects.setEnabled(False)
        run_async(lambda: self.data_handler.list_asana_projects(ws, pat),
                  self._populate_projects,
                  lambda e: (self.btn_load_projects.setEnabled(True),
                             self.projects_status.set_status(
                                 f"Error cargando proyectos: {e}", 'error')))

    def _populate_projects(self, projects) -> None:
        self.btn_load_projects.setEnabled(True)
        while self.projects_lay.count():
            item = self.projects_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self._project_checks.clear()
        if not projects:
            self.projects_status.set_status("No se encontraron proyectos en este workspace",
                                            'warn')
            self.projects_empty.set_text("Sin proyectos",
                                         "Este workspace no tiene proyectos visibles "
                                         "para tu usuario.")
            self.projects_box.hide()
            self.projects_empty.show()
            self.btn_all.hide()
            self.btn_none.hide()
            return
        ws = self.ed_workspace.text().strip()
        saved = self.data_handler.load_settings().get('asana_connections') or []
        selected = {c.get('project_gid') for c in saved
                    if c.get('workspace_gid') == ws and c.get('project_gid')}
        if not selected and self.settings.get('asana_project_gid'):
            selected.add(self.settings['asana_project_gid'])
        for p in projects:
            gid, name = p.get('gid'), p.get('name') or 'Sin nombre'
            if not gid:
                continue
            chk = QCheckBox(name, self.projects_box)
            chk.setMinimumHeight(28)
            chk.setCursor(Qt.PointingHandCursor)
            chk.setChecked(gid in selected)
            chk.toggled.connect(self._sync_no_project_hint)
            self._project_checks[gid] = (chk, name)
            self.projects_lay.addWidget(chk)
        self._sync_no_project_hint()
        self.projects_empty.hide()
        self.projects_box.show()
        self.btn_all.show()
        self.btn_none.show()
        self.projects_status.set_status(
            f"{len(projects)} proyectos disponibles: marca los que quieras sincronizar", 'ok')

    def _set_all_projects(self, value: bool) -> None:
        for chk, _name in self._project_checks.values():
            chk.setChecked(value)

    # ================== Red (ClickUp) ==================
    def _cu_client(self):
        from core.clickup_integration import ClickUpClient
        return ClickUpClient(self.ed_cu_token.text().strip())

    def _test_clickup(self) -> None:
        if not self.ed_cu_token.text().strip():
            self.cu_status.set_status("Ingresa primero el token de API", 'warn')
            return
        self.cu_status.set_status("Probando conexión…", 'busy')
        client = self._cu_client()
        run_async(client.test_connection,
                  lambda r: self.cu_status.set_status(str(r[1]), 'ok' if r[0] else 'error'),
                  lambda e: self.cu_status.set_status(str(e), 'error'))

    def _load_cu_workspaces(self) -> None:
        if not self.ed_cu_token.text().strip():
            self.cu_status.set_status("Ingresa primero el token de API", 'warn')
            return
        self.cu_status.set_status("Cargando workspaces…", 'busy')
        client = self._cu_client()

        def ok(teams):
            self._cu_workspaces = teams or []
            self.cb_cu_workspace.clear()
            for t in self._cu_workspaces:
                self.cb_cu_workspace.addItem(t.get('name', '¿?'), t.get('id'))
            self.cu_status.set_status(f"{len(self._cu_workspaces)} workspaces", 'ok')
            saved_ws = self.settings.get('clickup_workspace_id', '')
            idx = self.cb_cu_workspace.findData(saved_ws)
            if idx >= 0:
                self.cb_cu_workspace.setCurrentIndex(idx)
        run_async(client.get_teams, ok,
                  lambda e: self.cu_status.set_status(str(e), 'error'))

    def _on_cu_workspace(self, index: int) -> None:
        team_id = self.cb_cu_workspace.itemData(index)
        if not team_id:
            return
        client = self._cu_client()

        def ok(spaces):
            self._cu_spaces = spaces or []
            self.cb_cu_space.clear()
            for s in self._cu_spaces:
                self.cb_cu_space.addItem(s.get('name', '¿?'), s.get('id'))
            saved = self.settings.get('clickup_space_id', '')
            idx = self.cb_cu_space.findData(saved)
            if idx >= 0:
                self.cb_cu_space.setCurrentIndex(idx)
        run_async(lambda: client.get_spaces(team_id), ok,
                  lambda e: self.cu_status.set_status(str(e), 'error'))

    def _on_cu_space(self, index: int) -> None:
        space_id = self.cb_cu_space.itemData(index)
        if not space_id:
            return
        client = self._cu_client()

        def ok(lists):
            self._cu_lists = lists or []
            self.cb_cu_list.clear()
            for l in self._cu_lists:
                self.cb_cu_list.addItem(l.get('name', '¿?'), l.get('id'))
            saved = self.settings.get('clickup_list_id', '')
            idx = self.cb_cu_list.findData(saved)
            if idx >= 0:
                self.cb_cu_list.setCurrentIndex(idx)
        run_async(lambda: client.get_lists(space_id=space_id), ok,
                  lambda e: self.cu_status.set_status(str(e), 'error'))

    # ================== Datos ==================
    def _open_folder(self) -> None:
        try:
            os.startfile(str(Path.cwd()))
        except Exception as e:
            self.data_status.set_status(str(e), 'error')

    def _export_data(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Exportar datos", f"pomogrowth_backup_{stamp}.json", "JSON (*.json)")
        if not dest:
            return
        try:
            shutil.copy2("pomogrowth_data.json", dest)
            self.data_status.set_status(f"Copia guardada en {dest}", 'ok')
        except Exception as e:
            self.data_status.set_status(str(e), 'error')

    def _import_data(self) -> None:
        src, _ = QFileDialog.getOpenFileName(self, "Importar datos", "", "JSON (*.json)")
        if not src:
            return
        try:
            imported = json.loads(Path(src).read_text(encoding="utf-8"))
            if not isinstance(imported, dict) or 'tasks' not in imported:
                self.data_status.set_status("El archivo no parece un backup de PomoGrowth",
                                            'error')
                return
            if not confirm(self, "Importar datos",
                           f"Esto REEMPLAZA tus datos actuales por los de\n{src}\n"
                           f"({len(imported.get('tasks', []))} tareas). "
                           "Se hará una copia de seguridad antes.",
                           ok_text="Importar", danger=True):
                return
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2("pomogrowth_data.json", f"pomogrowth_data.pre-import-{stamp}.json")
            # Sin traer la cola hacia Asana del archivo (ver import_data)
            self.data_handler.import_data(imported)
            self.controller.refresh_task_list()
            self.data_status.set_status("Datos importados (backup previo creado)", 'ok')
        except Exception as e:
            self.data_status.set_status(str(e), 'error')

    def _clear_all(self) -> None:
        if not confirm(self, "Borrar todos los datos",
                       "Se eliminarán TODAS las tareas, tiempos e historial.\n"
                       "Esta acción no se puede deshacer (se crea un backup previo).",
                       ok_text="Borrar todo", danger=True):
            return
        try:
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            shutil.copy2("pomogrowth_data.json", f"pomogrowth_data.pre-clear-{stamp}.json")
            self.data_handler.clear_data()
            self.controller.refresh_task_list()
            self.data_status.set_status("Datos borrados (backup previo creado)", 'ok')
        except Exception as e:
            self.data_status.set_status(str(e), 'error')

    # ================== Guardar ==================
    def accept_settings(self) -> None:
        s = self.data_handler.load_settings()
        s['pomodoro_min'] = self.sp_pomodoro.value()
        s['break_min'] = self.sp_break.value()
        s['long_break_min'] = self.sp_long.value()
        s['pomodoros_per_cycle'] = self.sp_cycle.value()

        active = []
        if self.chk_asana.isChecked():
            active.append('asana')
        if self.chk_clickup.isChecked():
            active.append('clickup')
        s['active_integrations'] = active

        s['asana_pat'] = self.ed_pat.text().strip()
        s['asana_assignee_gid'] = self.ed_assignee.text().strip()
        s['asana_workspace_gid'] = self.ed_workspace.text().strip()
        s['asana_include_completed'] = self.chk_include_completed.isChecked()
        s['asana_include_no_project'] = self.chk_no_project.isChecked()
        s['asana_auto_sync_on_start'] = self.chk_auto_sync.isChecked()
        # Proyectos marcados → conexiones múltiples (solo si se cargó la lista)
        if self._project_checks:
            ws = self.ed_workspace.text().strip()
            selected = [(gid, name) for gid, (chk, name) in self._project_checks.items()
                        if chk.isChecked()]
            if ws:
                s['asana_connections'] = [
                    {'workspace_gid': ws, 'project_gid': gid, 'project_name': name}
                    for gid, name in selected]
                s['asana_project_gid'] = selected[0][0] if selected else ''

        s['clickup_api_token'] = self.ed_cu_token.text().strip()
        s['clickup_workspace_id'] = self.cb_cu_workspace.currentData() or \
            self.settings.get('clickup_workspace_id', '')
        s['clickup_space_id'] = self.cb_cu_space.currentData() or \
            self.settings.get('clickup_space_id', '')
        s['clickup_list_id'] = self.cb_cu_list.currentData() or \
            self.settings.get('clickup_list_id', '')
        # clickup_include_completed y clickup_auto_sync ya no se escriben (la
        # 1.0 no sincroniza con ClickUp); si existen, se quedan sin uso en los
        # datos para cuando la sincronización llegue

        s['n8n_webhook_url'] = self.ed_n8n.text().strip()
        # «Recibir tareas». Los ngrok_authtoken/ngrok_domain de antes se quedan
        # en los datos, sin uso (la app nunca arrancó ngrok)
        self._save_webhook_inbox(s)

        s['pet_provider'] = self.cb_pet_provider.currentData() or 'claude'
        s['pet_api_key'] = self.ed_pet_key.text().strip()
        s['pet_model'] = self.cb_pet_model.currentText().strip()
        s['pet_openai_key'] = self.ed_openai_key.text().strip()
        s['pet_openai_model'] = self.cb_openai_model.currentText().strip()
        s['pet_custom_url'] = self.ed_custom_url.text().strip()
        s['pet_custom_key'] = self.ed_custom_key.text().strip()
        s['pet_custom_model'] = self.cb_custom_model.currentText().strip()

        s['always_on_top'] = self.chk_topmost.isChecked()
        s['window_opacity'] = round(self.sl_opacity.value() / 100.0, 2)
        self._save_profile(s)
        s['ask_effort_on_complete'] = self.chk_ask_effort.isChecked()
        self._save_timer_sounds(s)  # Sonidos del temporizador

        self.data_handler.save_settings(s)
        self.controller.apply_settings(s)

    def done(self, result: int) -> None:
        if result == 1:
            try:
                self.accept_settings()
            except Exception as e:
                info(self.parentWidget(), "Error", f"No se pudo guardar: {e}")
        super().done(result)
