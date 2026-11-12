"""Reproductor falso para los sonidos de ambiente (services/ambient_service.py).

conftest.py lo pone como fábrica en toda la suite: ningún test crea un
QMediaPlayer de verdad ni hace ruido. Los tests de tests/test_ambient.py lo
manejan a mano: posición, duración, fin del archivo y errores.
"""


class FakeVoice:
    created = []          # todas las voces creadas (los tests la vacían)

    def __init__(self, parent=None):
        self.parent = parent
        self.source = None
        self.state = 'stopped'      # 'stopped' | 'playing' | 'paused'
        self.gain = 0.0
        self.pos = 0
        self.dur = 0                # 0: duración aún desconocida
        self.err = ''
        self.ended = False
        self.disposed = False
        self.plays = 0
        FakeVoice.created.append(self)

    def load(self, path) -> None:
        self.source = str(path)
        self.err = ''

    def play(self) -> None:
        self.state = 'playing'
        self.ended = False
        self.plays += 1

    def pause(self) -> None:
        self.state = 'paused'

    def stop(self) -> None:
        self.state = 'stopped'
        self.pos = 0
        self.ended = False

    def set_position(self, ms) -> None:
        self.pos = int(ms)

    def position(self) -> int:
        return self.pos

    def duration(self) -> int:
        return self.dur

    def set_gain(self, gain) -> None:
        self.gain = float(gain)

    def error(self) -> str:
        return self.err

    def at_end(self) -> bool:
        return self.ended

    def dispose(self) -> None:
        self.state = 'stopped'
        self.disposed = True
