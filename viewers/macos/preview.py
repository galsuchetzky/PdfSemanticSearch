"""Preview.app integration.

Preview's scripting support is limited to the standard suite: it can report the
paths of open documents (Automation permission) but has no "go to page" command.
Page navigation therefore uses Go > Go to Page… (⌥⌘G) through System Events,
which additionally needs Accessibility permission. Without it, Preview still
contributes open documents and results simply are not opened.

Automatic scans only run once Automation is already granted, so they can never
show a permission prompt; prompts come only from request_access().
"""
from __future__ import annotations

from ..base import AdapterState, Status, ViewerAdapter, ViewerUnavailable
from .applescript import (CONSENT_REQUIRED, GRANTED, KEY_G, KEY_RETURN, NOT_PERMITTED, AppleScriptError, Runner,
                          accessibility_trusted, app_running, automation_permission, cancel_all, digit_key_codes,
                          launch_hidden, lines, run_osascript)

BUNDLE_ID = "com.apple.Preview"
SYSTEM_EVENTS = "com.apple.systemevents"

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

# Open (or focus) the PDF and wait until its window is in front.
OPEN_SCRIPT = f'''
on run argv
    set targetPath to item 1 of argv
    set targetFile to POSIX file targetPath
    tell application id "{BUNDLE_ID}"
        set found to false
        repeat with d in documents
            try
                if (path of d) is targetPath then set found to true
            end try
        end repeat
        if not found then open targetFile
        activate
        repeat 25 times
            try
                repeat with w in windows
                    if (path of document of w) is targetPath then
                        set index of w to 1
                        return "ready"
                    end if
                end repeat
            end try
            delay 0.2
        end repeat
    end tell
    return "unmatched"
end run
'''


def navigation_script(page: int) -> str:
    keys = "\n".join(f"    key code {code}" for code in digit_key_codes(page))
    return f'''
tell application "System Events" to tell process "Preview"
    set frontmost to true
    delay 0.2
    key code {KEY_G} using {{option down, command down}}
    repeat 20 times
        if exists sheet 1 of window 1 then exit repeat
        delay 0.1
    end repeat
{keys}
    key code {KEY_RETURN}
end tell
'''


class PreviewAdapter(ViewerAdapter):
    name = "Preview"
    requires_permission = True

    def __init__(self, runner: Runner = run_osascript, trusted=accessibility_trusted, running=app_running,
                 permission=automation_permission):
        self._run = runner
        self._running = running
        self._trusted = trusted
        self._permission = permission

    def state(self) -> AdapterState:
        if not self._running(BUNDLE_ID):
            return AdapterState(Status.AVAILABLE, "Preview is not open.")
        code = self._permission(BUNDLE_ID, False)
        if code == NOT_PERMITTED:
            return AdapterState(Status.NEEDS_PERMISSION,
                                "Allow this app to control Preview in System Settings → Privacy & Security → Automation.")
        if code == CONSENT_REQUIRED:
            return AdapterState(Status.NEEDS_PERMISSION, "Not allowed yet. Choose “Ask for permissions” to allow access to Preview.")
        return AdapterState(Status.AVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        # Never launch Preview just to ask what it has open, and never prompt from a scan.
        if not self._running(BUNDLE_ID) or self._permission(BUNDLE_ID, False) not in (GRANTED, None):
            return []
        output = self._run(LIST_SCRIPT, (), 8.0)
        return [p for p in lines(output) if p.lower().endswith(".pdf")]

    def can_navigate(self) -> bool:
        if not self._trusted(False):
            return False
        return NOT_PERMITTED not in (self._permission(BUNDLE_ID, False), self._permission(SYSTEM_EVENTS, False))

    def goto_page(self, path: str, page: int) -> None:
        keys = navigation_script(page)
        try:
            self._run(OPEN_SCRIPT, (path,), 60.0)
        except AppleScriptError as exc:
            raise ViewerUnavailable(f"Preview could not open the PDF: {exc}") from exc
        try:
            self._run(keys, (), 60.0)
        except AppleScriptError as exc:
            # The PDF is already open in Preview; trying another viewer would open it twice.
            error = ViewerUnavailable(f"Preview opened the PDF but could not go to page {page}. "
                                      "Allow this app under Privacy & Security → Accessibility.")
            error.stop_fallback = True
            raise error from exc

    def request_access(self) -> None:
        self._trusted(True)
        if self._running(BUNDLE_ID):
            self._permission(BUNDLE_ID, True)
        if self._trusted(False) and (self._running(SYSTEM_EVENTS) or launch_hidden(SYSTEM_EVENTS)):
            self._permission(SYSTEM_EVENTS, True)

    def cancel(self) -> None:
        cancel_all()
