"""System-wide shortcut that brings the search window forward.

Windows: Ctrl+Alt+F via RegisterHotKey on a message thread.
macOS: ⌘⌥F via Carbon RegisterEventHotKey, which needs no privacy permission
(unlike global keyboard monitors). Failure only sets `error`; the app works without it.
"""
from __future__ import annotations
import logging
import os
import sys
import threading
from typing import Callable

logger = logging.getLogger("acrobat_semantic_search.hotkey")

LABEL = "⌘⌥F" if sys.platform == "darwin" else "Ctrl+Alt+F"


class GlobalHotkey:
    def __init__(self, callback: Callable[[], None]):
        self.callback = callback
        self.error: str | None = None
        self._impl = None

    def start(self) -> None:
        """Call from the GUI thread after QApplication exists."""
        if os.name == "nt":
            self._impl = _WindowsHotkey(self)
        elif sys.platform == "darwin":
            self._impl = _MacHotkey(self)
        else:
            self.error = "A global shortcut is not supported on this platform."
            return
        self._impl.start()

    def stop(self) -> None:
        if self._impl:
            self._impl.stop()


class _WindowsHotkey:
    """Register Ctrl+Alt+F without installing a global keystroke hook."""

    def __init__(self, owner: GlobalHotkey):
        self.owner = owner
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._native_thread_id: int | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._listen, name="Global-Hotkey", daemon=True)
        self._thread.start()

    def _listen(self) -> None:
        import ctypes
        from ctypes import wintypes
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.RegisterHotKey.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_uint, ctypes.c_uint)
        user32.RegisterHotKey.restype = ctypes.c_int
        hotkey_id = 0xA55
        MOD_ALT, MOD_CONTROL = 0x0001, 0x0002
        self._native_thread_id = threading.get_native_id()
        ctypes.set_last_error(0)
        if not user32.RegisterHotKey(None, hotkey_id, MOD_ALT | MOD_CONTROL, 0x46):
            code = ctypes.get_last_error()
            self.owner.error = f"Ctrl+Alt+F could not be registered (Windows error {code}). It may already be in use."
            logger.error("Global hotkey registration failed: %s", self.owner.error)
            return
        logger.info("Global hotkey Ctrl+Alt+F registered")
        msg = wintypes.MSG()
        try:
            while not self._stop.is_set():
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result <= 0:
                    break
                if msg.message == 0x0312 and msg.wParam == hotkey_id:
                    self.owner.callback()
        finally:
            user32.UnregisterHotKey(None, hotkey_id)

    def stop(self) -> None:
        self._stop.set()
        # GetMessage is awakened with WM_QUIT using the listener thread ID.
        if self._thread and self._thread.is_alive():
            import ctypes
            if self._native_thread_id is not None:
                ctypes.windll.user32.PostThreadMessageW(self._native_thread_id, 0x0012, 0, 0)


def _four_cc(code: bytes) -> int:
    return int.from_bytes(code, "big")


class _MacHotkey:
    """⌘⌥F through the Carbon event manager, dispatched on the main run loop."""
    CMD_KEY, OPTION_KEY = 0x0100, 0x0800
    KEY_F = 3  # kVK_ANSI_F
    HOTKEY_PRESSED = 5  # kEventHotKeyPressed

    def __init__(self, owner: GlobalHotkey):
        self.owner = owner
        self._hotkey_ref = None
        self._handler_ref = None
        self._handler = None

    def start(self) -> None:
        import ctypes
        try:
            carbon = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/Carbon.framework/Carbon")
        except OSError as exc:
            self.owner.error = f"The {LABEL} shortcut is unavailable ({exc})."
            return

        class EventHotKeyID(ctypes.Structure):
            _fields_ = [("signature", ctypes.c_uint32), ("id", ctypes.c_uint32)]

        class EventTypeSpec(ctypes.Structure):
            _fields_ = [("eventClass", ctypes.c_uint32), ("eventKind", ctypes.c_uint32)]

        handler_type = ctypes.CFUNCTYPE(ctypes.c_int32, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)
        carbon.GetApplicationEventTarget.restype = ctypes.c_void_p
        carbon.InstallEventHandler.argtypes = [ctypes.c_void_p, handler_type, ctypes.c_ulong,
                                               ctypes.POINTER(EventTypeSpec), ctypes.c_void_p,
                                               ctypes.POINTER(ctypes.c_void_p)]
        carbon.InstallEventHandler.restype = ctypes.c_int32
        carbon.RegisterEventHotKey.argtypes = [ctypes.c_uint32, ctypes.c_uint32, EventHotKeyID, ctypes.c_void_p,
                                               ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
        carbon.RegisterEventHotKey.restype = ctypes.c_int32
        carbon.UnregisterEventHotKey.argtypes = [ctypes.c_void_p]
        carbon.RemoveEventHandler.argtypes = [ctypes.c_void_p]
        self._carbon = carbon

        def on_event(_next, _event, _data):
            try:
                self.owner.callback()
            except Exception:
                logger.exception("Hotkey callback failed")
            return 0

        self._handler = handler_type(on_event)  # keep a reference for the callback's lifetime
        target = carbon.GetApplicationEventTarget()
        spec = EventTypeSpec(_four_cc(b"keyb"), self.HOTKEY_PRESSED)
        handler_ref = ctypes.c_void_p()
        status = carbon.InstallEventHandler(target, self._handler, 1, ctypes.byref(spec), None, ctypes.byref(handler_ref))
        if status != 0:
            self.owner.error = f"The {LABEL} shortcut could not be installed (error {status})."
            return
        self._handler_ref = handler_ref
        hotkey_ref = ctypes.c_void_p()
        status = carbon.RegisterEventHotKey(self.KEY_F, self.CMD_KEY | self.OPTION_KEY,
                                            EventHotKeyID(_four_cc(b"PSSh"), 1), target, 0, ctypes.byref(hotkey_ref))
        if status != 0:
            self.owner.error = f"{LABEL} could not be registered (error {status}). It may already be in use."
            logger.error("Global hotkey registration failed: %s", self.owner.error)
            return
        self._hotkey_ref = hotkey_ref
        logger.info("Global hotkey %s registered", LABEL)

    def stop(self) -> None:
        if self._hotkey_ref:
            self._carbon.UnregisterEventHotKey(self._hotkey_ref)
            self._hotkey_ref = None
        if self._handler_ref:
            self._carbon.RemoveEventHandler(self._handler_ref)
            self._handler_ref = None
