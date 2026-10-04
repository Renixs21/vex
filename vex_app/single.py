"""One Control Center at a time: a second launch asks the first to come to the front, then exits."""
from __future__ import annotations

import getpass
from typing import Callable

from PySide6.QtNetwork import QLocalServer, QLocalSocket

NAME = "VexControlCenter-" + "".join(c for c in getpass.getuser() if c.isalnum())


def ask_running_to_focus(name: str = NAME, timeout_ms: int = 500) -> bool:
    """True if another Control Center answered (it will raise its window); this process should then exit."""
    s = QLocalSocket()
    s.connectToServer(name)
    if not s.waitForConnected(timeout_ms):
        return False
    s.write(b"focus\n")
    s.flush()
    s.waitForBytesWritten(timeout_ms)
    s.disconnectFromServer()
    return True


class SingleInstance:
    """Listens for "focus" from later launches."""

    def __init__(self, on_focus: Callable[[], None], name: str = NAME):
        self.on_focus = on_focus
        self.server = QLocalServer()
        QLocalServer.removeServer(name)                      # a crashed earlier instance may have left the name behind
        self.ok = self.server.listen(name)
        self.server.newConnection.connect(self._accept)

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            conn = self.server.nextPendingConnection()
            conn.readyRead.connect(lambda c=conn: self._read(c))

    def _read(self, conn) -> None:
        if b"focus" in bytes(conn.readAll()):
            self.on_focus()
        conn.disconnectFromServer()
