"""Instancia única por socket local.

A diferencia de v8 (que buscaba un puerto libre y por tanto una segunda
instancia podía arrancar igual), aquí el puerto es FIJO: si el bind falla,
ya hay una instancia corriendo.
"""
import socket
import time
from typing import Optional

from core.config import Config

_socket: Optional[socket.socket] = None
# Bandera de la instancia relanzada por «Reiniciar como administrador»
# (core/focus_mode): main.py espera con acquire_waiting a que la anterior se
# cierre y suelte el puerto antes de dar el aviso de que ya hay otra abierta
ELEVATED_RESTART_FLAG = "--elevated-restart"


def acquire() -> bool:
    """True si somos la única instancia; False si ya hay otra corriendo."""
    global _socket
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.bind(("127.0.0.1", Config.SINGLETON_PORT))
        _socket = s  # mantener vivo el bind durante toda la sesión
        return True
    except OSError:
        return False


def acquire_waiting(timeout_s: float = 10.0, interval_s: float = 0.25) -> bool:
    """acquire() reintentando hasta `timeout_s`: tras un reinicio como
    administrador la instancia anterior puede tardar un poco en soltarlo."""
    deadline = time.monotonic() + timeout_s
    while not acquire():
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval_s)
    return True
