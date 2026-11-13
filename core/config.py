"""Configuración global de PomoGrowth (sin nada visual: la paleta vive en ui/theme)."""
from pathlib import Path


class Config:
    DATA_FILE = Path("pomogrowth_data.json")

    # Puertos distintos a v8 para poder convivir con ella en la misma máquina
    WEBHOOK_PORT = 8081
    SINGLETON_PORT = 61968
    N8N_WEBHOOK_URL = ""

    ASANA_API_URL = "https://app.asana.com/api/1.0"
    ASANA_POLL_INTERVAL_MS = 60000

    APP_NAME = "PomoGrowth"
    # 1.0: primera versión pública (las 9.0 a 9.4 fueron internas)
    APP_VERSION = "1.0.1"
    # No cambiarlo: con él Windows agrupa la ventana con sus accesos directos
    # y anclados (el «v9» es de cuando se creó, no la versión)
    APP_USER_MODEL_ID = "PomoGrowth.App.v9"
    # Mutex con nombre mientras la app está abierta: el desinstalador
    # (installer/PomoGrowth.iss, AppMutex) lo busca para pedir que se cierre
    APP_MUTEX = "PomoGrowth.App.Running"

    # Enlaces del proyecto y de GrowthLab: el único sitio donde corregirlos
    GITHUB_URL = "https://github.com/fabricioplus/pomogrowth"  # «Fabricioplus» (Ajustes)
    GROWTHLAB_URL = "https://growthlab.pe"
    KOFI_URL = "https://ko-fi.com/L3Q421O7ZW"  # «Donar» (despedida)
    # Redes de la despedida: por cuenta, (red, enlace) en el orden en que se
    # muestran. Verificados en la web de GrowthLab: Facebook, Instagram y
    # LinkedIn de @xgrowthlab y el LinkedIn de @fabricioplus; el resto sigue
    # el mismo usuario
    SOCIAL_LINKS = (
        ("@xgrowthlab", (
            ("facebook", "https://facebook.com/xgrowthlab"),
            ("instagram", "https://instagram.com/xgrowthlab"),
            ("youtube", "https://youtube.com/@xgrowthlab"),
            ("tiktok", "https://tiktok.com/@xgrowthlab"),
            ("linkedin", "https://linkedin.com/company/growthlabpe"),
        )),
        ("@fabricioplus", (
            ("facebook", "https://facebook.com/fabricioplus"),
            ("instagram", "https://instagram.com/fabricioplus"),
            ("youtube", "https://youtube.com/@fabricioplus"),
            ("tiktok", "https://tiktok.com/@fabricioplus"),
            ("linkedin", "https://linkedin.com/in/fabricioplus"),
        )),
    )

    # Duraciones por defecto del timer (persistidas en settings desde v9)
    DEFAULT_POMODORO_MIN = 25
    DEFAULT_BREAK_MIN = 5
    DEFAULT_LONG_BREAK_MIN = 15
    DEFAULT_POMODOROS_PER_CYCLE = 4
