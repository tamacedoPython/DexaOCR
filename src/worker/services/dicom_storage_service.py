"""Startup availability check for the configured DICOM storage roots."""
from __future__ import annotations

import os
import re
import time
from collections.abc import Callable, Sequence


class DicomStorageUnavailableError(ConnectionError):
    """Raised when one or more configured DICOM roots cannot be read."""


_UNC_SHARE_PATTERN = re.compile(r"^(\\\\[^\\]+\\[^\\]+)")


def _disconnect_windows_share(share: str) -> None:
    """Remove an SMB connection without invoking a command-line process."""
    import ctypes

    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    cancel = mpr.WNetCancelConnection2W
    cancel.argtypes = [ctypes.c_wchar_p, ctypes.c_uint, ctypes.c_bool]
    cancel.restype = ctypes.c_uint
    # A missing connection is expected on first startup, so cancellation
    # errors are intentionally ignored.
    cancel(share, 0, True)


def _connect_windows_share(share: str, username: str, password: str) -> int:
    """Connect an SMB share with credentials kept out of process arguments."""
    import ctypes
    from ctypes import wintypes

    class NETRESOURCEW(ctypes.Structure):
        _fields_ = [
            ("dwScope", wintypes.DWORD),
            ("dwType", wintypes.DWORD),
            ("dwDisplayType", wintypes.DWORD),
            ("dwUsage", wintypes.DWORD),
            ("lpLocalName", wintypes.LPWSTR),
            ("lpRemoteName", wintypes.LPWSTR),
            ("lpComment", wintypes.LPWSTR),
            ("lpProvider", wintypes.LPWSTR),
        ]

    resource = NETRESOURCEW()
    resource.dwType = 1  # RESOURCETYPE_DISK
    resource.lpRemoteName = share

    mpr = ctypes.WinDLL("mpr", use_last_error=True)
    connect = mpr.WNetAddConnection2W
    connect.argtypes = [
        ctypes.POINTER(NETRESOURCEW),
        wintypes.LPCWSTR,
        wintypes.LPCWSTR,
        wintypes.DWORD,
    ]
    connect.restype = wintypes.DWORD
    return int(connect(ctypes.byref(resource), password, username, 0))


def _windows_error_message(code: int) -> str:
    import ctypes

    return ctypes.FormatError(code).strip() or f"Windows error {code}"


def _unc_shares(dicom_roots: Sequence[str]) -> list[str]:
    r"""Return unique ``\\server\share`` paths from the configured roots."""
    shares: list[str] = []
    seen: set[str] = set()
    for root in dicom_roots:
        match = _UNC_SHARE_PATTERN.match(root.strip())
        if match is None:
            continue
        share = match.group(1)
        key = share.casefold()
        if key not in seen:
            shares.append(share)
            seen.add(key)
    return shares


def connect_dicom_network_shares(
    dicom_roots: Sequence[str],
    username: str,
    password: str,
    *,
    timeout_seconds: float = 30,
) -> None:
    """Authenticate UNC shares in the worker's Windows logon session.

    ``WNetAddConnection2W`` receives the password in process memory, so it is
    not exposed in a child process command line or in application logs.
    Existing connections to the configured shares are removed first to avoid
    Windows error 1219 (multiple credentials for the same server).
    """
    if os.name != "nt":
        raise DicomStorageUnavailableError(
            "DICOM network credentials can only be used on Windows"
        )
    if not username or not password:
        raise DicomStorageUnavailableError(
            "DICOM_NETWORK_USERNAME and DICOM_NETWORK_PASSWORD must both be set"
        )

    shares = _unc_shares(dicom_roots)
    if not shares:
        return

    for share in shares:
        _disconnect_windows_share(share)

    for share in shares:
        try:
            result = _connect_windows_share(share, username, password)
        except OSError as exc:
            raise DicomStorageUnavailableError(
                f"Could not connect to {share}: {exc}"
            ) from exc

        if result != 0:
            raise DicomStorageUnavailableError(
                f"SMB connection failed for {share} "
                f"(Windows error {result}): {_windows_error_message(result)}"
            )


def check_dicom_roots(dicom_roots: Sequence[str]) -> None:
    """Ensure every configured DICOM root exists and can be listed.

    Listing the directory is intentional: a simple path check is not enough for
    an SMB share because it may hide authentication and permission failures.
    This code runs in the worker process, so it validates the same Windows
    identity that will later open the DICOM files.
    """
    if not dicom_roots:
        raise DicomStorageUnavailableError("DICOM_ROOTS does not contain any paths")

    failures: list[str] = []
    for root in dicom_roots:
        try:
            with os.scandir(root) as entries:
                # Force Windows/SMB to perform the first directory read.  An
                # empty root is valid; the purpose here is checking access.
                next(entries, None)
        except (OSError, ValueError) as exc:
            detail = exc.strerror if isinstance(exc, OSError) and exc.strerror else str(exc)
            failures.append(f"{root} ({detail})")

    if failures:
        raise DicomStorageUnavailableError(
            "DICOM storage unavailable: " + "; ".join(failures)
        )


def wait_until_dicom_roots_available(
    dicom_roots: Sequence[str],
    *,
    max_wait_seconds: float = 120,
    retry_delay_seconds: float = 5,
    on_retry: Callable[[DicomStorageUnavailableError], None] | None = None,
    prepare_access: Callable[[], None] | None = None,
) -> None:
    """Wait until all DICOM roots are readable or the startup timeout expires."""
    deadline = time.monotonic() + max(0, max_wait_seconds)

    while True:
        try:
            if prepare_access is not None:
                prepare_access()
            check_dicom_roots(dicom_roots)
            return
        except DicomStorageUnavailableError as exc:
            if time.monotonic() >= deadline:
                raise
            if on_retry is not None:
                on_retry(exc)
            remaining = max(0, deadline - time.monotonic())
            time.sleep(min(max(0.01, retry_delay_seconds), remaining))
