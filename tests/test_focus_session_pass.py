"""Comodín del modo enfoque (bandera blanca): un sitio queda libre solo hasta
cerrar la app (también si se cerró de golpe) o hasta el día siguiente; y el
hosts se reescribe sin truncarlo ni cambiar sus finales de línea.

Nunca toca el hosts real ni el registro: HOSTS_PATH apunta a un archivo
temporal, el DNS y las notificaciones son no-ops y no se registra el atexit.
"""
import os
import stat
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import core.focus_mode as fm_mod  # noqa: E402
from core.focus_mode import MARKER_START, FocusMode  # noqa: E402

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
    return path


def _focus(tmp_path) -> FocusMode:
    fm = FocusMode(config_file=tmp_path / "focus_config.json")
    fm._atexit_registered = True  # que el test no limpie nada al salir
    fm.blocked_sites = set(SITES)
    return fm


def _blocked(path: Path) -> set:
    return {line.split()[1] for line in path.read_text(encoding="utf-8").splitlines()
            if line.startswith("127.0.0.1 ") and line.split()[1] != "localhost"}


def test_comodin_con_enfoque_activo_libera_al_momento(tmp_path, hosts):
    fm = _focus(tmp_path)
    assert fm.activate()[0]
    assert _blocked(hosts) == SITES
    ok, msg = fm.allow_for_session("https://www.WhatsApp.com/")
    assert ok and "whatsapp.com" in msg
    # El dominio, su www y sus subdominios de la lista quedan libres
    assert _blocked(hosts) == {'youtube.com', 'www.youtube.com'}
    text = hosts.read_text(encoding="utf-8")
    assert text.startswith(ORIGINAL) and text.count(MARKER_START) == 1
    assert fm.get_status()['session_allowed'] == ['whatsapp.com']


