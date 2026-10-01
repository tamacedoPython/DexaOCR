from __future__ import annotations

import pyodbc
from threading import Lock
from typing import Iterable

from .config import Settings

pyodbc.pooling = True


class DBConnectionManager:
    """Gerenciador inspirado no EchoMind, porém isolado para este projeto."""

    _instance = None
    _lock = Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._conn_str = None
                    cls._instance._driver = None
        return cls._instance

    def _pick_driver(self) -> str:
        drivers = pyodbc.drivers()
        for name in ("ODBC Driver 18 for SQL Server", "ODBC Driver 17 for SQL Server"):
            if name in drivers:
                return name
        raise RuntimeError(
            "Driver ODBC do SQL Server não encontrado. "
            f"Disponíveis: {drivers}"
        )

    def _build_conn_str(self, settings: Settings, host: str) -> str:
        driver = settings.odbc_driver or self._driver or self._pick_driver()
        self._driver = driver
        return (
            f"DRIVER={{{driver}}};"
            f"SERVER={host},{settings.sql_port};"
            f"DATABASE={settings.sql_db};"
            f"UID={settings.sql_uid};PWD={settings.sql_pwd};"
            f"Encrypt={settings.sql_encrypt};"
            f"TrustServerCertificate={settings.sql_trust_cert};"
        )

    def initialize(self, settings: Settings) -> None:
        with self._lock:
            if self._conn_str:
                return

            last_error: Exception | None = None
            for host in settings.sql_hosts:
                try:
                    conn_str = self._build_conn_str(settings, host)
                    conn = pyodbc.connect(conn_str, timeout=4)
                    conn.close()
                    self._conn_str = conn_str
                    return
                except Exception as exc:
                    last_error = exc

            raise ConnectionError(
                "Não foi possível conectar em nenhum host SQL configurado. "
                f"Último erro: {last_error}"
            )

    def connect(self, settings: Settings, autocommit: bool = False) -> pyodbc.Connection:
        if not self._conn_str:
            self.initialize(settings)
        return pyodbc.connect(self._conn_str, autocommit=autocommit)
