"""Logging configuration shared by the DexaOCR pipeline and worker."""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import IO, Optional


_INITIALIZED = False
_FALLBACK_STREAMS: list[IO[str]] = []


def ensure_standard_streams() -> None:
    """Provide writable standard streams when running without a console.

    Windows services can start Python with ``sys.stdout`` and ``sys.stderr``
    set to ``None``. PaddleOCR/Paddle and other dependencies sometimes write
    to those streams directly, even when their own logging is disabled. Give
    them a valid sink so diagnostic output cannot abort OCR.

    The opened handles are retained for the process lifetime because a library
    may keep a reference to them.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is not None and callable(getattr(stream, "write", None)):
            continue

        fallback = open(  # noqa: SIM115 - intentionally process-scoped
            os.devnull,
            mode="w",
            encoding="utf-8",
            errors="replace",
            buffering=1,
        )
        _FALLBACK_STREAMS.append(fallback)
        setattr(sys, stream_name, fallback)


def _configure_console_encoding(stream: IO[str]) -> IO[str]:
    """Use UTF-8 when the stream supports in-place reconfiguration."""
    reconfigure = getattr(stream, "reconfigure", None)
    if callable(reconfigure):
        try:
            reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
        except (AttributeError, OSError, ValueError):
            pass
    return stream


def setup_logging(
    level: str = "INFO",
    log_file: Optional[Path] = None,
) -> logging.Logger:
    """Configure application logging and return the ``dexa_ocr`` logger.

    Configuration is idempotent. The root logger is used deliberately so both
    ``dexa_ocr.*`` and ``worker.*`` records, including tracebacks, are written
    to the same destination.
    """
    global _INITIALIZED

    logger = logging.getLogger("dexa_ocr")
    if _INITIALIZED:
        return logger

    ensure_standard_streams()

    log_level = getattr(logging, level.upper(), logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # This executable owns the logging setup. Remove handlers installed by
    # basicConfig or imported libraries to avoid duplicate records.
    for existing_handler in list(root_logger.handlers):
        root_logger.removeHandler(existing_handler)
        try:
            existing_handler.close()
        except Exception:
            pass

    logger.setLevel(log_level)
    logging.getLogger("worker").setLevel(log_level)

    fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)-8s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Reconfigure the existing stream rather than wrapping stdout.buffer in a
    # second TextIOWrapper, which can invalidate the original console stream.
    console = logging.StreamHandler(_configure_console_encoding(sys.stdout))
    console.setFormatter(fmt)
    root_logger.addHandler(console)

    if log_file:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(str(log_file), encoding="utf-8")
        file_handler.setFormatter(fmt)
        root_logger.addHandler(file_handler)

    _INITIALIZED = True
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """Return the package logger or one of its children."""
    if name:
        return logging.getLogger(f"dexa_ocr.{name}")
    return logging.getLogger("dexa_ocr")
