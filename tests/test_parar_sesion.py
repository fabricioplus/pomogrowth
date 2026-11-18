"""«Parar»: terminar la sesión donde está y que el tiempo cuente.

Pomodoro: aparece al pausar a mitad (también tras una suspensión); registra el
tramo, vuelve al inicio con los puntos del ciclo y no completa nada (sin XP ni
monedas, sin descanso, sin Asana, sin n8n, sin mascota ni pregunta de
esfuerzo), con un aviso de lo que pasó. Libre: aparece con la sesión en marcha
o en pausa y la termina como siempre (finish_free_session); «Reiniciar» y la
bandera de «Terminar sesión» ya no están. Nunca en el descanso (ahí está
«Saltar descanso») y nunca a la vez que él. También en la mini ventana y en la
bandeja, y el play no se mueve al aparecer.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_parar_sesion.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import pytest

from tests.test_free_mode import (ID1, ID_ASANA, POMODORO_MIN, _focus_chunks,  # noqa: F401
                                  _go_free, _work, clock, ctrl, toasts)


def _stop_shown(ctrl) -> bool:
    return ctrl.timer_panel.stop_btn.isVisibleTo(ctrl.timer_panel)


def _pause_mid_pomodoro(ctrl, clock, task=ID1, minutes=10):
    ctrl.select_task(task)
    ctrl.toggle_timer()
    _work(ctrl, clock, minutes * 60)
    ctrl.toggle_timer()


def _to_break(ctrl, qtbot, clock):
    """Pomodoro completo: descanso listo, con «Saltar» ya a la vista."""
    tp = ctrl.timer_panel
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, POMODORO_MIN * 60)
    assert ctrl.model.is_break_time
    qtbot.waitUntil(lambda: tp.skip_btn.isVisibleTo(tp), timeout=3000)  # tras el crossfade
    qtbot.wait(500)  # el aviso de fin (400 ms) dentro del test, con la ventana viva


# ---------------------------------------------------------------- Pomodoro
def test_parar_aparece_solo_al_pausar_un_pomodoro_a_mitad(ctrl, clock):
    tp = ctrl.timer_panel
    ctrl.select_task(ID1)
    assert not _stop_shown(ctrl)                       # fresco: nada que parar
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    assert not _stop_shown(ctrl)                       # corriendo: no
    ctrl.toggle_timer()
    assert _stop_shown(ctrl)                           # pausado a mitad: sí
    assert tp.stop_btn.toolTip().startswith("Parar: termina este pomodoro")
    assert tp.stop_btn.accessibleName() == "Parar"
    assert tp.reset_btn.isVisibleTo(tp)                # «Reiniciar» sigue
    ctrl.toggle_timer()
    assert not _stop_shown(ctrl)                       # al reanudar se va


def test_parar_pomodoro_guarda_el_tiempo_sin_completarlo(ctrl, qtbot, clock, toasts,
                                                         monkeypatch):
    monkeypatch.setattr(ctrl, "_asana_sync_allowed", lambda: True)
    monkeypatch.setattr(ctrl, "effort_prompt_enabled", lambda: True)
    side = []
    monkeypatch.setattr(ctrl, "send_data_to_n8n", lambda payload: side.append(payload))
    monkeypatch.setattr(ctrl, "_pet_react", lambda *a, **k: side.append(a))
    monkeypatch.setattr(ctrl, "_ask_effort", lambda task: side.append(task))
    ctrl.model.pomodoro_cycle_count = 2
    before = ctrl.rewards.get_stats()
    _pause_mid_pomodoro(ctrl, clock, ID_ASANA, minutes=10)
    ctrl.toggle_timer()
    _work(ctrl, clock, 5 * 60)
    ctrl.toggle_timer()
    del toasts[:]
    ctrl.timer_panel.stop_btn.click()
    qtbot.wait(500)                                    # nada diferido detrás
    chunks = _focus_chunks(ctrl)
    assert [round(c['duration']) for c in chunks] == [600, 300]
    assert not any(c.get('cycle_completed') or c.get('mode') for c in chunks)
    m = ctrl.model
    assert not m.is_running and not m.is_break_time
    assert m.remaining_time == POMODORO_MIN * 60
    assert m.pomodoro_cycle_count == 2                 # los puntos del ciclo siguen
    assert m.last_pomodoro_task_id is None             # ningún pomodoro completado
    assert ctrl.rewards.get_stats() == before          # sin XP ni monedas
    assert ctrl.timer_panel.time_label.text() == f"{POMODORO_MIN}:00"
    assert toasts == [('show_info', "Pomodoro parado a los 15:00: el tiempo queda "
                       "guardado. La XP y las monedas llegan al completarlo.")]
    assert not ctrl.data_handler.has_pending_asana_ops()  # sin comentario
    assert side == []                                  # ni n8n, ni mascota, ni esfuerzo
    task = ctrl._task_by_id(ID_ASANA)
    assert not task.get('completed')
    assert not _stop_shown(ctrl)
    assert ctrl.timer_panel.kind_buttons['free'].isEnabled()
    ctrl.select_task(ID1)                              # la tarea ya no está bloqueada
    assert ctrl.model.active_task_id == ID1


def test_parar_con_la_duracion_acortada_a_mitad(ctrl, clock, toasts):
    """Pausado a los 5 min de 35 y se baja a 10 en Ajustes: sigue empezado."""
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    ctrl.apply_settings(dict(ctrl.data_handler.load_settings(), pomodoro_min=10))
    ctrl._sync_kind_selector()
    assert _stop_shown(ctrl)
    del toasts[:]
    ctrl.stop_session()
    assert any("a los 05:00" in msg for _, msg in toasts)
    assert ctrl.model.remaining_time == 10 * 60
    assert ctrl._session_total == 10 * 60


def test_parar_no_sale_en_el_descanso(ctrl, qtbot, clock, toasts):
    tp = ctrl.timer_panel
    _to_break(ctrl, qtbot, clock)
    for _ in range(2):                                 # listo y pausado a mitad
        assert not _stop_shown(ctrl) and tp.skip_btn.isVisibleTo(tp)
        ctrl.toggle_timer()
        _work(ctrl, clock, 60)
        ctrl.toggle_timer()
    ctrl.stop_session()                                # ni por código
    assert ctrl.model.is_break_time
    ctrl._sync_tray_stop()
    assert not ctrl._stop_action.isEnabled()           # ni en la bandeja


def test_saltar_y_parar_nunca_coinciden(ctrl, qtbot, clock, toasts):
    """Al saltar un descanso, «Saltar» sigue hasta la mitad del crossfade:
    si en ese instante se arranca y se pausa, «Parar» espera a que acabe en
    vez de salir encima (comparten el hueco de 44 px)."""
    tp = ctrl.timer_panel
    _to_break(ctrl, qtbot, clock)
    ctrl.skip_break()
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    ctrl.toggle_timer()                                # pausado a mitad, aún en el fundido
    assert tp.skip_btn.isVisibleTo(tp) and not _stop_shown(ctrl)
    qtbot.waitUntil(lambda: _stop_shown(ctrl), timeout=3000)
    assert not tp.skip_btn.isVisibleTo(tp)


def test_parar_tras_una_suspension(ctrl, clock, toasts):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 10 * 60)
    clock.advance(3600)
    ctrl._tick()                                       # despierta: pausa sola
    assert not ctrl.model.is_running
    assert _stop_shown(ctrl)


def test_parar_corriendo_por_codigo_registra_el_tramo(ctrl, clock, toasts):
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 7 * 60)
    ctrl.stop_session()
    assert not ctrl.model.is_running
    assert [round(c['duration']) for c in _focus_chunks(ctrl)] == [420]
    assert any("a los 07:00" in msg for _, msg in toasts)


def test_parar_un_pomodoro_fresco_no_hace_nada(ctrl, toasts):
    ctrl.select_task(ID1)
    ctrl.model.pomodoro_cycle_count = 3
    ctrl.stop_session()
    assert toasts == [] and _focus_chunks(ctrl) == []
    assert ctrl.model.pomodoro_cycle_count == 3


# ---------------------------------------------------------------- Libre
def test_libre_parar_en_marcha_y_en_pausa(ctrl, qtbot, clock, toasts):
    tp = ctrl.timer_panel
    _go_free(ctrl)
    assert not _stop_shown(ctrl)                       # 00:00: nada que parar
    assert not tp.reset_btn.isVisibleTo(tp)            # ni «Reiniciar» en Libre
    assert tp.stop_btn.toolTip().startswith("Parar: termina la sesión libre")
    ctrl.toggle_timer()
    assert _stop_shown(ctrl)                           # en marcha: sí
    _work(ctrl, clock, 12 * 60)
    ctrl.toggle_timer()
    assert _stop_shown(ctrl)                           # en pausa: sí
    ctrl.toggle_timer()
    _work(ctrl, clock, 13 * 60)
    before = ctrl.rewards.get_stats()
    tp.stop_btn.click()
    qtbot.wait(500)
    assert ctrl.model.free_elapsed == 0 and not ctrl.model.is_running
    assert ctrl.rewards.get_stats()['xp'] - before['xp'] == 25
    assert sum(c['duration'] for c in _focus_chunks(ctrl)) == pytest.approx(25 * 60, abs=1)
    assert any(k == 'show_success' and 'Sesión libre de 25 min' in msg for k, msg in toasts)
    assert not _stop_shown(ctrl)
    tp.kind_buttons['pomodoro'].click()
    assert tp.reset_btn.isVisibleTo(tp)                # vuelve en el Pomodoro
    assert tp.stop_btn.toolTip().startswith("Parar: termina este pomodoro")


# ---------------------------------------------------------------- disposición
def test_el_play_no_se_mueve(ctrl, qtbot, clock):
    """El play queda centrado bajo el anillo en todos los estados: «Parar»
    aparece justo después de pausar, con el cursor encima del play."""
    tp = ctrl.timer_panel

    def offset():
        qtbot.wait(50)
        ring = tp.ring.mapTo(tp, tp.ring.rect().center()).x()
        play = tp.play_btn.mapTo(tp, tp.play_btn.rect().center()).x()
        return play - ring
    ctrl.select_task(ID1)
    seen = [offset()]
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    seen.append(offset())
    ctrl.toggle_timer()                                # aparece «Parar»
    assert _stop_shown(ctrl)
    seen.append(offset())
    ctrl.toggle_timer()
    _work(ctrl, clock, POMODORO_MIN * 60)
    qtbot.waitUntil(lambda: tp.skip_btn.isVisibleTo(tp), timeout=3000)  # descanso: «Saltar»
    qtbot.wait(500)                                    # el aviso de fin, con la ventana viva
    seen.append(offset())
    ctrl.skip_break()
    ctrl.reset_timer()
    _go_free(ctrl)                                     # Libre: sin «Reiniciar»
    seen.append(offset())
    ctrl.toggle_timer()                                # Libre en marcha: «Parar»
    qtbot.waitUntil(lambda: _stop_shown(ctrl), timeout=3000)  # tras el crossfade
    seen.append(offset())
    assert all(abs(o) <= 1 for o in seen), seen


# ---------------------------------------------------------------- mini ventana
def test_parar_desde_la_mini_ventana(ctrl, qtbot, clock, toasts):
    _pause_mid_pomodoro(ctrl, clock, minutes=3)
    ctrl.show_compact()
    cw = ctrl.compact_window
    assert cw.stop_btn.isVisibleTo(cw) and not cw.skip_btn.isVisibleTo(cw)
    assert cw.stop_btn.toolTip().startswith("Parar: termina este pomodoro")
    del toasts[:]
    cw.stop_btn.click()
    assert not cw.stop_btn.isVisibleTo(cw)
    assert ctrl.model.remaining_time == POMODORO_MIN * 60
    assert [k for k, _ in toasts] == ['show_info']
    # Libre en la mini ventana: «Parar» con la sesión en marcha
    ctrl.show_normal()
    _go_free(ctrl)
    ctrl.show_compact()
    ctrl.toggle_timer()
    _work(ctrl, clock, 6 * 60)
    assert cw.stop_btn.isVisibleTo(cw)
    assert cw.stop_btn.toolTip().startswith("Parar: termina la sesión libre")
    cw.stop_btn.click()
    qtbot.wait(500)
    assert ctrl.model.free_elapsed == 0 and not ctrl.model.is_running
    assert not cw.stop_btn.isVisibleTo(cw)
    ctrl.show_normal()


def test_mini_ventana_sin_parar_en_el_descanso(ctrl, qtbot, clock, toasts):
    _to_break(ctrl, qtbot, clock)
    ctrl.show_compact()
    cw = ctrl.compact_window
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    ctrl.toggle_timer()
    assert cw.skip_btn.isVisibleTo(cw) and not cw.stop_btn.isVisibleTo(cw)
    ctrl.show_normal()


def test_mini_ventana_cabe_con_parar(ctrl, qtbot, clock):
    """Play, «Parar» y el resto caben en la tarjeta de 312 px sin recortes
    ni solaparse."""
    _go_free(ctrl)
    ctrl.toggle_timer()
    ctrl.show_compact()
    cw = ctrl.compact_window
    qtbot.wait(50)
    assert cw.stop_btn.isVisibleTo(cw)
    card = cw.card.rect()
    for b in (cw.play_btn, cw.stop_btn, cw.expand_btn):
        assert b.parentWidget() is cw.card
        assert card.contains(b.geometry()), (b, b.geometry())
    assert not cw.play_btn.geometry().intersects(cw.stop_btn.geometry())
    assert cw.stop_btn.width() == 32 and cw.stop_btn.height() == 32
    ctrl.show_normal()


def test_cambiar_de_tema_con_parar_visible(ctrl, clock, monkeypatch):
    """Lo que hace theme_changed (como test_ambient/test_brand: sin cambiar
    el tema global, que alcanzaría a ventanas de otros tests)."""
    from ui.theme import tokens as tk
    _pause_mid_pomodoro(ctrl, clock, minutes=2)
    ctrl.show_compact()
    other = next(v for k, v in tk.THEMES.items() if k != 'default')
    import ui.widgets.timer_panel as tpm
    import ui.windows.compact_window as cwm
    monkeypatch.setattr(tpm, "tokens", lambda: other)
    monkeypatch.setattr(cwm, "tokens", lambda: other)
    ctrl.timer_panel._retheme()
    ctrl.compact_window._retheme()
    assert ctrl.timer_panel.stop_btn._hover_color == other.accent
    assert ctrl.compact_window.stop_btn._hover_color == other.accent
    assert _stop_shown(ctrl)
    assert ctrl.compact_window.stop_btn.isVisibleTo(ctrl.compact_window)
    ctrl.show_normal()


# ---------------------------------------------------------------- bandeja
def test_parar_desde_la_bandeja(ctrl, qtbot, clock, toasts):
    action = ctrl._stop_action
    menu = ctrl.tray.contextMenu()
    assert action in menu.actions() and action.text() == "Parar la sesión"
    menu.aboutToShow.emit()
    assert not action.isEnabled()                      # nada que parar
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    menu.aboutToShow.emit()
    assert not action.isEnabled()                      # pomodoro corriendo: no
    ctrl.toggle_timer()
    menu.aboutToShow.emit()
    assert action.isEnabled()                          # pausado a mitad: sí
    del toasts[:]
    action.trigger()
    assert ctrl.model.remaining_time == POMODORO_MIN * 60 and not _stop_shown(ctrl)
    assert [k for k, _ in toasts] == ['show_info']
    menu.aboutToShow.emit()
    assert not action.isEnabled()
    # Libre: también con la sesión en marcha
    ctrl.timer_panel.kind_buttons['free'].click()
    ctrl.toggle_timer()
    _work(ctrl, clock, 6 * 60)
    menu.aboutToShow.emit()
    assert action.isEnabled()
    action.trigger()
    qtbot.wait(500)
    assert ctrl.model.free_elapsed == 0 and not ctrl.model.is_running
