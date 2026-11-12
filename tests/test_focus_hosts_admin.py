"""Modo enfoque como administrador: hosts de solo lectura (la marca se quita
solo mientras se escribe y se repone siempre), hosts en uso (reintentos y
escritura en el sitio), mensajes según la causa del PermissionError, bloque
heredado, aviso de 0 sitios y «Reiniciar como administrador» (ShellExecuteW
falso; la instancia única y el mutex no se sueltan hasta salir, y con una
sesión en marcha se pregunta antes).

Nunca toca el hosts real ni el registro: HOSTS_PATH apunta a un archivo de
tmp_path (al que se le devuelve la escritura en el teardown), el DNS y las
notificaciones son no-ops, no se registra el atexit y ShellExecuteW es falso.
"""
import ctypes
import logging
import os
import socket
import stat
import sys
import uuid
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core.focus_mode as fm_mod  # noqa: E402
from core.config import Config  # noqa: E402
from core.focus_mode import MARKER_START, OPEN_TABS_NOTE, FocusMode  # noqa: E402
from services import singleton  # noqa: E402
from utils import win_native  # noqa: E402

from tests.test_ui_smoke import ctrl  # noqa: E402,F401  (fixture del controlador)

ORIGINAL = "# hosts de prueba\n127.0.0.1 localhost\n"
SITES = {'whatsapp.com', 'www.whatsapp.com', 'web.whatsapp.com',
         'youtube.com', 'www.youtube.com'}


@pytest.fixture
def hosts(tmp_path, monkeypatch):
    path = tmp_path / "hosts"
    path.write_text(ORIGINAL, encoding="utf-8")
    monkeypatch.setattr(fm_mod, "HOSTS_PATH", path)
    monkeypatch.setattr(FocusMode, "_flush_dns", lambda self: None)
    monkeypatch.setattr(FocusMode, "_set_toasts_enabled", lambda self, on: (True, ""))
    yield path
    # Que tmp_path se pueda borrar aunque el test lo deje de solo lectura
    if path.exists():
        os.chmod(path, stat.S_IREAD | stat.S_IWRITE)


@pytest.fixture
def sleeps(monkeypatch):
    """Sin esperas reales entre reintentos: quedan anotadas."""
    waits = []
    monkeypatch.setattr(fm_mod.time, "sleep", waits.append)
    return waits


def _as_admin(monkeypatch, admin=True):
    monkeypatch.setattr(FocusMode, "_check_admin_privileges", lambda self: admin)


def _focus(tmp_path) -> FocusMode:
    fm = FocusMode(config_file=tmp_path / "focus_config.json")
    fm._atexit_registered = True  # que el test no limpie nada al salir
    fm.blocked_sites = set(SITES)
    return fm


def _blocked(path: Path) -> set:
    return {line.split()[1] for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("127.0.0.1 ") and line.split()[1] != "localhost"}


def _readonly(path: Path) -> bool:
    return bool(os.stat(path).st_file_attributes & stat.FILE_ATTRIBUTE_READONLY)


def _set_readonly(path: Path) -> None:
    os.chmod(path, stat.S_IREAD)
    assert _readonly(path)


def _replace_failing(monkeypatch, winerror, calls=None):
    """os.replace falla con `winerror` solo al sustituir el hosts (el JSON de
    la config sigue guardándose con el os.replace de verdad)."""
    real = os.replace

    def fake(src, dst, *a, **k):
        if Path(dst) == fm_mod.HOSTS_PATH:
            if calls is not None:
                calls.append(dst)
            raise PermissionError(13, "Acceso denegado", str(dst), winerror)
        return real(src, dst, *a, **k)
    monkeypatch.setattr(fm_mod.os, "replace", fake)


# ---------- solo lectura como administrador ----------

