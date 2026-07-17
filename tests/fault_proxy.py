from __future__ import annotations

import select
from contextlib import AbstractContextManager, suppress
from socket import (
    AF_INET,
    SHUT_RDWR,
    SO_REUSEADDR,
    SOCK_STREAM,
    SOL_SOCKET,
    socket,
)
from threading import Event, Lock, Thread, current_thread
from types import TracebackType
from urllib.parse import SplitResult, urlsplit, urlunsplit


class TcpFaultProxy(AbstractContextManager["TcpFaultProxy"]):
    """Small test-only TCP proxy that can sever and reject all connections."""

    def __init__(self, target_host: str, target_port: int) -> None:
        self._target = (target_host, target_port)
        self._listener = socket(AF_INET, SOCK_STREAM)
        self._listener.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen()
        self._listener.settimeout(0.1)
        self._available = Event()
        self._available.set()
        self._stopped = Event()
        self._lock = Lock()
        self._connections: set[socket] = set()
        self._workers: set[Thread] = set()
        self._acceptor = Thread(target=self._accept_connections, daemon=True)

    @property
    def port(self) -> int:
        return int(self._listener.getsockname()[1])

    def __enter__(self) -> TcpFaultProxy:
        self._acceptor.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        del exc_type, exc_value, traceback
        self.close()

    def set_available(self, available: bool) -> None:
        if available:
            self._available.set()
            return
        self._available.clear()
        with self._lock:
            connections = tuple(self._connections)
        for connection in connections:
            _close_socket(connection)

    def close(self) -> None:
        if self._stopped.is_set():
            return
        self._stopped.set()
        self.set_available(False)
        _close_socket(self._listener)
        self._acceptor.join(timeout=2)
        with self._lock:
            workers = tuple(self._workers)
        for worker in workers:
            worker.join(timeout=2)

    def _accept_connections(self) -> None:
        while not self._stopped.is_set():
            try:
                client, _address = self._listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            if not self._available.is_set():
                _close_socket(client)
                continue
            worker = Thread(target=self._proxy_connection, args=(client,), daemon=True)
            with self._lock:
                self._workers.add(worker)
            worker.start()

    def _proxy_connection(self, client: socket) -> None:
        upstream = socket(AF_INET, SOCK_STREAM)
        try:
            upstream.settimeout(1)
            upstream.connect(self._target)
            upstream.settimeout(None)
            with self._lock:
                if not self._available.is_set():
                    return
                self._connections.update((client, upstream))
            self._copy_bidirectionally(client, upstream)
        except (OSError, ValueError):
            pass
        finally:
            with self._lock:
                self._connections.discard(client)
                self._connections.discard(upstream)
                self._workers.discard(current_thread())
            _close_socket(client)
            _close_socket(upstream)

    def _copy_bidirectionally(self, client: socket, upstream: socket) -> None:
        peers = {client: upstream, upstream: client}
        while not self._stopped.is_set() and self._available.is_set():
            readable, _writable, _errors = select.select(tuple(peers), (), (), 0.1)
            for source in readable:
                data = source.recv(65_536)
                if not data:
                    return
                peers[source].sendall(data)


def postgres_target(database_url: str) -> tuple[str, int]:
    parsed = urlsplit(_normalize_scheme(database_url))
    if parsed.hostname is None:
        raise ValueError("PostgreSQL fault tests require a TCP database URL")
    return parsed.hostname, parsed.port or 5432


def database_url_through_proxy(database_url: str, proxy_port: int) -> str:
    parsed = urlsplit(database_url)
    userinfo, separator, _hostinfo = parsed.netloc.rpartition("@")
    prefix = f"{userinfo}{separator}" if separator else ""
    proxied = SplitResult(
        scheme=parsed.scheme,
        netloc=f"{prefix}127.0.0.1:{proxy_port}",
        path=parsed.path,
        query=parsed.query,
        fragment=parsed.fragment,
    )
    return urlunsplit(proxied)


def _normalize_scheme(database_url: str) -> str:
    return database_url.replace("postgresql+psycopg://", "postgresql://", 1)


def _close_socket(connection: socket) -> None:
    with suppress(OSError):
        connection.shutdown(SHUT_RDWR)
    connection.close()
