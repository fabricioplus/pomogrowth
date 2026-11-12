"""Agregación de time_chunks para estadísticas (sin UI).

Extraído de pomogrowth_v8_ultra/app_ultra.py (extract_from_v8.py, una sola vez).
A partir de aquí este archivo es fuente normal de v9.
"""
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Dict, Any, List


class StatsCalculator:
    def __init__(self, chunks: List[Dict[str, Any]]): self.chunks = chunks; self.daily_totals = self._aggregate_by_day()
    def _aggregate_by_day(self) -> defaultdict:
        daily_totals = defaultdict(float)
        for chunk in self.chunks:
            if 'timestamp' in chunk and chunk.get('type', 'pomodoro') == 'pomodoro':
                daily_totals[datetime.fromisoformat(chunk['timestamp']).date()] += chunk.get('duration', 0)
        return daily_totals
    def get_summary(self) -> Dict[str, Any]:
        total_time = sum(c.get('duration',0) for c in self.chunks if c.get('type', 'pomodoro') == 'pomodoro'); active_days = len(self.daily_totals)
        streak = self._calculate_streak(sorted(self.daily_totals.keys(), reverse=True)); today_time = self.daily_totals.get(datetime.now().date(), 0)
        # Cálculos adicionales
        last7_dates = [datetime.now().date() - timedelta(days=i) for i in range(0,7)]
        avg7 = sum(self.daily_totals.get(d,0) for d in last7_dates)/7.0
        pomodoros_total = sum(1 for c in self.chunks if c.get('type','pomodoro')=='pomodoro')
        pomodoros_hoy = sum(1 for c in self.chunks if c.get('type','pomodoro')=='pomodoro' and 'timestamp' in c and datetime.fromisoformat(c['timestamp']).date()==datetime.now().date())
        break_total = sum(c.get('duration',0) for c in self.chunks if c.get('type','pomodoro')!='pomodoro')
        focus_total = total_time
        ratio_break = (break_total/(focus_total+break_total)) if (focus_total+break_total)>0 else 0.0
        return {
            'total_time': total_time,
            'active_days': active_days,
            'streak': streak,
            'today_time': today_time,
            'avg7': avg7,
            'pomodoros_total': pomodoros_total,
            'pomodoros_hoy': pomodoros_hoy,
            'ratio_break': ratio_break
        }
    def get_chart_data(self, days=7) -> (List[str], List[float], List[float]):
        # Día: barras por hora (0-23) del día actual
        if days == 1:
            today = datetime.now().date()
            hourly = [0.0]*24
            for c in self.chunks:
                if c.get('type','pomodoro')=='pomodoro' and 'timestamp' in c:
                    try:
                        ts = datetime.fromisoformat(c['timestamp'])
                        if ts.date() == today:
                            h = ts.hour
                            hourly[h] += c.get('duration',0)/60.0
                    except Exception:
                        continue
            labels = [f"{h:02d}h" for h in range(24)]
            bar_data = hourly
            # Promedio móvil simple 3 horas
            line_data = [sum(bar_data[max(0,i-2):i+1])/(i-max(0,i-2)+1) for i in range(24)]
            return labels, bar_data, line_data
        # Semana/Mes: barras por día dentro del rango seleccionado
        labels, bar_data = [], []
        today = datetime.now().date()
        for i in range(days - 1, -1, -1):
            date = today - timedelta(days=i)
            label_fmt = '%a' if days <= 7 else '%d'
            labels.append(date.strftime(label_fmt))
            bar_data.append(self.daily_totals.get(date, 0) / 60)
        # Tendencia: promedio móvil de 3
        line_data = [sum(bar_data[:i+1])/(i+1) if i<2 else sum(bar_data[i-2:i+1])/3 for i in range(len(bar_data))]
        return labels, bar_data, line_data
    def get_hourly_by_weekday(self, days=7):
        # Matriz 7x24 de minutos de concentración
        # Filtramos a la ventana de días seleccionada
        start_date = (datetime.now().date() - timedelta(days=days-1)) if days and days>1 else (datetime.now().date())
        grid = [[0.0 for _ in range(24)] for _ in range(7)]  # 0=Lunes ... 6=Domingo
        for c in self.chunks:
            if c.get('type','pomodoro')=='pomodoro' and 'timestamp' in c:
                try:
                    ts = datetime.fromisoformat(c['timestamp'])
                    d = ts.date()
                    if d < start_date: continue
                    wd = (ts.weekday())  # 0-6
                    h = ts.hour
                    grid[wd][h] += c.get('duration',0)/60.0
                except Exception:
                    continue
        y_labels = ['Lun','Mar','Mié','Jue','Vie','Sáb','Dom']
        x_labels = [f"{h:02d}h" for h in range(24)]
        return y_labels, x_labels, grid
    def get_session_durations(self, days=7) -> List[float]:
        # Devuelve duraciones de pomodoro del rango en minutos
        start_date = (datetime.now().date() - timedelta(days=days-1)) if days and days>1 else (datetime.now().date())
        durations = []
        for c in self.chunks:
            if c.get('type','pomodoro')=='pomodoro' and 'timestamp' in c:
                try:
                    ts = datetime.fromisoformat(c['timestamp'])
                    if ts.date() < start_date: continue
                    durations.append(c.get('duration',0)/60.0)
                except Exception:
                    continue
        return durations
    def get_success_counts(self, target_min: float, days=7, tolerance: float=0.8) -> Dict[str,int]:
        # Cuenta sesiones "exitosas" si alcanzan >= tolerance*target_min
        durations = self.get_session_durations(days)
        threshold = max(1.0, tolerance * float(target_min))
        success = sum(1 for d in durations if d >= threshold)
        fail = max(0, len(durations) - success)
        return { 'success': success, 'fail': fail }
    def get_daily_streak_series(self, days=7) -> (List[str], List[int]):
        # Racha acumulada por día en el rango (concentración >0 cuenta como día activo)
        today = datetime.now().date(); labels=[]; values=[]
        active_days = set(d for d,v in self.daily_totals.items() if v>0)
        for i in range(days - 1, -1, -1):
            date = today - timedelta(days=i)
            labels.append(date.strftime('%d/%m'))
            # Calcular racha hasta este día
            streak=0; cur=date
            while cur in active_days:
                streak+=1; cur = cur - timedelta(days=1)
            values.append(streak)
        return labels, values
    def _calculate_streak(self, dates: List[datetime.date]) -> int:
        if not dates: return 0
        today = datetime.now().date()
        if dates[0] not in [today, today - timedelta(days=1)]: return 0
        streak, expected_date = 1, dates[0] - timedelta(days=1)
        for i in range(1, len(dates)):
            if dates[i] == expected_date: streak += 1; expected_date -= timedelta(days=1)
            else: break
        return streak

