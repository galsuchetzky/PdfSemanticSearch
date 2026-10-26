"""Reader-compatible navigation over Adobe's documented Windows DDE commands.

Acrobat Reader does not expose AcroExch.App (the COM API available in licensed
Acrobat). Reader does support FileOpen and DocGoTo over DDE.
"""
from __future__ import annotations

import ctypes
import os
import re
import subprocess
import time
from ctypes import wintypes
from pathlib import Path

from ..base import AdapterState, Status, ViewerAdapter


class ReaderDdeError(RuntimeError):
    pass


def _reader_executable() -> Path:
    """Return the installed Adobe desktop viewer executable."""
    import winreg
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for key_path in (
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\Acrobat.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\Acrobat.exe",
            r"SOFTWARE\Microsoft\Windows\CurrentVersion\App Paths\AcroRd32.exe",
            r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\App Paths\AcroRd32.exe",
        ):
            try:
                with winreg.OpenKey(hive, key_path) as key:
                    candidate = Path(winreg.QueryValue(key, None))
                if candidate.is_file():
                    return candidate
            except OSError:
                continue
    raise ReaderDdeError("Could not find the installed Adobe Acrobat/Reader desktop application.")


def build_reader_arguments(path: str, page: int) -> list[str]:
    """Build Adobe's Unicode-safe Windows open-at-page command line."""
    if page < 1:
        raise ValueError("Page number must be 1 or greater.")
    return ["/A", f"page={page}", os.path.abspath(path)]


def match_reader_titles_to_paths(titles: list[str], known_paths: list[str]) -> list[str]:
    """Match visible Reader window titles to known PDF paths by filename.

    Reader exposes the active tab's filename in its top-level window title, but
    the free Reader UI does not provide an API to enumerate every tab. This
    best-effort fallback only reports known paths whose PDF names are visible.
    """
    pdf_names = set()
    for title in titles:
        match = re.search(r"([^\\/:*?\"<>|]+\.pdf)", title, re.IGNORECASE)
        if match:
            pdf_names.add(match.group(1).casefold())
    result = []
    for path in known_paths:
        if os.path.basename(path).casefold() in pdf_names:
            normalized = os.path.normcase(os.path.abspath(path))
            if normalized not in result:
                result.append(normalized)
    return result


