"""Modo enfoque consolidado: bloqueo de sitios por archivo hosts + silenciado de notificaciones.

Única implementación del modo enfoque. Antes convivían dos (FocusMode y
EnhancedFocusMode): la "enhanced" corrompía el archivo hosts escribiendo
saltos de línea literales, creaba reglas de firewall con dominios donde
netsh exige IPs y configuraba un proxy PAC inerte. Se conserva solo el
método fiable (hosts), con limpieza garantizada de los bloques corruptos
que la versión antigua pudiera haber dejado.

Comodín (bandera blanca): un sitio de la lista puede quedar libre solo
durante esta sesión de la app (p. ej. el WhatsApp del trabajo para sacar un
dato). Vive en memoria a propósito, nunca en focus_config.json: al reiniciar
PomoGrowth (también tras un cierre brusco: el bloque que quedó en el hosts se
rehace con la lista entera), o al cambiar de día con la app abierta, vuelve
a bloquearse.

El hosts se reescribe sin dejarlo nunca a medias (temporal + os.replace) y
conservando sus finales de línea. Si otro programa lo marcó como solo lectura,
la marca se quita solo mientras se escribe y se repone siempre después.
"""
import atexit
import ctypes
import errno
import logging
import os
import re
import stat
import subprocess
import sys
import time
import winreg
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from services.singleton import ELEVATED_RESTART_FLAG
from utils.safe_json import save_json_atomic, load_json_safe

logger = logging.getLogger("PomoGrowth")

HOSTS_PATH = Path(r"C:\Windows\System32\drivers\etc\hosts")
MARKER_START = "# PomoGrowth Focus Mode - START"
MARKER_END = "# PomoGrowth Focus Mode - END"
# surrogateescape y newline='' garantizan reescribir el hosts byte a byte
# aunque tenga caracteres fuera de UTF-8 o finales LF (el archivo no es
# nuestro; no debemos alterarlo).
HOSTS_ENCODING = {"encoding": "utf-8", "errors": "surrogateescape", "newline": ""}
# WinError 32/33 (ERROR_SHARING_VIOLATION / ERROR_LOCK_VIOLATION): otro programa
# (antivirus, una herramienta de hosts) tiene el archivo abierto un instante
IN_USE_WINERRORS = (32, 33)
ACCESS_DENIED_WINERROR = 5
# ShellExecuteW devuelve 5 (SE_ERR_ACCESSDENIED) si se cancela el aviso de UAC
SE_ERR_ACCESSDENIED = 5
# Final del aviso de hosts en uso (el diálogo lo cambia para el bloque heredado)
RETRY_SOON = "inténtalo en unos segundos"
# Tras activar: el navegador sigue un rato con lo que ya tenía abierto
OPEN_TABS_NOTE = ("Las pestañas ya abiertas pueden tardar un poco en bloquearse: "
                  "recárgalas o reinicia el navegador.")


class HostsReadOnlyError(PermissionError):
    """El hosts está marcado como solo lectura y Windows no deja quitar la marca."""


def _eol(content: str) -> str:
    """Final de línea que predomina en el hosts (el de Windows si está vacío)."""
    crlf = content.count("\r\n")
    lf = content.count("\n") - crlf
    if crlf or lf:
        return "\r\n" if crlf >= lf else "\n"
    return os.linesep


