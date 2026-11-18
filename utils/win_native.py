"""Integraciones Win32 (Windows 11): esquinas redondeadas DWM para ventanas
frameless, el AppUserModelID, el mutex con nombre que ve el desinstalador y
el «encima de todo» nativo (WS_EX_TOPMOST) de las ventanas siempre visibles.

«Encima de todo»: Qt pone WS_EX_TOPMOST al crear la ventana con
WindowStaysOnTopHint, pero no vuelve a mirarlo. Si Windows u otra app se lo
quita, la ventana sigue creyéndose siempre visible y ya no lo es.
is_topmost() lo lee del HWND y set_topmost() lo repone sin activar, mover ni
redimensionar la ventana. Las ventanas que posee (sus diálogos y avisos) van
con ella y siguen encima: con SWP_NOOWNERZORDER la dueña subía por encima de
su propio modal y quedaba bloqueada con el modal escondido detrás.
keep_topmost() junta las dos y deja una línea en el log cada vez que hubo
que reponerlo. Fuera de Windows o sin ventanas nativas (Qt offscreen en los
tests) no hacen nada.
"""
import ctypes
import logging
import sys
from ctypes import wintypes
from typing import Optional

DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWCP_ROUND = 2
_mutex = None  # el handle vive mientras el proceso (Windows lo cierra al salir)

GWL_EXSTYLE = -20
WS_EX_TOPMOST = 0x00000008
HWND_TOPMOST, HWND_NOTOPMOST = -1, -2
SWP_NOSIZE, SWP_NOMOVE = 0x0001, 0x0002
SWP_NOACTIVATE, SWP_NOOWNERZORDER = 0x0010, 0x0200
# Solo la banda (topmost o no): ni mover, ni redimensionar, ni activar. Sin
# SWP_NOOWNERZORDER: Windows sube con la ventana a las que posee (diálogos,
# avisos) y las deja encima de ella, que es donde un modal tiene que estar
_TOPMOST_SWP = SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE
ASFW_ANY = 0xFFFFFFFF
_user32 = None  # WinDLL propia con los prototipos de abajo (ver _native_user32)


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


# ---------- «encima de todo» (WS_EX_TOPMOST) ----------

def _native_windows() -> bool:
    """True si las ventanas de Qt son HWND de verdad: Windows y la
    plataforma «windows» de Qt (con offscreen, winId() no es un HWND)."""
    if sys.platform != "win32":
        return False
    try:
        from PySide6.QtGui import QGuiApplication
        return QGuiApplication.platformName() == "windows"
    except Exception:
        return False


def _native_user32():
    """user32 con los prototipos de esta sección, o None si no aplica. Es una
    WinDLL propia: fijar argtypes en ctypes.windll.user32 afectaría a todo el
    proceso."""
    global _user32
    if not _native_windows():
        return None
    if _user32 is None:
        try:
            lib = ctypes.WinDLL("user32", use_last_error=True)
            lib.IsWindow.restype = wintypes.BOOL
            lib.IsWindow.argtypes = (wintypes.HWND,)
            lib.GetWindowLongW.restype = ctypes.c_long
            lib.GetWindowLongW.argtypes = (wintypes.HWND, ctypes.c_int)
            lib.SetWindowPos.restype = wintypes.BOOL
            lib.SetWindowPos.argtypes = (wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                         ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                         wintypes.UINT)
            _user32 = lib
        except Exception:
            return None
    return _user32


def is_topmost(hwnd) -> Optional[bool]:
    """¿Tiene el HWND el estilo WS_EX_TOPMOST? None si no se puede saber
    (fuera de Windows, Qt offscreen, un handle que no es una ventana)."""
    lib = _native_user32()
    if lib is None or not hwnd:
        return None
    try:
        if not lib.IsWindow(int(hwnd)):
            return None
        return bool(lib.GetWindowLongW(int(hwnd), GWL_EXSTYLE) & WS_EX_TOPMOST)
    except Exception:
        return None


def set_topmost(hwnd, on: bool) -> bool:
    """Pone (HWND_TOPMOST) o quita (HWND_NOTOPMOST) el «encima de todo» sin
    activar, mover ni redimensionar la ventana; las que posee van con ella y
    siguen encima (un modal no queda detrás de su ventana). True si Windows
    lo aceptó; inofensivo (False) si no aplica."""
    lib = _native_user32()
    if lib is None or not hwnd:
        return False
    try:
        if not lib.IsWindow(int(hwnd)):
            return False
        after = HWND_TOPMOST if on else HWND_NOTOPMOST
        return bool(lib.SetWindowPos(int(hwnd), after, 0, 0, 0, 0, _TOPMOST_SWP))
    except Exception:
        return False


def keep_topmost(hwnd, where: str, force: bool = False) -> bool:
    """Repone el «encima de todo» de una ventana que debe tenerlo. Si ya lo
    tiene, no la toca (no compite con otras ventanas siempre visibles), salvo
    con `force` (al mostrarla o restaurarla, cuando va a ir delante de todos
    modos). Devuelve True, y deja una línea en el log, si lo había perdido y
    se repuso; `where` dice cuál («principal», «compacta»…)."""
    state = is_topmost(hwnd)
    if state is None or (state and not force):
        return False
    if not set_topmost(hwnd, True) or state:
        return False
    logging.info("Siempre visible: la ventana %s había perdido el «encima de todo»; "
                 "repuesto", where)
    return True


def allow_foreground_to_others() -> None:
    """La segunda instancia (que el usuario acaba de abrir y por eso puede
    pasar al frente) cede ese permiso: así la ventana de la primera puede
    activarse en vez de solo parpadear en la barra de tareas."""
    if sys.platform != "win32":
        return
    try:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.AllowSetForegroundWindow.restype = wintypes.BOOL
        lib.AllowSetForegroundWindow.argtypes = (wintypes.DWORD,)
        lib.AllowSetForegroundWindow(ASFW_ANY)
    except Exception:
        pass
