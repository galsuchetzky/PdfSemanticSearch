"""macOS adapters with fake osascript/permission hooks; these run on any platform."""
import sys
import unittest

from viewers.base import Status, ViewerUnavailable
from viewers.macos.applescript import (CONSENT_REQUIRED, GRANTED, NOT_PERMITTED, NOT_RUNNING, AppleScriptError,
                                       digit_key_codes)
from viewers.macos.preview import OPEN_SCRIPT, PreviewAdapter, navigation_script
from viewers.macos.skim import GOTO_SCRIPT, SkimAdapter
from viewers.registry import ViewerRegistry


class FakeRunner:
    def __init__(self, output="", error=None, fail_on_call=None):
        self.output = output; self.error = error; self.fail_on_call = fail_on_call; self.calls = []

    def __call__(self, script, args, timeout):
        self.calls.append((script, tuple(args), timeout))
        if self.error and (self.fail_on_call is None or self.fail_on_call == len(self.calls)):
            raise self.error
        return self.output


class FakePermissions:
    """Records every permission query; `asked` lists queries that may prompt."""

    def __init__(self, codes=None, default=GRANTED):
        self.codes = codes or {}; self.default = default; self.asked = []; self.checked = []

    def __call__(self, bundle_id, ask):
        (self.asked if ask else self.checked).append(bundle_id)
        return self.codes.get(bundle_id, self.default)


def preview(runner=None, trusted=True, running=True, permission=None):
    return PreviewAdapter(runner=runner or FakeRunner(), trusted=lambda prompt: trusted,
                          running=lambda bundle_id: running, permission=permission or FakePermissions())


class PreviewTests(unittest.TestCase):
    def test_lists_only_pdf_paths_one_per_line(self):
        runner = FakeRunner("/Users/me/סיכום, גמר.pdf\n/Users/me/notes.txt\n/Users/me/B.PDF\n")
        self.assertEqual(preview(runner).open_documents([]), ["/Users/me/סיכום, גמר.pdf", "/Users/me/B.PDF"])

    def test_closed_preview_is_never_scripted_or_launched(self):
        runner = FakeRunner("/a.pdf\n"); permissions = FakePermissions()
        adapter = preview(runner, running=False, permission=permissions)
        self.assertEqual(adapter.state().status, Status.AVAILABLE)
        self.assertEqual(adapter.open_documents([]), [])
        self.assertEqual((runner.calls, permissions.asked), ([], []))

    def test_scan_never_prompts_when_permission_undecided_or_denied(self):
        for code in (CONSENT_REQUIRED, NOT_PERMITTED):
            runner = FakeRunner("/a.pdf\n"); permissions = FakePermissions(default=code)
            adapter = preview(runner, permission=permissions)
            self.assertEqual(adapter.state().status, Status.NEEDS_PERMISSION)
            self.assertEqual(adapter.open_documents([]), [])
            self.assertEqual(runner.calls, [])
            self.assertEqual(permissions.asked, [])

    def test_registry_skips_preview_until_permission_is_granted(self):
        runner = FakeRunner("/a.pdf\n")
        adapter = preview(runner, permission=FakePermissions(default=CONSENT_REQUIRED))
        self.assertEqual(ViewerRegistry([adapter]).scan([]).documents, [])
        self.assertEqual(runner.calls, [])

    def test_navigation_needs_accessibility_and_no_denials(self):
        self.assertFalse(preview(trusted=False).can_navigate())
        self.assertTrue(preview().can_navigate())
        self.assertTrue(preview(permission=FakePermissions(default=NOT_RUNNING)).can_navigate())
        denied = FakePermissions({"com.apple.systemevents": NOT_PERMITTED})
        self.assertFalse(preview(permission=denied).can_navigate())

    def test_navigation_opens_then_types_layout_independent_key_codes(self):
        script = navigation_script(120)
        self.assertIn("key code 5 using {option down, command down}", script)
        for code in digit_key_codes(120):
            self.assertIn(f"key code {code}", script)
        self.assertNotIn("keystroke", script)
        self.assertIn("set targetFile to POSIX file targetPath", OPEN_SCRIPT)
        runner = FakeRunner()
        preview(runner).goto_page("/x/a.pdf", 120)
        self.assertEqual([call[1] for call in runner.calls], [("/x/a.pdf",), ()])

    def test_key_failure_after_opening_stops_fallback_to_other_viewers(self):
        runner = FakeRunner(error=AppleScriptError("System Events got an error (-1719)", -1719), fail_on_call=2)
        with self.assertRaises(ViewerUnavailable) as caught:
            preview(runner).goto_page("/x/a.pdf", 2)
        self.assertTrue(caught.exception.stop_fallback)

    def test_open_failure_allows_fallback(self):
        runner = FakeRunner(error=AppleScriptError("boom (-1)", -1), fail_on_call=1)
        with self.assertRaises(ViewerUnavailable) as caught:
            preview(runner).goto_page("/x/a.pdf", 2)
        self.assertFalse(caught.exception.stop_fallback)

    def test_request_access_only_prompts_for_running_apps(self):
        permissions = FakePermissions()
        preview(running=False, trusted=False, permission=permissions).request_access()
        self.assertEqual(permissions.asked, [])

    def test_digit_key_codes_reject_invalid_pages(self):
        self.assertEqual(digit_key_codes(10), [18, 29])
        with self.assertRaises(ValueError):
            digit_key_codes(0)


