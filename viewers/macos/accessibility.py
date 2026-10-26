"""Find PDFs open in any macOS app through the Accessibility API.

Document-based apps (Preview, Acrobat, Reader, PDF Expert, …) expose each
window's file as the AXDocument attribute. Reading it needs Accessibility
permission; without it this adapter reports NEEDS_PERMISSION and lists nothing.
"""
from __future__ import annotations
import os
from urllib.parse import unquote, urlparse

from ..base import AdapterState, Status, ViewerAdapter
from .applescript import accessibility_trusted

# Seconds to wait for a single app to answer; a hung app must not stall the scan.
_MESSAGING_TIMEOUT = 0.5


def _document_path(value) -> str | None:
    if value is None:
        return None
    text = str(value)
    if text.startswith("file://"):
        return unquote(urlparse(text).path)
    return text if text.startswith("/") else None


class AccessibilityDocumentsAdapter(ViewerAdapter):
    name = "Other apps (Accessibility)"
    requires_permission = True

    def __init__(self, trusted=accessibility_trusted):
        self._trusted = trusted

    def state(self) -> AdapterState:
        try:
            import ApplicationServices  # type: ignore[import-not-found]  # noqa: F401
            import AppKit  # type: ignore[import-not-found]  # noqa: F401
        except ImportError:
            return AdapterState(Status.UNAVAILABLE, "pyobjc is not installed.")
        if not self._trusted(False):
            return AdapterState(Status.NEEDS_PERMISSION,
                                "Allow this app in System Settings → Privacy & Security → Accessibility.")
        return AdapterState(Status.AVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        from AppKit import NSApplicationActivationPolicyRegular, NSWorkspace  # type: ignore[import-not-found]
        from ApplicationServices import (  # type: ignore[import-not-found]
            AXUIElementCopyAttributeValue, AXUIElementCreateApplication, AXUIElementSetMessagingTimeout,
            kAXErrorSuccess,
        )
        found: list[str] = []
        own_pid = os.getpid()
        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if app.activationPolicy() != NSApplicationActivationPolicyRegular or app.processIdentifier() == own_pid:
                continue
            element = AXUIElementCreateApplication(app.processIdentifier())
            AXUIElementSetMessagingTimeout(element, _MESSAGING_TIMEOUT)
            error, windows = AXUIElementCopyAttributeValue(element, "AXWindows", None)
            if error != kAXErrorSuccess or not windows:
                continue
            for window in windows:
                error, document = AXUIElementCopyAttributeValue(window, "AXDocument", None)
                path = _document_path(document) if error == kAXErrorSuccess else None
                if path and path.lower().endswith(".pdf") and os.path.isfile(path) and path not in found:
                    found.append(path)
        return found

    def request_access(self) -> None:
        self._trusted(True)
