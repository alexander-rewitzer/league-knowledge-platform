"""Local Gold filesystem primitives; no semantic operations or history management."""

from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re
import stat
from uuid import uuid4

from .gold_document_models import GoldBusyError, GoldStorageError


def _check_links(path):
    for part in reversed((path, *path.parents)):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise GoldStorageError("LINK_PATH")


def safe_path(root, relative):
    root = Path(root).absolute()
    if ".." in root.parts:
        raise GoldStorageError("ROOT_PATH")
    if not isinstance(relative, str):
        raise GoldStorageError("RELATIVE_PATH")
    parts = relative.split("/")
    for part in parts:
        if (part in ("", ".", "..") or re.fullmatch(r"[A-Za-z0-9_.-]+", part) is None
                or part.endswith((".", " "))
                or part.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$"}
                or re.fullmatch(r"(?:COM|LPT)[0-9]", part.split(".")[0], re.I)):
            raise GoldStorageError("RELATIVE_PATH")
    path = root.joinpath(*parts)
    _check_links(path)
    return path


def sync_directory(path):
    if os.name == "posix":
        descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    elif os.name != "nt":
        raise GoldStorageError("UNSUPPORTED_FILESYSTEM")


def prepare_directory(path):
    _check_links(path)
    missing, current = [], path
    while not current.exists():
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir(exist_ok=True)
        _check_links(directory)
        sync_directory(directory.parent)


def write_exclusive(path, content):
    _check_links(path)
    prepare_directory(path.parent)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    sync_directory(path.parent)


def read_bytes(path):
    _check_links(path)
    if not path.is_file():
        raise GoldStorageError("MISSING_FILE")
    return path.read_bytes()


def replace_selector(root, content):
    """Called only under publication guard; staged snapshots are already durable."""
    temporary = safe_path(root, f".current-{uuid4().hex}.tmp")
    write_exclusive(temporary, content)
    final = safe_path(root, "CURRENT.json")
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        move = ctypes.WinDLL("kernel32", use_last_error=True).MoveFileExW
        move.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
        move.restype = wintypes.BOOL
        # REPLACE_EXISTING + WRITE_THROUGH, never COPY_ALLOWED across volumes.
        if not move(str(temporary), str(final), 0x1 | 0x8):
            raise ctypes.WinError(ctypes.get_last_error())
    elif os.name == "posix":
        os.replace(temporary, final)
        sync_directory(final.parent)
    else:
        raise GoldStorageError("UNSUPPORTED_FILESYSTEM")


@contextmanager
def publication_guard(root):
    path = safe_path(root, ".publish.lock")
    prepare_directory(path.parent)
    locked = False
    with path.open("a+b") as handle:
        try:
            try:
                if os.name == "nt":
                    import msvcrt
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                elif os.name == "posix":
                    import fcntl
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    raise GoldStorageError("UNSUPPORTED_FILESYSTEM")
                locked = True
            except OSError as error:
                if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                    raise GoldBusyError("PUBLISH_BUSY") from None
                raise
            yield
        finally:
            if locked:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            # Retain the guard inode/path so concurrent publishers cannot split ownership.
