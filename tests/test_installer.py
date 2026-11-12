"""Empaquetado: el instalador de Inno Setup (installer/PomoGrowth.iss) y el
build que empaqueta (PomoGrowth.spec). Misma versión e AppUserModelID que la
app, solo un build de esa versión, imágenes del asistente con la marca, nada
que instale o borre más datos del usuario que los suyos, y en el exe los
recursos que usa la app (dentro de _internal). No compila nada: el spec se
ejecuta con Analysis/EXE/COLLECT de mentira y el .iss lo compila ISCC.exe.

Ejecutar desde la raíz del repositorio:
    QT_QPA_PLATFORM=offscreen python -m pytest tests/test_installer.py -q
"""
import inspect
import os
import re
import struct
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

V9 = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(V9))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QColor, QImage  # noqa: E402

from core.config import Config  # noqa: E402
from ui.theme import brand  # noqa: E402

ISS = V9 / "installer" / "PomoGrowth.iss"
SPEC = V9 / "PomoGrowth.spec"
# Tamaños que admite Inno Setup 6, de 100 % a 250 % de escala
WIZARD_SIZES = [(164, 314), (192, 386), (246, 459), (273, 556),
                (328, 604), (355, 700), (410, 797)]
WIZARD_SMALL_SIZES = [(55, 55), (64, 68), (83, 80), (92, 97),
                      (110, 106), (119, 123), (138, 140)]


def _defines() -> dict:
    text = ISS.read_text(encoding="utf-8")
    return dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"', text, re.M))


def _sections() -> dict:
    """Líneas de cada sección, sin comentarios ni vacías."""
    out, current = {}, None
    for line in ISS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith(";"):
            continue
        m = re.fullmatch(r"\[(\w+)\]", line)
        if m:
            current = out.setdefault(m.group(1), [])
        elif current is not None:
            current.append(line)
    return out


def _setup() -> dict:
    return dict(line.split("=", 1) for line in _sections()["Setup"])


def _code() -> str:
    """La sección [Code] (Pascal), sin sus comentarios //."""
    text = ISS.read_text(encoding="utf-8").split("[Code]", 1)[1]
    return re.sub(r"//[^\n]*", "", text)


def _run_spec(distpath) -> dict:
    """Ejecuta PomoGrowth.spec como PyInstaller, pero con Analysis, PYZ, EXE y
    COLLECT de mentira (no compila nada): devuelve sus argumentos."""
    pytest.importorskip("PyInstaller.utils.win32.versioninfo")  # lo importa el spec
    calls = {}

    def target(name):
        def build(*args, **kwargs):
            calls[name] = kwargs
            return SimpleNamespace(pure=[], scripts=[], binaries=[], datas=[])
        return build

    namespace = {name: target(name) for name in ("Analysis", "PYZ", "EXE", "COLLECT")}
    namespace.update(SPECPATH=str(V9), DISTPATH=str(distpath))
    exec(compile(SPEC.read_bytes(), str(SPEC), "exec"), namespace)
    return calls


def _similar(px: QColor, color: str, tolerance: int = 2) -> bool:
    c = QColor(color)
    return max(abs(px.red() - c.red()), abs(px.green() - c.green()),
               abs(px.blue() - c.blue())) <= tolerance


def test_version_y_app_user_model_id_iguales_a_los_de_la_app():
    defines = _defines()
    assert defines["AppVersion"] == Config.APP_VERSION
    assert defines["AppUserModelID"] == Config.APP_USER_MODEL_ID
    icons = _sections()["Icons"]
    assert len(icons) == 2  # menú Inicio y escritorio
    assert all('AppUserModelID: "{#AppUserModelID}"' in line for line in icons)
    assert _setup()["VersionInfoVersion"] == "{#AppVersion}"


