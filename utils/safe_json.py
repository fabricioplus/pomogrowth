"""Persistencia JSON segura: escritura atómica y respaldo ante corrupción.

Escribir directamente sobre el archivo original deja una ventana en la que un
cierre abrupto (apagón, kill del proceso) trunca el JSON y se pierden todos los
datos. Aquí se escribe a un archivo temporal y se reemplaza con os.replace(),
que es atómico en Windows y POSIX.
"""
import json
import logging
import os
import shutil
import time

logger = logging.getLogger("PomoGrowth")


def save_json_atomic(path, data, keep_backup=True):
    """Guarda `data` como JSON en `path` de forma atómica.

    Si keep_backup es True, conserva la versión anterior válida en `<path>.bak`
    antes de reemplazarla. Devuelve True si se guardó correctamente.
    """
    path = os.fspath(path)
    tmp_path = f"{path}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        if keep_backup and os.path.exists(path):
            try:
                shutil.copy2(path, f"{path}.bak")
            except OSError as e:
                logger.warning("No se pudo crear backup de %s: %s", path, e)
        os.replace(tmp_path, path)
        return True
    except (OSError, TypeError, ValueError) as e:
        logger.error("Error guardando %s: %s", path, e)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        return False


def load_json_safe(path, default=None):
    """Carga JSON desde `path`.

    Devuelve (data, error):
      - (dict/list, None) si cargó bien.
      - (default, None) si el archivo no existe.
      - (data_del_backup, None) si el principal está corrupto pero el .bak es válido
        (el corrupto se preserva como `<path>.corrupt-<timestamp>` para diagnóstico).
      - (default, mensaje_de_error) si no hay forma de recuperar los datos.

    Nunca lanza: el llamador decide cómo avisar al usuario si error no es None.
    """
    path = os.fspath(path)
    if not os.path.exists(path):
        return default, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), None
    except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e:
        logger.error("Archivo dañado o ilegible %s: %s", path, e)
        corrupt_copy = f"{path}.corrupt-{time.strftime('%Y%m%d_%H%M%S')}"
        try:
            shutil.copy2(path, corrupt_copy)
            logger.info("Copia del archivo dañado preservada en %s", corrupt_copy)
        except OSError:
            corrupt_copy = None
        backup_path = f"{path}.bak"
        if os.path.exists(backup_path):
            try:
                with open(backup_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info("Datos recuperados desde el backup %s", backup_path)
                return data, None
            except (json.JSONDecodeError, OSError, UnicodeDecodeError) as e2:
                logger.error("El backup %s también está dañado: %s", backup_path, e2)
        detail = f" (copia preservada en {os.path.basename(corrupt_copy)})" if corrupt_copy else ""
        return default, f"No se pudo leer {os.path.basename(path)}: {e}{detail}"
