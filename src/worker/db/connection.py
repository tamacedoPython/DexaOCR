"""
Database connection manager for the Worker layer.
Uses the same SQL Server / AgileAI credentials as the existing dexa_ocr layer,
but provides a clean, worker-scoped singleton with health-check and retry logic.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, Optional

import pyodbc

logger = logging.getLogger("worker.db")


class WorkerDBConnection:
    """
    Thread-safe singleton connection manager for the Worker layer.

    Supports multiple fallback hosts and automatic reconnection.
    Keeps one long-lived connection; callers get a cursor from it.
    For transactional writes, call .begin() / .commit() / .rollback() explicitly.
    """

    _instance: Optional["WorkerDBConnection"] = None
    _lock: threading.Lock = threading.Lock()

    def __new__(cls) -> "WorkerDBConnection":
        with cls._lock:
            if cls._instance is None:
                inst = object.__new__(cls)
                inst._conn: Optional[pyodbc.Connection] = None
                inst._conn_lock = threading.Lock()
                inst._hosts: List[str] = []
                inst._db: str = ""
                inst._uid: str = ""
                inst._pwd: str = ""
                inst._port: int = 1433
                inst._encrypt: str = "yes"
                inst._trust_cert: str = "yes"
                inst._driver: str = "ODBC Driver 17 for SQL Server"
                inst._initialized: bool = False
                cls._instance = inst
        return cls._instance

    def configure(
        self,
        hosts: List[str],
        db: str,
        uid: str,
        pwd: str,
        port: int = 1433,
        encrypt: str = "yes",
        trust_cert: str = "yes",
        driver: str = "ODBC Driver 17 for SQL Server",
    ) -> None:
        """Call once at startup before any queries."""
        self._hosts = hosts
        self._db = db
        self._uid = uid
        self._pwd = pwd
        self._port = port
        self._encrypt = encrypt
        self._trust_cert = trust_cert
        self._driver = driver
        self._initialized = True

    # ── Internal connection helpers ──────────────────────────────────────────

    def _build_conn_str(self, host: str) -> str:
        return (
            f"DRIVER={{{self._driver}}};"
            f"SERVER={host},{self._port};"
            f"DATABASE={self._db};"
            f"UID={self._uid};"
            f"PWD={self._pwd};"
            f"Encrypt={self._encrypt};"
            f"TrustServerCertificate={self._trust_cert};"
        )

    def _try_connect(self) -> Optional[pyodbc.Connection]:
        for host in self._hosts:
            try:
                logger.debug("Attempting DB connection to %s …", host)
                conn = pyodbc.connect(self._build_conn_str(host), timeout=10)
                conn.autocommit = False
                logger.info("DB connected to %s/%s", host, self._db)
                return conn
            except pyodbc.Error as exc:
                logger.warning("DB connection failed for %s: %s", host, exc)
        return None

    def _ensure_connected(self) -> pyodbc.Connection:
        if not self._initialized:
            raise RuntimeError("WorkerDBConnection not configured — call .configure() first")
        with self._conn_lock:
            if self._conn is None:
                self._conn = self._try_connect()
            else:
                try:
                    self._conn.execute("SELECT 1")
                except Exception:
                    logger.warning("Lost DB connection — reconnecting …")
                    try:
                        self._conn.close()
                    except Exception:
                        pass
                    self._conn = self._try_connect()

            if self._conn is None:
                raise ConnectionError(
                    f"Could not connect to AgileAI on any host: {self._hosts}"
                )
            return self._conn

    # ── Public API ────────────────────────────────────────────────────────────

    def cursor(self) -> pyodbc.Cursor:
        return self._ensure_connected().cursor()

    def commit(self) -> None:
        if self._conn:
            self._conn.commit()

    def rollback(self) -> None:
        if self._conn:
            self._conn.rollback()

    def close(self) -> None:
        with self._conn_lock:
            if self._conn:
                try:
                    self._conn.close()
                except Exception:
                    pass
                self._conn = None
        logger.info("DB connection closed")

    def wait_until_available(self, max_wait_seconds: int = 60) -> None:
        """Block until a connection is established or timeout is reached."""
        deadline = time.monotonic() + max_wait_seconds
        while time.monotonic() < deadline:
            try:
                self._ensure_connected()
                return
            except ConnectionError:
                logger.warning("DB not available yet — retrying in 5s …")
                time.sleep(5)
        raise ConnectionError(
            f"DB unavailable after {max_wait_seconds}s on hosts: {self._hosts}"
        )
