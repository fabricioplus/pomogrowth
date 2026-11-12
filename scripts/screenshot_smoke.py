"""Humo de UI de la Fase 1: app real + interacciones + capturas + integridad de datos.

Uso: python scripts/screenshot_smoke.py   (desde la raíz del repositorio, con display real)
Capturas en shots/. Reporte en shots/smoke_report.txt. No toca los datos: se
ejecuta sobre una copia temporal y al final se restaura el original.
"""
import hashlib
import json
import os
import shutil
import sys
import traceback
from pathlib import Path

V9 = Path(__file__).resolve().parent.parent
os.chdir(V9)
sys.path.insert(0, str(V9))
SHOTS = V9 / "shots"
SHOTS.mkdir(exist_ok=True)
R = []


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()[:16]


def dump(tag="FIN"):
    (SHOTS / "smoke_report.txt").write_text(tag + "\n" + "\n".join(R), encoding="utf-8")


backup = {}
for f in ("pomogrowth_data.json", "user_progress.json", "focus_config.json"):
    backup[f] = Path(f).read_bytes()
    R.append(f"sha antes  {f}: {sha(f)}")

try:
    from PySide6.QtCore import QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication

    from core.config import Config
    from core.data_handler import DataHandler
    from ui.theme import fonts
    from ui.theme.qss import build_qss
    from ui.theme.tokens import ThemeManager

    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    fonts.init_fonts()
    app.setFont(fonts.font(fonts.SMALL))
    app.setStyleSheet(build_qss(ThemeManager.instance().current, fonts.ui_family()))

    data_handler = DataHandler(Config.DATA_FILE)
    # Un test NUNCA sale a la red: neutralizar la propagación a Asana (la
    # cola de cambios no envía nada: cada op vuelve como 'skipped')
    data_handler.send_asana_ops = lambda ops, *a, **k: [
        {'id': op['id'], 'rev': op.get('rev', 0), 'outcome': 'skipped',
         'detail': 'humo', 'offline': False} for op in ops]
    data_handler.delete_asana_task = lambda *a, **k: None
    from ui.windows.main_window import MainWindow
    window = MainWindow(data_handler.data.get('settings', {}))
    from ui.controllers.app_controller import AppController
    ctrl = AppController(window, data_handler)
    # Sin la pregunta de esfuerzo: las capturas de completar no la esperan
    ctrl.effort_prompt_enabled = lambda: False
    window.show()

    def cap(name):
        window.grab().save(str(SHOTS / name))

    def run():
        try:
            QTest.qWait(600)
            cap("01_inicio.png")
            n_cards = len(ctrl.tasks_panel._cards)
            R.append(f"inicio: {n_cards} tarjetas (filtro pendientes)")

            # Seleccionar la tercera tarea visible
            order = ctrl.tasks_panel.visible_order()
            ctrl.select_task(order[2])
            QTest.qWait(300)
            cap("02_seleccion.png")
            card = ctrl.tasks_panel._cards[order[2]]
            R.append(f"selección: activa={ctrl.model.active_task_id == order[2]} "
                     f"resaltada={card._active}")

            # Arrancar el timer
            ctrl.toggle_timer()
            QTest.qWait(1400)
            cap("03_timer_corriendo.png")
            R.append(f"timer corriendo: {ctrl.model.is_running} "
                     f"display={ctrl.timer_panel.time_label.text()}")
            ctrl.toggle_timer()  # pausa

            # Completar y descompletar una tarea NO activa bajo filtro 'all'
            ctrl.set_task_filter('all')
            QTest.qWait(200)
            tid = ctrl.tasks_panel.visible_order()[5]
            was = ctrl._task_by_id(tid).get('completed')
            ctrl.toggle_task_completion(tid)
            QTest.qWait(500)
            cap("04_toggle_all.png")
            now = ctrl._task_by_id(tid).get('completed')
            R.append(f"toggle filtro all: datos {was}->{now} "
                     f"tarjeta_viva={tid in ctrl.tasks_panel._cards}")
            ctrl.toggle_task_completion(tid)  # revertir
            QTest.qWait(300)

            # Completar bajo 'pendientes' → la tarjeta se colapsa sola
            ctrl.set_task_filter('pending')
            QTest.qWait(200)
            vis = ctrl.tasks_panel.visible_order()
            n0 = len(vis)
            ctrl.toggle_task_completion(vis[0])
            QTest.qWait(500)
            cap("05_completar_pendientes.png")
            n1 = len(ctrl.tasks_panel.visible_order())
            R.append(f"completar en pendientes: tarjetas {n0}->{n1} "
                     f"{'OK' if n1 == n0 - 1 else 'FALLO'}")
            ctrl.toggle_task_completion(vis[0])  # revertir (rebuild al cambiar filtro)
            ctrl.set_task_filter('pending')
            QTest.qWait(300)

            # Mover con flechas (requiere orden manual)
            ctrl.set_task_sort('manual')
            QTest.qWait(250)
            vis = ctrl.tasks_panel.visible_order()
            a, b = vis[0], vis[1]
            ctrl.move_task(a, +1)
            QTest.qWait(250)
            new_vis = ctrl.tasks_panel.visible_order()
            datos = [t['id'] for t in data_handler.get_tasks() if not t.get('completed')]
            ok_ui = new_vis[0] == b and new_vis[1] == a
            ok_data = datos[:2] == new_vis[:2]
            R.append(f"mover flecha: UI={'OK' if ok_ui else 'FALLO'} "
                     f"datos={'OK' if ok_data else 'FALLO'}")
            ctrl.move_task(a, -1)
            QTest.qWait(250)

            # Orden por nombre (tabs)
            ctrl.set_task_sort('name')
            QTest.qWait(300)
            cap("06_orden_nombre.png")
            names = [ctrl._task_by_id(t).get('name', '').lower()
                     for t in ctrl.tasks_panel.visible_order()]
            R.append(f"orden nombre: {'OK' if names == sorted(names) else 'FALLO'}")
            ctrl.set_task_sort('manual')
            QTest.qWait(200)

            # Añadir + eliminar tarea (sin diálogo: directo al controller/datos)
            ctrl.add_task("Tarea de humo v9", ["test"])
            QTest.qWait(300)
            ids = [t['id'] for t in data_handler.get_tasks() if t.get('name') == "Tarea de humo v9"]
            ok_add = len(ids) == 1 and ids[0] in ctrl.tasks_panel._cards
            data_handler.delete_task(ids[0])
            ctrl.refresh_task_list()
            QTest.qWait(200)
            R.append(f"añadir tarea: {'OK' if ok_add else 'FALLO'}; eliminada de nuevo")

            # Panel añadir expandido para la captura final
            ctrl.tasks_panel.add_panel.toggle()
            QTest.qWait(350)
            cap("07_panel_add.png")

            # --- F2: posición del panel (derecha y vuelta a la izquierda) ---
            for expected in ('right', 'left'):
                ctrl.cycle_panel_position()
                QTest.qWait(150)
                if expected == 'right':
                    cap("08_panel_derecha.png")
            pos_ok = ctrl.panel_position == 'left'
            from PySide6.QtCore import Qt as _Qt
            orient_ok = window.splitter.orientation() == _Qt.Horizontal
            R.append(f"panel posiciones: ciclo completo {'OK' if pos_ok and orient_ok else 'FALLO'}")

            # --- F2: diálogo de configuración (sin exec: show para capturar) ---
            from ui.dialogs.settings_dialog import SettingsDialog
            dlg = SettingsDialog(window, ctrl)
            dlg.show()
            QTest.qWait(500)
            dlg.grab().save(str(SHOTS / "09_settings_timer.png"))
            dlg.tabs.setCurrentIndex(1)
            QTest.qWait(300)
            dlg.grab().save(str(SHOTS / "10_settings_asana.png"))
            n_tabs = dlg.tabs.count()
            saved_pat = dlg.ed_pat.text() != ""
            dlg.done(0)
            if dlg._overlay:
                dlg._overlay.deleteLater()
            QTest.qWait(200)
            R.append(f"settings: {n_tabs} tabs, PAT cargado={saved_pat}")

            # --- F3: diálogo de estadísticas ---
            from ui.dialogs.stats_dialog import StatsDialog
            sdlg = StatsDialog(window, ctrl)
            sdlg.show()
            QTest.qWait(900)
            sdlg.grab().save(str(SHOTS / "11_stats_7d.png"))
            n_canvases = len(sdlg._canvases)
            sdlg._change_period(0)
            QTest.qWait(700)
            sdlg.grab().save(str(SHOTS / "12_stats_historico.png"))
            sdlg.done(0)
            if sdlg._overlay:
                sdlg._overlay.deleteLater()
            QTest.qWait(200)
            R.append(f"stats: {n_canvases} gráficos renderizados "
                     f"{'OK' if n_canvases == 5 else 'FALLO'}")

            # --- F4: recompensas + tema en caliente ---
            from ui.dialogs.rewards_dialog import RewardsDialog
            rdlg = RewardsDialog(window, ctrl)
            rdlg.show()
            QTest.qWait(500)
            rdlg.grab().save(str(SHOTS / "13_rewards_tienda.png"))
            rdlg.tabs.setCurrentIndex(2)
            QTest.qWait(300)
            rdlg.grab().save(str(SHOTS / "14_rewards_progreso.png"))
            n_items = len(ctrl.rewards.SHOP_ITEMS)
            n_tabs = rdlg.tabs.count()
            rdlg.done(0)
            if rdlg._overlay:
                rdlg._overlay.deleteLater()
            QTest.qWait(200)
            R.append(f"rewards: {n_tabs} pestañas, {n_items} items en tienda")

            # --- Música: el diálogo, sin reproducir nada ---
            from ui.dialogs.music_dialog import MusicDialog
            mdlg = MusicDialog(window, ctrl.music)
            mdlg.show()
            QTest.qWait(400)
            mdlg.grab().save(str(SHOTS / "14b_musica.png"))
            n_lists = len(ctrl.music.lists())
            mdlg.done(0)
            if mdlg._overlay:
                mdlg._overlay.deleteLater()
            QTest.qWait(150)
            R.append(f"música: diálogo OK, {n_lists} listas con la carpeta lofi "
                     f"({len(ctrl.music.tracks)} pistas en ella)")

            from ui.theme.tokens import ThemeManager
            ctrl.apply_theme('theme_ocean')
            QTest.qWait(500)
            ocean_ok = ThemeManager.instance().current.accent == '#0ea5e9'
            cap("15_tema_ocean.png")
            ctrl.apply_theme('default')
            QTest.qWait(400)
            default_ok = ThemeManager.instance().current.accent == '#00f9a0'
            R.append(f"tema en caliente: ocean={'OK' if ocean_ok else 'FALLO'} "
                     f"vuelta a default={'OK' if default_ok else 'FALLO'}")

            # --- F5: bienvenida, compacto, enfoque (solo UI, sin tocar hosts), tray ---
            from ui.windows.welcome_farewell import WelcomeDialog, FarewellWindow
            wdlg = WelcomeDialog(window, ctrl._welcome_lines())
            wdlg.show()
            QTest.qWait(400)
            wdlg.grab().save(str(SHOTS / "16_bienvenida.png"))
            wdlg.accept()
            if wdlg._overlay:
                wdlg._overlay.deleteLater()
            QTest.qWait(150)

            ctrl.show_compact()
            QTest.qWait(400)
            compact_ok = (ctrl.compact_window.isVisible()
                          and not window.isVisible())
            ctrl.compact_window.grab().save(str(SHOTS / "17_compacto.png"))
            ctrl.show_normal()
            QTest.qWait(300)
            normal_ok = window.isVisible() and not ctrl.compact_window.isVisible()
            R.append(f"compacto: ida={'OK' if compact_ok else 'FALLO'} "
                     f"vuelta={'OK' if normal_ok else 'FALLO'}")

            from ui.dialogs.focus_dialog import FocusDialog
            fdlg = FocusDialog(window, ctrl.focus_mode)
            fdlg.show()
            QTest.qWait(400)
            fdlg.grab().save(str(SHOTS / "18_enfoque.png"))
            n_sites = len(ctrl.focus_mode.get_blocked_sites())
            fdlg.done(0)
            if fdlg._overlay:
                fdlg._overlay.deleteLater()
            QTest.qWait(150)
            R.append(f"enfoque: diálogo OK, {n_sites} sitios configurados; "
                     f"tray visible={ctrl.tray.isVisible()}")

            done_flag = {"v": False}
            fw = FarewellWindow(lambda: done_flag.__setitem__("v", True),
                                duration_ms=700)
            QTest.qWait(300)
            fw.grab().save(str(SHOTS / "19_despedida.png"))
            QTest.qWait(700)
            fw.close()
            R.append(f"despedida: mostrada y callback={'OK' if done_flag['v'] else 'FALLO'}")

            # Splash (standalone)
            from ui.windows.splash import SplashWindow
            sp = SplashWindow()
            sp.show()
            sp.set_progress(0.6, "Sincronizando Asana… 60/100")
            QTest.qWait(400)
            sp.grab().save(str(SHOTS / "20_splash.png"))
            sp.close()
            R.append("splash: OK")

            # Validación de esquema tras escritura
            data = json.loads(Path("pomogrowth_data.json").read_text(encoding="utf-8"))
            keys_ok = all(k in data for k in ("tasks", "deleted_tasks", "time_chunks",
                                              "history", "ui_snapshot", "settings"))
            R.append(f"esquema tras sesión: {'OK' if keys_ok else 'FALLO'} "
                     f"tareas={len(data['tasks'])}")
        except Exception:
            R.append("EXC:\n" + traceback.format_exc())
        finally:
            dump()
            app.quit()

    QTimer.singleShot(800, run)
    QTimer.singleShot(60000, lambda: (dump("TIMEOUT"), app.quit()))
    app.exec()
except Exception:
    R.append(traceback.format_exc())
    dump("CRASH")
finally:
    for f, content in backup.items():
        Path(f).write_bytes(content)
    with open(SHOTS / "smoke_report.txt", "a", encoding="utf-8") as fh:
        fh.write("\nDatos restaurados al estado previo (sha verificable).")
