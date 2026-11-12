# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec de PomoGrowth (onedir).

Build (desde la raíz del repositorio; nunca con la salida en dist/, ver abajo):
    python -m PyInstaller PomoGrowth.spec --noconfirm --distpath build/release --workpath build/work
Salida: build/release/PomoGrowth/ — los datos del usuario (JSON, logs,
música) se crean JUNTO al exe; los recursos (iconos) van dentro de _internal.
COLLECT vacía la carpeta de salida entera antes de copiar (con --noconfirm,
sin preguntar) y en dist/ puede haber una instalación con los datos reales
(la diaria del autor, dist/PomoGrowth9): el spec se niega a compilar sobre
una carpeta con datos. Para actualizar una instalación así se sustituyen su
exe y su _internal por los del build (hasta la 9.4 el exe era PomoGrowth9.exe:
se borra el viejo).
El exe lleva la versión de Config.APP_VERSION: installer/PomoGrowth.iss la
compara para no empaquetar un build viejo con la etiqueta nueva.
Los iconos del sistema (assets/icon.ico/.png) salen de la mascota con
scripts/build_brand_assets.py: regenerarlos antes si cambia assets/icono.svg.
"""
import os
import sys

from PyInstaller.utils.win32.versioninfo import (
    FixedFileInfo, StringFileInfo, StringStruct, StringTable, VarFileInfo, VarStruct,
    VSVersionInfo)

if SPECPATH not in sys.path:
    sys.path.insert(0, SPECPATH)
from core.config import Config  # noqa: E402  (solo constantes)

# Datos del usuario que la app escribe junto al exe (utils/paths.app_base)
USER_DATA = ('pomogrowth_data.json', 'user_progress.json', 'focus_config.json')
_out = os.path.join(DISTPATH, 'PomoGrowth')
_found = [name for name in USER_DATA if os.path.exists(os.path.join(_out, name))]
if _found:
    raise SystemExit(
        f"{_out} tiene datos del usuario ({', '.join(_found)}) y PyInstaller la "
        "vaciaría entera. Compila aparte: python -m PyInstaller PomoGrowth.spec "
        "--noconfirm --distpath build/release --workpath build/work (si son datos "
        "de prueba, muévelos o bórralos antes)")

# Recurso de versión del exe (Propiedades > Detalles; el Administrador de
# tareas muestra FileDescription). x.y.z -> x.y.z.0; x.y -> x.y.0.0
_version = tuple(int(n) for n in Config.APP_VERSION.split('.'))
_version += (0,) * (4 - len(_version))
version_info = VSVersionInfo(
    ffi=FixedFileInfo(filevers=_version, prodvers=_version),
    kids=[StringFileInfo([StringTable('0C0A04B0', [  # español, Unicode
              StringStruct('CompanyName', 'Fabricio S.'),
              StringStruct('FileDescription', Config.APP_NAME),
              StringStruct('FileVersion', Config.APP_VERSION),
              StringStruct('InternalName', 'PomoGrowth'),
              StringStruct('LegalCopyright', '© 2026 Fabricio S.'),
              StringStruct('OriginalFilename', 'PomoGrowth.exe'),
              StringStruct('ProductName', Config.APP_NAME),
              StringStruct('ProductVersion', Config.APP_VERSION)])]),
          VarFileInfo([VarStruct('Translation', [0x0C0A, 1200])])])

a = Analysis(
    ['main.py'],
    pathex=[],
    binaries=[],
    datas=[('assets/icons', 'assets/icons'),
           ('assets/icon.ico', 'assets'),
           ('assets/icon.png', 'assets'),
           # Marca dentro de la app (ui/theme/brand.py): la mascota y el
           # logo horizontal con el texto en trazados (splash)
           ('assets/icono.svg', 'assets'),
           ('assets/pomogrowth-horizontal-mono-white.svg', 'assets'),
           ('assets/music/lofi/LEEME.txt', 'assets/music/lofi'),
           # Sonidos del temporizador (scripts/build_sounds.py)
           ('assets/sounds/timer', 'assets/sounds/timer'),
           # Sonidos de ambiente (core/ambient_catalog.py): los .ogg y su
           # CREDITOS.txt con las licencias; sin un archivo, ese sonido sale
           # desactivado en el mezclador
           ('assets/sounds/ambient', 'assets/sounds/ambient')],
    # anthropic se importa perezosamente (services/pet_service.py): explícito
    # aquí para que la mascota IA funcione también en el exe
    hiddenimports=['PySide6.QtSvg', 'PySide6.QtMultimedia', 'anthropic'],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # Qt que no usamos (los grandes)
        'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebEngineQuick', 'PySide6.QtWebChannel', 'PySide6.QtWebSockets',
        'PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuickWidgets',
        'PySide6.QtQuick3D', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender',
        'PySide6.Qt3DInput', 'PySide6.Qt3DLogic', 'PySide6.Qt3DAnimation',
        'PySide6.Qt3DExtras', 'PySide6.QtDataVisualization', 'PySide6.QtCharts',
        'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtDesigner',
        'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning',
        'PySide6.QtLocation', 'PySide6.QtSensors', 'PySide6.QtSerialPort',
        'PySide6.QtMultimediaWidgets', 'PySide6.QtRemoteObjects',
        'PySide6.QtScxml', 'PySide6.QtStateMachine', 'PySide6.QtTextToSpeech',
        'PySide6.QtVirtualKeyboard', 'PySide6.QtHelp', 'PySide6.QtUiTools',
        # Python que no usamos
        'tkinter', 'IPython', 'jupyter', 'notebook', 'pytest', 'pyflakes',
        'PIL.ImageQt',
    ],
    noarchive=False,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='PomoGrowth',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='assets/icon.ico',
    version=version_info,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='PomoGrowth',
)
