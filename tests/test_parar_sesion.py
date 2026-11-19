"""«Parar»: terminar la sesión donde está y que el tiempo cuente.

Pomodoro: aparece al pausar a mitad (también tras una suspensión); registra el
tramo, vuelve al inicio con los puntos del ciclo y no completa nada (sin XP ni
monedas, sin descanso, sin Asana, sin n8n, sin mascota ni pregunta de
esfuerzo), con un aviso de lo que pasó. Libre: aparece con la sesión en marcha
o en pausa y la termina como siempre (finish_free_session); «Reiniciar» y la
bandera de «Terminar sesión» ya no están. Nunca en el descanso (ahí está
«Saltar descanso») y nunca a la vez que él. También en la mini ventana y en la
bandeja, y el play no se mueve al aparecer.

«En curso» se mide contra el total de la sesión (_session_total): acortar la
duración a mitad no deja el pomodoro por fresco ni «Parar» sin efecto, y
volver al Pomodoro con otra duración no lo da por empezado. El aviso dice el
minuto que marcaba el reloj, y el del selector bloqueado nombra solo los
botones de ese estado. El aspecto (icono propio, redondo, gris con acento) y
el aviso junto a la mini ventana también quedan fijados.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_parar_sesion.py -q
Trabaja sobre datos sintéticos en un directorio temporal — nunca los reales.
"""
import pytest

from tests.test_free_mode import (ID1, ID_ASANA, POMODORO_MIN, _focus_chunks,  # noqa: F401
                                  _go_free, _work, clock, ctrl, toasts)
from ui.theme.tokens import tokens
from ui.widgets.icon_button import ICONS_DIR, tinted_icon


def _stop_shown(ctrl) -> bool:
    return ctrl.timer_panel.stop_btn.isVisibleTo(ctrl.timer_panel)


def _assert_stop_look(btn) -> None:
    """El aspecto pedido: icono propio «stop», redondo como «Reiniciar» y
    «Saltar», gris (fg_dim) con acento al pasar, y nombre accesible."""
    t = tokens()
    assert btn._icon_name == "stop"
    assert btn.property("variant") == "round"
    assert (btn._color, btn._hover_color) == (t.fg_dim, t.accent)
    assert btn.accessibleName() == "Parar"


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
    _assert_stop_look(tp.stop_btn)
    assert tp.reset_btn.isVisibleTo(tp)                # «Reiniciar» sigue
    ctrl.toggle_timer()
    assert not _stop_shown(ctrl)                       # al reanudar se va


def test_el_icono_de_parar_existe_y_se_pinta(qtbot):
    """assets/icons/stop.svg está y pinta algo: si falta, tinted_icon da un
    pixmap transparente sin avisar y «Parar» sería un círculo vacío."""
    assert (ICONS_DIR / "stop.svg").is_file()
    center = tinted_icon("stop", "#ffffff", 18).pixmap(18, 18).toImage().pixelColor(9, 9)
    assert center.alpha() > 200
    missing = tinted_icon("no-existe", "#ffffff", 18).pixmap(18, 18).toImage().pixelColor(9, 9)
    assert missing.alpha() == 0                        # la comprobación sirve


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


def test_acortado_a_mitad_el_selector_sigue_bloqueado(ctrl, clock, toasts):
    """Pausado a los 5 min de 35 y bajado a 10: el modelo lo daría por fresco
    (le quedan más de 10 min), pero sigue empezado. El selector no se abre al
    volver a pausar y pulsar «Pomodoro» (ya elegido) no rehace el total, que
    dejaba «Parar» a la vista sin efecto y el anillo en negativo."""
    tp = ctrl.timer_panel
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    ctrl.apply_settings(dict(ctrl.data_handler.load_settings(), pomodoro_min=10))
    ctrl.toggle_timer()
    _work(ctrl, clock, 30)
    ctrl.toggle_timer()                                # se re-evalúa el selector
    assert not tp.kind_buttons['free'].isEnabled()
    assert not tp.kind_buttons['pomodoro'].isEnabled()
    ctrl.set_timer_kind('pomodoro')                    # el mismo modo, por código
    assert ctrl._session_total == POMODORO_MIN * 60
    assert 0 <= ctrl._timer_fraction(ctrl.model.get_display_time()) <= 1
    ctrl.set_timer_kind('free')                        # y Libre tampoco entra
    assert not ctrl.model.is_free and ctrl.model.remaining_time == POMODORO_MIN * 60 - 330
    assert _stop_shown(ctrl) and ctrl._stop_visible()
    del toasts[:]
    tp.stop_btn.click()
    assert any("a los 05:30" in msg for _, msg in toasts)
    assert ctrl.model.remaining_time == ctrl._session_total == 10 * 60
    assert not _stop_shown(ctrl) and tp.kind_buttons['free'].isEnabled()


