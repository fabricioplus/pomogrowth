"""Modo enfoque: bloqueo de webs por hosts + silenciar notificaciones.

Port del focus_tab_content de v8. El bloqueo real vive en core/focus_mode
(hosts de Windows, requiere admin; triple limpieza: cierre/atexit/arranque).
Cada sitio tiene su comodín (bandera blanca): libre solo hasta cerrar la app.
"""
import re
from typing import Callable, List, Optional

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                               QPushButton, QScrollArea, QVBoxLayout, QWidget)

from core.focus_mode import OPEN_TABS_NOTE, RETRY_SOON
from ui.dialogs.branded_dialogs import BrandedDialog, confirm
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import IconButton, tinted_icon
from ui.widgets.mini_player import ElidedLabel

# core/focus_mode antepone «✓» a sus mensajes: la interfaz pone un icono
_STATUS_PREFIX = re.compile(r"^[\s✓✔✅⚠❌️]+")
_DOMAIN = re.compile(r"[^\s./:@]+(\.[^\s./:@]+)+")


def normalize_site(text: str) -> str:
    """«https://www.YouTube.com/watch?v=1» → «youtube.com». El núcleo añade
    la variante www por su cuenta, así que la lista trabaja con el dominio
    base (quitar «www.x.com» dejaba vivo «x.com»)."""
    s = text.strip().lower()
    s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
    s = re.split(r"[/?#]", s, maxsplit=1)[0]
    s = s.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
    return s[4:] if s.startswith("www.") else s