class FocusMode:
    """Bloquea distracciones: sitios web (archivo hosts) y notificaciones de Windows."""

    DEFAULT_BLOCKED_SITES = [
        'facebook.com', 'www.facebook.com',
        'youtube.com', 'www.youtube.com', 'm.youtube.com',
        'tiktok.com', 'www.tiktok.com',
        'instagram.com', 'www.instagram.com',
        'twitter.com', 'www.twitter.com', 'x.com',
        'reddit.com', 'www.reddit.com', 'old.reddit.com',
        'twitch.tv', 'www.twitch.tv',
        'netflix.com', 'www.netflix.com',
        'discord.com', 'www.discord.com',
        'whatsapp.com', 'web.whatsapp.com',
    ]

    def __init__(self, config_file: Path = Path('focus_config.json')):
        self.config_file = config_file
        self.blocked_sites: Set[str] = set(self.DEFAULT_BLOCKED_SITES)
        self.allowed_apps: List[str] = []
        self.notifications_muted = False
        self.blocking_method = 'hosts'
        self.is_admin = self._check_admin_privileges()
        self._atexit_registered = False
        # Comodines de esta sesión (dominios base) y el día en que valen
        self.session_allowed: Set[str] = set()
        self._session_day = date.today()
        # Caducidad que no se pudo aplicar al hosts: motivo y próximo intento
        self.expire_error = ''
        self._expire_retry_at = 0.0
        # Bloque heredado (encontrado al arrancar, sin pasar por activate())
        # y, si no se pudo rehacer, el motivo para enseñarlo en el diálogo
        self.inherited_block = False
        self.sync_error = ''
        self.load_config()
        # Estado real, no asumido: si el hosts aún contiene nuestro bloque
        # (p. ej. la app se cerró de golpe con el enfoque activo), reflejarlo
        # para que deactivate()/cleanup_system_blocks() puedan limpiarlo.
        self.is_active = self._hosts_has_block()
        self.inherited_block = self.is_active
        if self.is_active:
            # Ese bloque puede llevar aplicados comodines de la sesión que
            # murió (sitios sin bloquear que la app mostraría bloqueados):
            # se rehace con la lista entera
            ok, msg = self._sync_block()
            if not ok:
                self.sync_error = msg
                logger.error("No se pudo rehacer el bloqueo heredado: %s", msg)

    # ------------------------------------------------------------------
    # Permisos
    # ------------------------------------------------------------------
    def _check_admin_privileges(self) -> bool:
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (AttributeError, OSError):
            return False

    def request_admin_privileges(self) -> Tuple[bool, str]:
        """Relanza PomoGrowth elevado (UAC). No suelta antes la instancia
        única ni el mutex: la elevada espera con acquire_waiting a que esta
        se cierre (y guarde) y abre el mismo mutex. Con True, quien llama
        cierra esta instancia (sin despedida: la elevada ya se está abriendo)."""
        if self.is_admin:
            return True, "Ya tienes permisos de administrador"
        exe, params = self._elevated_command()
        try:
            # > 32 es éxito; ShellExecuteW no lanza: devuelve el código
            result = int(ctypes.windll.shell32.ShellExecuteW(
                None, "runas", exe, params, None, 1))
        except (AttributeError, OSError, ValueError) as e:
            logger.error("ShellExecuteW para reiniciar como administrador: %s", e)
            result = 0
        if result > 32:
            logger.info("Reinicio como administrador lanzado: %s %s", exe, params)
            return True, "Reiniciando con permisos de administrador..."
        logger.warning("Reinicio como administrador no lanzado (ShellExecuteW → %s)", result)
        if result == SE_ERR_ACCESSDENIED:
            return False, ("Cancelaste el aviso de Windows (UAC): PomoGrowth sigue abierto "
                           "sin permisos de administrador.")
        return False, ("Windows no pudo reiniciar PomoGrowth como administrador "
                       f"(código {result}). Ciérralo y ábrelo con «Ejecutar como administrador».")

    @staticmethod
    def _elevated_command() -> Tuple[str, str]:
        """(ejecutable, parámetros) para relanzar esta misma app con la
        bandera del reinicio. Congelada, sys.argv[0] es el propio exe y no va
        en los parámetros; en desarrollo, el intérprete con el script y sus
        argumentos. Comillas de Windows con list2cmdline (rutas con espacios)."""
        args = [a for a in sys.argv[1:] if a != ELEVATED_RESTART_FLAG]
        if getattr(sys, 'frozen', False):
            return sys.executable, subprocess.list2cmdline(args + [ELEVATED_RESTART_FLAG])
        main = sys.modules.get('__main__')
        script = os.path.abspath(getattr(main, '__file__', None) or sys.argv[0])
        return sys.executable, subprocess.list2cmdline([script] + args + [ELEVATED_RESTART_FLAG])

    # ------------------------------------------------------------------
    # Configuración
    # ------------------------------------------------------------------
    def load_config(self):
        data, error = load_json_safe(self.config_file, default=None)
        if error:
            logger.error("Config de enfoque no recuperable: %s", error)
            return
        if isinstance(data, dict):
            self.blocked_sites = set(data.get('blocked_sites', self.DEFAULT_BLOCKED_SITES))
            self.allowed_apps = data.get('allowed_apps', [])

    def save_config(self):
        data = {
            'blocked_sites': sorted(self.blocked_sites),
            'allowed_apps': self.allowed_apps,
            'blocking_method': self.blocking_method,
            'last_activation': datetime.now().isoformat() if self.is_active else None,
        }
        if not save_json_atomic(self.config_file, data):
            logger.error("No se pudo guardar %s", self.config_file)

    # ------------------------------------------------------------------
    # Activación / desactivación
    # ------------------------------------------------------------------
    def activate(self) -> Tuple[bool, str]:
        # Con un bloque heredado no basta con «ya está activo»: se vuelve a
        # sincronizar (_block_websites lo ajusta a la lista) y se completa la
        # activación (notificaciones y limpieza al salir)
        if self.is_active and not self.inherited_block:
            return True, "El modo enfoque ya está activo"
        self.expire_session_allowances()
        ok, hosts_msg = self._block_websites()
        if not ok:
            if self.inherited_block:
                self.sync_error = hosts_msg
            return False, hosts_msg
        notif_ok, notif_msg = self._mute_notifications()
        self.is_active = True
        self.inherited_block = False
        self.sync_error = ''
        # Red de seguridad: al terminar el proceso (cierre normal, excepción
        # fatal…) nunca dejar el hosts bloqueado ni las notificaciones
        # silenciadas. El bloqueo por hosts es persistente en el sistema; sin
        # esto, cerrar la app con el enfoque activo lo dejaba "pegado".
        if not self._atexit_registered:
            atexit.register(self._atexit_cleanup)
            self._atexit_registered = True
        self.save_config()
        if not self.effective_blocked_sites():
            why = ("todos los sitios de la lista tienen comodín" if self.blocked_sites
                   else "la lista de sitios está vacía")
            return True, f"⚠ Modo enfoque activado, pero no bloquea ningún sitio: {why}."
        detail = f"{len(self.effective_blocked_sites())} sitios bloqueados"
        if not notif_ok:
            detail += f" · {notif_msg}"
        return True, f"✓ Modo enfoque activado ({detail}). {OPEN_TABS_NOTE}"

    def deactivate(self) -> Tuple[bool, str]:
        """Desactiva el enfoque. Limpia el hosts aunque el estado interno diga inactivo."""
        ok, hosts_msg = self._unblock_websites()
        self._unmute_notifications()
        if not ok:
            return False, hosts_msg
        self.is_active = False
        self.inherited_block = False
        self.sync_error = ''
        self.save_config()
        return True, "✓ Modo enfoque desactivado"

    def cleanup_system_blocks(self) -> bool:
        """Limpieza idempotente de TODO rastro del enfoque en el sistema.

        Reconsulta el hosts real (el estado en memoria puede estar
        desactualizado si otra instancia activó el enfoque), quita nuestro
        bloque si existe y restaura las notificaciones. Pensada para el
        cierre de la app, el hook atexit y la reparación al arrancar.
        Devuelve True si había un bloqueo y se limpió.
        """
        had_block = self._hosts_has_block()
        cleaned = False
        if had_block:
            ok, _ = self._unblock_websites()
            cleaned = ok
        if had_block or self.notifications_muted:
            self._unmute_notifications()
        if cleaned or not had_block:
            self.is_active = False
            self.inherited_block = False
            self.sync_error = ''
            self.save_config()
        return cleaned

    def _atexit_cleanup(self):
        """Hook de salida del proceso: no puede lanzar excepciones."""
        try:
            self.cleanup_system_blocks()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Bloqueo por archivo hosts
    # ------------------------------------------------------------------
    def _read_hosts(self) -> str:
        if not HOSTS_PATH.exists():
            return ""
        with open(HOSTS_PATH, 'r', **HOSTS_ENCODING) as f:
            return f.read()

    def _hosts_has_block(self) -> bool:
        try:
            return MARKER_START in self._read_hosts()
        except OSError:
            return False

    @staticmethod
    def _render_block(sites: List[str], eol: str = "\n") -> str:
        lines = [f"{eol}{MARKER_START}{eol}"]
        for site in sites:
            lines.append(f"127.0.0.1 {site}{eol}")
            lines.append(f"::1 {site}{eol}")
        lines.append(f"{MARKER_END}{eol}")
        return "".join(lines)

    @staticmethod
    def _block_sites(content: str) -> Set[str]:
        """Sitios que bloquea nuestro bloque del hosts."""
        sites: Set[str] = set()
        inside = False
        for line in content.splitlines():
            if MARKER_START in line and MARKER_END not in line:
                inside = True
            elif MARKER_END in line:
                inside = False
            elif inside:
                parts = line.split()
                if len(parts) >= 2:
                    sites.add(parts[1].lower())
        return sites

    @staticmethod
    def _strip_block(content: str) -> str:
        """El hosts sin nuestro bloque (ni la línea en blanco que lo precede)."""
        filtered = []
        skip = False
        for line in content.splitlines(keepends=True):
            has_start = MARKER_START in line
            has_end = MARKER_END in line
            if has_start and has_end:
                # Línea única corrupta dejada por la versión antigua
                # (escribía el bloque entero con \n literales): descartarla.
                continue
            if has_start:
                skip = True
                # Quitar también la línea en blanco que nuestro bloque
                # añade antes del marcador (evita que se acumulen).
                if filtered and filtered[-1].strip() == "":
                    filtered.pop()
                continue
            if has_end:
                skip = False
                continue
            if not skip:
                filtered.append(line)
        return "".join(filtered)

    # Esperas entre reintentos de os.replace con el hosts en uso: con tope
    # (se escribe desde el hilo de la UI), 0,65 s en el peor caso
    REPLACE_RETRY_DELAYS = (0.15, 0.2, 0.3)

    def _write_hosts(self, text: str, original: str) -> None:
        """Único punto de escritura del hosts (bloquear, desbloquear, comodín
        y sincronía). Si otro programa lo marcó como solo lectura, como
        administrador se quita la marca solo mientras se escribe y se repone
        siempre al terminar, también si la escritura falla: el hosts final la
        conserva. Lanza OSError (PermissionError sin permisos; HostsReadOnlyError
        si no se pudo quitar la marca) si no se pudo."""
        readonly = self._unset_readonly()
        try:
            self._replace_hosts(text, original)
        finally:
            if readonly:
                self._restore_readonly()

    def _unset_readonly(self) -> bool:
        """Quita la marca de solo lectura del hosts si la tiene. Solo como
        administrador: sin permisos Windows no deja escribir el hosts igual y
        el aviso correcto es el de administrador. True si la quitó."""
        if not self.is_admin:
            return False
        try:
            readonly = os.stat(HOSTS_PATH).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
        except (OSError, AttributeError):
            return False  # no existe (se creará) o no es Windows
        if not readonly:
            return False
        try:
            os.chmod(HOSTS_PATH, stat.S_IWRITE)  # en Windows solo toca la marca R
        except OSError as e:
            raise HostsReadOnlyError(
                errno.EACCES, f"no se pudo quitar la marca de solo lectura ({e})",
                str(HOSTS_PATH), getattr(e, 'winerror', None) or ACCESS_DENIED_WINERROR) from e
        logger.info("El hosts es de solo lectura: marca quitada mientras se escribe")
        return True

    @staticmethod
    def _restore_readonly() -> None:
        try:
            os.chmod(HOSTS_PATH, stat.S_IREAD)
        except OSError as e:
            logger.error("No se pudo reponer la marca de solo lectura del hosts: %s", e)

    def _replace_hosts(self, text: str, original: str) -> None:
        """Reescribe el hosts entero sin dejarlo nunca a medias: un temporal en
        la misma carpeta que sustituye al original de una vez (os.replace).
        Si ahí no se pueden crear archivos (permiso solo sobre el hosts), se
        escribe en el sitio y, si falla a mitad, se restaura `original`. Si
        os.replace choca con el hosts en uso, se reintenta unas veces; si
        sigue en uso, o deniega el acceso siendo administrador, se prueba a
        escribir en el sitio antes de rendirse."""
        tmp = HOSTS_PATH.with_name(HOSTS_PATH.name + ".pomogrowth.tmp")
        try:
            with open(tmp, 'w', **HOSTS_ENCODING) as f:
                f.write(text)
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            self._discard(tmp)
            self._write_in_place(text, original)
            return
        error: Optional[OSError] = None
        for delay in (*self.REPLACE_RETRY_DELAYS, None):
            try:
                os.replace(tmp, HOSTS_PATH)
                return
            except OSError as e:
                error = e
                if delay is None or getattr(e, 'winerror', None) not in IN_USE_WINERRORS:
                    break
                logger.warning("Hosts en uso (WinError %s): reintento en %d ms",
                               e.winerror, round(delay * 1000))
                time.sleep(delay)
        self._discard(tmp)
        winerror = getattr(error, 'winerror', None)
        if winerror in IN_USE_WINERRORS or (winerror == ACCESS_DENIED_WINERROR and self.is_admin):
            try:
                self._write_in_place(text, original)
                logger.info("Hosts escrito en el sitio tras fallar os.replace (WinError %s)",
                            winerror)
                return
            except OSError as e:
                logger.warning("Tampoco se pudo escribir el hosts en el sitio: %s", e)
        raise error

    @staticmethod
    def _write_in_place(text: str, original: str) -> None:
        f = open(HOSTS_PATH, 'w', **HOSTS_ENCODING)  # sin permisos falla aquí, sin tocarlo
        try:
            with f:
                f.write(text)
        except OSError:
            # Falló a mitad (disco lleno, antivirus): dejarlo como estaba
            try:
                with open(HOSTS_PATH, 'w', **HOSTS_ENCODING) as g:
                    g.write(original)
            except OSError as e:
                logger.error("No se pudo restaurar el hosts tras un fallo: %s", e)
            raise

    @staticmethod
    def _discard(path: Path) -> None:
        try:
            path.unlink()
        except OSError:
            pass

    def _permission_message(self, e: OSError, accion: str) -> str:
        """Mensaje de un PermissionError al escribir el hosts según su causa
        (antes era siempre «Ejecuta como administrador», también siéndolo) y
        su rastro en el log con el código de Windows."""
        winerror = getattr(e, 'winerror', None)
        logger.error("%s: PermissionError (WinError %s, admin=%s): %s",
                     accion, winerror, self.is_admin, e, exc_info=e)
        if winerror in IN_USE_WINERRORS:
            return (f"{accion}: otro programa (antivirus o una herramienta de hosts) tiene "
                    f"el archivo hosts en uso; {RETRY_SOON}.")
        if not self.is_admin:
            return (f"{accion}: se requieren permisos de administrador. "
                    "Ejecuta PomoGrowth como administrador.")
        if isinstance(e, HostsReadOnlyError):
            return (f"{accion}: el archivo hosts está marcado como solo lectura (lo protegió "
                    "otro programa) y Windows no deja quitar esa marca. Quítala en las "
                    f"propiedades de {HOSTS_PATH} y vuelve a intentarlo.")
        return (f"{accion}: Windows denegó escribir el hosts aun como administrador "
                f"(antivirus o protección del hosts). Detalle: {e}")

    def _block_websites(self) -> Tuple[bool, str]:
        try:
            content = self._read_hosts()
            if MARKER_START in content:
                # Bloque de una sesión anterior: se ajusta a la lista actual
                ok, msg = self._sync_block()
                return (True, "Los sitios ya estaban bloqueados") if ok else (False, msg)
            sites = self.effective_blocked_sites()
            self._write_hosts(content + self._render_block(sites, _eol(content)), content)
            self._flush_dns()
            logger.info("Modo enfoque: %d sitios bloqueados en hosts", len(sites))
            return True, "Sitios bloqueados"
        except PermissionError as e:
            return False, self._permission_message(e, "No se pudieron bloquear los sitios")
        except OSError as e:
            logger.error("Error bloqueando sitios en hosts: %s", e)
            return False, f"Error bloqueando sitios: {e}"

    def _unblock_websites(self) -> Tuple[bool, str]:
        try:
            content = self._read_hosts()
            if MARKER_START not in content:
                return True, "No había sitios bloqueados"
            self._write_hosts(self._strip_block(content), content)
            self._flush_dns()
            logger.info("Modo enfoque: bloqueos eliminados del hosts")
            return True, "Sitios desbloqueados"
        except PermissionError as e:
            return False, self._permission_message(e, "No se pudieron desbloquear los sitios")
        except OSError as e:
            logger.error("Error desbloqueando sitios del hosts: %s", e)
            return False, f"Error desbloqueando sitios: {e}"

    def _sync_block(self) -> Tuple[bool, str]:
        """Con nuestro bloque en el hosts, que bloquee exactamente la lista
        efectiva (solo reescribe si difiere)."""
        try:
            current = self._block_sites(self._read_hosts())
        except OSError as e:
            return False, f"Error leyendo el hosts: {e}"
        if current == set(self.effective_blocked_sites()):
            return True, "Bloqueo al día"
        return self._rewrite_block()

    def _rewrite_block(self) -> Tuple[bool, str]:
        """Con el enfoque activo, cambia nuestro bloque por la lista efectiva en
        una sola escritura (el comodín se nota al momento, sin desactivar)."""
        try:
            original = self._read_hosts()
            sites = self.effective_blocked_sites()
            self._write_hosts(self._strip_block(original)
                              + self._render_block(sites, _eol(original)), original)
            self._flush_dns()
            # El bloque ya coincide con la lista: el fallo del arranque quedó atrás
            self.sync_error = ''
            logger.info("Modo enfoque: bloqueo actualizado (%d sitios)", len(sites))
            return True, "Bloqueo actualizado"
        except PermissionError as e:
            return False, self._permission_message(e, "No se pudo cambiar el bloqueo")
        except OSError as e:
            logger.error("Error actualizando el bloqueo del hosts: %s", e)
            return False, f"Error actualizando el bloqueo: {e}"

    def _flush_dns(self):
        try:
            subprocess.run(
                ['ipconfig', '/flushdns'],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
                timeout=15,
            )
        except (OSError, subprocess.SubprocessError) as e:
            logger.warning("No se pudo limpiar la caché DNS: %s", e)

    # ------------------------------------------------------------------
    # Notificaciones de Windows
    # ------------------------------------------------------------------
    def _set_toasts_enabled(self, enabled: bool) -> Tuple[bool, str]:
        key_path = r"Software\Microsoft\Windows\CurrentVersion\Notifications\Settings"
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0,
                                winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, "NOC_GLOBAL_SETTING_TOASTS_ENABLED", 0,
                                  winreg.REG_DWORD, 1 if enabled else 0)
            return True, ""
        except OSError as e:
            action = "restaurar" if enabled else "silenciar"
            logger.warning("No se pudieron %s las notificaciones: %s", action, e)
            return False, f"no se pudieron {action} las notificaciones"

    def _mute_notifications(self) -> Tuple[bool, str]:
        ok, msg = self._set_toasts_enabled(False)
        if ok:
            self.notifications_muted = True
        return ok, msg

    def _unmute_notifications(self) -> Tuple[bool, str]:
        ok, msg = self._set_toasts_enabled(True)
        if ok:
            self.notifications_muted = False
        return ok, msg

    # ------------------------------------------------------------------
    # Lista de sitios
    # ------------------------------------------------------------------
    def add_blocked_site(self, site: str):
        """Añade un sitio (con y sin www). Aplica al (re)activar el enfoque."""
        site = site.lower().strip()
        if not site:
            return
        self.blocked_sites.add(site)
        if not site.startswith('www.'):
            self.blocked_sites.add(f'www.{site}')
        self.save_config()

    def remove_blocked_site(self, site: str) -> Tuple[bool, str]:
        """Quita el sitio (y su www): se aplica al (re)activar el enfoque. Su
        comodín se retira y, si liberaba subdominios que siguen en la lista,
        con el enfoque activo se rehace el bloqueo al momento (si no, la app
        los daría por bloqueados estando libres). → (ok, mensaje)."""
        site = site.lower().strip()
        base = self._base_domain(site)
        self.blocked_sites.discard(site)
        self.blocked_sites.discard(f'www.{site}')
        ok, msg = True, ""
        if base in self.session_allowed:
            self.session_allowed.discard(base)
            if self.is_active and any(self._covers(base, s) for s in self.blocked_sites):
                ok, msg = self._rewrite_block()
                if not ok:
                    self.session_allowed.add(base)  # el hosts aún los libera
        self.save_config()
        return ok, msg

    def get_blocked_sites(self) -> List[str]:
        """Lista de sitios sin duplicar la variante www."""
        unique_sites = []
        processed = set()
        for site in sorted(self.blocked_sites):
            base_site = site.replace('www.', '')
            if base_site not in processed:
                unique_sites.append(site)
                processed.add(base_site)
        return unique_sites

    def get_status(self) -> Dict:
        return {
            'active': self.is_active,
            'is_admin': self.is_admin,
            'blocked_sites_count': len(self.blocked_sites),
            'notifications_muted': self.notifications_muted,
            'blocking_method': self.blocking_method,
            'session_allowed': self.get_session_allowed(),
            'sync_error': self.sync_error,
        }

    # ------------------------------------------------------------------
    # Comodín de sesión (bandera blanca)
    # ------------------------------------------------------------------
    @staticmethod
    def _base_domain(site: str) -> str:
        """«https://www.WhatsApp.com/x» → «whatsapp.com» (como la lista del diálogo)."""
        s = site.strip().lower()
        s = re.sub(r"^[a-z][a-z0-9+.-]*://", "", s)
        s = re.split(r"[/?#]", s, maxsplit=1)[0]
        s = s.rsplit("@", 1)[-1].split(":", 1)[0].strip(".")
        return s[4:] if s.startswith("www.") else s

    @staticmethod
    def _covers(allowed: str, host: str) -> bool:
        """¿El comodín de `allowed` libera `host`? Cubre el dominio, su www y
        sus subdominios (whatsapp.com libera también web.whatsapp.com)."""
        return host == allowed or host.endswith('.' + allowed)

    def is_session_allowed(self, host: str) -> bool:
        host = host.lower().strip()
        return any(self._covers(a, host) for a in self.session_allowed)

    def session_allowances_for(self, host: str) -> List[str]:
        """Los comodines que liberan `host` (el suyo o el de un dominio padre)."""
        host = self._base_domain(host)
        return sorted(a for a in self.session_allowed if self._covers(a, host))

    def effective_blocked_sites(self) -> List[str]:
        """Lo que de verdad va al hosts: la lista menos los comodines."""
        return sorted(s for s in self.blocked_sites if not self.is_session_allowed(s))

    def get_session_allowed(self) -> List[str]:
        self.expire_session_allowances()
        return sorted(self.session_allowed)

    def allow_for_session(self, site: str) -> Tuple[bool, str]:
        """Libera `site` (y sus subdominios) hasta cerrar la app o hasta
        mañana. Con el enfoque activo se aplica al momento."""
        site = self._base_domain(site)
        if not site:
            return False, "No hay ningún sitio que permitir"
        self.expire_session_allowances()
        if site in self.session_allowed:
            return True, f"{site} ya estaba permitido en esta sesión"
        self.session_allowed.add(site)
        if self.is_active:
            ok, msg = self._rewrite_block()
            if not ok:
                self.session_allowed.discard(site)
                return False, msg
        return True, f"{site} queda libre hasta que cierres PomoGrowth"

    def revoke_session_allowance(self, site: str) -> Tuple[bool, str]:
        """Vuelve a bloquear `site`: retira los comodines que lo cubren (el
        mensaje nombra el comodín real si era el de un dominio padre)."""
        host = self._base_domain(site)
        covering = {a for a in self.session_allowed if self._covers(a, host)}
        if not covering:
            return True, f"{host} no tenía comodín"
        self.session_allowed -= covering
        if self.is_active:
            ok, msg = self._rewrite_block()
            if not ok:
                self.session_allowed |= covering
                return False, msg
        if covering == {host}:
            return True, f"{host} vuelve a estar bloqueado"
        return True, f"{' y '.join(sorted(covering))} y sus subdominios vuelven a estar bloqueados"

    EXPIRE_RETRY_S = 60

    def expire_session_allowances(self, today: Optional[date] = None) -> bool:
        """Al cambiar de día los comodines caducan (la app puede pasar la
        noche en la bandeja). Devuelve True si había alguno y se retiró.
        Si el hosts no se deja reescribir, los comodines siguen (el hosts aún
        libera esos sitios: la app no debe darlos por bloqueados), el motivo
        queda en expire_error y se reintenta como mucho cada EXPIRE_RETRY_S."""
        today = today or date.today()
        if today == self._session_day:
            return False
        if not self.session_allowed:
            self._session_day = today
            return False
        if self.is_active:
            now = time.monotonic()
            if now < self._expire_retry_at:
                return False
            kept = set(self.session_allowed)
            self.session_allowed.clear()
            ok, msg = self._rewrite_block()
            if not ok:
                self.session_allowed = kept
                self._expire_retry_at = now + self.EXPIRE_RETRY_S
                self.expire_error = msg
                logger.warning("Modo enfoque: no se pudieron caducar los comodines: %s", msg)
                return False
        self.session_allowed.clear()
        self._session_day = today
        self._expire_retry_at = 0.0
        self.expire_error = ''
        logger.info("Modo enfoque: comodines caducados por cambio de día")
        return True
