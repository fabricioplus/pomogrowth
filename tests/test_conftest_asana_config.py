"""Bloque «asana y config en tests» de tests/conftest.py: nada de un test
se filtra al siguiente.

- Un AsanaService que ya sincronizó no se libera solo: su temporizador de
  reintento podría vencer y salir a la red en medio de otro test. Al acabar
  cada test se apagan (shutdown) todos los creados durante él.
- Config.N8N_WEBHOOK_URL es de clase: el DataHandler y AppController (al
  arrancar y en apply_settings, al guardar Ajustes) la escriben. Al acabar
  cada test vuelve a su valor.

Los tests van en este orden (el 2 y el 3 miran lo que dejó el 1); si
corren sin el 1, no tienen nada que comprobar y pasan.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_conftest_asana_config.py -q
"""
import json
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from core.config import Config  # noqa: E402
from core.data_handler import DataHandler  # noqa: E402
from services.asana_service import AsanaService  # noqa: E402

_DEJADO = {}   # lo que el primer test deja «olvidado» para el segundo


def test_1_deja_un_reintento_programado_y_la_url_de_n8n_cambiada(qtbot, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = tmp_path / "data.json"
    path.write_text(json.dumps({'tasks': [], 'settings': {}}), encoding="utf-8")
    svc = AsanaService(DataHandler(path))
    svc._schedule_retry(sync=True)          # como tras un listado con 503
    assert svc.retry_scheduled
    _DEJADO['url'] = Config.N8N_WEBHOOK_URL
    Config.N8N_WEBHOOK_URL = "https://n8n.ejemplo/webhook/de-un-test"   # como apply_settings
    _DEJADO['svc'] = svc                     # el test no lo apaga


def test_2_el_servicio_del_test_anterior_esta_apagado(qtbot):
    svc = _DEJADO.pop('svc', None)
    if svc is not None:
        assert not svc.retry_scheduled and svc._closing


def test_3_la_url_de_n8n_vuelve_a_su_valor(qtbot):
    assert Config.N8N_WEBHOOK_URL == _DEJADO.pop('url', Config.N8N_WEBHOOK_URL)
