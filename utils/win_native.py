"""Integraciones Win32 (Windows 11): esquinas redondeadas DWM para ventanas
frameless, el AppUserModelID y el mutex con nombre que ve el desinstalador."""
import ctypes
import sys

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
_mutex = None  # el handle vive mientras el proceso (Windows lo cierra al salir)


def round_corners(win_id: int) -> None:
    """Pide a DWM esquinas redondeadas para el HWND dado. Inofensivo si falla
    (Windows 10 u otros): la ventana queda rectangular."""
    if sys.platform != "win32":
        return
    try:
        pref = ctypes.c_int(DWMWCP_ROUND)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(
            ctypes.c_void_p(int(win_id)), DWMWA_WINDOW_CORNER_PREFERENCE,
            ctypes.byref(pref), ctypes.sizeof(pref))
    except Exception:
        pass


def set_app_user_model_id(app_id: str) -> None:
    """Agrupa la app en la taskbar con su propio icono (como v8)."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        pass


def hold_named_mutex(name: str) -> bool:
    """Crea el mutex `name` y lo mantiene hasta que el proceso termina: el
    desinstalador de Inno Setup lo busca (CheckForMutexes) para saber si la
    app sigue abierta, porque el bind TCP del singleton no lo ve. Inofensivo
    si falla: True si tenemos un handle. Si ya existe (la instancia relanzada
    como administrador, mientras la anterior termina de cerrarse), CreateMutexW
    lo abre con ERROR_ALREADY_EXISTS: no es «otra instancia» (eso lo decide el
    singleton) y el handle propio lo mantiene vivo cuando la anterior sale."""
    global _mutex
    if sys.platform != "win32":
        return False
    try:
        create = ctypes.windll.kernel32.CreateMutexW
        create.restype = ctypes.c_void_p
        create.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
        _mutex = create(None, False, name)
        return bool(_mutex)
    except Exception:
        return False