def test_imagenes_del_asistente_en_todas_las_escalas():
    """BMP de 24 bits con el tamaño exacto de cada escala, en el orden en que
    las lista el .iss; la grande, mascota y texto sobre BRAND_INK, y la
    pequeña, la mascota sobre blanco."""
    setup = _setup()
    for key, sizes, background in (
            ("WizardImageFile", WIZARD_SIZES, brand.BRAND_INK),
            ("WizardSmallImageFile", WIZARD_SMALL_SIZES, "#FFFFFF")):
        files = setup[key].split(",")
        assert len(files) == len(sizes)
        for name, (w, h) in zip(files, sizes):
            data = (ISS.parent / name).read_bytes()
            width, height, _, bpp = struct.unpack("<iiHH", data[18:30])
            assert data[:2] == b"BM" and bpp == 24, name
            assert (width, abs(height)) == (w, h), name
            image = QImage.fromData(data, "BMP")
            corners = (image.pixelColor(0, 0), image.pixelColor(w - 1, h - 1))
            assert all(_similar(px, background) for px in corners), name
            pixels = [image.pixelColor(x, y) for y in range(0, h, 2) for x in range(0, w, 2)]
            assert any(_similar(px, brand.BRAND_GREEN) for px in pixels), name
            if background == brand.BRAND_INK:  # cara y «Pomo» en blanco
                assert any(_similar(px, "#FFFFFF") for px in pixels), name


def test_no_instala_ni_borra_datos_del_usuario():
    """Por usuario y sin administrador (los datos se escriben junto al exe);
    el build nunca aporta JSON, copias, logs ni música, y el desinstalador no
    lleva [UninstallDelete] (borrarlos se pregunta en [Code])."""
    setup, sections = _setup(), _sections()
    assert setup["PrivilegesRequired"] == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in setup
    assert setup["DefaultDirName"].startswith("{autopf}")
    files = sections["Files"]
    assert _defines()["BuildDir"] == "..\\build\\release\\PomoGrowth"
    build = [line for line in files if line.startswith('Source: "{#BuildDir}\\*"')]
    assert len(build) == 1
    excludes = re.search(r'Excludes:\s*"([^"]*)"', build[0]).group(1).split(",")
    for pattern in ("\\*.json", "\\*.bak", "\\*.tmp", "\\*.corrupt-*", "\\logs", "\\assets"):
        assert pattern in excludes
    assert not any(".json" in line for line in files if line not in build)
    # La licencia (GPL: va con el programa), junto al exe
    assert 'Source: "..\\LICENSE"; DestDir: "{app}"; DestName: "LICENSE.txt"; Flags: ignoreversion' in files
    assert (V9 / "LICENSE").read_text(encoding="utf-8").lstrip().startswith(
        "GNU GENERAL PUBLIC LICENSE\n                       Version 3, 29 June 2007")
    assert "UninstallDelete" not in sections
    # Lo único que se borra al instalar: el _internal anterior y el exe de la 9.4
    assert sections["InstallDelete"] == ['Type: filesandordirs; Name: "{app}\\_internal"',
                                         'Type: files; Name: "{app}\\{#OldAppExe}"']


