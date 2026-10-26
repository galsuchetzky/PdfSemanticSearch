"""Skim (free, fully scriptable PDF reader) integration via AppleScript.

Automatic scans only run once Automation is already granted, so they can never
show a permission prompt; prompts come only from request_access() or from the
user opening a result.
"""
from __future__ import annotations

from ..base import AdapterState, Status, ViewerAdapter, ViewerUnavailable
from .applescript import (CONSENT_REQUIRED, GRANTED, NOT_PERMITTED, AppleScriptError, Runner, app_installed,
                          app_running, automation_permission, cancel_all, lines, run_osascript)

BUNDLE_ID = "net.sourceforge.skim-app.skim"

LIST_SCRIPT = f'''
tell application id "{BUNDLE_ID}"
    set out to ""
    repeat with d in documents
        try
            set p to path of d
            if p is not missing value then set out to out & p & linefeed
        end try
    end repeat
    return out
end tell
'''

GOTO_SCRIPT = f'''
on run argv
    set targetPath to item 1 of argv
    set pageNumber to (item 2 of argv) as integer
    set targetFile to POSIX file targetPath
    tell application id "{BUNDLE_ID}"
        open targetFile
        set d to missing value
        repeat with candidate in documents
            if (path of candidate) is targetPath then set d to contents of candidate
        end repeat
        if d is missing value then set d to document 1
        if pageNumber > (count of pages of d) then error "Page " & pageNumber & " is beyond the end of the PDF."
        go d to page pageNumber of d
        activate
    end tell
end run
'''


class SkimAdapter(ViewerAdapter):
    name = "Skim"
    requires_permission = True

    def __init__(self, runner: Runner = run_osascript, installed=app_installed, running=app_running,
                 permission=automation_permission):
        self._run = runner
        self._running = running
        self._installed = installed
        self._permission = permission

    def state(self) -> AdapterState:
        if not self._installed(BUNDLE_ID):
            return AdapterState(Status.UNAVAILABLE, "Skim is not installed.")
        if not self._running(BUNDLE_ID):
            return AdapterState(Status.AVAILABLE, "Skim is not open.")
        code = self._permission(BUNDLE_ID, False)
        if code == NOT_PERMITTED:
            return AdapterState(Status.NEEDS_PERMISSION,
                                "Allow this app to control Skim in System Settings → Privacy & Security → Automation.")
        if code == CONSENT_REQUIRED:
            return AdapterState(Status.NEEDS_PERMISSION, "Not allowed yet. Choose “Ask for permissions” to allow access to Skim.")
        return AdapterState(Status.AVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        # Never launch Skim just to ask what it has open, and never prompt from a scan.
        if not self._running(BUNDLE_ID) or self._permission(BUNDLE_ID, False) not in (GRANTED, None):
            return []
        output = self._run(LIST_SCRIPT, (), 8.0)
        return [p for p in lines(output) if p.lower().endswith(".pdf")]

    def can_navigate(self) -> bool:
        return self._permission(BUNDLE_ID, False) != NOT_PERMITTED

    def goto_page(self, path: str, page: int) -> None:
        if page < 1:
            raise ValueError("Page number must be 1 or greater.")
        try:
            self._run(GOTO_SCRIPT, (path, str(page)), 60.0)
        except AppleScriptError as exc:
            if exc.permission_denied:
                raise ViewerUnavailable("Opening pages in Skim needs Automation permission.") from exc
            raise ViewerUnavailable(f"Skim could not open the page: {exc}") from exc

    def request_access(self) -> None:
        if self._installed(BUNDLE_ID) and self._running(BUNDLE_ID):
            self._permission(BUNDLE_ID, True)

    def cancel(self) -> None:
        cancel_all()