def skim(runner=None, installed=True, running=True, permission=None):
    return SkimAdapter(runner=runner or FakeRunner(), installed=lambda bundle_id: installed,
                       running=lambda bundle_id: running, permission=permission or FakePermissions())


class SkimTests(unittest.TestCase):
    def test_not_installed_is_unavailable(self):
        self.assertEqual(skim(installed=False).state().status, Status.UNAVAILABLE)

    def test_goto_passes_path_and_one_based_page(self):
        runner = FakeRunner()
        adapter = skim(runner)
        self.assertTrue(adapter.can_navigate())
        adapter.goto_page("/x/a.pdf", 7)
        script, args, _timeout = runner.calls[0]
        self.assertEqual(args, ("/x/a.pdf", "7"))
        self.assertIn("go d to page pageNumber of d", script)
        self.assertIn("set targetFile to POSIX file targetPath", GOTO_SCRIPT)

    def test_denied_automation_disables_navigation_and_listing(self):
        runner = FakeRunner("/a.pdf\n")
        adapter = skim(runner, permission=FakePermissions(default=NOT_PERMITTED))
        self.assertEqual(adapter.state().status, Status.NEEDS_PERMISSION)
        self.assertFalse(adapter.can_navigate())
        self.assertEqual(adapter.open_documents([]), [])
        self.assertEqual(runner.calls, [])

    def test_goto_permission_error_becomes_viewer_unavailable(self):
        runner = FakeRunner(error=AppleScriptError("Not authorized (-1743)", NOT_PERMITTED))
        with self.assertRaises(ViewerUnavailable):
            skim(runner).goto_page("/x/a.pdf", 1)


@unittest.skipUnless(sys.platform == "darwin", "macOS Accessibility API")
class MacSystemTests(unittest.TestCase):
    def test_accessibility_state_never_prompts(self):
        from viewers.macos.accessibility import AccessibilityDocumentsAdapter
        prompts = []
        adapter = AccessibilityDocumentsAdapter(trusted=lambda prompt: prompts.append(prompt) or False)
        self.assertIn(adapter.state().status, (Status.NEEDS_PERMISSION, Status.UNAVAILABLE))
        self.assertNotIn(True, prompts)

    def test_document_url_parsing(self):
        from viewers.macos.accessibility import _document_path
        self.assertEqual(_document_path("file:///Users/me/%D7%A1%D7%99%D7%9B%D7%95%D7%9D%20a.pdf"), "/Users/me/סיכום a.pdf")
        self.assertIsNone(_document_path(None))
        self.assertIsNone(_document_path("untitled"))

    def test_permission_check_without_prompt_for_app_that_is_not_running(self):
        from viewers.macos.applescript import automation_permission
        self.assertEqual(automation_permission("com.example.not-installed", ask=False), NOT_RUNNING)


if __name__ == "__main__":
    unittest.main()