def test_desinstalar_solo_borra_los_datos_de_la_lista():
    """Borrar los datos se pregunta (solo si los hay, nunca en una
    desinstalación silenciosa y con «No» por defecto) y se limita a los
    archivos de la app: ni todo {app} ni la música o lo exportado. Y el
    asistente, en español."""
    assert _sections()["Languages"] == [
        'Name: "spanish"; MessagesFile: "compiler:Languages\\Spanish.isl"']
    code = _code()
    targets = {}
    for func, args in re.findall(r"\b(DeleteFile|DelTree)\(([^;]*)\);", code):
        m = re.fullmatch(r"App \+ '([^']+)'(.*)", args)
        assert m, f"{func}({args}): fuera de la lista"
        targets[m.group(1)] = (func, m.group(2).replace(" ", ""))
    assert targets == {
        "\\pomogrowth_data.json": ("DeleteFile", ""),
        "\\user_progress.json": ("DeleteFile", ""),
        "\\focus_config.json": ("DeleteFile", ""),
        # Comodines: solo archivos de la carpeta, sin entrar en subcarpetas
        "\\*.bak": ("DelTree", ",False,True,False"),
        "\\*.tmp": ("DelTree", ",False,True,False"),
        "\\*.corrupt-*": ("DelTree", ",False,True,False"),
        "\\pomogrowth_data.pre-*.json": ("DelTree", ",False,True,False"),
        "\\logs": ("DelTree", ",True,True,True"),
    }
    # Las carpetas, solo si se han quedado vacías (RemoveDir no borra otras)
    for args in re.findall(r"\bRemoveDir\(([^;]*)\);", code):
        assert args == "App" or re.fullmatch(r"App \+ '\\assets(\\music(\\lofi)?)?'", args), args
    step = code[code.index("procedure CurUninstallStepChanged"):]
    ask = re.search(r"if (.*?) then\s+BorrarDatos\(App\);", step, re.S)
    assert ask, "BorrarDatos sin preguntar"
    condition = " ".join(ask.group(1).split())
    for part in ("HayDatos(App)", "not UninstallSilent", "MB_DEFBUTTON2", "= IDYES"):
        assert part in condition, part
    assert code.count("BorrarDatos(App)") == 1  # solo esa llamada


def test_solo_empaqueta_un_build_de_esta_version(tmp_path):
    """El exe del build lleva Config.APP_VERSION con cuatro números (del spec:
    x.y.z.0, o x.y.0.0 para una x.y como la 1.0) y el .iss no compila si el
    de build/release es otro o no lleva versión: así salió un «instalador
    9.3.1» con el exe de la 9.3 (ISCC para en el #error)."""
    text = ISS.read_text(encoding="utf-8")
    defines = _defines()
    assert re.search(r"^#define BuildVersion GetVersionNumbersString\(.*BuildDir.*AppExe\)$",
                     text, re.M)
    guard = re.search(r'^#if BuildVersion != AppVersion \+ "\.0" && '
                      r'BuildVersion != AppVersion \+ "\.0\.0"$(.*?)^#endif$', text,
                      re.M | re.S)
    assert guard and re.search(r"^\s*#error ", guard.group(1), re.M)
    assert guard.end() < text.index("[Setup]")  # antes de compilar nada

    version = _run_spec(tmp_path)["EXE"]["version"]
    ffi = version.ffi
    stamped = (ffi.fileVersionMS >> 16, ffi.fileVersionMS & 0xFFFF,
               ffi.fileVersionLS >> 16, ffi.fileVersionLS & 0xFFFF)
    parts = [int(n) for n in Config.APP_VERSION.split(".")]
    assert list(stamped) == parts + [0] * (4 - len(parts))

    def blocked(build: str) -> bool:  # el #if de la guarda, en Python
        app = defines["AppVersion"]
        return build != app + ".0" and build != app + ".0.0"
    # GetVersionNumbersString da siempre cuatro números: el exe de esta
    # versión pasa (sea x.y o x.y.z) y uno sin versión u otro, no
    assert not blocked(".".join(map(str, stamped)))
    assert blocked("") and blocked("0.0.0.0")
    strings = {s.name: s.val for s in version.kids[0].kids[0].kids}
    assert strings["FileVersion"] == strings["ProductVersion"] == Config.APP_VERSION
    assert strings["OriginalFilename"] == defines["AppExe"]


