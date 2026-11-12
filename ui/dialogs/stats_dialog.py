"""Estadísticas completas: periodo, tarjetas resumen, 5 gráficos matplotlib
(QtAgg) y tablas por proyecto/tarea. Port de stats_view_complete (v8) con el
lenguaje visual del rediseño: tarjetas, control segmentado y color semántico.

matplotlib se importa de forma perezosa: solo paga su coste quien abre stats.
Su estilo sale de los tokens del tema vía rcParams (_apply_mpl_style): misma
tipografía (Segoe UI Variable) y paleta que el resto de la interfaz. Los
títulos de cada gráfico son QLabel del encabezado de su tarjeta, no set_title.
"""
import csv
import math
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QCursor
from PySide6.QtWidgets import (QButtonGroup, QFileDialog, QFrame, QGridLayout,
                               QHBoxLayout, QLabel, QPushButton, QScrollArea,
                               QSizePolicy, QToolTip, QVBoxLayout, QWidget)

from core import effort_stats
from core.stats_extended import StatsCalculator
from ui.dialogs.branded_dialogs import BrandedDialog
from ui.theme import fonts
from ui.theme.tokens import ThemeManager, tokens
from ui.widgets.icon_button import tinted_icon

PERIODS = [("Hoy", 1), ("7 días", 7), ("30 días", 30), ("90 días", 90), ("Histórico", 0)]
MONTHS = ("ene", "feb", "mar", "abr", "may", "jun",
          "jul", "ago", "sep", "oct", "nov", "dic")
WEEKLY_FROM = 92   # con más días, el gráfico de barras agrupa por semanas
BAR_MAX_PX = 24    # una barra nunca pasa de 24 px: el resto del hueco es aire
TOP_ROWS = 15      # filas de las tablas por proyecto/tarea


def _fmt_time(seconds: float) -> str:
    """Duración legible: «0 min», «<1 min», «45 min», «2 h 05 min»."""
    seconds = int(seconds or 0)
    if seconds <= 0:
        return "0 min"
    if seconds < 60:
        return "<1 min"
    h, rem = divmod(seconds, 3600)
    m = rem // 60
    if h:
        return f"{h} h {m:02d} min"
    return f"{m} min"


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def _short_date(d: date) -> str:
    """«27 oct», como las fechas de las tarjetas de tarea."""
    return f"{d.day} {MONTHS[d.month - 1]}"


def _moving_avg(values: Sequence[float], window: int = 3) -> List[float]:
    out = []
    for i in range(len(values)):
        chunk = values[max(0, i - window + 1):i + 1]
        out.append(sum(chunk) / len(chunk))
    return out


def _tick_positions(n: int, max_labels: int = 8) -> List[int]:
    """Índices con etiqueta en el eje X: como mucho max_labels, repartidos
    hacia atrás desde el último (hoy siempre lleva etiqueta)."""
    if n <= 0:
        return []
    step = max(1, math.ceil(n / max_labels))
    return list(range(n - 1, -1, -step))[::-1]


def _apply_mpl_style() -> None:
    """Estilo global de matplotlib desde el tema activo. Solo este diálogo usa
    matplotlib, así que tocar rcParams no afecta a nada más."""
    import matplotlib
    matplotlib.use("QtAgg", force=False)
    from cycler import cycler
    t = tokens()
    matplotlib.rcParams.update({
        'font.family': 'sans-serif',
        'font.sans-serif': ['Segoe UI Variable', 'Segoe UI', 'DejaVu Sans'],
        'font.size': fonts.XS,
        'text.color': t.fg_dim,
        # 96 = DPI lógico de Qt en Windows: 9 pt aquí miden lo mismo que en la UI
        'figure.dpi': 96,
        'figure.facecolor': t.surface,
        'savefig.facecolor': t.surface,
        'axes.facecolor': t.surface,
        'axes.edgecolor': t.border,
        'axes.linewidth': 0.75,
        'axes.labelcolor': t.fg_dim,
        'axes.titlecolor': t.fg,
        'axes.spines.top': False,
        'axes.spines.right': False,
        'axes.spines.left': False,
        'axes.grid': True,
        'axes.grid.axis': 'y',
        'axes.axisbelow': True,
        'axes.prop_cycle': cycler(color=[t.accent]),
        'grid.color': t.border,
        'grid.linestyle': '-',
        'grid.linewidth': 0.75,
        'grid.alpha': 1.0,
        'xtick.color': t.fg_muted,
        'ytick.color': t.fg_muted,
        'xtick.labelcolor': t.fg_muted,
        'ytick.labelcolor': t.fg_muted,
        'xtick.labelsize': fonts.XS,
        'ytick.labelsize': fonts.XS,
        'xtick.major.size': 0,
        'ytick.major.size': 0,
        'xtick.major.pad': 6,
        'ytick.major.pad': 8,
        'legend.frameon': False,
        'lines.solid_capstyle': 'round',
        'lines.solid_joinstyle': 'round',
        'patch.linewidth': 0,
    })


_canvas_cls = None


def _canvas_class():
    """FigureCanvasQTAgg que deja pasar la rueda del ratón: el original la
    acepta siempre y, con el cursor sobre un gráfico, el diálogo no hacía
    scroll (aquí no se usan los scroll_event de matplotlib)."""
    global _canvas_cls
    if _canvas_cls is None:
        from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg

        class _Canvas(FigureCanvasQTAgg):
            def wheelEvent(self, event):
                event.ignore()

        _canvas_cls = _Canvas
    return _canvas_cls


