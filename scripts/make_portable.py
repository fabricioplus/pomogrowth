"""Versión portable: comprime el build en dist/PomoGrowth_v<versión>_portable.zip.

Uso: python scripts/make_portable.py   (desde la raíz del repositorio, tras
compilar con PomoGrowth.spec en build/release)

El zip lleva una carpeta PomoGrowth_v<versión>/ con PomoGrowth.exe, _internal/,
LICENSE.txt (la GPL va con el programa) y LEEME_PORTABLE.txt (UTF-8 con BOM y
CRLF, para el Bloc de notas). Se niega si junto al exe del build hay datos del
usuario (si se abrió para probarlo, ahí quedan sus JSON, con el token de
Asana, copias, logs y música: los mismos que excluye el instalador) y si el
zip ya existe.
"""
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from core.config import Config  # noqa: E402

BUILD = ROOT / "build" / "release" / "PomoGrowth"
LICENSE = ROOT / "LICENSE"
# Lo que la app escribe junto al exe (installer/PomoGrowth.iss lo excluye igual)
USER_DATA_SUFFIXES = (".json", ".bak", ".tmp", ".csv", ".xlsx")
USER_DATA_DIRS = ("logs", "assets")

LEEME = """PomoGrowth {version} — versión portable
{rule}

1. Descomprime esta carpeta donde quieras, mejor en una ruta corta
   (p. ej. D:\\Apps\\PomoGrowth): Windows no carga las DLL de rutas de más
   de 260 caracteres.
2. Abre PomoGrowth.exe.

Tus datos (tareas, progreso, configuración y logs) se guardan junto al exe,
en esta misma carpeta. Para traer los datos de una versión anterior, cierra
las dos apps y copia aquí, desde la carpeta anterior:
  pomogrowth_data.json, user_progress.json y focus_config.json
  (y la carpeta assets\\music\\lofi si tenías música en ella).

El modo enfoque edita el archivo hosts de Windows: para usarlo, abre
PomoGrowth como administrador.

PomoGrowth es software libre bajo la licencia GPL-3.0 o posterior (ver
LICENSE.txt). Código fuente: {repo}

Es gratis, y su versión Comunidad (local) lo será siempre. Si te ayuda,
puedes apoyarnos en {kofi}
"""


def user_data(build: Path) -> list:
    """Lo que hay junto al exe y no es del programa."""
    return sorted(p.name for p in build.iterdir()
                  if (p.is_file() and p.suffix.lower() in USER_DATA_SUFFIXES)
                  or (p.is_dir() and p.name in USER_DATA_DIRS)
                  or ".corrupt-" in p.name)


def make(build: Path, out_dir: Path, version: str) -> Path:
    """Escribe el zip y devuelve su ruta; SystemExit si no se puede."""
    folder = f"PomoGrowth_v{version}"
    out = out_dir / f"{folder}_portable.zip"
    if not (build / "PomoGrowth.exe").is_file():
        sys.exit(f"No hay build en {build}: compila antes con PomoGrowth.spec")
    found = user_data(build)
    if found:
        sys.exit(f"El build tiene datos del usuario ({', '.join(found)}): compila de "
                 "nuevo sin abrir el exe, o muévelos antes")
    if out.exists():
        sys.exit(f"{out} ya existe")
    title = f"PomoGrowth {version} — versión portable"
    readme = LEEME.format(version=version, rule="=" * len(title),
                          repo=Config.GITHUB_URL, kofi=Config.KOFI_URL)
    out_dir.mkdir(parents=True, exist_ok=True)
    files = sorted(p for p in build.rglob("*") if p.is_file())
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for f in files:
            z.write(f, f"{folder}/{f.relative_to(build).as_posix()}")
        z.write(LICENSE, f"{folder}/LICENSE.txt")
        z.writestr(f"{folder}/LEEME_PORTABLE.txt",
                   readme.replace("\n", "\r\n").encode("utf-8-sig"))
    with zipfile.ZipFile(out) as z:
        if z.testzip() is not None:
            sys.exit(f"{out} salió dañado")
    return out


def main() -> int:
    out = make(BUILD, ROOT / "dist", Config.APP_VERSION)
    with zipfile.ZipFile(out) as z:
        count = len(z.namelist())
    print(f"{out.name}: {out.stat().st_size} bytes, {count} archivos")
    return 0


if __name__ == "__main__":
    sys.exit(main())
