"""Sistema de recompensas, niveles y tienda."""
import logging
from pathlib import Path
from typing import Dict, List, Any
from dataclasses import dataclass, field, asdict, fields
from datetime import datetime

from utils.safe_json import save_json_atomic, load_json_safe

logger = logging.getLogger("PomoGrowth")


@dataclass
class UserProgress:
    """Progreso del usuario."""
    level: int = 1
    experience: int = 0
    coins: int = 0
    total_pomodoros: int = 0
    total_cycles: int = 0  # ciclos completos (N pomodoros + descansos)
    total_time_minutes: int = 0
    streak_days: int = 0
    last_pomodoro_date: str = ''  # 'YYYY-MM-DD' del último pomodoro (para la racha)
    achievements: List[str] = field(default_factory=list)
    purchased_items: List[str] = field(default_factory=list)
    current_theme: str = 'dark'
    current_icon_pack: str = 'default'
    current_background: str = 'none'
    current_sound_pack: str = 'default'
    current_pet: str = 'none'  # mascota pixel equipada (pet_cat/pet_dog/pet_rat)


@dataclass
class ShopItem:
    """Item de la tienda."""
    id: str
    name: str
    description: str
    price: int
    category: str  # 'theme', 'icon_pack', 'sound_pack', 'background', 'pet'
    preview_color: str = '#00f9a0'
    