def test_solo_lectura_como_admin_activa_y_conserva_la_marca(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    _set_readonly(hosts)
    fm = _focus(tmp_path)
    ok, msg = fm.activate()
    assert ok, msg
    assert _blocked(hosts) == SITES
    assert _readonly(hosts)


def test_solo_lectura_como_admin_desactiva(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    fm = _focus(tmp_path)
    assert fm.activate()[0]
    _set_readonly(hosts)
    ok, msg = fm.deactivate()
    assert ok, msg
    assert hosts.read_text(encoding="utf-8") == ORIGINAL
    assert _readonly(hosts)


def test_solo_lectura_comodin_con_enfoque_activo(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    fm = _focus(tmp_path)
    assert fm.activate()[0]
    _set_readonly(hosts)
    ok, msg = fm.allow_for_session("whatsapp.com")
    assert ok, msg
    assert _blocked(hosts) == {'youtube.com', 'www.youtube.com'}
    assert _readonly(hosts)


def test_solo_lectura_bloque_heredado_se_rehace_al_arrancar(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    fm.allow_for_session("whatsapp.com")       # el bloque libera whatsapp
    _set_readonly(hosts)
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and restarted.sync_error == ''
    assert _blocked(hosts) == SITES
    assert hosts.read_text(encoding="utf-8").count(MARKER_START) == 1
    assert _readonly(hosts)


def test_la_marca_se_repone_aunque_falle_la_escritura(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    _set_readonly(hosts)
    fm = _focus(tmp_path)
    real = os.replace

    def disk_full(src, dst, *a, **k):
        if Path(dst) == fm_mod.HOSTS_PATH:
            raise OSError(28, "No queda espacio en el disco")
        return real(src, dst, *a, **k)
    monkeypatch.setattr(fm_mod.os, "replace", disk_full)
    ok, msg = fm.activate()
    assert not ok and "espacio" in msg
    assert hosts.read_text(encoding="utf-8") == ORIGINAL
    assert _readonly(hosts)
    assert not (hosts.parent / "hosts.pomogrowth.tmp").exists()


def test_solo_lectura_irremediable_lo_dice_claro(tmp_path, hosts, monkeypatch, caplog):
    _as_admin(monkeypatch)
    _set_readonly(hosts)
    fm = _focus(tmp_path)

    def denied(*_a, **_k):
        raise PermissionError(13, "Acceso denegado", None, 5)
    with monkeypatch.context() as m:  # el teardown necesita el chmod de verdad
        m.setattr(fm_mod.os, "chmod", denied)
        with caplog.at_level(logging.ERROR, logger="PomoGrowth"):
            ok, msg = fm.activate()
    assert not ok
    assert "solo lectura" in msg and "Ejecuta PomoGrowth como administrador" not in msg
    assert hosts.read_text(encoding="utf-8") == ORIGINAL
    assert any("WinError 5" in r.getMessage() for r in caplog.records)


# ---------- hosts en uso y acceso denegado ----------

def test_en_uso_reintenta_y_escribe_en_el_sitio(tmp_path, hosts, sleeps, monkeypatch):
    _as_admin(monkeypatch)
    calls = []
    _replace_failing(monkeypatch, 32, calls)
    fm = _focus(tmp_path)
    ok, msg = fm.activate()
    assert ok, msg
    assert len(calls) == 1 + len(FocusMode.REPLACE_RETRY_DELAYS)
    assert sleeps == list(FocusMode.REPLACE_RETRY_DELAYS)
    assert len(sleeps) == 3 and all(0.15 <= d <= 0.3 for d in sleeps)
    assert _blocked(hosts) == SITES
    assert hosts.read_text(encoding="utf-8").startswith(ORIGINAL)
    assert not (hosts.parent / "hosts.pomogrowth.tmp").exists()


def test_en_uso_sin_remedio_avisa_en_vez_de_culpar_al_admin(tmp_path, hosts, sleeps,
                                                            monkeypatch):
    _as_admin(monkeypatch)
    _replace_failing(monkeypatch, 33)

    def busy(text, original):
        raise PermissionError(13, "En uso", str(fm_mod.HOSTS_PATH), 32)
    monkeypatch.setattr(FocusMode, "_write_in_place", staticmethod(busy))
    fm = _focus(tmp_path)
    ok, msg = fm.activate()
    assert not ok
    assert "en uso" in msg and "unos segundos" in msg
    assert "Ejecuta PomoGrowth como administrador" not in msg
    assert hosts.read_text(encoding="utf-8") == ORIGINAL


def test_acceso_denegado_como_admin_prueba_en_el_sitio(tmp_path, hosts, sleeps, monkeypatch):
    _as_admin(monkeypatch)
    _replace_failing(monkeypatch, 5)
    fm = _focus(tmp_path)
    ok, msg = fm.activate()
    assert ok, msg
    assert _blocked(hosts) == SITES
    assert sleeps == []                # acceso denegado: sin reintentos


def test_denegado_como_admin_da_el_detalle(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    _replace_failing(monkeypatch, 5)

    def denied(text, original):
        raise PermissionError(13, "Acceso denegado", str(fm_mod.HOSTS_PATH), 5)
    monkeypatch.setattr(FocusMode, "_write_in_place", staticmethod(denied))
    fm = _focus(tmp_path)
    ok, msg = fm.activate()
    assert not ok
    assert "aun como administrador" in msg and "Detalle" in msg


def test_sin_admin_pide_abrir_como_administrador(tmp_path, hosts, sleeps, monkeypatch,
                                                caplog):
    _as_admin(monkeypatch, False)
    calls = []
    _replace_failing(monkeypatch, 5, calls)
    fm = _focus(tmp_path)
    with caplog.at_level(logging.ERROR, logger="PomoGrowth"):
        ok, msg = fm.activate()
    assert not ok and "Ejecuta PomoGrowth como administrador" in msg
    assert len(calls) == 1 and sleeps == []          # sin admin no insiste
    assert hosts.read_text(encoding="utf-8") == ORIGINAL
    errors = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert any("WinError 5" in r.getMessage() and "admin=False" in r.getMessage()
               for r in errors)


@pytest.mark.parametrize("action", ["deactivate", "allow"])
def test_cada_rama_permission_error_queda_en_el_log(tmp_path, hosts, monkeypatch, caplog,
                                                     action):
    _as_admin(monkeypatch, False)
    fm = _focus(tmp_path)
    assert fm.activate()[0]

    def denied(self, text, original):
        raise PermissionError(13, "Acceso denegado", None, 5)
    monkeypatch.setattr(FocusMode, "_write_hosts", denied)
    with caplog.at_level(logging.ERROR, logger="PomoGrowth"):
        ok, msg = (fm.deactivate() if action == "deactivate"
                   else fm.allow_for_session("whatsapp.com"))
    assert not ok and "administrador" in msg
    assert any("WinError 5" in r.getMessage() for r in caplog.records)


# ---------- estado: bloque heredado y 0 sitios ----------

def test_bloque_heredado_que_no_se_pudo_rehacer_guarda_el_motivo(tmp_path, hosts,
                                                                monkeypatch, qtbot):
    _as_admin(monkeypatch, False)
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    _set_readonly(hosts)                      # sin admin: no se puede reescribir
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and "administrador" in restarted.sync_error
    assert restarted.get_status()['sync_error'] == restarted.sync_error
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, restarted)
    qtbot.addWidget(dlg)
    assert "sesión anterior" in dlg.feedback.text() and not dlg._feedback_ok
    dlg.done(0)
    # activate() no responde «ya está activo» sin mirar: vuelve a sincronizar
    os.chmod(hosts, stat.S_IREAD | stat.S_IWRITE)
    ok, msg = restarted.activate()
    assert ok and "ya está activo" not in msg
    assert _blocked(hosts) == SITES and restarted.sync_error == ''
    assert not restarted.inherited_block


def test_activar_con_bloque_heredado_lo_vuelve_a_sincronizar(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and restarted.inherited_block
    # Otra herramienta toca el bloque mientras tanto
    hosts.write_text(hosts.read_text(encoding="utf-8").replace(
        "127.0.0.1 youtube.com\n", "").replace("::1 youtube.com\n", ""), encoding="utf-8")
    assert 'youtube.com' not in FocusMode._block_sites(hosts.read_text(encoding="utf-8"))
    ok, msg = restarted.activate()
    assert ok and "ya está activo" not in msg
    assert _blocked(hosts) == SITES
    # Ya no es heredado: un segundo activate() sí es «ya está activo»
    assert restarted.activate() == (True, "El modo enfoque ya está activo")


def test_bloque_heredado_sin_admin_ofrece_reiniciar_como_administrador(
        tmp_path, hosts, monkeypatch, qtbot):
    """Sin permisos, un bloque heredado no se puede quitar: el botón de
    reinicio tiene que verse, con un texto que lo diga (no «bloquear»)."""
    _as_admin(monkeypatch, False)
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    _set_readonly(hosts)                      # sin admin: no se puede tocar
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and restarted.inherited_block
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, restarted)
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.admin_row.isVisibleTo(dlg) and dlg.admin_btn.isVisibleTo(dlg)
    assert "quitar el bloqueo que quedó de la sesión anterior" in dlg.admin_label.text()
    dlg.toggle_btn.click()                    # «Desactivar» falla sin permisos
    assert restarted.is_active and dlg.admin_btn.isVisibleTo(dlg)
    dlg.done(0)
    # Un enfoque activado en esta sesión no ofrece el reinicio
    os.chmod(hosts, stat.S_IREAD | stat.S_IWRITE)
    assert restarted.deactivate()[0]
    assert restarted.activate()[0] and not restarted.inherited_block
    dlg2 = fd.FocusDialog(None, restarted)
    qtbot.addWidget(dlg2)
    dlg2.show()
    assert not dlg2.admin_row.isVisibleTo(dlg2)
    dlg2.done(0)
    assert restarted.deactivate()[0]
    dlg3 = fd.FocusDialog(None, restarted)    # inactivo: el texto de siempre
    qtbot.addWidget(dlg3)
    assert "no se pueden bloquear los sitios" in dlg3.admin_label.text()
    dlg3.done(0)


def test_bloque_heredado_al_dia_tras_reescribirlo_borra_el_aviso(tmp_path, hosts,
                                                                monkeypatch, qtbot):
    """El arranque no pudo rehacer el bloque (hosts en uso): el aviso no
    promete un reintento que no existe, y si después el bloque se reescribe
    bien (un comodín), el aviso de la sesión anterior desaparece."""
    _as_admin(monkeypatch)
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    real_write = FocusMode._write_hosts
    busy = [True]

    def write(self, text, original):
        if busy[0]:
            raise PermissionError(13, "En uso", str(fm_mod.HOSTS_PATH), 32)
        return real_write(self, text, original)
    monkeypatch.setattr(FocusMode, "_write_hosts", write)
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and "en uso" in restarted.sync_error
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, restarted)
    qtbot.addWidget(dlg)
    assert "sesión anterior" in dlg.feedback.text()
    assert "desactívalo y vuelve a activarlo" in dlg.feedback.text()
    assert fm_mod.RETRY_SOON not in dlg.feedback.text()
    dlg.done(0)
    busy[0] = False                           # el antivirus lo suelta
    ok, msg = restarted.allow_for_session("youtube.com")
    assert ok, msg
    assert _blocked(hosts) == {'whatsapp.com', 'www.whatsapp.com', 'web.whatsapp.com'}
    assert restarted.sync_error == ''
    dlg2 = fd.FocusDialog(None, restarted)
    qtbot.addWidget(dlg2)
    assert "sesión anterior" not in dlg2.feedback.text()
    dlg2.done(0)


def test_todos_con_comodin_avisa_de_cero_sitios(tmp_path, hosts, monkeypatch, qtbot):
    fm = _focus(tmp_path)
    fm.allow_for_session("whatsapp.com")
    fm.allow_for_session("youtube.com")
    assert fm.effective_blocked_sites() == []
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    dlg.toggle_btn.click()
    assert fm.is_active
    assert "no bloquea ningún sitio" in dlg.feedback.text()
    assert "comodín" in dlg.feedback.text() and not dlg._feedback_ok
    assert OPEN_TABS_NOTE not in dlg.feedback.text()
    dlg.done(0)


def test_lista_vacia_tambien_avisa(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.blocked_sites = set()
    ok, msg = fm.activate()
    assert ok and "no bloquea ningún sitio" in msg and "vacía" in msg


def test_al_activar_avisa_de_las_pestanas_abiertas(tmp_path, hosts, qtbot):
    fm = _focus(tmp_path)
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    dlg.toggle_btn.click()
    assert fm.is_active
    assert dlg.feedback.text() == OPEN_TABS_NOTE and dlg._feedback_ok
    dlg.toggle_btn.click()                     # al desactivar no queda la nota
    assert not fm.is_active and dlg.feedback.text() == ""
    dlg.done(0)
    fm2 = _focus(tmp_path)
    assert OPEN_TABS_NOTE in fm2.activate()[1]


# ---------- instancia única, mutex y «Reiniciar como administrador» ----------

def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def instance(monkeypatch):
    """Instancia única en un puerto libre y un mutex con nombre propio del
    test (nunca los de la app)."""
    monkeypatch.setattr(Config, "SINGLETON_PORT", _free_port())
    monkeypatch.setattr(Config, "APP_MUTEX", f"PomoGrowth.Test.{uuid.uuid4().hex}")
    assert singleton.acquire()
    assert win_native.hold_named_mutex(Config.APP_MUTEX)
    yield
    _exit_process()


def _exit_process() -> None:
    """Lo que hace Windows al terminar el proceso: suelta el puerto de la
    instancia única y cierra el handle del mutex."""
    if singleton._socket is not None:
        singleton._socket.close()
        singleton._socket = None
    if win_native._mutex:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(win_native._mutex))
        win_native._mutex = None


def _mutex_exists(name: str) -> bool:
    open_mutex = ctypes.windll.kernel32.OpenMutexW
    open_mutex.restype = ctypes.c_void_p
    open_mutex.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    handle = open_mutex(0x00100000, False, name)  # SYNCHRONIZE, como Inno
    if handle:
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))
    return bool(handle)


def _port_taken() -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(("127.0.0.1", Config.SINGLETON_PORT))
        return False
    except OSError:
        return True
    finally:
        s.close()


def test_singleton_una_sola_instancia_hasta_salir(monkeypatch):
    monkeypatch.setattr(Config, "SINGLETON_PORT", _free_port())
    try:
        assert singleton.acquire()
        assert _port_taken()                       # una segunda instancia no puede
        assert not singleton.acquire()
        _exit_process()                            # la primera sale
        assert not _port_taken()
        assert singleton.acquire()
    finally:
        _exit_process()


def test_acquire_waiting_espera_a_que_la_otra_suelte_el_puerto(monkeypatch):
    import threading
    monkeypatch.setattr(Config, "SINGLETON_PORT", _free_port())
    other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    other.bind(("127.0.0.1", Config.SINGLETON_PORT))
    try:
        assert not singleton.acquire_waiting(0.2, 0.05)
        threading.Timer(0.2, other.close).start()
        assert singleton.acquire_waiting(5.0, 0.05)
    finally:
        other.close()
        _exit_process()


def test_la_elevada_abre_el_mutex_de_la_anterior_y_lo_mantiene(monkeypatch):
    """La instancia elevada llama a hold_named_mutex con el mutex de la
    anterior aún vivo: CreateMutexW da un handle con ERROR_ALREADY_EXISTS, que
    no es un fallo, y al salir la anterior el mutex sigue existiendo."""
    name = f"PomoGrowth.Test.{uuid.uuid4().hex}"
    create = ctypes.windll.kernel32.CreateMutexW
    create.restype = ctypes.c_void_p
    create.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    previous = create(None, False, name)           # la instancia anterior
    assert previous
    monkeypatch.setattr(win_native, "_mutex", None)
    try:
        assert win_native.hold_named_mutex(name)
        assert win_native._mutex and win_native._mutex != previous
        ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(previous))  # sale
        assert _mutex_exists(name)                 # el desinstalador la sigue viendo
    finally:
        _exit_process()
    assert not _mutex_exists(name)


def _fake_shell_execute(monkeypatch, result):
    calls = []

    def fake(hwnd, verb, exe, params, cwd, show):
        calls.append({'verb': verb, 'exe': exe, 'params': params,
                      'port_free': not _port_taken(), 'mutex': win_native._mutex})
        return result
    monkeypatch.setattr(fm_mod.ctypes.windll.shell32, "ShellExecuteW", fake)
    return calls


def test_request_admin_uac_cancelada(tmp_path, hosts, monkeypatch, instance):
    _as_admin(monkeypatch, False)
    calls = _fake_shell_execute(monkeypatch, 5)
    fm = _focus(tmp_path)
    ok, msg = fm.request_admin_privileges()
    assert not ok and "UAC" in msg
    assert len(calls) == 1 and calls[0]['verb'] == "runas"
    # Nunca se soltaron: nadie pudo colarse mientras estaba el aviso de UAC
    assert not calls[0]['port_free'] and calls[0]['mutex']
    assert singleton._socket is not None and _port_taken()
    assert win_native._mutex == calls[0]['mutex'] and _mutex_exists(Config.APP_MUTEX)


def test_request_admin_otro_error_da_el_codigo(tmp_path, hosts, monkeypatch, instance):
    _as_admin(monkeypatch, False)
    _fake_shell_execute(monkeypatch, 2)
    ok, msg = _focus(tmp_path).request_admin_privileges()
    assert not ok and "código 2" in msg
    assert singleton._socket is not None and win_native._mutex


def test_request_admin_congelada_sin_la_ruta_del_exe(tmp_path, hosts, monkeypatch, instance):
    _as_admin(monkeypatch, False)
    exe = r"C:\Program Files\PomoGrowth\PomoGrowth.exe"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", exe)
    monkeypatch.setattr(sys, "argv", [exe, "--minimized", "--elevated-restart"])
    calls = _fake_shell_execute(monkeypatch, 42)
    ok, msg = _focus(tmp_path).request_admin_privileges()
    assert ok, msg
    assert calls[0]['exe'] == exe
    assert calls[0]['params'] == "--minimized --elevated-restart"   # sin duplicar
    assert "PomoGrowth.exe" not in calls[0]['params']
    # Éxito: se conservan hasta salir; la elevada espera con acquire_waiting
    # a que esta guarde y se cierre (nada se suelta antes de ShellExecuteW)
    assert not calls[0]['port_free'] and calls[0]['mutex']
    assert singleton._socket is not None and _port_taken()
    assert win_native._mutex == calls[0]['mutex'] and _mutex_exists(Config.APP_MUTEX)


def test_request_admin_en_desarrollo_con_espacios(tmp_path, hosts, monkeypatch, instance):
    _as_admin(monkeypatch, False)
    script = r"C:\Mis apps\PomoGrowth\main.py"
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setattr(sys.modules['__main__'], "__file__", script, raising=False)
    monkeypatch.setattr(sys, "argv", [script])
    calls = _fake_shell_execute(monkeypatch, 42)
    assert _focus(tmp_path).request_admin_privileges()[0]
    assert calls[0]['exe'] == sys.executable
    assert calls[0]['params'] == f'"{script}" --elevated-restart'


def test_request_admin_ya_siendo_admin_no_relanza(tmp_path, hosts, monkeypatch):
    _as_admin(monkeypatch)
    calls = _fake_shell_execute(monkeypatch, 42)
    assert _focus(tmp_path).request_admin_privileges()[0]
    assert calls == []


# ---------- el botón del diálogo y la salida sin despedida ----------

def test_boton_reiniciar_como_administrador(tmp_path, hosts, monkeypatch, qtbot):
    _as_admin(monkeypatch, False)
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    fm = _focus(tmp_path)
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    dlg.show()
    assert dlg.admin_row.isVisibleTo(dlg) and dlg.admin_btn.isVisibleTo(dlg)
    assert dlg.admin_btn.text() == "Reiniciar como administrador"
    restarts = []
    dlg.elevated_restart.connect(lambda: restarts.append(1))
    # UAC cancelada: se queda abierto con el motivo
    monkeypatch.setattr(fm, "request_admin_privileges",
                        lambda: (False, "Cancelaste el aviso de Windows (UAC)"))
    dlg.admin_btn.click()
    assert restarts == [] and "UAC" in dlg.feedback.text() and dlg.isVisible()
    # Windows la abre: el diálogo se cierra y avisa al controlador
    monkeypatch.setattr(fm, "request_admin_privileges",
                        lambda: (True, "Reiniciando con permisos de administrador..."))
    dlg.admin_btn.click()
    assert restarts == [1] and not dlg.isVisible()


def test_siendo_admin_no_hay_boton_de_reinicio(tmp_path, hosts, monkeypatch, qtbot):
    _as_admin(monkeypatch)
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    dlg = fd.FocusDialog(None, _focus(tmp_path))
    qtbot.addWidget(dlg)
    dlg.show()
    assert not dlg.admin_btn.isVisibleTo(dlg)
    dlg.done(0)


def test_reinicio_como_admin_sale_sin_despedida(ctrl, monkeypatch):  # noqa: F811
    """El botón del diálogo cierra la app por la ruta normal (limpieza del
    enfoque incluida) pero sin la ventana de despedida."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    from ui.windows import welcome_farewell
    farewells, cleanups, quits = [], [], []
    monkeypatch.setattr(welcome_farewell, "FarewellWindow",
                        lambda on_done: farewells.append(on_done))
    monkeypatch.setattr(ctrl.focus_mode, "cleanup_system_blocks",
                        lambda: cleanups.append(1) or False)
    monkeypatch.setattr(ctrl.focus_mode, "is_admin", False)
    monkeypatch.setattr(ctrl.focus_mode, "request_admin_privileges",
                        lambda: (True, "Reiniciando con permisos de administrador..."))
    monkeypatch.setattr(QApplication.instance(), "quit", lambda: quits.append(1))
    seen = []

    def click():
        dlg = QApplication.activeModalWidget()
        seen.append(dlg)
        dlg.admin_btn.click()

    def failsafe():
        dlg = QApplication.activeModalWidget()
        if dlg is not None and hasattr(dlg, "admin_btn"):
            seen.append("atascado")
            dlg.done(0)
    QTimer.singleShot(100, click)
    QTimer.singleShot(3000, failsafe)
    ctrl.open_focus()
    assert len(seen) == 1 and "atascado" not in seen
    assert ctrl._is_closing and cleanups == [1] and quits == [1] and farewells == []
    assert not ctrl.window.isVisible()


# ---------- con una sesión en marcha se pregunta antes de reiniciar ----------

def test_reiniciar_con_sesion_en_marcha_pregunta_antes(tmp_path, hosts, monkeypatch, qtbot):
    _as_admin(monkeypatch, False)
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    fm = _focus(tmp_path)
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    dlg.show()
    asked, launched, restarts = [], [], []
    answer = [False]

    def fake_confirm(parent, title, message, **kw):
        asked.append(message)
        return answer[0]
    monkeypatch.setattr(fd, "confirm", fake_confirm)
    monkeypatch.setattr(fm, "request_admin_privileges",
                        lambda: launched.append(1) or (True, "Reiniciando..."))
    dlg.elevated_restart.connect(lambda: restarts.append(1))
    warning = "Tienes un pomodoro en marcha: el tiempo que llevas se guarda."
    dlg.session_warning = lambda: warning
    dlg.admin_btn.click()                     # cancela: no se lanza la UAC
    assert asked == [warning] and launched == [] and restarts == [] and dlg.isVisible()
    answer[0] = True
    dlg.admin_btn.click()
    assert len(asked) == 2 and launched == [1] and restarts == [1]
    # Sin sesión en marcha no pregunta
    dlg2 = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg2)
    dlg2.session_warning = lambda: ''
    dlg2.admin_btn.click()
    assert len(asked) == 2 and launched == [1, 1]


def test_aviso_de_la_sesion_que_corta_el_reinicio(ctrl):  # noqa: F811
    m = ctrl.model
    assert ctrl._admin_restart_warning() == ''              # nada en marcha
    m.remaining_time = m.pomodoro_duration - 600             # pomodoro en pausa a mitad
    text = ctrl._admin_restart_warning()
    assert text.startswith("Tienes un pomodoro en marcha: el tiempo que llevas se guarda, "
                           "pero el pomodoro no se completa.")
    assert text.endswith("¿Reiniciar PomoGrowth como administrador?")
    m.is_break_time = True                                   # descanso recién empezado
    m.remaining_time = ctrl._session_total = m.break_duration
    assert ctrl._admin_restart_warning() == ''
    m.remaining_time -= 60                                   # descanso a mitad
    assert "Tienes un descanso en marcha" in ctrl._admin_restart_warning()
    m.is_break_time = False
    m.remaining_time = m.pomodoro_duration
    m.timer_kind = 'free'
    assert ctrl._admin_restart_warning() == ''
    m.free_elapsed = 120.0                                   # sesión libre con tiempo
    text = ctrl._admin_restart_warning()
    assert "sesión libre en marcha" in text and "se termina y se guarda" in text
    assert f"{ctrl.rewards.FREE_SESSION_MIN_MINUTES} min" in text
    m.free_elapsed = 0.0
    m.timer_kind = 'pomodoro'


def test_reinicio_como_admin_con_pomodoro_a_medias_cancelado_no_sale(ctrl, monkeypatch):  # noqa: F811
    """El controlador conecta la consulta: con un pomodoro a medias el botón
    pregunta, y «Cancelar» no lanza la UAC ni cierra nada."""
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    import ui.dialogs.focus_dialog as fd
    ctrl.model.remaining_time = ctrl.model.pomodoro_duration - 900
    asked, launched = [], []
    monkeypatch.setattr(fd, "confirm", lambda parent, title, message, **kw:
                        asked.append(message) or False)
    monkeypatch.setattr(ctrl.focus_mode, "is_admin", False)
    monkeypatch.setattr(ctrl.focus_mode, "request_admin_privileges",
                        lambda: launched.append(1) or (True, "Reiniciando..."))

    def click():
        dlg = QApplication.activeModalWidget()
        dlg.admin_btn.click()
        dlg.done(0)
    QTimer.singleShot(100, click)
    ctrl.open_focus()
    assert len(asked) == 1 and "pomodoro en marcha" in asked[0]
    assert launched == [] and not ctrl._is_closing
