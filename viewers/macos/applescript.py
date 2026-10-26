"""Run AppleScript through osascript and classify macOS automation errors."""
from __future__ import annotations
import re
import subprocess
import threading
import time
from typing import Callable, Sequence

GRANTED = 0
# errAEEventNotPermitted: the user denied Automation access.
NOT_PERMITTED = -1743
# errAEEventWouldRequireUserConsent: not decided yet; asking would show a prompt.
CONSENT_REQUIRED = -1744
# procNotFound: the target app is not running, so permission cannot be checked.
NOT_RUNNING = -600
# System Events: osascript is not allowed assistive (Accessibility) access.
ASSISTIVE_DENIED = (-1719, -25211)

Runner = Callable[[str, Sequence[str], float], str]

_active: set[subprocess.Popen] = set()
_active_lock = threading.Lock()


class AppleScriptError(RuntimeError):
    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code

    @property
    def permission_denied(self) -> bool:
        return self.code in (NOT_PERMITTED, CONSENT_REQUIRED) or self.code in ASSISTIVE_DENIED


def run_osascript(script: str, args: Sequence[str] = (), timeout: float = 8.0) -> str:
    """Run script with `on run argv` arguments; return stdout without the final newline."""
    try:
        process = subprocess.Popen(["/usr/bin/osascript", "-", *args], stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    except OSError as exc:
        raise AppleScriptError(f"osascript could not run: {exc}") from exc
    with _active_lock:
        _active.add(process)
    try:
        stdout, stderr = process.communicate(script, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        process.kill(); process.communicate()
        raise AppleScriptError("The PDF viewer did not respond in time.") from exc
    finally:
        with _active_lock:
            _active.discard(process)
    if process.returncode != 0:
        message = stderr.strip()
        if process.returncode < 0:
            raise AppleScriptError("The viewer request was cancelled.")
        match = re.search(r"\((-?\d+)\)\s*$", message)
        raise AppleScriptError(message or "AppleScript failed.", int(match.group(1)) if match else None)
    return stdout.rstrip("\n")


def cancel_all() -> None:
    """Stop every running osascript (used when the app quits)."""
    with _active_lock:
        processes = list(_active)
    for process in processes:
        try:
            process.kill()
        except OSError:
            pass


def lines(output: str) -> list[str]:
    return [line for line in output.splitlines() if line.strip()]


def app_running(bundle_id: str) -> bool:
    """Check without AppleScript: compiling `tell application …` launches the app
    to load its terminology, even when the script would bail out first."""
    try:
        from AppKit import NSRunningApplication  # type: ignore[import-not-found]
    except ImportError:
        completed = subprocess.run(["/usr/bin/osascript", "-e", f'application id "{bundle_id}" is running'],
                                   capture_output=True, text=True, timeout=5)
        return completed.stdout.strip() == "true"
    return len(NSRunningApplication.runningApplicationsWithBundleIdentifier_(bundle_id)) > 0


def launch_hidden(bundle_id: str, wait: float = 3.0) -> bool:
    subprocess.run(["/usr/bin/open", "-g", "-b", bundle_id], capture_output=True, timeout=10)
    deadline = time.monotonic() + wait
    while time.monotonic() < deadline:
        if app_running(bundle_id):
            return True
        time.sleep(0.1)
    return False


def automation_permission(bundle_id: str, ask: bool = False) -> int | None:
    """Automation (Apple events) permission for a running app, without sending events.

    With ask=False this never shows a prompt: it returns GRANTED, NOT_PERMITTED,
    CONSENT_REQUIRED, or NOT_RUNNING. With ask=True it may show the macOS prompt and
    blocks until the user answers. Returns None if the check itself is unavailable.
    """
    import ctypes
    try:
        services = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/CoreServices.framework/CoreServices")
    except OSError:
        return None

    class AEDesc(ctypes.Structure):
        _fields_ = [("descriptorType", ctypes.c_uint32), ("dataHandle", ctypes.c_void_p)]

    services.AECreateDesc.argtypes = [ctypes.c_uint32, ctypes.c_void_p, ctypes.c_long, ctypes.POINTER(AEDesc)]
    services.AECreateDesc.restype = ctypes.c_int16
    services.AEDisposeDesc.argtypes = [ctypes.POINTER(AEDesc)]
    services.AEDeterminePermissionToAutomateTarget.argtypes = [ctypes.POINTER(AEDesc), ctypes.c_uint32,
                                                               ctypes.c_uint32, ctypes.c_bool]
    services.AEDeterminePermissionToAutomateTarget.restype = ctypes.c_int32
    four_cc = lambda code: int.from_bytes(code, "big")  # noqa: E731
    data = bundle_id.encode("utf-8")
    target = AEDesc()
    if services.AECreateDesc(four_cc(b"bund"), data, len(data), ctypes.byref(target)) != 0:
        return None
    try:
        return int(services.AEDeterminePermissionToAutomateTarget(
            ctypes.byref(target), four_cc(b"****"), four_cc(b"****"), bool(ask)))
    finally:
        services.AEDisposeDesc(ctypes.byref(target))


def app_installed(bundle_id: str) -> bool:
    try:
        from AppKit import NSWorkspace  # type: ignore[import-not-found]
    except ImportError:
        completed = subprocess.run(["/usr/bin/mdfind", f"kMDItemCFBundleIdentifier == '{bundle_id}'"],
                                   capture_output=True, text=True, timeout=5)
        return bool(completed.stdout.strip())
    return NSWorkspace.sharedWorkspace().URLForApplicationWithBundleIdentifier_(bundle_id) is not None


def accessibility_trusted(prompt: bool = False) -> bool:
    """Whether this process may use Accessibility (GUI scripting / window inspection)."""
    try:
        from ApplicationServices import AXIsProcessTrustedWithOptions  # type: ignore[import-not-found]
    except ImportError:
        return False
    return bool(AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": bool(prompt)}))


# Layout-independent key codes (kVK_ANSI_*): typing characters through System
# Events breaks under non-Latin keyboard layouts such as Hebrew.
DIGIT_KEY_CODES = {"0": 29, "1": 18, "2": 19, "3": 20, "4": 21, "5": 23, "6": 22, "7": 26, "8": 28, "9": 25}
KEY_G = 5
KEY_RETURN = 36


def digit_key_codes(page: int) -> list[int]:
    if page < 1:
        raise ValueError("Page number must be 1 or greater.")
    return [DIGIT_KEY_CODES[ch] for ch in str(page)]
