"""Tests for logging in console-less Windows service environments."""

from __future__ import annotations

import logging
import sys

import pytest

from src.dexa_ocr.utils import logger as logger_module


@pytest.fixture(autouse=True)
def reset_logging_state():
    original_stdout = sys.stdout
    original_stderr = sys.stderr
    original_handlers = list(logging.getLogger().handlers)
    original_initialized = logger_module._INITIALIZED

    logger_module._INITIALIZED = False
    yield

    root_logger = logging.getLogger()
    for handler in list(root_logger.handlers):
        if handler not in original_handlers:
            root_logger.removeHandler(handler)
            handler.close()
    for handler in original_handlers:
        if handler not in root_logger.handlers:
            root_logger.addHandler(handler)

    sys.stdout = original_stdout
    sys.stderr = original_stderr
    logger_module._INITIALIZED = original_initialized


def test_ensure_standard_streams_replaces_missing_streams(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)

    logger_module.ensure_standard_streams()

    assert callable(sys.stdout.write)
    assert callable(sys.stderr.write)
    sys.stdout.write("safe stdout")
    sys.stderr.write("safe stderr")


def test_setup_logging_captures_worker_namespace(tmp_path):
    log_file = tmp_path / "worker.log"

    logger_module.setup_logging(level="INFO", log_file=log_file)
    try:
        raise RuntimeError("traceback marker")
    except RuntimeError:
        logging.getLogger("worker.services.test").exception("request failed")

    for handler in logging.getLogger().handlers:
        handler.flush()

    contents = log_file.read_text(encoding="utf-8")
    assert "worker.services.test: request failed" in contents
    assert "RuntimeError: traceback marker" in contents
