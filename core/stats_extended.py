"""Calculadora de estadísticas COMPLETA (por periodo, heatmap, histograma, racha).

Extraída de v8 ui/views/stats_view_complete.py (L915-1189) sin cambios.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Any, List


class StatsCalculator:
    """Calculadora de estadísticas COMPLETA."""
    
    def __init__(self, chunks: List[Dict[str, Any]]):
        self.chunks = chunks
        self.daily_totals = self._aggregate_by_day()

    @staticmethod
    def _is_pomodoro(chunk: Dict[str, Any]) -> bool:
        """Tramo de un pomodoro. Los del modo Libre (mode 'free') son tiempo
        enfocado (type 'pomodoro') pero no pomodoros: no entran en los
        conteos ni en las duraciones de sesión frente al objetivo."""
        return chunk.get('type', 'pomodoro') == 'pomodoro' and chunk.get('mode') != 'free'

    def _aggregate_by_day(self) -> defaultdict:
        """Agrupa tiempo de POMODOROS por día (no incluye breaks)."""
        daily_totals = defaultdict(float)
        
        for chunk in self.chunks:
            # Solo contar pomodoros, no breaks
            if chunk.get('type', 'pomodoro') != 'pomodoro':
                continue
            try:
                # IMPORTANTE: El campo es 'timestamp', no 'date'
                date_obj = datetime.fromisoformat(chunk['timestamp'])
                day_key = date_obj.strftime('%Y-%m-%d')
                daily_totals[day_key] += chunk.get('duration', 0)
            except Exception as e:
                print(f"Error procesando chunk: {e}, chunk={chunk}")
                pass
        return daily_totals
    
    def _effective_days(self, days: int) -> int:
        """Convierte days=0 (histórico) en el rango real de días con datos (cap 365)."""
        if days:
            return days
        if not self.daily_totals:
            return 7
        try:
            first_date = datetime.strptime(min(self.daily_totals), '%Y-%m-%d').date()
            span = (datetime.now().date() - first_date).days + 1
            return max(7, min(span, 365))
        except ValueError:
            return 7

    def get_summary(self, period_days: int = None) -> Dict[str, Any]:
        """Obtiene resumen COMPLETO con todas las métricas (opcionalmente filtrado por período)."""
        # Filtrar chunks por período si se especifica
        if period_days:
            cutoff_date = datetime.now() - timedelta(days=period_days)
            # Recalcular daily_totals para el período
            daily_totals_period = defaultdict(float)
            for chunk in self.chunks:
                if chunk.get('type', 'pomodoro') != 'pomodoro':
                    continue
                try:
                    date_obj = datetime.fromisoformat(chunk['timestamp'])
                    if date_obj >= cutoff_date:
                        day_key = date_obj.strftime('%Y-%m-%d')
                        daily_totals_period[day_key] += chunk.get('duration', 0)
                except Exception:
                    pass
            total_time = sum(daily_totals_period.values())
        else:
            daily_totals_period = self.daily_totals
            total_time = sum(self.daily_totals.values())
        
        # Tiempo hoy
        today = datetime.now().strftime('%Y-%m-%d')
        today_time = self.daily_totals.get(today, 0)
        
        # Promedio 7 días
        last_7_days = [(datetime.now() - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(7)]
        time_7d = sum(self.daily_totals.get(d, 0) for d in last_7_days)
        avg7 = time_7d / 7 if time_7d > 0 else 0
        
        # Pomodoros (filtrados por período)
        cutoff_for_count = datetime.now() - timedelta(days=period_days) if period_days else None
        
        # Contar pomodoros de hoy
        pomodoros_hoy = 0
        for c in self.chunks:
            if self._is_pomodoro(c):
                try:
                    if datetime.fromisoformat(c.get('timestamp', '')).strftime('%Y-%m-%d') == today:
                        pomodoros_hoy += 1
                except Exception:
                    pass
        
        # Contar pomodoros totales según período
        if period_days and cutoff_for_count:
            pomodoros_total = 0
            for c in self.chunks:
                if self._is_pomodoro(c):
                    try:
                        if datetime.fromisoformat(c.get('timestamp', '')) >= cutoff_for_count:
                            pomodoros_total += 1
                    except Exception:
                        pass
        else:
            pomodoros_total = sum(1 for c in self.chunks if self._is_pomodoro(c))
        
        # Ratio de descanso (filtrado por período)
        total_pomo = 0
        total_break = 0
        for c in self.chunks:
            try:
                if period_days and cutoff_for_count:
                    date_obj = datetime.fromisoformat(c.get('timestamp', ''))
                    if date_obj < cutoff_for_count:
                        continue
            except Exception:
                continue
            
            if c.get('type', 'pomodoro') == 'pomodoro':
                total_pomo += c.get('duration', 0)
            elif c.get('type') == 'break':
                total_break += c.get('duration', 0)
        
        ratio_break = total_break / (total_pomo + total_break) if (total_pomo + total_break) > 0 else 0

        # Ciclos completos (chunks de pomodoro marcados con cycle_completed)
        cycles_total = 0
        for c in self.chunks:
            if c.get('type', 'pomodoro') != 'pomodoro' or not c.get('cycle_completed'):
                continue
            try:
                if cutoff_for_count and datetime.fromisoformat(c.get('timestamp', '')) < cutoff_for_count:
                    continue
            except Exception:
                continue
            cycles_total += 1

        return {
            'total_time': total_time,
            'today_time': today_time,
            'avg7': avg7,
            'streak': self._calculate_streak(),
            'pomodoros_hoy': pomodoros_hoy,
            'pomodoros_total': pomodoros_total,
            'cycles_total': cycles_total,
            'ratio_break': ratio_break
        }
    
    def _calculate_streak(self) -> int:
        """Calcula racha de días consecutivos."""
        if not self.daily_totals:
            return 0
        
        streak = 0
        current_date = datetime.now().date()
        
        while True:
            day_key = current_date.strftime('%Y-%m-%d')
            if day_key in self.daily_totals and self.daily_totals[day_key] > 0:
                streak += 1
                current_date -= timedelta(days=1)
            else:
                break
        
        return streak
    
    def get_chart_data(self, days: int):
        """Obtiene datos para gráfico de barras (solo pomodoros). days=0 → histórico."""
        days = self._effective_days(days)
        if days == 1:
            # Por horas
            labels = [f"{h:02d}:00" for h in range(24)]
            bar_data = [0.0] * 24
            
            today = datetime.now().strftime('%Y-%m-%d')
            for chunk in self.chunks:
                # Solo pomodoros
                if chunk.get('type', 'pomodoro') != 'pomodoro':
                    continue
                try:
                    date_obj = datetime.fromisoformat(chunk['timestamp'])
                    if date_obj.strftime('%Y-%m-%d') == today:
                        hour = date_obj.hour
                        bar_data[hour] += chunk.get('duration', 0) / 60  # A minutos
                except Exception:
                    pass
        else:
            # Por días
            labels = []
            bar_data = []
            for i in range(days - 1, -1, -1):
                date = datetime.now().date() - timedelta(days=i)
                day_key = date.strftime('%Y-%m-%d')
                labels.append(date.strftime('%d/%m'))
                bar_data.append(self.daily_totals.get(day_key, 0) / 60)  # A minutos
        
        # Línea de tendencia (promedio móvil simple)
        line_data = []
        window = 3
        for i in range(len(bar_data)):
            start = max(0, i - window + 1)
            end = i + 1
            avg = sum(bar_data[start:end]) / len(bar_data[start:end])
            line_data.append(avg)
        
        return labels, bar_data, line_data
    
    def get_hourly_by_weekday(self, days: int):
        """Datos para heatmap de horas vs días de semana (filtrado por período)."""
        # Días de la semana
        y_labels = ['Lun', 'Mar', 'Mié', 'Jue', 'Vie', 'Sáb', 'Dom']
        x_labels = [f"{h:02d}h" for h in range(24)]
        
        # Grid 7x24
        grid = [[0.0 for _ in range(24)] for _ in range(7)]

        # Calcular fecha límite (days=0 → sin filtro, histórico completo)
        cutoff_date = datetime.now() - timedelta(days=days) if days else None

        for chunk in self.chunks:
            # Solo pomodoros
            if chunk.get('type', 'pomodoro') != 'pomodoro':
                continue
            try:
                date_obj = datetime.fromisoformat(chunk['timestamp'])
                # Filtrar por período
                if cutoff_date and date_obj < cutoff_date:
                    continue
                weekday = date_obj.weekday()  # 0=Lun, 6=Dom
                hour = date_obj.hour
                grid[weekday][hour] += chunk.get('duration', 0) / 60  # A minutos
            except Exception:
                pass
        
        return y_labels, x_labels, grid
    
    def get_session_durations(self, days: int) -> List[float]:
        """Obtiene lista de duraciones de sesiones (days=0 → histórico completo)."""
        durations = []
        cutoff_date = datetime.now() - timedelta(days=days) if days else None

        for chunk in self.chunks:
            if self._is_pomodoro(chunk):
                try:
                    date_obj = datetime.fromisoformat(chunk['timestamp'])
                    if cutoff_date is None or date_obj >= cutoff_date:
                        durations.append(chunk.get('duration', 0) / 60)  # A minutos
                except (KeyError, ValueError, TypeError):
                    pass
        return durations
    
    def get_success_counts(self, target_min: float, days: int) -> Dict[str, int]:
        """Cuenta pomodoros completados vs incompletos (filtrado por período)."""
        success = 0
        fail = 0
        
        target_seconds = target_min * 60
        cutoff_date = datetime.now() - timedelta(days=days) if days else None

        for chunk in self.chunks:
            if self._is_pomodoro(chunk):
                try:
                    date_obj = datetime.fromisoformat(chunk['timestamp'])
                    if cutoff_date is None or date_obj >= cutoff_date:
                        duration = chunk.get('duration', 0)
                        if duration >= target_seconds * 0.9:  # 90% del objetivo
                            success += 1
                        else:
                            fail += 1
                except (KeyError, ValueError, TypeError):
                    pass
        
        return {'success': success, 'fail': fail}
    
    def get_daily_streak_series(self, days: int):
        """Serie de datos de racha por día (days=0 → rango histórico)."""
        days = self._effective_days(days)
        labels = []
        values = []

        for i in range(days - 1, -1, -1):
            date = datetime.now().date() - timedelta(days=i)
            day_key = date.strftime('%Y-%m-%d')
            labels.append(date.strftime('%d/%m'))
            
            # Calcular racha hasta ese día
            streak = 0
            current = date
            while True:
                check_key = current.strftime('%Y-%m-%d')
                if check_key in self.daily_totals and self.daily_totals[check_key] > 0:
                    streak += 1
                    current -= timedelta(days=1)
                else:
                    break
            
            values.append(streak)
        
        return labels, values
