"""Configuración de pytest para `python -m pytest tests`.

test_core_parity.py es un script (`python tests/test_core_parity.py`): hace su
comprobación al importarse, lee los datos reales y necesita la carpeta de v8,
así que pytest no debe recolectarlo.

Ningún test hace ruido: los sonidos del sistema del timer (winsound) quedan
anulados y la música de los tests va silenciada (QAudioOutput en mute).
Ninguno toca el hosts real: el modo enfoque apunta a uno temporal (que no
existe salvo que el test lo cree). La pregunta de esfuerzo al completar una
tarea viene apagada (los tests que la prueban la encienden).
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

collect_ignore = ["test_core_parity.py"]


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
