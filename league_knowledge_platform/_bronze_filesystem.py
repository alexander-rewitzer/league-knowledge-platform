"""Local Bronze staging and non-overwriting publication; callers serialize writes."""

from contextlib import suppress
import os
from pathlib import Path
import re
from uuid import uuid4

_PLATFORM = os.name


def prepare_directory(directory: Path) -> None:
    missing = []
    current = directory
    while not current.exists():
        missing.append(current)
        current = current.parent
    directory.mkdir(parents=True, exist_ok=True)
    if _PLATFORM == "posix":
        for created in reversed(missing):
            sync_directory(created.parent)
        sync_directory(directory)
    elif _PLATFORM != "nt":
        raise OSError("Unsupported Bronze filesystem platform")


def sync_directory(directory: Path) -> None:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def cleanup_temporary_files(directory: Path, encoded_id: str, *, namespace: str = "bronze") -> None:
    """Only for a caller-serialized retry; never remove a canonical JSON path."""
    if namespace not in ("bronze", "silver", "segmentation"):
        raise ValueError("Unsupported artifact namespace")
    pattern = re.compile(r"\." + namespace + "-tmp-" + re.escape(encoded_id) + r"-[0-9a-f]{32}\.tmp\Z")
    for path in directory.iterdir():
        if pattern.fullmatch(path.name) and path.is_file() and not path.is_symlink():
            path.unlink()


def stage(directory: Path, encoded_id: str, content: bytes, *, namespace: str = "bronze") -> Path:
    if namespace not in ("bronze", "silver", "segmentation"):
        raise ValueError("Unsupported artifact namespace")
    path = directory / f".{namespace}-tmp-{encoded_id}-{uuid4().hex}.tmp"
    descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        return path
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise


def _windows_publish(temporary: Path, final: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    move = kernel.MoveFileExW
    move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    move.restype = wintypes.BOOL
    # WRITE_THROUGH only: neither replacement nor cross-volume copying is allowed.
    if not move(str(temporary), str(final), 0x8):
        code = ctypes.get_last_error()
        if code in (80, 183):
            raise FileExistsError(code, "Canonical Bronze path already exists")
        raise ctypes.WinError(code)


def publish(temporary: Path, final: Path) -> None:
    if _PLATFORM == "nt":
        _windows_publish(temporary, final)
    elif _PLATFORM == "posix":
        os.link(temporary, final)
        sync_directory(final.parent)
        temporary.unlink()
        sync_directory(final.parent)
    else:
        raise OSError("Unsupported Bronze filesystem platform")


def confirm_durable(final: Path) -> None:
    # Also used for recovery after a previous publication/directory-sync failure.
    # Opening without truncation and flushing never changes artifact bytes.
    with open(final, "r+b") as handle:
        os.fsync(handle.fileno())
    if _PLATFORM == "posix":
        current = final.parent
        while True:
            sync_directory(current)
            if current == current.parent:
                break
            current = current.parent
    elif _PLATFORM != "nt":
        raise OSError("Unsupported Bronze filesystem platform")
