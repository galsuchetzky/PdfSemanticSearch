"""Common interface for PDF viewer integrations.

Viewer integration is optional. Indexing and search never depend on it: an
adapter that is missing, switched off, or denied permission simply contributes
no open documents and cannot open results.
"""
from __future__ import annotations
from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    AVAILABLE = "available"
    OFF = "off"  # needs an OS permission and the user has not switched integration on
    NEEDS_PERMISSION = "needs_permission"
    UNAVAILABLE = "unavailable"  # viewer not installed or automation unsupported


@dataclass(frozen=True)
class AdapterState:
    status: Status
    detail: str = ""


@dataclass(frozen=True)
class OpenDocument:
    path: str
    viewer: str


class ViewerUnavailable(RuntimeError):
    # Set when the viewer already opened the PDF, so trying another viewer
    # would only open it a second time.
    stop_fallback = False


class ViewerAdapter:
    name = "PDF viewer"
    # Adapters that need an OS permission (macOS) can show prompts from
    # request_access(); integration is opt-in on platforms that have them.
    requires_permission = False
    # Fallback adapters are only asked for open documents when no primary
    # adapter could list them (for example Reader window titles on Windows).
    fallback_only = False
    # Shown when a fallback adapter supplied the open-document list.
    scan_note = ""

    def state(self) -> AdapterState:
        return AdapterState(Status.UNAVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        """Return paths of PDFs open in this viewer. May raise on failure."""
        return []

    def can_navigate(self) -> bool:
        return False

    def goto_page(self, path: str, page: int) -> None:
        raise ViewerUnavailable(f"{self.name} cannot open pages.")

    def request_access(self) -> None:
        """Ask the OS for the permissions this adapter needs (may show prompts)."""

    def cancel(self) -> None:
        """Abort in-flight requests (called when the app quits)."""

    def close(self) -> None:
        pass
