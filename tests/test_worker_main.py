"""Entrypoint routing tests that prevent duplicate RabbitMQ consumers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import worker_main


def _settings():
    settings = MagicMock()
    settings.worker_service_name = "DexaOCRWorker"
    settings.log_file = worker_main.Path("logs/dexa_worker.log")
    return settings


def test_monitor_mode_never_starts_worker():
    with patch.object(worker_main, "get_settings", return_value=_settings()), patch.object(
        worker_main, "is_service_running", return_value=True
    ), patch.object(worker_main, "run_log_monitor", return_value=0) as monitor, patch.object(
        worker_main, "run_console"
    ) as console:
        assert worker_main.main(["--monitor"]) == 0

    monitor.assert_called_once()
    console.assert_not_called()


def test_console_mode_is_refused_while_service_runs():
    with patch.object(worker_main, "get_settings", return_value=_settings()), patch.object(
        worker_main, "is_service_running", return_value=True
    ), patch.object(worker_main, "run_console") as console:
        assert worker_main.main(["--console"]) == 3

    console.assert_not_called()


def test_console_mode_runs_when_service_is_stopped():
    with patch.object(worker_main, "get_settings", return_value=_settings()), patch.object(
        worker_main, "is_service_running", return_value=False
    ), patch.object(worker_main, "run_console", return_value=0) as console:
        assert worker_main.main(["--console"]) == 0

    console.assert_called_once_with()


def test_packaged_default_monitors_running_service():
    with patch.object(worker_main, "get_settings", return_value=_settings()), patch.object(
        worker_main, "is_service_running", return_value=True
    ), patch.object(worker_main.sys, "frozen", True, create=True), patch.object(
        worker_main, "run_log_monitor", return_value=0
    ) as monitor, patch.object(worker_main, "run_console") as console:
        assert worker_main.main([]) == 0

    monitor.assert_called_once()
    console.assert_not_called()
