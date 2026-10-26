"""One running app per user: a second launch asks the first to show its window."""
from __future__ import annotations
import getpass
import hashlib

from PySide6.QtCore import QObject, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket

from core.paths import APP_NAME


def _server_name() -> str:
    user = getpass.getuser()
    return "pdf-semantic-search-" + hashlib.sha256(f"{APP_NAME}|{user}".encode("utf-8")).hexdigest()[:16]


class SingleInstance(QObject):
    show_requested = Signal()

    def __init__(self):
        super().__init__()
        self.name = _server_name()
        self.server: QLocalServer | None = None

    def notify_running_instance(self) -> bool:
        """Return True if another instance answered (and was asked to show itself)."""
        socket = QLocalSocket()
        socket.connectToServer(self.name)
        if not socket.waitForConnected(500):
            return False
        socket.write(b"show\n")
        socket.flush()
        socket.waitForBytesWritten(500)
        socket.disconnectFromServer()
        return True

    def listen(self) -> bool:
        # A crashed instance can leave a stale Unix socket behind.
        QLocalServer.removeServer(self.name)
        self.server = QLocalServer(self)
        self.server.newConnection.connect(self._on_connection)
        return self.server.listen(self.name)

    def _on_connection(self) -> None:
        while self.server and self.server.hasPendingConnections():
            connection = self.server.nextPendingConnection()
            connection.readyRead.connect(lambda c=connection: self._read(c))
            connection.disconnected.connect(connection.deleteLater)

    def _read(self, connection: QLocalSocket) -> None:
        if bytes(connection.readAll()).strip() == b"show":
            self.show_requested.emit()

    def close(self) -> None:
        if self.server:
            self.server.close()
            self.server = None