def test_volver_a_bloquear_desde_un_subdominio(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    assert fm.is_session_allowed("web.whatsapp.com")
    ok, msg = fm.revoke_session_allowance("web.whatsapp.com")
    assert ok and fm.get_session_allowed() == []
    assert msg == "whatsapp.com y sus subdominios vuelven a estar bloqueados"
    assert _blocked(hosts) == SITES


def test_solo_el_subdominio_no_libera_el_dominio(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("web.whatsapp.com")
    assert _blocked(hosts) == SITES - {'web.whatsapp.com'}


def test_comodin_sin_enfoque_se_aplica_al_activar(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.allow_for_session("youtube.com")
    assert hosts.read_text(encoding="utf-8") == ORIGINAL  # nada que reescribir aún
    fm.activate()
    assert _blocked(hosts) == {'whatsapp.com', 'www.whatsapp.com', 'web.whatsapp.com'}


def test_no_se_guarda_y_al_reiniciar_vuelve_a_bloquearse(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    fm.save_config()
    assert "whatsapp" not in (tmp_path / "focus_config.json").read_text(
        encoding="utf-8").split('"blocked_sites"')[0]
    assert "session" not in (tmp_path / "focus_config.json").read_text(encoding="utf-8")
    # Cierre de la app (limpieza) y nuevo arranque: sin comodines
    assert fm.cleanup_system_blocks()
    restarted = _focus(tmp_path)
    assert restarted.get_session_allowed() == []
    restarted.activate()
    assert _blocked(hosts) == SITES


def test_caduca_al_cambiar_de_dia(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    assert not fm.expire_session_allowances(today=date.today())
    assert fm.expire_session_allowances(today=date.today() + timedelta(days=1))
    assert fm.session_allowed == set()
    assert _blocked(hosts) == SITES


def test_si_falla_el_hosts_no_queda_un_comodin_fantasma(tmp_path, hosts, monkeypatch):
    fm = _focus(tmp_path)
    fm.activate()
    monkeypatch.setattr(FocusMode, "_rewrite_block",
                        lambda self: (False, "se requieren permisos de administrador"))
    ok, msg = fm.allow_for_session("whatsapp.com")
    assert not ok and "administrador" in msg
    assert fm.session_allowed == set()


def test_quitar_el_sitio_retira_su_comodin(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.allow_for_session("youtube.com")
    fm.remove_blocked_site("youtube.com")
    assert fm.session_allowed == set()


def test_bandera_blanca_en_el_dialogo(qtbot, tmp_path, hosts, monkeypatch):
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    from ui.widgets.icon_button import IconButton
    monkeypatch.setattr(fd, "confirm", lambda *a, **k: True)
    fm = _focus(tmp_path)
    fm.activate()
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)

    def flag(site):
        tips = (f"Comodín: permitir {site} solo hasta que cierres PomoGrowth",
                f"Volver a bloquear {site}")
        # La lista se reconstruye en cada cambio: buscar el botón vigente
        return next(b for b in dlg.findChildren(IconButton)
                    if b.toolTip() in tips and b.isVisibleTo(dlg))

    flag("whatsapp.com").click()
    assert fm.get_session_allowed() == ['whatsapp.com']
    assert "whatsapp.com queda libre" in dlg.feedback.text()
    assert "Comodín hasta cerrar la app: whatsapp.com" in dlg.status_detail.text()
    # El subdominio nombra el comodín real: el del dominio padre
    assert "Volver a bloquear whatsapp.com y sus subdominios" in [
        b.toolTip() for b in dlg.findChildren(IconButton) if b.isVisibleTo(dlg)]
    assert dlg.count_label.text() == "3 · 2 libres"
    # Volver a bloquear: sin confirmación
    monkeypatch.setattr(fd, "confirm", lambda *a, **k: pytest.fail("no debe preguntar"))
    flag("whatsapp.com").click()
    assert fm.get_session_allowed() == []
    assert _blocked(hosts) == SITES
    dlg.done(0)


def test_cancelar_el_comodin_no_cambia_nada(qtbot, tmp_path, hosts, monkeypatch):
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    from ui.widgets.icon_button import IconButton
    monkeypatch.setattr(fd, "confirm", lambda *a, **k: False)
    fm = _focus(tmp_path)
    fm.activate()
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    btn = next(b for b in dlg.findChildren(IconButton)
               if b.toolTip() == "Comodín: permitir youtube.com solo hasta que cierres PomoGrowth")
    btn.click()
    assert fm.session_allowed == set() and _blocked(hosts) == SITES
    dlg.done(0)


def test_cierre_brusco_no_deja_el_comodin_al_reiniciar(tmp_path, hosts):
    """Proceso matado (o corte de luz) con el enfoque activo y un comodín: al
    volver a abrir, el bloque heredado se rehace con la lista entera."""
    fm = _focus(tmp_path)
    fm.save_config()
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    assert _blocked(hosts) == {'youtube.com', 'www.youtube.com'}
    # Sin quit_with_farewell ni atexit: otra sesión sobre el mismo hosts
    restarted = FocusMode(config_file=tmp_path / "focus_config.json")
    restarted._atexit_registered = True
    assert restarted.is_active and restarted.get_session_allowed() == []
    assert _blocked(hosts) == SITES
    assert hosts.read_text(encoding="utf-8").count(MARKER_START) == 1


def test_activar_con_un_bloque_viejo_lo_ajusta(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.is_active = False        # la memoria dice inactivo, pero el bloque sigue
    fm.blocked_sites.add('reddit.com')
    assert fm.activate()[0]
    assert _blocked(hosts) == SITES | {'reddit.com'}


def test_quitar_un_dominio_con_comodin_vuelve_a_bloquear_sus_subdominios(tmp_path, hosts):
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    assert fm.remove_blocked_site("whatsapp.com")[0]
    # web.whatsapp.com sigue en la lista: sin comodín, y el hosts lo bloquea
    assert not fm.is_session_allowed("web.whatsapp.com")
    assert _blocked(hosts) == {'web.whatsapp.com', 'youtube.com', 'www.youtube.com'}


def test_caducar_sin_poder_escribir_no_miente_y_se_reintenta(tmp_path, hosts, monkeypatch):
    monkeypatch.setattr(FocusMode, "EXPIRE_RETRY_S", 0)
    # Sin admin: la marca de solo lectura no se toca y la escritura falla
    # (como administrador se quitaría solo mientras se escribe)
    monkeypatch.setattr(FocusMode, "_check_admin_privileges", lambda self: False)
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    tomorrow = date.today() + timedelta(days=1)
    os.chmod(hosts, stat.S_IREAD)                    # hosts de solo lectura
    try:
        assert not fm.expire_session_allowances(today=tomorrow)
        # El hosts aún lo libera: la app no lo da por bloqueado y avisa
        assert fm.is_session_allowed("whatsapp.com") and fm.expire_error
        assert _blocked(hosts) == {'youtube.com', 'www.youtube.com'}
    finally:
        os.chmod(hosts, stat.S_IREAD | stat.S_IWRITE)
    assert fm.expire_session_allowances(today=tomorrow)       # reintento
    assert fm.session_allowed == set() and fm.expire_error == ''
    assert _blocked(hosts) == SITES


class _HalfWrite:
    """Archivo que escribe un trozo y falla (como un disco lleno)."""

    def __init__(self, f):
        self._f = f

    def write(self, text):
        self._f.write(text[:20])
        self._f.flush()
        raise OSError(28, "No queda espacio en el disco")

    def flush(self):
        self._f.flush()

    def fileno(self):
        return self._f.fileno()

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        self._f.close()


def test_un_fallo_a_mitad_de_escritura_no_trunca_el_hosts(tmp_path, hosts, monkeypatch):
    with open(hosts, "a", encoding="utf-8") as f:
        f.write("10.0.0.5 intranet.empresa.local\n")
    fm = _focus(tmp_path)
    fm.activate()
    before = hosts.read_bytes()
    failures = []
    real_open = open

    def fake_open(file, mode='r', *args, **kwargs):
        f = real_open(file, mode, *args, **kwargs)
        # Fallan el temporal y la escritura en el sitio; la restauración, no
        if 'w' in mode and len(failures) < 2:
            failures.append(file)
            return _HalfWrite(f)
        return f
    with monkeypatch.context() as m:
        m.setattr(fm_mod, "open", fake_open, raising=False)
        ok, msg = fm.allow_for_session("whatsapp.com")
    assert not ok and "espacio" in msg and len(failures) == 2
    assert hosts.read_bytes() == before                    # intacto, con el bloque
    assert fm.session_allowed == set()
    assert not (hosts.parent / "hosts.pomogrowth.tmp").exists()


@pytest.mark.parametrize("eol", ["\n", "\r\n"])
def test_se_conservan_los_finales_de_linea(tmp_path, hosts, eol):
    original = f"# hosts de prueba{eol}192.168.1.10 nas.local{eol}".encode()
    hosts.write_bytes(original)
    fm = _focus(tmp_path)
    fm.activate()
    fm.allow_for_session("whatsapp.com")
    raw = hosts.read_bytes()
    assert raw.startswith(original)
    assert raw.count(b"\r\n") == (raw.count(b"\n") if eol == "\r\n" else 0)
    assert fm.deactivate()[0]
    assert hosts.read_bytes() == original


def test_dominio_largo_libre_no_esconde_la_bandera_ni_la_papelera(qtbot, tmp_path, hosts):
    from PySide6.QtCore import QPoint
    from ui.theme import fonts
    fonts.init_fonts()
    import ui.dialogs.focus_dialog as fd
    from ui.widgets.icon_button import IconButton
    long_site = "campus-virtual.universidad-nacional-de-ingenieria.edu.pe"
    fm = _focus(tmp_path)
    fm.blocked_sites.add(long_site)
    fm.activate()
    fm.allow_for_session(long_site)
    dlg = fd.FocusDialog(None, fm)
    qtbot.addWidget(dlg)
    dlg.show()
    qtbot.wait(50)
    viewport = dlg.scroll.viewport()
    buttons = [b for b in dlg._sites_body.findChildren(IconButton) if b.isVisibleTo(dlg)]
    assert len(buttons) == 2 * len(dlg._sites())
    for b in buttons:
        right = b.mapTo(viewport, QPoint(b.width(), 0)).x()
        assert right <= viewport.width(), b.toolTip()
    tags = [w.text() for w in dlg._sites_body.findChildren(fd.QLabel) if w.text() == "Libre"]
    assert tags == ["Libre"]
    dlg.done(0)