def test_libre_acortado_y_vuelta_al_pomodoro_sin_parar(ctrl, clock, toasts):
    """En Libre se baja el pomodoro de 35 a 10 y se vuelve al Pomodoro: es un
    pomodoro fresco y «Parar» no sale (se decidía con el total viejo)."""
    tp = ctrl.timer_panel
    _go_free(ctrl)
    ctrl.apply_settings(dict(ctrl.data_handler.load_settings(), pomodoro_min=10))
    tp.kind_buttons['pomodoro'].click()
    assert not ctrl.model.is_free and ctrl.model.remaining_time == 10 * 60
    assert ctrl._session_total == 10 * 60
    assert not _stop_shown(ctrl) and not ctrl._stop_visible()
    assert tp.time_label.text() == "10:00"


# ------------------------------------------- el candado de la tarea, igual que «Parar»
def _shorten(ctrl, minutes=10):
    """Bajar la duración del pomodoro en Ajustes (el diálogo deja guardar con
    uno pausado a mitad)."""
    ctrl.apply_settings(dict(ctrl.data_handler.load_settings(), pomodoro_min=minutes))


SHORTEN = pytest.mark.parametrize("shorten", [False, True], ids=["igual", "acortada"])


@SHORTEN
def test_el_candado_de_la_tarea_sigue_a_parar(ctrl, clock, toasts, shorten):
    """Pausado a los 5 min de 35 y, «acortada», bajado a 10: el pomodoro
    sigue empezado («Parar» a la vista), así que la tarea sigue bloqueada.
    Con el criterio del modelo (contra pomodoro_duration) se podía cambiar."""
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    if shorten:
        _shorten(ctrl)
    assert _stop_shown(ctrl)
    del toasts[:]
    ctrl.select_task(ID_ASANA)
    assert ctrl.model.active_task_id == ID1
    assert [k for k, _ in toasts] == ['show_warning']
    assert "la tarea activa está bloqueada" in toasts[0][1]


def test_acortado_a_mitad_el_pomodoro_no_se_reparte_entre_tareas(ctrl, qtbot, clock,
                                                                  toasts):
    """El resto del MISMO pomodoro (le quedaban 30 min) iba a la otra tarea:
    sus minutos, el pomodoro completado (XP, Asana, n8n) y el de la primera,
    repartido. Ahora todo queda en la tarea con la que empezó."""
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    _shorten(ctrl)
    ctrl.select_task(ID_ASANA)                         # bloqueada: no cambia
    ctrl.toggle_timer()
    _work(ctrl, clock, 30 * 60 + 30)
    qtbot.wait(600)                                    # el tramo final va a los 50 ms
    assert ctrl.model.is_break_time
    assert ctrl.model.last_pomodoro_task_id == ID1
    assert {c['task_id'] for c in _focus_chunks(ctrl)} == {ID1}


@pytest.mark.parametrize("how", ["completar", "borrar"])
@SHORTEN
def test_completar_o_borrar_la_activa_cierra_el_pomodoro(ctrl, clock, toasts, monkeypatch,
                                                        how, shorten):
    """Completar o borrar la tarea activa cierra su pomodoro a medias. Con la
    duración acortada se quedaba abierto y sin tarea: «Parar» a la vista, el
    selector bloqueado y el play diciendo «Sin tarea activa»."""
    from ui.dialogs import branded_dialogs as dialogs
    monkeypatch.setattr(dialogs, "confirm", lambda *a, **k: True)
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    if shorten:
        _shorten(ctrl)
    if how == "completar":
        ctrl.toggle_task_completion(ID1)
    else:
        ctrl.delete_task(ID1)
    m = ctrl.model
    assert m.active_task_id is None and not ctrl._work_in_progress()
    assert m.remaining_time == ctrl._session_total == m.pomodoro_duration
    assert not _stop_shown(ctrl) and ctrl.timer_panel.kind_buttons['free'].isEnabled()
    assert [round(c['duration']) for c in _focus_chunks(ctrl)] == [300]   # nada de más


@SHORTEN
def test_un_sync_que_completa_la_activa_la_conserva_a_mitad(ctrl, clock, toasts, shorten):
    """Con el pomodoro a medias, un sync que trae la tarea activa completada
    no la suelta (el play preguntará si reabrirla); acortado, la soltaba."""
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    if shorten:
        _shorten(ctrl)
    for task in ctrl.data_handler.data['tasks']:
        if task['id'] == ID1:
            task['completed'] = True
    ctrl._on_sync_finished(1)
    assert ctrl.model.active_task_id == ID1


@SHORTEN
def test_reiniciar_como_administrador_avisa_del_pomodoro_a_mitad(ctrl, clock, shorten):
    _pause_mid_pomodoro(ctrl, clock, minutes=5)
    if shorten:
        _shorten(ctrl)
    assert ctrl._admin_restart_warning().startswith("Tienes un pomodoro en marcha")


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


