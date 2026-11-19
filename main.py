"""PomoGrowth — entrypoint."""
import sys
from pathlib import Path
from typing import Optional

# Ejecutable desde cualquier cwd; congelada, los datos viven junto al exe
if getattr(sys, 'frozen', False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).resolve().parent
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

from utils.logging_setup import (enable_fault_log, fault_log_file, log_dir, setup_logging,
                                 start_ui_watchdog)
from utils.paths import app_icon_path
from utils.win_native import hold_named_mutex, is_admin, set_app_user_model_id
from core.config import Config


def _already_running(elevated: bool) -> Optional[int]:
    """Ya hay una PomoGrowth abierta (no se ganó el singleton). Sin construir
    toda la app:
    - se le pide que se muestre (sale al frente, sin la compacta) y esta se
      cierra sin más (0);
    - si contesta que se está cerrando (su despedida), esta espera a que
      suelte el singleton y arranca normal (None);
    - si es de otra carpeta (la instalada y una portable, otro build: cada
      una con sus datos), no se muestra y un aviso lo dice (1);
    - si no contesta a tiempo, o esta es administradora (la relanzada o
      «Ejecutar como administrador» a mano: la otra tendría que cerrarse, no
      mostrarse), el aviso de siempre (1), que explica qué hacer."""
    from PySide6.QtWidgets import QApplication, QMessageBox
    from services import singleton
    # El canal y el aviso necesitan una QApplication viva hasta el final
    app = QApplication.instance() or QApplication(sys.argv)  # noqa: F841
    if not elevated:
        reply = singleton.request_show()
        if reply == singleton.SHOWN:
            return 0
        if reply == singleton.CLOSING and singleton.acquire_waiting(singleton.CLOSING_WAIT_S):
            return None
        if reply == singleton.OTHER:
            QMessageBox.warning(
                None, "PomoGrowth",
                "Ya hay otra PomoGrowth abierta desde otra carpeta (la instalada, "
                "una portable u otra copia), con sus propios datos.\n\nPara usar la "
                "de esta carpeta, cierra la otra (clic derecho en su icono de la "
                "bandeja → Cerrar) y vuelve a abrirla.")
            return 1
    QMessageBox.warning(
        None, "PomoGrowth",
        "PomoGrowth ya está abierto; quizá esté en la bandeja del sistema, "
        "junto al reloj.\n\nPara usar el modo enfoque como administrador, "
        "ciérralo (clic derecho en su icono de la bandeja → Cerrar) y vuelve a "
        "abrir PomoGrowth con «Ejecutar como administrador».")
    return 1


def _claim_instance() -> Optional[int]:
    """None si esta PomoGrowth se queda: es la única (singleton y mutex) y
    activa el log de fallos. Si ya hay otra, el código de salida
    (_already_running). Solo la que se queda escribe en el log de fallos:
    una segunda escribiría en el de la primera."""
    from services import singleton
    # Relanzada por «Reiniciar como administrador» (core/focus_mode): la
    # instancia anterior puede tardar un poco en cerrarse
    relaunched = singleton.ELEVATED_RESTART_FLAG in sys.argv
    if not (singleton.acquire_waiting(10.0) if relaunched else singleton.acquire()):
        code = _already_running(relaunched or is_admin())
        if code is not None:
            return code
    hold_named_mutex(Config.APP_MUTEX)
    enable_fault_log(log_dir(), Config.APP_VERSION)
    return None


def _serve_session(app, controller):
    """Con la app construida: el canal por el que una segunda PomoGrowth (el
    acceso directo con esta ya abierta) pide que esta se muestre, y el vigía
    del hilo de la interfaz (si se bloquea, sus pilas van al log de fallos;
    nada mientras todo va bien), que se para al salir. Devuelve (canal,
    vigía); cualquiera puede ser None si no se pudo."""
    from services import singleton
    server = singleton.listen_for_show(controller.show_from_other_instance, parent=controller,
                                       is_closing=controller.is_closing)
    watchdog = start_ui_watchdog(fault_log_file(), parent=app)
    if watchdog is not None:
        app.aboutToQuit.connect(watchdog.stop)
    return server, watchdog


def main() -> int:
    import os
    os.chdir(BASE)  # DataHandler y user_progress usan rutas relativas
    setup_logging(Config.APP_VERSION)
    set_app_user_model_id(Config.APP_USER_MODEL_ID)

    code = _claim_instance()
    if code is not None:
        return code

    from PySide6.QtWidgets import QApplication
    # Puede existir ya: la de _already_running, si esta esperó a que la
    # anterior terminara de despedirse
    app = QApplication.instance() or QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setApplicationName(Config.APP_NAME)
    # Con tray: ocultar la última ventana NO cierra la app (salir es la X, que
    # pregunta antes, o «Cerrar» del tray; las dos pasan por la despedida)
    app.setQuitOnLastWindowClosed(False)
    from PySide6.QtGui import QIcon
    # El icono va con los recursos (_internal en el exe), no junto al exe
    app.setWindowIcon(QIcon(str(app_icon_path())))

    from ui.theme import fonts
    fonts.init_fonts()
    # Fuente por defecto vía QFont (el QSS NO fija font-size: pisaría las
    # fuentes programáticas como la del timer a 48pt)
    app.setFont(fonts.font(fonts.SMALL))
    from ui.theme.qss import build_qss
    from ui.theme.tokens import THEMES, ThemeManager
    # Tema equipado en recompensas (user_progress.json), antes de construir la UI
    try:
        import json
        progress = json.loads(Path("user_progress.json").read_text(encoding="utf-8"))
        equipped = progress.get('current_theme', 'default')
        if equipped in THEMES:
            ThemeManager.instance().current = THEMES[equipped]
    except (OSError, ValueError):
        pass
    app.setStyleSheet(build_qss(ThemeManager.instance().current, fonts.ui_family()))

    from core.data_handler import DataHandler
    data_handler = DataHandler(Config.DATA_FILE)

    from ui.windows.main_window import MainWindow
    window = MainWindow(data_handler.data.get('settings', {}))

    from ui.controllers.app_controller import AppController
    controller = AppController(window, data_handler)
    _serve_session(app, controller)
    # launch() decide: splash antes de renderizar (auto-sync) o ventana directa
    controller.launch()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
