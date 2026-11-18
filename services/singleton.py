"""Instancia única por socket local, y el aviso «muéstrate» a la que ya está.

A diferencia de v8 (que buscaba un puerto libre y por tanto una segunda
instancia podía arrancar igual), aquí el puerto es FIJO: si el bind falla,
ya hay una instancia corriendo.

Segunda instancia (doble clic en el acceso directo con PomoGrowth ya abierta,
quizá minimizada, en la bandeja o detrás de otras ventanas): en vez de solo
avisar de que ya está abierta, le pide a la primera que se muestre. La
primera escucha con listen_for_show() en un canal local (QLocalServer; en
Windows, una tubería con nombre) con un nombre por usuario; la segunda envía
«show» con request_show() y espera su respuesta unos instantes:
- «ok»: la primera se mostró; la segunda sale sin más.
- «closing»: la primera se está cerrando (su despedida dura unos segundos) y
  no se muestra; la segunda espera a que suelte el puerto y arranca ella,
  o el usuario se quedaría sin ninguna PomoGrowth abierta.
- Nada: no conecta o no contesta a tiempo (p. ej. la primera está colgada):
  main.py da el aviso de siempre.
Los plazos son cortos: el arranque nunca se queda esperando a una primera
que no contesta.
"""
import getpass
import hashlib
import logging
import os
import socket
import time
from typing import Callable, Optional

from core.config import Config

_socket: Optional[socket.socket] = None
# Bandera de la instancia relanzada por «Reiniciar como administrador»
# (core/focus_mode): main.py espera con acquire_waiting a que la anterior se
# cierre y suelte el puerto antes de dar el aviso de que ya hay otra abierta
ELEVATED_RESTART_FLAG = "--elevated-restart"

SHOW_COMMAND = b"show"
SHOW_REPLY = b"ok"
CLOSING_REPLY = b"closing"
_MAX_REQUEST = 64       # bytes que se leen de un cliente como mucho
_CLIENT_TTL_MS = 2000   # una conexión que no dice nada se cierra a los 2 s
# Lo que devuelve request_show() (compárese con estas: CLOSING también es «verdadero»)
SHOWN, CLOSING, NO_ANSWER = "shown", "closing", ""
# La segunda espera como mucho esto a que una primera que se está cerrando
# suelte el puerto: su despedida (ui/windows/welcome_farewell, 8 s, que se
# alarga con el ratón encima) y el cierre del proceso
CLOSING_WAIT_S = 20.0


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


# ---------- «muéstrate»: de la segunda instancia a la primera ----------

def channel_name() -> str:
    """Nombre del canal, uno por usuario de Windows (dos usuarios del mismo
    equipo no se piden nada entre sí). Lleva un resumen del nombre de
    usuario, no el nombre, para no dejarlo a la vista de otros procesos."""
    try:
        user = getpass.getuser()
    except Exception:
        user = os.environ.get("USERNAME", "")
    digest = hashlib.sha1(user.lower().encode("utf-8")).hexdigest()[:12]
    return f"PomoGrowth-mostrar-{digest}"


def listen_for_show(on_show: Callable[[], Optional[bool]], name: Optional[str] = None,
                    parent=None):
    """La primera instancia escucha las peticiones «show» y llama a
    `on_show` (en el hilo de la interfaz) por cada una. Contesta «ok» si se
    mostró y «closing» si `on_show` devuelve False (se está cerrando); si
    falla, cierra sin contestar (la segunda da el aviso de siempre).
    Devuelve el QLocalServer, o None si no se pudo abrir (la app sigue sin
    él: una segunda instancia daría el aviso de siempre). Solo el mismo
    usuario puede conectarse; lo que no sea «show» se ignora."""
    from PySide6.QtCore import QTimer
    from PySide6.QtNetwork import QLocalServer
    server = QLocalServer(parent)
    server.setSocketOptions(QLocalServer.UserAccessOption)
    if not server.listen(name or channel_name()):
        logging.info("No se pudo abrir el canal para una segunda instancia: %s",
                     server.errorString())
        server.deleteLater()
        return None

    def serve(client) -> None:
        received = bytearray()
        answered = []

        def read() -> None:
            if answered:
                return  # una petición por conexión
            received.extend(bytes(client.read(_MAX_REQUEST - len(received))))
            if b"\n" not in received and len(received) < _MAX_REQUEST:
                return  # la línea aún no ha llegado entera
            answered.append(True)
            if bytes(received).split(b"\n", 1)[0].strip() != SHOW_COMMAND:
                client.abort()
                return
            try:
                shown = on_show() is not False
            except Exception:
                logging.exception("Al mostrar la ventana para otra instancia")
                client.abort()
                return
            client.write((SHOW_REPLY if shown else CLOSING_REPLY) + b"\n")
            client.flush()
            client.disconnectFromServer()
        client.readyRead.connect(read)
        client.disconnected.connect(client.deleteLater)
        # Una conexión muda no se queda abierta
        QTimer.singleShot(_CLIENT_TTL_MS, client, client.abort)

    def accept() -> None:
        while server.hasPendingConnections():
            serve(server.nextPendingConnection())
    server.newConnection.connect(accept)
    return server


def request_show(name: Optional[str] = None, timeout_ms: int = 1500) -> str:
    """La segunda instancia pide a la primera que se muestre. Devuelve SHOWN
    si contestó «ok» dentro del plazo, CLOSING si contestó que se está
    cerrando y NO_ANSWER si no hay nadie escuchando, no contesta a tiempo o
    algo falla (entonces, el aviso). Bloquea como mucho ~`timeout_ms`
    (conectar, enviar y esperar la respuesta comparten el plazo)."""
    from PySide6.QtNetwork import QLocalSocket
    from utils.win_native import allow_foreground_to_others
    deadline = time.monotonic() + timeout_ms / 1000

    def left() -> int:
        return max(1, int((deadline - time.monotonic()) * 1000))
    sock = QLocalSocket()
    try:
        sock.connectToServer(name or channel_name())
        if not sock.waitForConnected(left()):
            return NO_ANSWER
        # Que la ventana de la primera pueda pasar al frente (Windows solo lo
        # permite al proceso que el usuario acaba de abrir: este)
        allow_foreground_to_others()
        sock.write(SHOW_COMMAND + b"\n")
        if not sock.waitForBytesWritten(left()):
            return NO_ANSWER
        reply = b""
        while b"\n" not in reply and time.monotonic() < deadline:
            if not sock.waitForReadyRead(left()):
                break
            reply += bytes(sock.readAll())
        line = reply.split(b"\n", 1)[0].strip()
        return {SHOW_REPLY: SHOWN, CLOSING_REPLY: CLOSING}.get(line, NO_ANSWER)
    except Exception:
        return NO_ANSWER
    finally:
        sock.abort()
