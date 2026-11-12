"""PomoGrowth — entrypoint."""
import sys
from pathlib import Path

# Ejecutable desde cualquier cwd; congelada, los datos viven junto al exe
if getattr(sys, 'frozen', False):
    BASE = Path(sys.executable).parent
else:
    BASE = Path(__file__).resolve().parent
    if str(BASE) not in sys.path:
        sys.path.insert(0, str(BASE))

from utils.logging_setup import setup_logging
from utils.paths import app_icon_path
from utils.win_native import hold_named_mutex, set_app_user_model_id
from core.config import Config


def main() -> int:
    import os
    os.chdir(BASE)  # DataHandler y user_progress usan rutas relativas
    setup_logging(Config.APP_VERSION)
    set_app_user_model_id(Config.APP_USER_MODEL_ID)

    from services import singleton
    # Relanzada por «Reiniciar como administrador» (core/focus_mode): la
    # instancia anterior puede tardar un poco en cerrarse
    elevated = singleton.ELEVATED_RESTART_FLAG in sys.argv
    if not (singleton.acquire_waiting(10.0) if elevated else singleton.acquire()):
        # Aviso mínimo sin construir toda la app
        from PySide6.QtWidgets import QApplication, QMessageBox
        app = QApplication(sys.argv)
        QMessageBox.warning(
            None, "PomoGrowth",
            "PomoGrowth ya está abierto; quizá esté en la bandeja del sistema, "
            "junto al reloj.\n\nPara usar el modo enfoque como administrador, "
            "ciérralo (clic derecho en su icono de la bandeja → Cerrar) y vuelve a "
            "abrir PomoGrowth con «Ejecutar como administrador».")
        return 1
    hold_named_mutex(Config.APP_MUTEX)

    from PySide6.QtWidgets import QApplication
    app = QApplication(sys.argv)
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
    # launch() decide: splash antes de renderizar (auto-sync) o ventana directa
    controller.launch()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