def visible_reader_titles() -> list[str]:
    """Return visible top-level Adobe Acrobat/Reader window titles."""
    if os.name != "nt":
        return []
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    titles: list[str] = []
    enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
    user32.GetWindowTextW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL

    def visit(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buffer, len(buffer))
        title = buffer.value
        lowered = title.casefold()
        if ".pdf" in lowered and ("acrobat" in lowered or "reader" in lowered):
            titles.append(title)
        return True

    callback = enum_proc(visit)
    user32.EnumWindows.argtypes = (enum_proc, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL
    user32.EnumWindows(callback, 0)
    return titles


def _open_at_page(path: str, page: int) -> None:
    exe = _reader_executable()
    try:
        subprocess.Popen(
            [str(exe), *build_reader_arguments(path, page)],
            cwd=str(exe.parent), close_fds=True,
        )
    except OSError as exc:
        raise ReaderDdeError(f"Windows could not send this PDF to Adobe Reader: {exc}") from exc


def _server_names() -> list[str]:
    """Return likely DDE service names, newest Reader naming first."""
    import winreg
    major = "26"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Adobe\Adobe Acrobat\DC\Installer") as key:
            raw, _ = winreg.QueryValueEx(key, "InitInstallVersion")
        match = re.match(r"\s*(\d+)", str(raw))
        if match:
            major = match.group(1)
    except OSError:
        pass
    # Reader has used both R and A forms in different releases.  `acroview`
    # remains the documented legacy service name.
    return list(dict.fromkeys((f"AcroViewA{major}", f"AcroViewR{major}", "acroview")))


def _ascii_path(path: str) -> str:
    """DDE's execute strings are ANSI; use a DOS short path for Unicode paths."""
    full = os.path.abspath(path)
    try:
        encoded = full.encode("mbcs")
        if encoded.decode("mbcs") == full:
            return full
    except (UnicodeError, LookupError):
        pass
    get_short = ctypes.windll.kernel32.GetShortPathNameW
    get_short.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
    get_short.restype = wintypes.DWORD
    size = get_short(full, None, 0)
    if not size:
        raise ReaderDdeError("This file path contains characters that Adobe Reader's automation cannot represent.")
    buf = ctypes.create_unicode_buffer(size + 1)
    if not get_short(full, buf, len(buf)):
        raise ReaderDdeError("Windows could not create a compatible path for Adobe Reader.")
    short = buf.value
    try:
        short.encode("mbcs")
    except UnicodeError as exc:
        raise ReaderDdeError("This file path cannot be sent to Adobe Reader's automation interface.") from exc
    return short


def build_navigation_command(path: str, page: int) -> str:
    if page < 1:
        raise ValueError("Page number must be 1 or greater.")
    dde_path = _ascii_path(path).replace('"', '""')
    quoted = f'"{dde_path}"'
    # DDE document page numbers are zero-based. DocOpen makes a document
    # addressable; FileOpen focuses it (or opens it if needed); DocGoTo jumps.
    return f"[DocOpen({quoted})][FileOpen({quoted})][DocGoTo({quoted},{page - 1})]"


class _DdeSession:
    """Small DDEML client for one Reader command batch."""

    APPCMD_CLIENTONLY = 0x00000010
    CP_WINUNICODE = 1200
    CF_TEXT = 1
    XTYP_EXECUTE = 0x4050
    TIMEOUT_MS = 5000

    def __init__(self):
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._callback_type = ctypes.WINFUNCTYPE(
            ctypes.c_void_p, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p,
            ctypes.c_size_t, ctypes.c_size_t,
        )
        self._callback = self._callback_type(lambda *_args: None)
        self.instance = wintypes.DWORD()
        self._declare()
        error = self.user32.DdeInitializeW(
            ctypes.byref(self.instance), self._callback, self.APPCMD_CLIENTONLY, 0,
        )
        if error:
            raise ReaderDdeError(f"Windows DDE initialization failed ({error}).")
        self.conversation = None
        self.service_handles = []

    def _declare(self):
        u = self.user32
        u.DdeInitializeW.argtypes = [ctypes.POINTER(wintypes.DWORD), self._callback_type, wintypes.DWORD, wintypes.DWORD]
        u.DdeInitializeW.restype = wintypes.UINT
        u.DdeCreateStringHandleW.argtypes = [wintypes.DWORD, wintypes.LPCWSTR, wintypes.UINT]
        u.DdeCreateStringHandleW.restype = ctypes.c_void_p
        u.DdeConnect.argtypes = [wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        u.DdeConnect.restype = ctypes.c_void_p
        u.DdeClientTransaction.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT, wintypes.UINT, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
        u.DdeClientTransaction.restype = ctypes.c_void_p
        u.DdeGetLastError.argtypes = [wintypes.DWORD]
        u.DdeGetLastError.restype = wintypes.UINT
        u.DdeDisconnect.argtypes = [ctypes.c_void_p]
        u.DdeFreeStringHandle.argtypes = [wintypes.DWORD, ctypes.c_void_p]
        u.DdeUninitialize.argtypes = [wintypes.DWORD]

    def connect(self) -> None:
        for name in _server_names():
            service = self.user32.DdeCreateStringHandleW(self.instance, name, self.CP_WINUNICODE)
            topic = self.user32.DdeCreateStringHandleW(self.instance, "control", self.CP_WINUNICODE)
            self.service_handles.extend((service, topic))
            conv = self.user32.DdeConnect(self.instance, service, topic, None)
            if conv:
                self.conversation = conv
                return
        raise ReaderDdeError(
            "Adobe Reader's DDE service was not found. Open a PDF in the desktop Reader app, then retry."
        )

    def execute(self, command: str) -> None:
        try:
            payload = command.encode("mbcs") + b"\0"
        except UnicodeError as exc:
            raise ReaderDdeError("The path contains characters Adobe Reader's DDE interface cannot represent.") from exc
        data = ctypes.create_string_buffer(payload)
        result = wintypes.DWORD()
        tx = self.user32.DdeClientTransaction(
            data, len(payload), self.conversation, None, self.CF_TEXT,
            self.XTYP_EXECUTE, self.TIMEOUT_MS, ctypes.byref(result),
        )
        if not tx:
            code = self.user32.DdeGetLastError(self.instance)
            raise ReaderDdeError(f"Adobe Reader did not accept the navigation command (DDE error {code}).")

    def close(self):
        if self.conversation:
            self.user32.DdeDisconnect(self.conversation)
            self.conversation = None
        for handle in self.service_handles:
            if handle:
                self.user32.DdeFreeStringHandle(self.instance, handle)
        self.user32.DdeUninitialize(self.instance)


def goto_page(path: str, page: int) -> None:
    """Open or focus a PDF in Reader, then navigate to the one-based page."""
    if os.name != "nt":
        raise ReaderDdeError("Adobe Reader navigation is available only on Windows.")
    full = os.path.abspath(path)
    if not Path(full).is_file():
        raise ReaderDdeError("The indexed PDF could not be found at its saved location.")
    try:
        command = build_navigation_command(full, page)
    except (ReaderDdeError, UnicodeError) as exc:
        # DDE execute strings use the active Windows ANSI code page. Many
        # modern volumes have 8.3 names disabled, so non-ASCII PDF paths have
        # no representable DDE alias. Adobe's /A command-line route is Unicode.
        _open_at_page(full, page)
        return
    last_error = None
    for attempt in range(2):
        session = None
        try:
            session = _DdeSession()
            session.connect()
            session.execute(command)
            return
        except ReaderDdeError as exc:
            last_error = exc
            if attempt == 0:
                time.sleep(0.8)
        finally:
            if session:
                session.close()
    # DDE isn't consistently available in modern Reader builds. The Adobe
    # command-line open parameter also focuses/reuses the existing document and
    # accepts full Unicode Windows paths.
    try:
        _open_at_page(full, page)
        return
    except ReaderDdeError as launch_error:
        raise ReaderDdeError(f"{last_error or 'Reader DDE navigation failed.'} {launch_error}") from launch_error


class ReaderAdapter(ViewerAdapter):
    """Free Reader: window-title matching plus DDE / command-line page opening."""
    name = "Adobe Reader"
    fallback_only = True
    scan_note = ("Reader only exposes the selected PDF tab here. Showing indexed PDFs matched to its window title; "
                 "use Add PDFs for other open tabs.")

    def state(self) -> AdapterState:
        # Window titles and DDE need no executable path; only the command-line
        # fallback does, and goto_page reports that if it is missing.
        if os.name != "nt":
            return AdapterState(Status.UNAVAILABLE, "Adobe Reader integration is Windows-only.")
        return AdapterState(Status.AVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        return match_reader_titles_to_paths(visible_reader_titles(), known_paths)

    def can_navigate(self) -> bool:
        return True

    def goto_page(self, path: str, page: int) -> None:
        goto_page(path, page)
