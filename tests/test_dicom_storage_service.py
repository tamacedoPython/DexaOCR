from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import call
from unittest.mock import patch

import pytest

from src.worker.services.dicom_storage_service import (
    DicomStorageUnavailableError,
    check_dicom_roots,
    connect_dicom_network_shares,
    wait_until_dicom_roots_available,
)


def test_check_dicom_roots_accepts_readable_directories(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    (first / "study").mkdir()

    check_dicom_roots([str(first), str(second)])


def test_check_dicom_roots_reports_every_unavailable_path(tmp_path: Path) -> None:
    missing_one = tmp_path / "missing-one"
    missing_two = tmp_path / "missing-two"

    with pytest.raises(DicomStorageUnavailableError) as error:
        check_dicom_roots([str(missing_one), str(missing_two)])

    assert str(missing_one) in str(error.value)
    assert str(missing_two) in str(error.value)


def test_check_dicom_roots_requires_at_least_one_path() -> None:
    with pytest.raises(DicomStorageUnavailableError, match="DICOM_ROOTS"):
        check_dicom_roots([])


def test_wait_retries_a_transient_network_failure(tmp_path: Path) -> None:
    root = str(tmp_path)
    real_scandir = os.scandir
    attempts = 0
    retries: list[str] = []

    def transient_scandir(path: str):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError("temporary SMB failure")
        return real_scandir(path)

    with patch(
        "src.worker.services.dicom_storage_service.os.scandir",
        side_effect=transient_scandir,
    ), patch("src.worker.services.dicom_storage_service.time.sleep"):
        wait_until_dicom_roots_available(
            [root],
            max_wait_seconds=10,
            retry_delay_seconds=0.01,
            on_retry=lambda exc: retries.append(str(exc)),
        )

    assert attempts == 2
    assert len(retries) == 1


def test_wait_fails_immediately_when_timeout_is_zero(tmp_path: Path) -> None:
    with pytest.raises(DicomStorageUnavailableError):
        wait_until_dicom_roots_available(
            [str(tmp_path / "missing")],
            max_wait_seconds=0,
        )


def test_wait_retries_network_authentication(tmp_path: Path) -> None:
    attempts = 0

    def prepare_access() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise DicomStorageUnavailableError("temporary net use failure")

    with patch("src.worker.services.dicom_storage_service.time.sleep"):
        wait_until_dicom_roots_available(
            [str(tmp_path)],
            max_wait_seconds=10,
            retry_delay_seconds=0.01,
            prepare_access=prepare_access,
        )

    assert attempts == 2


def test_connect_uses_each_unc_share_once() -> None:
    roots = [
        r"\\server\share\folder",
        r"\\SERVER\SHARE\other",
        r"\\server\second",
        r"C:\local",
    ]

    with patch("src.worker.services.dicom_storage_service.os.name", "nt"), patch(
        "src.worker.services.dicom_storage_service._disconnect_windows_share"
    ) as disconnect, patch(
        "src.worker.services.dicom_storage_service._connect_windows_share",
        return_value=0,
    ) as connect:
        connect_dicom_network_shares(roots, "DOMAIN\\user", "secret")

    assert disconnect.call_args_list == [
        call(r"\\server\share"),
        call(r"\\server\second"),
    ]
    assert connect.call_args_list == [
        call(r"\\server\share", "DOMAIN\\user", "secret"),
        call(r"\\server\second", "DOMAIN\\user", "secret"),
    ]


def test_connect_reports_native_windows_error() -> None:
    with patch("src.worker.services.dicom_storage_service.os.name", "nt"), patch(
        "src.worker.services.dicom_storage_service._disconnect_windows_share"
    ), patch(
        "src.worker.services.dicom_storage_service._connect_windows_share",
        return_value=86,
    ), patch(
        "src.worker.services.dicom_storage_service._windows_error_message",
        return_value="The specified network password is not correct.",
    ):
        with pytest.raises(DicomStorageUnavailableError, match="Windows error 86"):
            connect_dicom_network_shares(
                [r"\\server\share\folder"], "DOMAIN\\user", "secret"
            )