class _Divider(QWidget):
    """Separador de 1 px entre filas (pintado: sigue al tema)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(1)

    def paintEvent(self, event):
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(tokens().border))
        p.end()


class FocusDialog(BrandedDialog):
    state_changed = Signal(bool)
    # La instancia elevada ya se está abriendo: el controlador cierra esta
    elevated_restart = Signal()

    def __init__(self, parent_window, focus_mode):
        super().__init__(parent_window, "Modo enfoque", icon="target")
        self.focus = focus_mode
        self._live = True
        # Consulta del controlador (el diálogo no conoce el modelo): el aviso
        # de la sesión que cortaría «Reiniciar como administrador», o ''
        self.session_warning: Optional[Callable[[], str]] = None
        host = self._lay.parentWidget()  # la tarjeta del diálogo
        host.setMinimumWidth(540)

        # ---- Estado: icono, texto y la acción principal ----
        self.status_card = QFrame(host)
        self.status_card.setObjectName("focusStatus")
        sc = QHBoxLayout(self.status_card)
        sc.setContentsMargins(16, 16, 16, 16)
        sc.setSpacing(14)
        self.status_icon = QLabel(self.status_card)
        self.status_icon.setFixedSize(44, 44)
        self.status_icon.setAlignment(Qt.AlignCenter)
        sc.addWidget(self.status_icon, 0, Qt.AlignVCenter)
        texts = QVBoxLayout()
        texts.setSpacing(2)
        self.status_label = QLabel(self.status_card)
        self.status_label.setFont(fonts.font(fonts.NORMAL, bold=True))
        texts.addWidget(self.status_label)
        self.status_detail = QLabel(self.status_card)
        self.status_detail.setFont(fonts.font(fonts.SMALL))
        self.status_detail.setProperty("role", "dim")
        self.status_detail.setWordWrap(True)
        texts.addWidget(self.status_detail)
        sc.addLayout(texts, 1)
        self.toggle_btn = QPushButton(self.status_card)
        self.toggle_btn.setMinimumWidth(112)
        self.toggle_btn.setCursor(Qt.PointingHandCursor)
        self.toggle_btn.clicked.connect(self._toggle)
        sc.addWidget(self.toggle_btn, 0, Qt.AlignVCenter)
        self._lay.addWidget(self.status_card)

        # Resultado de la última acción (oculto hasta que haya una)
        self.feedback_row, self.feedback_icon, self.feedback = self._note_row(host)
        self._lay.addWidget(self.feedback_row)
        self.feedback_row.setVisible(False)
        self._feedback_ok = True
        # Aviso de permisos: el hosts solo se puede editar como administrador
        self.admin_row, self.admin_icon, self.admin_label = self._note_row(host)
        # …y cómo conseguirlos sin buscar el acceso directo: relanzar elevada
        self.admin_btn = QPushButton("Reiniciar como administrador", self.admin_row)
        self.admin_btn.setProperty("variant", "ghost")
        self.admin_btn.setCursor(Qt.PointingHandCursor)
        self.admin_btn.setToolTip("Windows pedirá permiso (UAC); PomoGrowth se cierra "
                                  "y vuelve a abrirse con permisos de administrador")
        self.admin_btn.clicked.connect(self._restart_as_admin)
        self.admin_row.layout().addWidget(self.admin_btn, 0, Qt.AlignVCenter)
        self._lay.addWidget(self.admin_row)
        self.admin_row.setVisible(False)

        # ---- Sitios bloqueados ----
        self._lay.addSpacing(8)
        head = QHBoxLayout()
        head.setContentsMargins(4, 0, 0, 0)
        head.setSpacing(8)
        title = QLabel("Sitios bloqueados", host)
        title.setFont(fonts.font(fonts.SMALL, bold=True))
        head.addWidget(title)
        self.count_label = QLabel(host)
        self.count_label.setFont(fonts.font(fonts.XS))
        self.count_label.setProperty("role", "muted")
        head.addWidget(self.count_label)
        head.addStretch(1)
        self._lay.addLayout(head)

        add_row = QHBoxLayout()
        add_row.setSpacing(8)
        self.site_edit = QLineEdit(host)
        self.site_edit.setPlaceholderText("Añadir un sitio, p. ej. youtube.com")
        self.site_edit.returnPressed.connect(self._add_site)
        add_row.addWidget(self.site_edit, 1)
        self.add_btn = QPushButton("Añadir", host)
        self.add_btn.setIconSize(QSize(15, 15))
        self.add_btn.setCursor(Qt.PointingHandCursor)
        self.add_btn.clicked.connect(self._add_site)
        add_row.addWidget(self.add_btn)
        self._lay.addLayout(add_row)

        self.sites_card = QFrame(host)
        self.sites_card.setProperty("card", True)
        lc = QVBoxLayout(self.sites_card)
        lc.setContentsMargins(0, 4, 0, 4)
        self.scroll = QScrollArea(self.sites_card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        # Seis filas a la vista: el sizeHint del scroll pedía la lista entera
        # y el diálogo crecía más que la ventana
        self.scroll.setFixedHeight(6 * 40 + 5)
        self._sites_body = QWidget(self.scroll)
        self.sites_box = QVBoxLayout(self._sites_body)
        self.sites_box.setContentsMargins(0, 0, 0, 0)
        self.sites_box.setSpacing(0)
        self.scroll.setWidget(self._sites_body)
        lc.addWidget(self.scroll)
        self._lay.addWidget(self.sites_card, 1)

        # Los cambios de la lista no tocan un bloqueo ya aplicado
        self.pending_row, self.pending_icon, self.pending_label = self._note_row(host)
        self.pending_label.setText("Añadir o quitar sitios se aplica la próxima vez "
                                   "que actives el enfoque; el comodín, al momento.")
        self._lay.addWidget(self.pending_row)
        self.pending_row.setVisible(False)

        note = QLabel("El bloqueo edita el archivo hosts de Windows y se limpia "
                      "solo al cerrar la app, incluso tras un cierre abrupto.", host)
        note.setFont(fonts.font(fonts.XS))
        note.setProperty("role", "muted")
        note.setWordWrap(True)
        note.setContentsMargins(4, 0, 0, 0)
        self._lay.addWidget(note)

        self.add_buttons([("Cerrar", "ghost", 0)])
        self._refresh()
        # Un bloque de la sesión anterior que el arranque no pudo poner al día
        sync_error = self.focus.get_status().get('sync_error')
        if sync_error:
            # Nada lo reintenta solo: el reintento es desactivar y activar
            sync_error = sync_error.replace(
                RETRY_SOON, "desactívalo y vuelve a activarlo en unos segundos")
            self._set_feedback("Quedó un bloqueo de la sesión anterior que no se pudo "
                               f"poner al día. {sync_error}", False)
        ThemeManager.instance().theme_changed.connect(self._restyle)
        self.finished.connect(self._disconnect_live)

    # ---- piezas ----
    @staticmethod
    def _note_row(parent: QWidget):
        """Línea de aviso: icono + texto pequeño. Devuelve (fila, icono, texto)."""
        row = QWidget(parent)
        h = QHBoxLayout(row)
        h.setContentsMargins(4, 0, 0, 0)
        h.setSpacing(8)
        icon = QLabel(row)
        icon.setFixedSize(14, 16)
        h.addWidget(icon, 0, Qt.AlignTop)
        label = QLabel(row)
        label.setFont(fonts.font(fonts.XS))
        label.setWordWrap(True)
        h.addWidget(label, 1)
        return row, icon, label

    def _sites(self) -> List[str]:
        """Dominios base de la lista, sin duplicar la variante www."""
        return sorted({normalize_site(s) for s in self.focus.get_blocked_sites()} - {""})

    # ---- estado ----
    def _restyle(self) -> None:
        self._refresh()

    def _disconnect_live(self, *_args) -> None:
        if not self._live:
            return
        self._live = False
        try:
            ThemeManager.instance().theme_changed.disconnect(self._restyle)
        except (RuntimeError, TypeError):
            pass

    def _refresh(self) -> None:
        t = tokens()
        status = self.focus.get_status()
        active = bool(status.get('active'))
        n = len(self._sites())
        sites = f"{n} sitio" if n == 1 else f"{n} sitios"
        passes = status.get('session_allowed') or []
        blocked = sum(1 for s in self._sites() if not self.focus.is_session_allowed(s))
        self.status_card.setStyleSheet(
            f"#focusStatus {{ background: {t.surface_hover if active else t.bg_header};"
            f" border: 1px solid {t.accent if active else t.border};"
            f" border-radius: {t.radius}px; }}")
        if active:
            c = QColor(t.accent)
            tile_bg = f"rgba({c.red()}, {c.green()}, {c.blue()}, 38)"
            icon, icon_color = "shield-check", t.accent
            self.status_label.setText("Enfoque activo")
            muted = status.get('notifications_muted')
            self.status_detail.setText(
                f"Bloqueando {blocked} de {sites}"
                + (" y con las notificaciones de Windows en silencio." if muted else ".")
                + (f" Comodín hasta cerrar la app: {', '.join(passes)}." if passes else ""))
            self.toggle_btn.setText("Desactivar")
            self.toggle_btn.setProperty("variant", "danger")
        else:
            tile_bg, icon, icon_color = t.surface_active, "shield", t.fg_dim
            self.status_label.setText("Enfoque desactivado")
            self.status_detail.setText(
                f"Bloquea {sites} que te distraen y silencia las notificaciones "
                "de Windows mientras trabajas."
                + (f" Comodín en esta sesión: {', '.join(passes)}." if passes else ""))
            self.toggle_btn.setText("Activar")
            self.toggle_btn.setProperty("variant", "accent")
        self.status_icon.setPixmap(tinted_icon(icon, icon_color, 22).pixmap(22, 22))
        self.status_icon.setStyleSheet(
            f"background: {tile_bg}; border-radius: {t.radius_small + 2}px;")
        # repolish para que el QSS relea la propiedad variant
        self.toggle_btn.style().unpolish(self.toggle_btn)
        self.toggle_btn.style().polish(self.toggle_btn)

        self.admin_icon.setPixmap(tinted_icon("triangle-alert", t.warning, 14).pixmap(14, 14))
        self.admin_label.setStyleSheet(f"color: {t.fg_dim};")
        # Sin permisos, «activo» solo puede ser un bloque heredado de una
        # sesión como administrador: entonces el problema es no poder quitarlo
        inherited = active and self.focus.inherited_block
        self.admin_label.setText(
            "PomoGrowth no se está ejecutando como administrador; sin esos permisos "
            + ("no se puede quitar el bloqueo que quedó de la sesión anterior."
               if inherited else "no se pueden bloquear los sitios."))
        self.admin_row.setVisible(not status.get('is_admin') and (not active or inherited))
        self.pending_icon.setPixmap(tinted_icon("info", t.fg_muted, 14).pixmap(14, 14))
        self.pending_label.setStyleSheet(f"color: {t.fg_muted};")
        self.pending_row.setVisible(active)
        self._style_feedback()
        self.add_btn.setIcon(tinted_icon("add", t.fg, 15))
        free = n - blocked
        self.count_label.setText(f"{n} · {free} {'libre' if free == 1 else 'libres'}"
                                 if free else str(n))
        self._populate_sites()

    def _set_feedback(self, message: str, ok: bool) -> None:
        self.feedback.setText(_STATUS_PREFIX.sub("", str(message)))
        self._feedback_ok = ok
        self._style_feedback()

    def _style_feedback(self) -> None:
        t = tokens()
        icon, color = (("check-circle", t.accent) if self._feedback_ok
                       else ("triangle-alert", t.warning))
        self.feedback_icon.setPixmap(tinted_icon(icon, color, 14).pixmap(14, 14))
        self.feedback.setStyleSheet(f"color: {t.fg_dim};")
        self.feedback_row.setVisible(bool(self.feedback.text()))

    # ---- lista ----
    def _populate_sites(self) -> None:
        t = tokens()
        while self.sites_box.count():
            item = self.sites_box.takeAt(0)
            if item.widget():
                item.widget().hide()
                item.widget().deleteLater()
        body = self._sites_body
        sites = self._sites()
        if not sites:
            empty = QLabel("La lista está vacía: añade los sitios que te distraen.", body)
            empty.setFont(fonts.font(fonts.SMALL))
            empty.setProperty("role", "muted")
            empty.setAlignment(Qt.AlignCenter)
            empty.setContentsMargins(16, 24, 16, 24)
            self.sites_box.addWidget(empty)
        for i, site in enumerate(sites):
            if i:
                self.sites_box.addWidget(_Divider(body))
            row = QWidget(body)
            row.setFixedHeight(40)
            h = QHBoxLayout(row)
            h.setContentsMargins(14, 0, 8, 0)
            h.setSpacing(10)
            icon = QLabel(row)
            icon.setPixmap(tinted_icon("globe", t.fg_muted, 15).pixmap(15, 15))
            h.addWidget(icon)
            # Elidido y con mínimo 0: un dominio largo nunca empuja la
            # bandera ni la papelera fuera de la lista
            name = ElidedLabel(site, row)
            name.setFont(fonts.font(fonts.SMALL))
            name.setToolTip(site)
            h.addWidget(name, 1)
            # Comodín: bandera blanca rellena mientras el sitio está libre
            allowed = self.focus.is_session_allowed(site)
            if allowed:
                tag = QLabel("Libre", row)
                tag.setFont(fonts.font(fonts.XS))
                tag.setToolTip("Libre hasta que cierres PomoGrowth (o hasta mañana)")
                tag.setStyleSheet(f"color: {t.fg}; background: {t.surface_active};"
                                  f" border-radius: {t.radius_small}px; padding: 2px 8px;")
                h.addWidget(tag, 0, Qt.AlignVCenter)
                # El comodín puede ser el del dominio padre: se nombra el real
                others = [a for a in self.focus.session_allowances_for(site) if a != site]
                tip = (f"Volver a bloquear {' y '.join(others)} y sus subdominios" if others
                       else f"Volver a bloquear {site}")
            else:
                tip = f"Comodín: permitir {site} solo hasta que cierres PomoGrowth"
            flag = IconButton(
                "flag-fill" if allowed else "flag", tip,
                color=t.fg if allowed else t.fg_muted, hover_color=t.fg, size=15, parent=row)
            flag.clicked.connect(lambda _=False, s=site, a=allowed: self._toggle_pass(s, a))
            h.addWidget(flag)
            rm = IconButton("delete", f"Quitar {site}", color=t.fg_muted,
                            hover_color=t.danger, size=15, parent=row)
            rm.clicked.connect(lambda _=False, s=site: self._remove_site(s))
            h.addWidget(rm)
            self.sites_box.addWidget(row)
        self.sites_box.addStretch(1)

    def _toggle(self) -> None:
        active = bool(self.focus.get_status().get('active'))
        if active:
            ok, msg = self.focus.deactivate()
        else:
            ok, msg = self.focus.activate()
        status = self.focus.get_status()
        if not ok:
            self._set_feedback(msg, False)
        elif status.get('active') and not self.focus.effective_blocked_sites():
            # Activo, pero sin nada que bloquear: que no pase por un éxito
            self._set_feedback(msg, False)
        elif status.get('active') and not status.get('notifications_muted'):
            self._set_feedback("Sitios bloqueados, pero no se pudieron silenciar "
                               "las notificaciones de Windows.", False)
        elif status.get('active'):
            # El éxito ya lo cuenta la tarjeta de estado (y el mensaje del
            # núcleo cuenta también las variantes www: «34 sitios»); queda
            # avisar de que lo ya abierto en el navegador tarda un poco
            self._set_feedback(OPEN_TABS_NOTE, True)
        else:
            self._set_feedback("", True)
        self._refresh()
        self.state_changed.emit(bool(self.focus.get_status().get('active')))

    def _restart_as_admin(self) -> None:
        """Relanza PomoGrowth elevado. Si Windows lo abre, esta instancia se
        cierra (el controlador, sin despedida); si se cancela la UAC, sigue.
        Con una sesión en marcha pregunta antes: el reinicio la corta."""
        warning = self.session_warning() if self.session_warning else ''
        if warning and not confirm(self, "¿Reiniciar como administrador?", warning,
                                   ok_text="Reiniciar"):
            return
        ok, msg = self.focus.request_admin_privileges()
        if not ok:
            self._set_feedback(msg, False)
            return
        self.done(0)
        self.elevated_restart.emit()

    def _add_site(self) -> None:
        raw = self.site_edit.text().strip()
        site = normalize_site(raw)
        if not site:
            return
        if not _DOMAIN.fullmatch(site):
            self._set_feedback(f"«{raw}» no parece un dominio: escribe algo como "
                               "youtube.com.", False)
            self.site_edit.selectAll()
            return
        if site in self._sites():
            self._set_feedback(f"{site} ya está en la lista.", False)
            self.site_edit.selectAll()
            return
        self.focus.add_blocked_site(site)
        self.site_edit.clear()
        self._refresh()

    def _remove_site(self, site: str) -> None:
        ok, msg = self.focus.remove_blocked_site(site)
        if not ok:
            self._set_feedback(msg, False)
        self._refresh()

    def _toggle_pass(self, site: str, allowed: bool) -> None:
        """Bandera blanca: libera el sitio hasta cerrar la app (con
        confirmación: es para casos excepcionales) o lo vuelve a bloquear."""
        if allowed:
            ok, msg = self.focus.revoke_session_allowance(site)
            self._set_feedback(f"{msg}." if ok else msg, ok)
            self._refresh()
            return
        subs = [s for s in self._sites() if s.endswith('.' + site)]
        also = f" (y {', '.join(subs)})" if subs else ""
        if not confirm(self, "¿Usar el comodín?",
                       f"{site}{also} quedará libre hasta que cierres PomoGrowth; al "
                       "volver a abrirla, o mañana, se bloquea de nuevo.\n\n"
                       "Pensado para casos excepcionales, como consultar el WhatsApp "
                       "del trabajo sin dejar la puerta abierta a distraerte.",
                       ok_text="Permitir esta sesión", icon="flag"):
            return
        ok, msg = self.focus.allow_for_session(site)
        if ok and self.focus.get_status().get('active'):
            msg += (". Si la página aún no carga, recárgala en un minuto: el navegador "
                    "recuerda las direcciones un rato.")
        elif ok:
            msg += "; se tendrá en cuenta al activar el enfoque."
        self._set_feedback(msg, ok)
        self._refresh()