class _ElidedLabel(QLabel):
    """Etiqueta de una línea que se corta con «…» según el ancho disponible
    (el nombre completo queda en el tooltip)."""

    def __init__(self, text: str, parent: QWidget, mode=Qt.ElideRight):
        super().__init__(text, parent)
        self._full = text
        self._mode = mode
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setMinimumWidth(60)

    def set_full_text(self, text: str) -> None:
        self._full = text
        self._elide()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._elide()

    def _elide(self) -> None:
        shown = self.fontMetrics().elidedText(self._full, self._mode, self.width())
        if shown != self.text():
            self.setText(shown)
        self.setToolTip(self._full if shown != self._full else "")


class StatsDialog(BrandedDialog):
    def __init__(self, parent_window, controller):
        self.controller = controller
        self.data_handler = controller.data_handler
        self._chunks = self.data_handler.get_time_chunks()
        self.calc = StatsCalculator(self._chunks)
        self.period_days = 7
        self._pcalc = self.calc  # calculadora del periodo (ver _period_calc)
        self._canvases: List = []
        super().__init__(parent_window, "Estadísticas", icon="estadisticas",
                         subtitle=self._range_text())
        card = self._card  # tarjeta del diálogo (BrandedDialog)

        self.setMinimumSize(980, 680)

        # Selector de periodo (control segmentado) a la derecha del título
        segment = QFrame(card)
        segment.setProperty("segmented", True)
        segment_lay = QHBoxLayout(segment)
        segment_lay.setContentsMargins(3, 3, 3, 3)
        segment_lay.setSpacing(2)
        self._period_group = QButtonGroup(self)
        self._period_group.setExclusive(True)
        self._period_buttons: Dict[int, QPushButton] = {}
        for label, days in PERIODS:
            b = QPushButton(label, segment)
            b.setProperty("pill", True)
            b.setCheckable(True)
            b.setChecked(days == self.period_days)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, d=days: self._change_period(d))
            self._period_group.addButton(b)
            self._period_buttons[days] = b
            segment_lay.addWidget(b)
        self._header.addWidget(segment, 0, Qt.AlignVCenter)
        self.greeting_label = self._greeting_line(card)

        # Contenido con scroll vertical: nada se corta, por estrecho que sea
        self.scroll = QScrollArea(card)
        self.scroll.setWidgetResizable(True)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.scroll.setFrameShape(QFrame.NoFrame)
        self._lay.addWidget(self.scroll, 1)

        # Botonera: exportar (y su resultado) a la izquierda, «Cerrar» a la derecha
        self.add_buttons([("Cerrar", "", 0)])
        footer = self._lay.itemAt(self._lay.count() - 1).layout()
        if footer is None:  # por si la base cambia de estructura
            footer = QHBoxLayout()
            self._lay.addLayout(footer)
        self._export_buttons: List[QPushButton] = []
        for i, (text, tip, handler) in enumerate((
                ("Exportar CSV", "Guardar todas las sesiones en un archivo CSV",
                 self._export_csv),
                ("Exportar XLSX", "Guardar todas las sesiones en un libro de Excel",
                 self._export_xlsx))):
            b = QPushButton(text, card)
            b.setProperty("variant", "ghost")
            b.setIconSize(QSize(16, 16))
            b.setToolTip(tip)
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(handler)
            footer.insertWidget(i, b)
            self._export_buttons.append(b)
        # Resultado de la exportación: vacío hasta que haya algo que decir
        self.export_status = _ElidedLabel("", card, Qt.ElideMiddle)
        self.export_status.setFont(fonts.font(fonts.XS))
        self._status_error = False
        footer.insertWidget(len(self._export_buttons), self.export_status, 1)

        self._retint()
        self._rebuild()
        ThemeManager.instance().theme_changed.connect(self._retheme_stats)
        self._theme_hooked = True

    def adjustSize(self) -> None:
        """BrandedDialog.exec() llama a adjustSize(), que Qt limita a 2/3 de
        la pantalla (el diálogo quedaba en el mínimo): aquí se pide un tamaño
        cómodo para leer los gráficos, acotado a la pantalla disponible."""
        want = QSize(1080, 860)
        screen = self.screen()
        if screen is not None:
            area = screen.availableGeometry()
            want = want.boundedTo(QSize(area.width() - 32, area.height() - 32))
        self.resize(want.expandedTo(self.minimumSize()))

    # ---------- construcción ----------
    def _greeting_line(self, card) -> Optional[QLabel]:
        """«Así va tu progreso, Fabricio» sobre las tarjetas (solo con nombre)."""
        from ui.greeting import clean_name, with_name
        name = clean_name(self.data_handler.load_settings().get('user_name', ''))
        if not name:
            return None
        label = QLabel(with_name("Así va tu progreso", name, tokens().accent), card)
        label.setTextFormat(Qt.RichText)
        label.setFont(fonts.font(fonts.SMALL))
        label.setProperty("role", "dim")
        self._lay.addWidget(label)
        return label

    def _period_start(self) -> Optional[datetime]:
        """Medianoche del primer día del periodo (None = histórico). Días
        naturales, como el gráfico de barras y el subtítulo «Del … al …»."""
        if not self.period_days:
            return None
        first = date.today() - timedelta(days=self.period_days - 1)
        return datetime.combine(first, datetime.min.time())

    def _period_calc(self) -> StatsCalculator:
        """StatsCalculator solo con los tramos del periodo. Los cortes por
        `days` del núcleo son móviles (ahora − N×24 h): con «Hoy» mezclaban
        la tarde de ayer con el gráfico de hoy. Filtrando aquí y llamando con
        days=0, todas las cifras del periodo cuentan los mismos días."""
        start = self._period_start()
        if start is None:
            return self.calc
        chunks = []
        for c in self._chunks:
            try:
                if datetime.fromisoformat(c.get('timestamp', '')) >= start:
                    chunks.append(c)
            except (TypeError, ValueError):
                continue
        return StatsCalculator(chunks)

    def _rebuild(self) -> None:
        _apply_mpl_style()
        self._pcalc = self._period_calc()
        self.setUpdatesEnabled(False)
        try:
            content = QWidget(self.scroll)
            lay = QVBoxLayout(content)
            lay.setContentsMargins(0, 0, 10, 4)
            lay.setSpacing(12)
            self._canvases.clear()

            lay.addWidget(self._summary_cards(content))
            lay.addWidget(self._charts_section(content))
            lay.addWidget(self._tables_section(content))
            lay.addWidget(self._effort_section(content))
            lay.addStretch(1)
            self.scroll.setWidget(content)
        finally:
            self.setUpdatesEnabled(True)

    def _range_text(self) -> str:
        """Subtítulo del diálogo: las fechas que cubre el periodo elegido."""
        today = date.today()
        days = self.period_days
        if days == 1:
            return f"Hoy, {_short_date(today)}"
        if days:
            start = today - timedelta(days=days - 1)
            year = f" {start.year}" if start.year != today.year else ""
            return f"Del {_short_date(start)}{year} al {_short_date(today)}"
        if not self.calc.daily_totals:
            return "Todavía no hay sesiones registradas"
        first = datetime.strptime(min(self.calc.daily_totals), '%Y-%m-%d').date()
        return f"Desde el {_short_date(first)} {first.year}"

    def _change_period(self, days: int) -> None:
        self.period_days = days
        if self._subtitle_label is not None:
            self._subtitle_label.setText(self._range_text())
        button = self._period_buttons.get(days)
        if button is not None and not button.isChecked():
            button.setChecked(True)
        # Conservar el punto de lectura: el selector queda fuera del scroll
        bar = self.scroll.verticalScrollBar()
        pos = bar.value()
        self._rebuild()
        if pos:
            QTimer.singleShot(0, lambda: bar.setValue(pos))

    def _retint(self) -> None:
        """Estilos en línea que dependen del tema (iconos y estado)."""
        t = tokens()
        for b in self._export_buttons:
            b.setIcon(tinted_icon("download", t.fg_dim, 16))
        color = t.error if self._status_error else t.fg_dim
        self.export_status.setStyleSheet(f"color: {color};")

    def _retheme_stats(self) -> None:
        self._retint()
        self._rebuild()

    def done(self, result: int) -> None:
        if getattr(self, "_theme_hooked", False):
            self._theme_hooked = False
            try:
                ThemeManager.instance().theme_changed.disconnect(self._retheme_stats)
            except (RuntimeError, TypeError):
                pass
        super().done(result)

    # ---------- piezas comunes ----------
    def _section_card(self, parent: QWidget, title: str, caption: str = "",
                      keys: Optional[QWidget] = None) -> Tuple[QFrame, QVBoxLayout]:
        """Tarjeta con encabezado: título y subtítulo a la izquierda, leyenda
        (opcional, sin caja) a la derecha. Devuelve (tarjeta, layout)."""
        card = QFrame(parent)
        card.setProperty("card", True)
        v = QVBoxLayout(card)
        v.setContentsMargins(16, 14, 16, 12)
        v.setSpacing(10)
        head = QHBoxLayout()
        head.setSpacing(12)
        titles = QVBoxLayout()
        titles.setSpacing(2)
        title_label = QLabel(title, card)
        title_label.setFont(fonts.font(fonts.SMALL, bold=True))
        titles.addWidget(title_label)
        if caption:
            caption_label = QLabel(caption, card)
            caption_label.setFont(fonts.font(fonts.XS))
            caption_label.setProperty("role", "muted")
            titles.addWidget(caption_label)
        head.addLayout(titles, 1)
        if keys is not None:
            keys.setParent(card)
            head.addWidget(keys, 0, Qt.AlignTop)
        v.addLayout(head)
        return card, v

    def _keys(self, parent: QWidget, items: Sequence[Tuple[str, str, str]]) -> QWidget:
        """Leyenda sin caja: [(tipo, color, texto)], tipo 'bar' o 'line'."""
        box = QWidget(parent)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(6)
        for i, (kind, color, text) in enumerate(items):
            if i:
                lay.addSpacing(10)
            swatch = QFrame(box)
            if kind == 'line':
                swatch.setFixedSize(14, 2)
                radius = 1
            else:
                swatch.setFixedSize(10, 10)
                radius = 3
            swatch.setStyleSheet(f"background: {color}; border: none;"
                                 f" border-radius: {radius}px;")
            lay.addWidget(swatch, 0, Qt.AlignVCenter)
            label = QLabel(text, box)
            label.setFont(fonts.font(fonts.XS))
            label.setProperty("role", "dim")
            lay.addWidget(label)
        return box

    def _ramp_keys(self, parent: QWidget, colors: Sequence[str]) -> QWidget:
        """Leyenda de escala del mapa de calor: «Menos ▪▪▪▪▪ Más»."""
        box = QWidget(parent)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 2, 0, 0)
        lay.setSpacing(3)
        less = QLabel("Menos", box)
        less.setFont(fonts.font(fonts.XS))
        less.setProperty("role", "dim")
        lay.addWidget(less)
        lay.addSpacing(3)
        for color in colors:
            swatch = QFrame(box)
            swatch.setFixedSize(11, 11)
            swatch.setStyleSheet(f"background: {color}; border: none; border-radius: 3px;")
            lay.addWidget(swatch, 0, Qt.AlignVCenter)
        lay.addSpacing(3)
        more = QLabel("Más", box)
        more.setFont(fonts.font(fonts.XS))
        more.setProperty("role", "dim")
        lay.addWidget(more)
        return box

    # ---------- resumen ----------
    def _summary_cards(self, parent: QWidget) -> QWidget:
        t = tokens()
        s = self._pcalc.get_summary()     # cifras del periodo elegido
        full = self.calc.get_summary()    # hoy, media de 7 días y racha
        box = QWidget(parent)
        grid = QGridLayout(box)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(12)
        # Fila 1: el periodo elegido; fila 2: hoy y la constancia. Color solo
        # con significado: el acento marca la métrica principal, nada más
        cards = [
            ("Tiempo enfocado", _fmt_time(s['total_time']), True),
            ("Pomodoros", str(s['pomodoros_total']), False),
            ("Ciclos completos", str(s.get('cycles_total', 0)), False),
            ("Descanso sobre el total", f"{s['ratio_break'] * 100:.0f} %", False),
            ("Hoy", _fmt_time(full['today_time']), False),
            ("Pomodoros hoy", str(full['pomodoros_hoy']), False),
            ("Media diaria (7 días)", _fmt_time(full['avg7']), False),
            ("Racha actual", _plural(full['streak'], "día", "días"), False),
        ]
        for i, (label, value, main) in enumerate(cards):
            card = QFrame(box)
            card.setProperty("card", True)
            v = QVBoxLayout(card)
            v.setContentsMargins(16, 12, 16, 14)
            v.setSpacing(4)
            lab = QLabel(label, card)
            lab.setFont(fonts.font(fonts.XS))
            lab.setProperty("role", "muted")
            v.addWidget(lab)
            val = QLabel(value, card)
            # Cifras proporcionales: las tabulares se ven sueltas en grande
            val.setFont(fonts.display(fonts.LARGE, tabular=False))
            val.setStyleSheet(f"color: {t.accent if main else t.fg};")
            v.addWidget(val)
            grid.addWidget(card, i // 4, i % 4)
        for col in range(4):
            grid.setColumnStretch(col, 1)
        return box

    # ---------- gráficos ----------
    def _figure(self, height: int, constrained: bool = True):
        """Figura con su canvas de alto fijo (px lógicos). El ancho lo pone el
        layout de Qt y constrained layout recoloca los ejes en cada resize."""
        from matplotlib.figure import Figure
        fig = Figure(figsize=(8, height / 96), dpi=96,
                     layout="constrained" if constrained else None)
        if constrained:
            fig.get_layout_engine().set(w_pad=2 / 72, h_pad=2 / 72)
        canvas = _canvas_class()(fig)
        canvas.setStyleSheet("background: transparent;")
        canvas.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        canvas.setFixedHeight(height)
        canvas.setMinimumWidth(160)
        self._canvases.append(canvas)
        return fig, canvas

    @staticmethod
    def _y_axis(ax, peak: float, unit: str = "", floor: float = 1) -> None:
        """Eje Y en enteros (minutos, horas, sesiones o días), con el techo en
        la última marca: nada de 0,05 min."""
        from matplotlib.ticker import FuncFormatter, MaxNLocator
        locator = MaxNLocator(nbins=4, integer=True, min_n_ticks=2)
        ticks = [v for v in locator.tick_values(0, max(peak, floor)) if v >= 0]
        ax.set_ylim(0, ticks[-1])
        ax.yaxis.set_major_locator(locator)
        suffix = f" {unit}" if unit else ""
        ax.yaxis.set_major_formatter(FuncFormatter(
            lambda v, _pos: f"{v:.0f}{suffix}" if v else "0"))

    @staticmethod
    def _empty(ax, text: str) -> None:
        t = tokens()
        ax.text(0.5, 0.55, text, transform=ax.transAxes, ha='center', va='center',
                color=t.fg_muted, fontsize=fonts.SMALL)

    @staticmethod
    def _fit_bars(canvas, ax, bars, n: int, frac: float = 0.62) -> None:
        """Ancho de barra: frac del hueco, pero nunca más de BAR_MAX_PX.
        Se recalcula al redimensionar (el ancho del eje es px físicos)."""
        def fit(_event=None):
            slot = ax.bbox.width / max(1, n)
            cap = BAR_MAX_PX * canvas.device_pixel_ratio
            width = min(frac, cap / slot) if slot > 0 else frac
            for rect in bars:
                center = rect.get_x() + rect.get_width() / 2
                rect.set_width(width)
                rect.set_x(center - width / 2)
        fit()
        canvas.mpl_connect('resize_event', fit)

    @staticmethod
    def _hover(canvas, ax, describe: Callable[[float, float], Optional[str]]) -> None:
        """Tooltip al pasar el ratón: describe(x, y) en coordenadas de datos
        devuelve el texto, o None fuera de una marca."""
        def on_move(event):
            text = None
            if event.inaxes is ax and event.xdata is not None:
                text = describe(event.xdata, event.ydata)
            if text:
                QToolTip.showText(QCursor.pos(), text, canvas)
            else:
                QToolTip.hideText()
        canvas.mpl_connect('motion_notify_event', on_move)
        canvas.mpl_connect('figure_leave_event', lambda _e: QToolTip.hideText())

    def _charts_section(self, parent: QWidget) -> QWidget:
        box = QWidget(parent)
        lay = QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        def add(build, target_lay, stretch=0):
            try:
                target_lay.addWidget(build(box), stretch)
            except Exception as e:
                print(f"Error en gráfico {build.__name__}: {e}")

        add(self._focus_chart, lay)
        row = QHBoxLayout()
        row.setSpacing(12)
        add(self._success_chart, row, 2)
        add(self._durations_chart, row, 3)
        lay.addLayout(row)
        add(self._heatmap_chart, lay)
        add(self._streak_chart, lay)
        return box

    def _focus_chart(self, parent: QWidget) -> QFrame:
        """1. Tiempo enfocado por hora (hoy), día o semana (histórico largo)."""
        t = tokens()
        days = self.period_days
        _labels, minutes, _trend = self.calc.get_chart_data(days)
        n = len(minutes)
        if days == 1:
            per = "hora"
            ticks_text = [f"{h:02d}:00" for h in range(n)]
            tips = [f"{h:02d}:00–{(h + 1) % 24:02d}:00" for h in range(n)]
        else:
            today = date.today()
            dates = [today - timedelta(days=n - 1 - i) for i in range(n)]
            per = "día"
            if n > WEEKLY_FROM:
                # Semanas que acaban hoy: la última barra son los últimos 7 días
                weeks, starts = [], []
                for end in range(n, 0, -7):
                    start = max(0, end - 7)
                    weeks.append(sum(minutes[start:end]))
                    starts.append(dates[start])
                minutes, dates = weeks[::-1], starts[::-1]
                per = "semana"
            ticks_text = [_short_date(d) for d in dates]
            tips = ([f"Semana del {_short_date(d)}" for d in dates] if per == "semana"
                    else [_short_date(d) for d in dates])
        n = len(minutes)
        hours = max(minutes, default=0) >= 120
        values = [m / 60 for m in minutes] if hours else list(minutes)
        has_data = any(values)
        show_trend = days != 1 and has_data and n > 2

        keys = None
        if show_trend:
            keys = self._keys(parent, [
                ('bar', t.accent, f"Por {per}"),
                ('line', t.fg_dim, f"Media de 3 {'semanas' if per == 'semana' else 'días'}")])
        caption = f"{'Horas' if hours else 'Minutos'} de enfoque por {per}"
        card, body = self._section_card(parent, "Tiempo enfocado", caption, keys)

        fig, canvas = self._figure(230)
        ax = fig.add_subplot(111)
        x = list(range(n))
        bars = ax.bar(x, values, color=t.accent, zorder=2)
        self._fit_bars(canvas, ax, bars, n)
        if show_trend:
            ax.plot(x, _moving_avg(values), color=t.fg_dim, linewidth=1.5, zorder=3)
        self._y_axis(ax, max(values, default=0), "h" if hours else "min")
        positions = list(range(0, n, 3)) if days == 1 else _tick_positions(n, 7)
        ax.set_xticks(positions, [ticks_text[i] for i in positions])
        ax.set_xlim(-0.6, n - 0.4)
        if not has_data:
            self._empty(ax, "Sin tiempo enfocado en este periodo")

        def describe(xd, _yd):
            i = int(round(xd))
            if 0 <= i < n:
                return f"{tips[i]} · {_fmt_time(minutes[i] * 60)}"
            return None
        self._hover(canvas, ax, describe)
        body.addWidget(canvas)
        return card

    def _success_chart(self, parent: QWidget) -> QFrame:
        """2. Anillo: pomodoros que llegan al 90 % de la duración objetivo."""
        t = tokens()
        target = self.controller.model.pomodoro_duration / 60
        counts = self._pcalc.get_success_counts(target, 0)
        ok, cut = counts['success'], counts['fail']
        total = ok + cut
        card, body = self._section_card(
            parent, "Pomodoros completos",
            f"Sesiones que llegan al 90 % de los {target:.0f} min")
        row = QHBoxLayout()
        row.setSpacing(20)
        fig, canvas = self._figure(150, constrained=False)
        canvas.setFixedWidth(150)
        ax = fig.add_axes([0.02, 0.02, 0.96, 0.96])
        ax.set_axis_off()
        if total:
            ax.pie([ok, cut], colors=[t.accent, t.surface_active], startangle=90,
                   counterclock=False,
                   wedgeprops={'width': 0.2, 'edgecolor': t.surface,
                               'linewidth': 1.5 if ok and cut else 0})
            center = f"{ok / total * 100:.0f} %"
        else:
            ax.pie([1], colors=[t.surface_active], wedgeprops={'width': 0.2})
            center = "–"
        ax.set_aspect('equal')
        ax.text(0, 0, center, ha='center', va='center', color=t.fg,
                fontsize=fonts.LARGE, fontfamily='Segoe UI', fontweight='semibold')
        row.addWidget(canvas, 0, Qt.AlignVCenter)

        facts = QVBoxLayout()
        facts.setSpacing(8)
        facts.addStretch(1)
        for color, text, value in ((t.accent, "Completos", ok),
                                   (t.surface_active, "Incompletos", cut)):
            line = QHBoxLayout()
            line.setSpacing(8)
            swatch = QFrame(card)
            swatch.setFixedSize(10, 10)
            swatch.setStyleSheet(f"background: {color}; border: none; border-radius: 3px;")
            line.addWidget(swatch, 0, Qt.AlignVCenter)
            label = QLabel(text, card)
            label.setFont(fonts.font(fonts.SMALL))
            label.setProperty("role", "dim")
            line.addWidget(label, 1)
            number = QLabel(str(value), card)
            number.setFont(fonts.font(fonts.SMALL, bold=True))
            number.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            line.addWidget(number)
            facts.addLayout(line)
        note = QLabel("Sin sesiones en este periodo" if not total
                      else f"{_plural(total, 'sesión', 'sesiones')} de enfoque", card)
        note.setFont(fonts.font(fonts.XS))
        note.setProperty("role", "muted")
        facts.addWidget(note)
        facts.addStretch(1)
        row.addLayout(facts, 1)
        body.addLayout(row)
        return card

    def _durations_chart(self, parent: QWidget) -> QFrame:
        """3. Histograma de la duración de las sesiones, con el objetivo."""
        t = tokens()
        from matplotlib.ticker import FuncFormatter, MaxNLocator
        target = self.controller.model.pomodoro_duration / 60
        durations = self._pcalc.get_session_durations(0)
        keys = self._keys(parent, [('line', t.fg_dim, f"Objetivo {target:.0f} min")])
        card, body = self._section_card(parent, "Duración de las sesiones",
                                        "Número de sesiones según los minutos que duraron",
                                        keys)
        fig, canvas = self._figure(150)
        ax = fig.add_subplot(111)
        hi = max(max(durations, default=0), target)
        step = 1 if hi <= 12 else 5 if hi <= 75 else 10 if hi <= 150 else 15
        # El último borde queda estrictamente por encima del objetivo y de la
        # sesión más larga: la línea del objetivo nunca cae sobre el borde
        last = (math.floor(hi / step) + 1) * step
        edges = list(range(0, last + 1, step))
        counts = [0] * (len(edges) - 1)
        if durations:
            counts_arr, _edges, _bars = ax.hist(durations, bins=edges, color=t.accent,
                                                rwidth=0.86, zorder=2)
            counts = [int(c) for c in counts_arr]
        ax.axvline(target, color=t.fg_dim, linewidth=1.25, zorder=3)
        ax.set_xlim(0, edges[-1])
        ax.xaxis.set_major_locator(MaxNLocator(nbins=7, integer=True, steps=[1, 2, 5, 10]))
        ax.xaxis.set_major_formatter(FuncFormatter(lambda v, _pos: f"{v:.0f}"))
        self._y_axis(ax, max(counts, default=0))
        if not durations:
            self._empty(ax, "Sin sesiones en este periodo")

        def describe(xd, _yd):
            i = int(xd // step)
            if 0 <= i < len(counts) and durations:
                a, b = edges[i], edges[i + 1]
                return f"{_plural(counts[i], 'sesión', 'sesiones')} de {a}–{b} min"
            return None
        self._hover(canvas, ax, describe)
        body.addWidget(canvas)
        return card

    def _heatmap_chart(self, parent: QWidget) -> QFrame:
        """4. Mapa de calor hora × día de la semana (rampa del acento)."""
        t = tokens()
        from matplotlib.colors import LinearSegmentedColormap
        y_labels, _x, grid = self._pcalc.get_hourly_by_weekday(0)
        peak = max((max(r) for r in grid), default=0)
        cmap = LinearSegmentedColormap.from_list(
            "pomo", [t.surface_hover, t.accent_dim, t.accent])
        ramp = [cmap(v) for v in (0.0, 0.25, 0.5, 0.75, 1.0)]
        ramp_hex = ["#{:02x}{:02x}{:02x}".format(*(round(c * 255) for c in rgba[:3]))
                    for rgba in ramp]
        caption = "Minutos enfocados por día de la semana y hora"
        if peak:
            caption += f" · máximo {_fmt_time(peak * 60)} en una hora"
        card, body = self._section_card(parent, "Cuándo te concentras", caption,
                                        self._ramp_keys(parent, ramp_hex))
        fig, canvas = self._figure(196)
        ax = fig.add_subplot(111)
        # El borde del color de la superficie hace de hueco de 2 px entre celdas
        ax.pcolormesh(grid, cmap=cmap, vmin=0, vmax=peak or 1,
                      edgecolors=t.surface, linewidth=1.5)
        ax.set_ylim(7, 0)  # lunes arriba
        ax.set_yticks([i + 0.5 for i in range(7)], y_labels)
        hours = list(range(0, 24, 3))
        ax.set_xticks([h + 0.5 for h in hours], [f"{h:02d}:00" for h in hours])
        ax.grid(False)
        for spine in ax.spines.values():
            spine.set_visible(False)

        def describe(xd, yd):
            col, row_i = int(xd), int(yd)
            if 0 <= col < 24 and 0 <= row_i < 7:
                return (f"{y_labels[row_i]} {col:02d}:00 · "
                        f"{_fmt_time(grid[row_i][col] * 60)}")
            return None
        self._hover(canvas, ax, describe)
        body.addWidget(canvas)
        return card

    def _streak_chart(self, parent: QWidget) -> QFrame:
        """5. Racha: días seguidos con tiempo enfocado (pomodoros o sesiones
        Libres), día a día."""
        t = tokens()
        _labels, values = self.calc.get_daily_streak_series(self.period_days)
        n = len(values)
        today = date.today()
        dates = [today - timedelta(days=n - 1 - i) for i in range(n)]
        best = max(values, default=0)
        caption = "Días seguidos con tiempo enfocado"
        if best:
            caption += f" · mejor racha {_plural(best, 'día', 'días')}"
        card, body = self._section_card(parent, "Racha de días", caption)
        fig, canvas = self._figure(150)
        ax = fig.add_subplot(111)
        x = list(range(n))
        # Escalones: la racha es un entero por día, no una curva continua
        ax.fill_between(x, values, step='mid', color=t.accent, alpha=0.10,
                        linewidth=0, zorder=2)
        ax.plot(x, values, drawstyle='steps-mid', color=t.accent, linewidth=1.5,
                zorder=3)
        if n:
            # Punto final (hoy) con anillo del color de la superficie
            ax.plot([n - 1], [values[-1]], 'o', markersize=7, color=t.accent,
                    markeredgecolor=t.surface, markeredgewidth=1.5, zorder=4,
                    clip_on=False)
        self._y_axis(ax, best, floor=3)
        positions = _tick_positions(n, 7)
        ax.set_xticks(positions, [_short_date(dates[i]) for i in positions])
        ax.set_xlim(-0.5, max(n, 1) - 0.5)

        def describe(xd, _yd):
            i = int(round(xd))
            if 0 <= i < n:
                return (f"{_short_date(dates[i])} · racha de "
                        f"{_plural(values[i], 'día', 'días')}")
            return None
        self._hover(canvas, ax, describe)
        body.addWidget(canvas)
        return card

    # ---------- tablas ----------
    def _chunks_with_names(self, since: Optional[datetime] = None) -> List[Dict]:
        """Pomodoros con nombre de tarea y proyecto; `since` limita al periodo
        (None = todos, lo que usa la exportación)."""
        tasks = {t.get('id'): t for t in (self.data_handler.get_tasks()
                                          + self.data_handler.data.get('deleted_tasks', []))}
        out = []
        for c in self.data_handler.get_time_chunks():
            if c.get('type', 'pomodoro') != 'pomodoro':
                continue
            if since is not None:
                try:
                    if datetime.fromisoformat(c.get('timestamp', '')) < since:
                        continue
                except (TypeError, ValueError):
                    continue
            task = tasks.get(c.get('task_id')) or {}
            out.append({
                'timestamp': c.get('timestamp', ''),
                'duration': c.get('duration', 0),
                'task': task.get('name') or f"(tarea {c.get('task_id')})",
                # log_time_chunk guarda la clave 'project' (no 'project_name')
                'project': c.get('project')
                or self.controller._task_project_name(task) or 'Sin proyecto',
            })
        return out

    def _tables_section(self, parent: QWidget) -> QWidget:
        t = tokens()
        box = QWidget(parent)
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        by_project = defaultdict(float)
        by_task = defaultdict(float)
        for c in self._chunks_with_names(self._period_start()):
            by_project[c['project']] += c['duration']
            by_task[c['task']] += c['duration']

        for title, data, caption in (
                ("Tiempo por proyecto", by_project, "Pomodoros del periodo por proyecto"),
                ("Tiempo por tarea", by_task, f"Las {TOP_ROWS} tareas con más tiempo")):
            card, v = self._section_card(box, title, caption)
            v.setSpacing(8)
            rows = sorted(data.items(), key=lambda kv: kv[1], reverse=True)[:TOP_ROWS]
            peak = rows[0][1] if rows and rows[0][1] > 0 else 1
            for name, secs in rows:
                r = QHBoxLayout()
                r.setSpacing(12)
                label = _ElidedLabel(name, card)
                label.setFont(fonts.font(fonts.SMALL))
                r.addWidget(label, 1)
                # Barra relativa a la fila mayor, sobre un carril hundido
                track = QFrame(card)
                track.setFixedSize(88, 6)
                track.setStyleSheet(f"background: {t.bg_header}; border: none;"
                                    f" border-radius: 3px;")
                fill = QFrame(track)
                fill.setGeometry(0, 0, max(6, round(88 * secs / peak)), 6)
                fill.setStyleSheet(f"background: {t.accent}; border: none;"
                                   f" border-radius: 3px;")
                r.addWidget(track, 0, Qt.AlignVCenter)
                value = QLabel(_fmt_time(secs), card)
                value.setFont(fonts.font(fonts.XS))
                value.setProperty("role", "dim")
                value.setMinimumWidth(78)
                value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
                r.addWidget(value)
                v.addLayout(r)
            if not rows:
                empty = QLabel("Sin sesiones en este periodo", card)
                empty.setFont(fonts.font(fonts.SMALL))
                empty.setProperty("role", "muted")
                v.addWidget(empty)
            v.addStretch(1)
            lay.addWidget(card, 1)
        return box

    # ---------- esfuerzo ----------
    def _effort_ratings(self, since: Optional[datetime] = None) -> List[Dict]:
        """Valoraciones de esfuerzo (1-5) desde `since` (None = todas)."""
        return effort_stats.ratings_since(
            self.data_handler.data.get('effort_ratings') or [], since)

    def _bar_row(self, card: QWidget, name: str, fraction: float, value: str) -> QHBoxLayout:
        """Fila etiqueta · barra sobre carril · cifra, como las de las tablas."""
        t = tokens()
        r = QHBoxLayout()
        r.setSpacing(12)
        label = _ElidedLabel(name, card)
        label.setFont(fonts.font(fonts.SMALL))
        r.addWidget(label, 1)
        track = QFrame(card)
        track.setFixedSize(88, 6)
        track.setStyleSheet(f"background: {t.bg_header}; border: none;"
                            f" border-radius: 3px;")
        if fraction > 0:
            fill = QFrame(track)
            fill.setGeometry(0, 0, max(6, round(88 * min(1.0, fraction))), 6)
            fill.setStyleSheet(f"background: {t.accent}; border: none;"
                               f" border-radius: 3px;")
        r.addWidget(track, 0, Qt.AlignVCenter)
        val = QLabel(value, card)
        val.setFont(fonts.font(fonts.XS))
        val.setProperty("role", "dim")
        val.setMinimumWidth(78)
        val.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        r.addWidget(val)
        return r

    def _effort_section(self, parent: QWidget) -> QWidget:
        """«Esfuerzo» del periodo: media y número de valoraciones con su
        distribución 1-5, y la media por proyecto."""
        t = tokens()
        data = effort_stats.summary(self._effort_ratings(self._period_start()))
        box = QWidget(parent)
        box.setObjectName("effortSection")
        lay = QHBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(12)

        card, v = self._section_card(box, "Esfuerzo",
                                     "Qué tan difíciles te resultaron las tareas completadas")
        v.setSpacing(8)
        head = QHBoxLayout()
        head.setSpacing(10)
        avg = data['average']
        self.effort_average = QLabel(
            "—" if avg is None else f"{avg:.1f}".replace('.', ',') + " / 5", card)
        self.effort_average.setFont(fonts.display(fonts.LARGE, tabular=False))
        self.effort_average.setStyleSheet(f"color: {t.fg};")
        head.addWidget(self.effort_average, 0, Qt.AlignBottom)
        detail = _plural(data['count'], "valoración", "valoraciones")
        per_point = data['minutes_per_point']
        if per_point is not None:
            detail += f" · {per_point:.0f} min de foco por punto"
        self.effort_count = QLabel(detail, card)
        self.effort_count.setFont(fonts.font(fonts.XS))
        self.effort_count.setProperty("role", "muted")
        head.addWidget(self.effort_count, 1, Qt.AlignBottom)
        v.addLayout(head)
        counts = data['distribution']
        peak = max(counts) or 1
        for n in range(5, 0, -1):  # de más a menos esfuerzo, como un histograma de reseñas
            v.addLayout(self._bar_row(card, f"{n} · {effort_stats.EFFORT_LABELS[n]}",
                                      counts[n - 1] / peak, str(counts[n - 1])))
        v.addStretch(1)
        lay.addWidget(card, 1)

        card, v = self._section_card(box, "Esfuerzo por proyecto",
                                     "Media de 1 a 5 de las tareas valoradas")
        v.setSpacing(8)
        rows = data['by_project'][:TOP_ROWS]
        for name, mean, count in rows:
            v.addLayout(self._bar_row(card, name, mean / 5,
                                      f"{mean:.1f}".replace('.', ',') + f" · {count}"))
        if not rows:
            empty = QLabel("Sin valoraciones en este periodo", card)
            empty.setFont(fonts.font(fonts.SMALL))
            empty.setProperty("role", "muted")
            v.addWidget(empty)
        v.addStretch(1)
        lay.addWidget(card, 1)
        return box

    def _effort_rows(self) -> List[List]:
        """Todas las valoraciones de esfuerzo para la exportación."""
        rows = [["fecha", "tarea", "proyecto", "etiquetas", "fija", "esfuerzo",
                 "minutos_foco"]]
        for r in self._effort_ratings():
            try:
                focus_min = round(float(r.get('focus_s') or 0) / 60, 1)
            except (TypeError, ValueError):
                focus_min = 0
            rows.append([r.get('timestamp', ''), r.get('task_name', ''),
                         r.get('project') or 'Sin proyecto',
                         ", ".join(str(x) for x in r.get('tags') or []),
                         r.get('recurring') or '', r['rating'], focus_min])
        return rows

    def _export_effort_csv(self, dest: str) -> Optional[str]:
        """Segundo CSV «<nombre>_esfuerzo.csv» junto al de pomodoros (solo si
        hay valoraciones). Devuelve su ruta o None."""
        rows = self._effort_rows()
        if len(rows) < 2:
            return None
        from pathlib import Path
        target = Path(dest)
        target = target.with_name(f"{target.stem}_esfuerzo{target.suffix or '.csv'}")
        with open(target, "w", newline="", encoding="utf-8-sig") as f:
            csv.writer(f, delimiter=';').writerows(rows)
        return str(target)

    # ---------- export ----------
    def _set_status(self, text: str, error: bool = False) -> None:
        self._status_error = error
        self.export_status.set_full_text(text)
        self._retint()

    def _export_rows(self) -> List[List]:
        rows = [["fecha", "tarea", "proyecto", "duracion_min"]]
        for c in self._chunks_with_names():
            rows.append([c['timestamp'], c['task'], c['project'],
                         round(c['duration'] / 60, 1)])
        return rows

    def _export_csv(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Exportar CSV", f"pomogrowth_stats_{stamp}.csv", "CSV (*.csv)")
        if not dest:
            return
        try:
            with open(dest, "w", newline="", encoding="utf-8-sig") as f:
                csv.writer(f, delimiter=';').writerows(self._export_rows())
            effort_dest = self._export_effort_csv(dest)
            self._set_status(f"CSV exportado en {dest}" + (
                f" (esfuerzo en {effort_dest})" if effort_dest else ""))
        except Exception as e:
            self._set_status(f"No se pudo exportar el CSV: {e}", error=True)

    def _export_xlsx(self) -> None:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest, _ = QFileDialog.getSaveFileName(
            self, "Exportar XLSX", f"pomogrowth_stats_{stamp}.xlsx", "Excel (*.xlsx)")
        if not dest:
            return
        try:
            from openpyxl import Workbook
            wb = Workbook()
            ws = wb.active
            ws.title = "PomoGrowth"
            for row in self._export_rows():
                ws.append(row)
            effort_ws = wb.create_sheet("Esfuerzo")
            for row in self._effort_rows():
                effort_ws.append(row)
            wb.save(dest)
            self._set_status(f"XLSX exportado en {dest}")
        except ImportError:
            self._set_status("Falta openpyxl para exportar a Excel (pip install openpyxl)",
                             error=True)
        except Exception as e:
            self._set_status(f"No se pudo exportar el XLSX: {e}", error=True)