def test_exe_y_archivos_se_llaman_pomogrowth_sin_el_9(tmp_path):
    """Desde la 1.0 el exe, la carpeta del build y el instalador se llaman
    PomoGrowth (hasta la 9.4, PomoGrowth9). Al actualizar desde la 9.4 el exe
    viejo se borra, para que no quede junto al nuevo, y si no se pudo (seguía
    abierto) se avisa."""
    defines, setup, sections = _defines(), _setup(), _sections()
    assert defines["AppExe"] == "PomoGrowth.exe"
    assert setup["OutputBaseFilename"] == "PomoGrowth_v{#AppVersion}_instalador"
    assert defines["OldAppExe"] == "PomoGrowth9.exe"
    assert 'Type: files; Name: "{app}\\{#OldAppExe}"' in sections["InstallDelete"]
    assert all('Filename: "{app}\\{#AppExe}"' in line
               for line in sections["Icons"] + sections["Run"])
    code = _code()
    step = code[code.index("procedure CurStepChanged"):]
    assert re.search(r"CurStep = ssPostInstall\) and FileExists\(ExpandConstant\("
                     r"'\{app\}\\\{#OldAppExe\}'\)\)", step)
    assert "MsgBox(CustomMessage('ExeViejo')" in step
    # Lo que necesita PyInstaller, al final: sin él, lo del .iss ya se comprobó
    calls = _run_spec(tmp_path)
    assert calls["EXE"]["name"] == calls["COLLECT"]["name"] == "PomoGrowth"
    assert defines["BuildDir"].endswith("\\" + calls["COLLECT"]["name"])


@pytest.mark.skipif(sys.platform != "win32", reason="mutex con nombre de Windows")
def test_desinstalar_pide_cerrar_la_app_abierta(monkeypatch):
    """El desinstalador no usa Restart Manager: busca el mutex que la app crea
    al abrirse (Config.APP_MUTEX) y pide cerrarla antes (salvo en silencio).
    Sin eso, desinstalar con la app abierta dejaba su exe y sus DLL."""
    import ctypes
    from utils import win_native
    assert _defines()["AppMutex"] == Config.APP_MUTEX
    code = _code()
    uninstall = code[code.index("function InitializeUninstall"):]
    uninstall = uninstall[:uninstall.index("end;\n\n")]
    assert "while not UninstallSilent and CheckForMutexes('{#AppMutex}') do" in uninstall
    assert "CustomMessage('CerrarAntes')" in uninstall and "Result := False" in uninstall
    main = (V9 / "main.py").read_text(encoding="utf-8")
    assert "hold_named_mutex(Config.APP_MUTEX)" in main
    assert main.index("singleton.acquire()") < main.index("hold_named_mutex(")
    # El mutex existe de verdad mientras el proceso vive (con otro nombre: la
    # app real podría estar abierta)
    name = f"PomoGrowth.Test.{os.getpid()}"
    monkeypatch.setattr(win_native, "_mutex", None)
    assert win_native.hold_named_mutex(name)
    open_mutex = ctypes.windll.kernel32.OpenMutexW
    open_mutex.restype = ctypes.c_void_p
    open_mutex.argtypes = (ctypes.c_uint32, ctypes.c_int, ctypes.c_wchar_p)
    handle = open_mutex(0x00100000, False, name)  # SYNCHRONIZE, como Inno
    assert handle
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_portable_no_empaqueta_datos_del_usuario(tmp_path):
    """scripts/make_portable.py: el zip lleva el exe, _internal, la licencia
    (LICENSE.txt) y el LEEME, y se niega si junto al exe del build hay datos
    (si se abrió para probarlo): los mismos que excluye el instalador."""
    import importlib.util
    import zipfile
    spec = importlib.util.spec_from_file_location(
        "make_portable", V9 / "scripts" / "make_portable.py")
    portable = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(portable)
    build, dist = tmp_path / "build", tmp_path / "dist"
    (build / "_internal").mkdir(parents=True)
    (build / "PomoGrowth.exe").write_bytes(b"MZ")
    (build / "_internal" / "base_library.zip").write_bytes(b"PK")
    assert portable.user_data(build) == []
    out = portable.make(build, dist, "1.0")
    assert out.name == "PomoGrowth_v1.0_portable.zip"
    with zipfile.ZipFile(out) as z:
        assert sorted(z.namelist()) == [
            "PomoGrowth_v1.0/LEEME_PORTABLE.txt", "PomoGrowth_v1.0/LICENSE.txt",
            "PomoGrowth_v1.0/PomoGrowth.exe", "PomoGrowth_v1.0/_internal/base_library.zip"]
        assert z.read("PomoGrowth_v1.0/LICENSE.txt") == (V9 / "LICENSE").read_bytes()
        leeme = z.read("PomoGrowth_v1.0/LEEME_PORTABLE.txt").decode("utf-8-sig")
        assert "GPL-3.0" in leeme and Config.GITHUB_URL in leeme and "\r\n" in leeme
    with pytest.raises(SystemExit, match="ya existe"):
        portable.make(build, dist, "1.0")
    for name in ("pomogrowth_data.json", "user_progress.json.bak",
                 "focus_config.json.corrupt-20261029_010203", "export.csv"):
        (build / name).write_text("{}", encoding="utf-8")
    for folder in ("logs", "assets"):
        (build / folder).mkdir()
    assert portable.user_data(build) == sorted([
        "assets", "export.csv", "focus_config.json.corrupt-20261029_010203", "logs",
        "pomogrowth_data.json", "user_progress.json.bak"])
    with pytest.raises(SystemExit, match="datos del usuario"):
        portable.make(build, tmp_path / "otra", "1.0")
    # Lo mismo que el instalador no copia del build
    build = [line for line in _sections()["Files"] if line.startswith('Source: "{#BuildDir}\\*"')]
    excludes = re.search(r'Excludes:\s*"([^"]*)"', build[0]).group(1).split(",")
    for suffix in portable.USER_DATA_SUFFIXES:
        assert f"\\*{suffix}" in excludes
    for folder in portable.USER_DATA_DIRS:
        assert f"\\{folder}" in excludes


