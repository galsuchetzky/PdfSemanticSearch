"""Combine the platform's viewer adapters behind one optional integration layer."""
from __future__ import annotations
from dataclasses import dataclass, field
import logging
import unicodedata

from core.paths import IS_MACOS, IS_WINDOWS, normalize_pdf_path
from .base import AdapterState, OpenDocument, Status, ViewerAdapter, ViewerUnavailable

logger = logging.getLogger("acrobat_semantic_search.viewers")


@dataclass
class ScanResult:
    documents: list[OpenDocument] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class ViewerRegistry:
    def __init__(self, adapters: list[ViewerAdapter], integration_enabled: bool = True):
        self.adapters = adapters
        self.integration_enabled = integration_enabled

    def _effective_state(self, adapter: ViewerAdapter) -> AdapterState:
        if not self.integration_enabled:
            return AdapterState(Status.OFF, "Viewer integration is switched off.")
        try:
            return adapter.state()
        except Exception as exc:
            logger.exception("Viewer adapter state check failed: %s", adapter.name)
            return AdapterState(Status.UNAVAILABLE, str(exc))

    def states(self) -> list[tuple[str, AdapterState]]:
        return [(adapter.name, self._effective_state(adapter)) for adapter in self.adapters]

    def _active(self) -> list[ViewerAdapter]:
        return [a for a in self.adapters if self._effective_state(a).status == Status.AVAILABLE]

    @property
    def has_permission_adapters(self) -> bool:
        return any(a.requires_permission for a in self.adapters)

    def scan(self, known_paths: list[str]) -> ScanResult:
        """List open PDFs from every usable viewer. Never raises."""
        result = ScanResult()
        seen: set[str] = set()
        active = self._active()
        primary_listed = False

        def collect(adapter: ViewerAdapter) -> bool:
            try:
                paths = adapter.open_documents(known_paths)
            except Exception as exc:
                logger.warning("%s could not list open PDFs: %s", adapter.name, exc)
                result.notes.append(f"{adapter.name}: {exc}")
                return False
            for path in paths:
                key = normalize_pdf_path(path)
                if key not in seen:
                    seen.add(key)
                    result.documents.append(OpenDocument(unicodedata.normalize("NFC", path), adapter.name))
            return True

        for adapter in active:
            if not adapter.fallback_only:
                primary_listed = collect(adapter) or primary_listed
        if not primary_listed:
            for adapter in active:
                if adapter.fallback_only and collect(adapter) and result.documents and adapter.scan_note:
                    result.notes = [adapter.scan_note]
        return result

    def can_navigate(self) -> bool:
        return any(a.can_navigate() for a in self._active())

    def goto_page(self, path: str, page: int, preferred_viewer: str | None = None) -> str:
        """Open path at page in the first viewer that succeeds; return its name."""
        candidates = [a for a in self._active() if a.can_navigate()]
        candidates.sort(key=lambda a: a.name != preferred_viewer)
        if not candidates:
            raise ViewerUnavailable("No PDF viewer integration is available to open pages.")
        errors = []
        for adapter in candidates:
            try:
                adapter.goto_page(path, page)
                return adapter.name
            except Exception as exc:
                logger.warning("%s could not open %s at page %s: %s", adapter.name, path, page, exc)
                errors.append(str(exc))
                if getattr(exc, "stop_fallback", False):
                    break
        raise ViewerUnavailable(" ".join(errors))

    def request_access(self) -> None:
        for adapter in self.adapters:
            if adapter.requires_permission:
                try:
                    adapter.request_access()
                except Exception:
                    logger.exception("Permission request failed for %s", adapter.name)

    def cancel_pending(self) -> None:
        """Abort in-flight viewer requests so background work can finish quickly."""
        for adapter in self.adapters:
            try:
                adapter.cancel()
            except Exception:
                logger.exception("Cancelling viewer adapter failed: %s", adapter.name)

    def close(self) -> None:
        for adapter in self.adapters:
            try:
                adapter.close()
            except Exception:
                logger.exception("Closing viewer adapter failed: %s", adapter.name)


def platform_adapters() -> list[ViewerAdapter]:
    if IS_WINDOWS:
        from .windows.acrobat import AcrobatAdapter
        from .windows.reader_dde import ReaderAdapter
        return [AcrobatAdapter(), ReaderAdapter()]
    if IS_MACOS:
        from .macos.preview import PreviewAdapter
        from .macos.skim import SkimAdapter
        from .macos.accessibility import AccessibilityDocumentsAdapter
        return [SkimAdapter(), PreviewAdapter(), AccessibilityDocumentsAdapter()]
    return []


def default_registry(integration_enabled: bool) -> ViewerRegistry:
    return ViewerRegistry(platform_adapters(), integration_enabled)