class RewardsSystem:
    """Sistema de recompensas y progresión."""
    
    # XP necesaria por nivel
    XP_PER_LEVEL = {
        1: 0, 2: 100, 3: 250, 4: 450, 5: 700,
        6: 1000, 7: 1350, 8: 1750, 9: 2200, 10: 2700,
        11: 3250, 12: 3850, 13: 4500, 14: 5200, 15: 6000,
    }
    
    # Recompensas por acción
    REWARDS = {
        'pomodoro_completed': {'xp': 25, 'coins': 5},
        'cycle_completed': {'xp': 50, 'coins': 15},
        'day_streak_3': {'xp': 50, 'coins': 20},
        'day_streak_7': {'xp': 100, 'coins': 50},
        'day_streak_30': {'xp': 500, 'coins': 200},
        'level_up': {'coins': 50},
    }

    # Modo Libre: recompensa proporcional al tiempo, desde un mínimo de minutos
    FREE_SESSION_MIN_MINUTES = 5
    FREE_XP_PER_MINUTE = 1
    FREE_MINUTES_PER_COIN = 5

    # Items de la tienda
    SHOP_ITEMS = [
        # Temas
        ShopItem('theme_ocean', 'Tema Océano', 'Colores azules tranquilos', 100, 'theme', '#0ea5e9'),
        ShopItem('theme_forest', 'Tema Bosque', 'Verde natural y relajante', 100, 'theme', '#22c55e'),
        ShopItem('theme_sunset', 'Tema Atardecer', 'Naranjas y rosas cálidos', 150, 'theme', '#f97316'),
        ShopItem('theme_midnight', 'Tema Medianoche', 'Azul oscuro elegante', 150, 'theme', '#1e3a8a'),
        ShopItem('theme_cherry', 'Tema Cereza', 'Rosa vibrante energético', 200, 'theme', '#ec4899'),
        ShopItem('theme_matrix', 'Tema Matrix', 'Verde fosforescente tech', 250, 'theme', '#00ff41'),
        ShopItem('theme_golden', 'Tema Dorado', 'Oro premium lujoso', 500, 'theme', '#fbbf24'),
        
        # Packs de iconos
        ShopItem('icons_minimal', 'Iconos Minimalistas', 'Diseño limpio y simple', 80, 'icon_pack', '#9ca3af'),
        ShopItem('icons_emoji', 'Iconos Emoji Plus', 'Emojis mejorados y coloridos', 120, 'icon_pack', '#fbbf24'),
        ShopItem('icons_neon', 'Iconos Neón', 'Estilo cyberpunk brillante', 200, 'icon_pack', '#a855f7'),
        
        # Fondos animados
        ShopItem('bg_particles', 'Fondo Partículas', 'Partículas flotantes animadas', 150, 'background', '#4a9eff'),
        ShopItem('bg_waves', 'Fondo Ondas', 'Ondas suaves en movimiento', 180, 'background', '#0ea5e9'),
        ShopItem('bg_stars', 'Fondo Estrellas', 'Cielo estrellado animado', 250, 'background', '#1e293b'),
        
        # Packs de sonidos
        ShopItem('sounds_nature', 'Sonidos Naturaleza', 'Pájaros, agua, viento', 100, 'sound_pack', '#22c55e'),
        ShopItem('sounds_lofi', 'Sonidos Lo-Fi', 'Beats relajantes', 150, 'sound_pack', '#a855f7'),
        ShopItem('sounds_8bit', 'Sonidos 8-Bit', 'Retro arcade style', 120, 'sound_pack', '#f59e0b'),

        # Mascotas pixel (estilo 8-bit) — te acompañan y aconsejan con IA
        ShopItem('pet_cat', 'Gato Pixel', 'Compañero felino 8-bit: te observa trabajar y da consejos', 300, 'pet', '#f59e0b'),
        ShopItem('pet_dog', 'Perro Pixel', 'Fiel amigo 8-bit que celebra cada pomodoro contigo', 300, 'pet', '#a1887f'),
        ShopItem('pet_rat', 'Rata Pixel', 'Roedor astuto 8-bit con trucos de productividad', 350, 'pet', '#90a4ae'),
    ]
    
    def __init__(self, progress_file: Path = Path('user_progress.json')):
        self.progress_file = progress_file
        self.progress = UserProgress()
        self.load_error = None
        self.load()

    def load(self):
        """Carga el progreso del usuario sin destruirlo ante errores.

        Un archivo dañado se preserva (copia .corrupt-*) y se intenta el .bak;
        solo se parte de cero si no hay nada recuperable, dejando constancia
        en self.load_error para que la UI pueda avisar.
        """
        if not self.progress_file.exists():
            self.save()
            return
        data, error = load_json_safe(self.progress_file, default=None)
        if error:
            self.load_error = error
            logger.error("Progreso no recuperable, se parte de cero: %s", error)
            return
        if not isinstance(data, dict):
            self.load_error = f"Formato inesperado en {self.progress_file.name}"
            logger.error(self.load_error)
            return
        valid_fields = {f.name for f in fields(UserProgress)}
        unknown = set(data) - valid_fields
        if unknown:
            logger.warning("Claves desconocidas ignoradas en %s: %s",
                           self.progress_file.name, sorted(unknown))
        self.progress = UserProgress(**{k: v for k, v in data.items() if k in valid_fields})

    def save(self):
        """Guarda el progreso del usuario (escritura atómica con backup)."""
        if not save_json_atomic(self.progress_file, asdict(self.progress)):
            logger.error("No se pudo guardar el progreso en %s", self.progress_file)
    
    def _update_streak(self):
        """Actualiza la racha de días consecutivos con pomodoros.

        Antes la racha existía pero nada la incrementaba (siempre 0):
        mismo día → se mantiene; día siguiente → +1; hueco → reinicia a 1.
        """
        today = datetime.now().date()
        last = None
        try:
            if self.progress.last_pomodoro_date:
                last = datetime.strptime(self.progress.last_pomodoro_date, '%Y-%m-%d').date()
        except ValueError:
            last = None
        if last == today:
            self.progress.streak_days = max(1, self.progress.streak_days)
        elif last is not None and (today - last).days == 1:
            self.progress.streak_days += 1
        else:
            self.progress.streak_days = 1
        self.progress.last_pomodoro_date = today.strftime('%Y-%m-%d')

    def add_pomodoro(self, duration_minutes: int) -> Dict[str, Any]:
        """Registra un pomodoro completado y otorga recompensas."""
        rewards = self.REWARDS['pomodoro_completed'].copy()

        self._update_streak()
        self.progress.total_pomodoros += 1
        self.progress.total_time_minutes += duration_minutes

        # Bonus por racha (se otorga de verdad, no solo se calcula)
        if self.progress.streak_days >= 30:
            rewards['xp'] += 10
            rewards['coins'] += 5
        elif self.progress.streak_days >= 7:
            rewards['xp'] += 5
            rewards['coins'] += 2

        self.progress.experience += rewards['xp']
        self.progress.coins += rewards['coins']

        # Verificar level up
        level_up = self.check_level_up()

        self.save()

        return {
            'xp_gained': rewards['xp'],
            'coins_gained': rewards['coins'],
            'level_up': level_up,
            'current_level': self.progress.level,
            'current_xp': self.progress.experience,
            'current_coins': self.progress.coins,
            'streak_days': self.progress.streak_days,
        }
    
    def add_free_session(self, duration_minutes: int) -> Dict[str, Any]:
        """Registra una sesión del modo Libre: 1 XP por minuto y 1 moneda por
        cada 5 min. Suma al tiempo total y a la racha como un pomodoro, pero
        no cuenta como pomodoro. Por debajo del mínimo no otorga nada."""
        minutes = max(0, int(duration_minutes))
        if minutes < self.FREE_SESSION_MIN_MINUTES:
            return {'xp_gained': 0, 'coins_gained': 0, 'level_up': False,
                    'current_level': self.progress.level,
                    'streak_days': self.progress.streak_days}
        xp = minutes * self.FREE_XP_PER_MINUTE
        coins = minutes // self.FREE_MINUTES_PER_COIN
        self._update_streak()
        self.progress.total_time_minutes += minutes
        self.progress.experience += xp
        self.progress.coins += coins
        level_up = self.check_level_up()
        self.save()
        return {
            'xp_gained': xp,
            'coins_gained': coins,
            'level_up': level_up,
            'current_level': self.progress.level,
            'current_xp': self.progress.experience,
            'current_coins': self.progress.coins,
            'streak_days': self.progress.streak_days,
        }

    def add_cycle(self) -> Dict[str, Any]:
        """Registra un ciclo completo (N pomodoros + descansos) con su bonus."""
        bonus = self.REWARDS['cycle_completed']
        self.progress.total_cycles += 1
        self.progress.experience += bonus['xp']
        self.progress.coins += bonus['coins']
        level_up = self.check_level_up()
        self.save()
        return {
            'xp_gained': bonus['xp'],
            'coins_gained': bonus['coins'],
            'level_up': level_up,
            'current_level': self.progress.level,
            'total_cycles': self.progress.total_cycles,
        }

    def check_level_up(self) -> bool:
        """Sube todos los niveles alcanzados con la XP actual (puede ser más de uno)."""
        leveled_up = False
        next_level = self.progress.level + 1
        while (next_level in self.XP_PER_LEVEL
               and self.progress.experience >= self.XP_PER_LEVEL[next_level]):
            self.progress.level = next_level
            self.progress.coins += self.REWARDS['level_up']['coins']
            leveled_up = True
            next_level = self.progress.level + 1
        return leveled_up
    
    def get_xp_for_next_level(self) -> int:
        """Retorna XP necesario para siguiente nivel."""
        next_level = self.progress.level + 1
        if next_level in self.XP_PER_LEVEL:
            return self.XP_PER_LEVEL[next_level]
        return self.progress.experience
    
    def get_stats(self) -> Dict[str, Any]:
        """Retorna estadísticas del usuario para mostrar en UI."""
        return {
            'level': self.progress.level,
            'xp': self.progress.experience,
            'xp_for_next_level': self.get_xp_for_next_level(),
            'coins': self.progress.coins,
            'total_pomodoros': self.progress.total_pomodoros,
            'total_cycles': self.progress.total_cycles,
            'total_time_minutes': self.progress.total_time_minutes,
            'streak_days': self.progress.streak_days,
        }
    
    def purchase_item(self, item_id: str) -> tuple[bool, str]:
        """Intenta comprar un item."""
        item = next((i for i in self.SHOP_ITEMS if i.id == item_id), None)
        if not item:
            return False, "Item no encontrado"
        
        if item_id in self.progress.purchased_items:
            return False, "Ya tienes este item"
        
        if self.progress.coins < item.price:
            return False, f"Necesitas {item.price - self.progress.coins} monedas más"
        
        self.progress.coins -= item.price
        self.progress.purchased_items.append(item_id)
        self.save()
        
        return True, f"¡{item.name} comprado!"
    
    def equip_item(self, item_id: str) -> bool:
        """Equipa un item comprado (cada categoría tiene su slot)."""
        if item_id not in self.progress.purchased_items:
            return False

        item = next((i for i in self.SHOP_ITEMS if i.id == item_id), None)
        if not item:
            return False

        if item.category == 'theme':
            self.progress.current_theme = item_id
        elif item.category == 'icon_pack':
            self.progress.current_icon_pack = item_id
        elif item.category == 'background':
            self.progress.current_background = item_id
        elif item.category == 'sound_pack':
            self.progress.current_sound_pack = item_id
        elif item.category == 'pet':
            self.progress.current_pet = item_id
        else:
            return False

        self.save()
        return True
    
    def get_available_items(self) -> List[ShopItem]:
        """Retorna items disponibles para comprar."""
        return [item for item in self.SHOP_ITEMS if item.id not in self.progress.purchased_items]
    
    def get_owned_items(self) -> List[ShopItem]:
        """Retorna items que el usuario posee."""
        return [item for item in self.SHOP_ITEMS if item.id in self.progress.purchased_items]