def test_spec_empaqueta_los_recursos_que_usa_la_app(tmp_path):
    """En el exe los recursos se buscan en _internal (resource_base): el spec
    debe llevar cada uno que use la app. Sin icono.svg, la cabecera salía sin
    logo y sin ningún error."""
    from ui.theme import brand
    from ui.widgets import icon_button
    from utils.paths import app_icon_path, resource_base
    packaged = set()
    for src, dest in _run_spec(tmp_path)["Analysis"]["datas"]:
        source = V9 / src
        if source.is_dir():
            packaged |= {(Path(dest) / f.relative_to(source)).as_posix()
                         for f in source.rglob("*") if f.is_file()}
        else:
            packaged.add((Path(dest) / source.name).as_posix())
    needed = [brand.MASCOT_SVG, brand.WORDMARK_SVG, app_icon_path(),
              *icon_button.ICONS_DIR.glob("*.svg")]
    needed = [p.relative_to(resource_base()).as_posix() for p in needed]
    assert "assets/icons/compacto.svg" in needed  # el glob no venía vacío
    assert [p for p in needed if p not in packaged] == []


def test_spec_no_compila_sobre_una_carpeta_con_datos(tmp_path):
    """COLLECT vacía su carpeta entera (con --noconfirm, sin preguntar): con
    los datos del usuario junto al exe (una instalación en dist/), el spec se
    niega."""
    out = tmp_path / _run_spec(tmp_path)["COLLECT"]["name"]
    out.mkdir()
    (out / "pomogrowth_data.json").write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit, match="datos del usuario"):
        _run_spec(tmp_path)


def test_en_el_exe_el_icono_sale_de_internal(tmp_path, monkeypatch):
    """Congelada, el icono de la ventana y el de la bandeja van con los
    recursos (_internal), no junto al exe (ahí no estaba). main() no se puede
    ejecutar en un test: se comprueba que los dos usan app_icon_path()."""
    from ui.controllers.app_controller import AppController
    from utils import paths
    internal = tmp_path / "_internal"
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(internal), raising=False)
    monkeypatch.setattr(sys, "executable", str(tmp_path / "PomoGrowth.exe"))
    assert paths.app_base() == tmp_path
    assert paths.app_icon_path() == internal / "assets" / "icon.ico"
    assert "QIcon(str(app_icon_path()))" in (V9 / "main.py").read_text(encoding="utf-8")
    assert "QIcon(str(app_icon_path()))" in inspect.getsource(AppController._setup_tray)