def test_el_aviso_cuadra_con_el_reloj(ctrl, clock, toasts):
    """Con tiempos reales (fracciones de segundo) el reloj trunca lo que
    queda: el aviso cuenta lo trabajado según lo que se ve, así los dos suman
    la duración (antes, con el reloj en 20:00, decía «a los 14:59»)."""
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 15 * 60 - 0.3)
    ctrl.toggle_timer()
    assert ctrl.timer_panel.time_label.text() == "20:00"
    del toasts[:]
    ctrl.timer_panel.stop_btn.click()
    assert toasts and "Pomodoro parado a los 15:00:" in toasts[0][1]
    # Medio segundo no deja nada guardado: «Parar» vuelve al inicio sin aviso
    ctrl.toggle_timer()
    clock.advance(0.5)
    ctrl.toggle_timer()
    assert _stop_shown(ctrl)
    del toasts[:]
    ctrl.stop_session()
    assert toasts == [] and ctrl.model.remaining_time == POMODORO_MIN * 60


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


# ---------------------------------------------------------------- selector bloqueado
def test_el_aviso_del_selector_nombra_lo_que_se_ve(ctrl, qtbot, clock):
    """Con el selector bloqueado, el aviso dice cómo terminar la sesión con
    los botones de ese estado: en Libre no hay «Reiniciar» y en el descanso
    no sale «Parar» (ahí está «Saltar descanso»)."""
    tp = ctrl.timer_panel

    def tips():
        return {tp.kind_buttons[k].toolTip() for k in ('pomodoro', 'free')}
    ctrl.select_task(ID1)
    ctrl.toggle_timer()
    _work(ctrl, clock, 60)
    assert tips() == {tp.KIND_HINTS_LOCKED['pomodoro']}   # pomodoro en marcha
    assert "«Parar»" in tp.KIND_HINTS_LOCKED['pomodoro']
    assert "«Reiniciar»" in tp.KIND_HINTS_LOCKED['pomodoro']
    ctrl.toggle_timer()
    assert tips() == {tp.KIND_HINTS_LOCKED['pomodoro']}   # pausado: sale «Parar»
    assert _stop_shown(ctrl) and tp.reset_btn.isVisibleTo(tp)
    ctrl.toggle_timer()
    _work(ctrl, clock, POMODORO_MIN * 60)                 # descanso
    qtbot.waitUntil(lambda: tp.skip_btn.isVisibleTo(tp), timeout=3000)
    qtbot.wait(500)
    assert tips() == {tp.KIND_HINTS_LOCKED['break']}
    assert "Parar" not in tp.KIND_HINTS_LOCKED['break'] and not _stop_shown(ctrl)
    ctrl.skip_break()
    ctrl.reset_timer()
    _go_free(ctrl)
    ctrl.toggle_timer()                                   # Libre en marcha
    _work(ctrl, clock, 60)
    assert tips() == {tp.KIND_HINTS_LOCKED['free']}
    assert "Reiniciar" not in tp.KIND_HINTS_LOCKED['free']
    assert "«Parar»" in tp.KIND_HINTS_LOCKED['free']
    assert not tp.reset_btn.isVisibleTo(tp)
    ctrl.toggle_timer()
    assert tips() == {tp.KIND_HINTS_LOCKED['free']}       # en pausa, igual
    ctrl.stop_session()
    assert tp.kind_buttons['pomodoro'].toolTip().startswith("Pomodoro:")  # libre de nuevo


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
    # «Parar» llena su hueco como sus vecinos: ni más pequeño ni descentrado
    assert tp.stop_btn.size() == tp.skip_btn.size() == tp.reset_btn.size()
    assert tp.stop_btn.size() == tp.right_slot.size()
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
def test_parar_desde_la_mini_ventana(ctrl, qtbot, clock, toasts, monkeypatch):
    from ui.widgets import toast
    parents = []                                       # dónde se ancla cada aviso
    monkeypatch.setattr(toast, "show_info", lambda parent, msg, duration=3000: (
        parents.append(parent), toasts.append(('show_info', msg))))
    _pause_mid_pomodoro(ctrl, clock, minutes=3)
    ctrl.show_compact()
    cw = ctrl.compact_window
    assert cw.stop_btn.isVisibleTo(cw) and not cw.skip_btn.isVisibleTo(cw)
    assert cw.stop_btn.toolTip().startswith("Parar: termina este pomodoro")
    _assert_stop_look(cw.stop_btn)
    del toasts[:]
    cw.stop_btn.click()
    assert not cw.stop_btn.isVisibleTo(cw)
    assert ctrl.model.remaining_time == POMODORO_MIN * 60
    assert [k for k, _ in toasts] == ['show_info']
    # El aviso sale junto a la mini ventana, no sobre la principal oculta
    assert parents == [cw]
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
    for btn in (ctrl.timer_panel.stop_btn, ctrl.compact_window.stop_btn):
        # Gris del tema nuevo con su acento al pasar, y sigue siendo redondo
        assert (btn._color, btn._hover_color) == (other.fg_dim, other.accent)
        assert btn.property("variant") == "round"
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
