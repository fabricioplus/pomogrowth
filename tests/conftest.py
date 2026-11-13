"""Configuración de pytest para `python -m pytest tests`.

test_core_parity.py es un script (`python tests/test_core_parity.py`): hace su
comprobación al importarse, lee los datos reales y necesita la carpeta de v8,
así que pytest no debe recolectarlo.

Ningún test hace ruido: los sonidos del sistema del timer (winsound) quedan
anulados y la música de los tests va silenciada (QAudioOutput en mute).
Ninguno toca el hosts real: el modo enfoque apunta a uno temporal (que no
existe salvo que el test lo cree). La pregunta de esfuerzo al completar una
tarea viene apagada (los tests que la prueban la encienden).

Ninguno sale a internet: la guarda de red (tests/fakes/net.py, GuardaDeRed)
bloquea toda petición real fuera de loopback (127.0.0.1, localhost, ::1) por
requests (Session.request/send: también requests.delete y compañía), urllib
y httpx, y hace fallar el test que la intentó. Para simular Asana, ClickUp,
n8n… se usa el fixture `fake_net`.
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

collect_ignore = ["test_core_parity.py"]


# --- Red: guarda para toda la sesión (también cubre los hilos que sigan
# vivos entre un test y el siguiente) y comprobación por test ---
@pytest.fixture(scope="session", autouse=True)
def _guarda_de_red_sesion():
    from tests.fakes.net import GuardaDeRed, describe
    with pytest.MonkeyPatch.context() as mp:
        guard = GuardaDeRed().install(mp)
        yield guard
    orphans = guard.orphans()
    if orphans:
        pytest.fail("Peticiones a internet bloqueadas fuera de un test (¿un hilo "
                    "que siguió vivo?):\n" + describe(orphans), pytrace=False)


@pytest.fixture(autouse=True)
def red_bloqueada(_guarda_de_red_sesion, request):
    """La guarda de red, para el test en curso. Un test que provoca un
    bloqueo a propósito lo retira con `red_bloqueada.take()`."""
    from tests.fakes.net import describe
    guard = _guarda_de_red_sesion
    guard.start(request.node.nodeid)
    yield guard
    attempts = guard.finish()
    if attempts:
        pytest.fail("El test intentó salir a internet (bloqueado). Usa el fixture "
                    "fake_net:\n" + describe(attempts), pytrace=False)


@pytest.fixture
def fake_net(monkeypatch):
    """Red falsa instalada (tests/fakes/net.py). Falla el test si alguna
    petición no tenía ruta o agotó su secuencia, aunque el código de la app
    se tragara el error."""
    from tests.fakes.net import FakeNet
    net = FakeNet().install(monkeypatch)
    yield net
    if net.errors:
        pytest.fail("FakeNet recibió peticiones sin respuesta preparada:\n"
                    + "\n".join(net.errors), pytrace=False)


@pytest.fixture(autouse=True)
def _sin_sonidos_del_sistema(monkeypatch):
    from services import sound_service
    monkeypatch.setattr(sound_service, "play", lambda kind: None)


@pytest.fixture(autouse=True)
def _hosts_de_prueba(tmp_path, monkeypatch):
    import core.focus_mode as focus_mode
    monkeypatch.setattr(focus_mode, "HOSTS_PATH", tmp_path / "hosts_de_prueba")


@pytest.fixture(autouse=True)
def _sin_pregunta_de_esfuerzo(monkeypatch):
    from ui.controllers.app_controller import AppController
    monkeypatch.setattr(AppController, "ASK_EFFORT_DEFAULT", False)


# Sonidos del temporizador: el botón «Probar» de Ajustes tampoco suena (los
# tests de tests/test_timer_sounds.py restauran el servicio real con dobles)
@pytest.fixture(autouse=True)
def _sin_probar_sonidos(monkeypatch):
    from services import sound_service
    monkeypatch.setattr(sound_service, "preview", lambda *args, **kwargs: None)


# --- Sonidos de ambiente: reproductores falsos en toda la suite (ningún
# QMediaPlayer de verdad, nada suena; ver tests/ambient_fakes.py) ---
@pytest.fixture(autouse=True)
def _ambiente_sin_reproductores_reales(monkeypatch):
    from services import ambient_service
    from tests.ambient_fakes import FakeVoice
    monkeypatch.setattr(ambient_service, "VOICE_FACTORY", FakeVoice)
    FakeVoice.created.clear()
