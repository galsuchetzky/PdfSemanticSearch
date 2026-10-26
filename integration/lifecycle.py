"""Per-user single-instance and stop signaling for the tray process."""
from __future__ import annotations
import ctypes
from ctypes import wintypes
import os
import threading

STOP_EVENT_NAME = r"Local\AcrobatSemanticSearchStop"
INSTANCE_MUTEX_NAME = r"Local\AcrobatSemanticSearchInstance"
ERROR_ALREADY_EXISTS = 183
EVENT_MODIFY_STATE = 0x0002
WAIT_OBJECT_0 = 0


def request_stop() -> bool:
    if os.name != "nt":
        return False
    kernel = ctypes.windll.kernel32
    handle = kernel.OpenEventW(EVENT_MODIFY_STATE, False, STOP_EVENT_NAME)
    if not handle:
        return False
    try:
        return bool(kernel.SetEvent(handle))
    finally:
        kernel.CloseHandle(handle)


class SingleInstance:
    def __init__(self):
        self.handle = None
        self.already_running = False
        if os.name == "nt":
            kernel = ctypes.windll.kernel32
            kernel.SetLastError(0)
            self.handle = kernel.CreateMutexW(None, False, INSTANCE_MUTEX_NAME)
            self.already_running = kernel.GetLastError() == ERROR_ALREADY_EXISTS

    def close(self) -> None:
        if self.handle:
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None


class StopWatcher:
    def __init__(self, callback):
        if os.name != "nt":
            raise OSError("Stop signaling is available only on Windows.")
        self.callback = callback
        self.handle = ctypes.windll.kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)
        if not self.handle:
            raise ctypes.WinError()
        self.thread = threading.Thread(target=self._wait, name="Stop-Signal", daemon=True)
        self.thread.start()

    def _wait(self):
        result = ctypes.windll.kernel32.WaitForSingleObject(self.handle, 0xFFFFFFFF)
        if result == WAIT_OBJECT_0:
            self.callback()

    def close(self):
        if self.handle:
            ctypes.windll.kernel32.SetEvent(self.handle)
            self.thread.join(timeout=2)
            ctypes.windll.kernel32.CloseHandle(self.handle)
            self.handle = None

