import unittest

from viewers.base import AdapterState, Status, ViewerAdapter, ViewerUnavailable
from viewers.registry import ViewerRegistry


class FakeAdapter(ViewerAdapter):
    def __init__(self, name, status=Status.AVAILABLE, docs=(), navigate=False, requires_permission=False,
                 fallback_only=False, fail_list=None, fail_goto=None, scan_note=""):
        self.name = name; self._status = status; self._docs = list(docs); self._navigate = navigate
        self.requires_permission = requires_permission; self.fallback_only = fallback_only
        self.fail_list = fail_list; self.fail_goto = fail_goto; self.scan_note = scan_note
        self.opened = []; self.access_requested = False; self.list_calls = 0

    def state(self):
        return AdapterState(self._status)

    def open_documents(self, known_paths):
        self.list_calls += 1
        if self.fail_list:
            raise RuntimeError(self.fail_list)
        return self._docs

    def can_navigate(self):
        return self._navigate

    def goto_page(self, path, page):
        if self.fail_goto:
            raise ViewerUnavailable(self.fail_goto)
        self.opened.append((path, page))

    def request_access(self):
        self.access_requested = True


class RegistryTests(unittest.TestCase):
    def test_permission_adapters_stay_off_and_untouched_until_enabled(self):
        preview = FakeAdapter("Preview", docs=["/a.pdf"], navigate=True, requires_permission=True)
        registry = ViewerRegistry([preview], integration_enabled=False)
        self.assertEqual(registry.states()[0][1].status, Status.OFF)
        self.assertEqual(registry.scan([]).documents, [])
        self.assertEqual(preview.list_calls, 0)
        self.assertFalse(registry.can_navigate())
        with self.assertRaises(ViewerUnavailable):
            registry.goto_page("/a.pdf", 3)
        registry.request_access()  # still only on explicit request
        self.assertTrue(preview.access_requested)

    def test_switching_off_disables_every_adapter_including_windows_ones(self):
        acrobat = FakeAdapter("Adobe Acrobat", docs=["/a.pdf"], navigate=True)  # requires_permission=False
        registry = ViewerRegistry([acrobat], integration_enabled=False)
        self.assertEqual(registry.states()[0][1].status, Status.OFF)
        self.assertEqual(registry.scan([]).documents, [])
        self.assertFalse(registry.can_navigate())
        self.assertEqual(acrobat.list_calls, 0)

    def test_stop_fallback_prevents_opening_the_pdf_twice(self):
        error = ViewerUnavailable("opened but could not navigate"); error.stop_fallback = True
        preview = FakeAdapter("Preview", navigate=True); preview.goto_page = lambda path, page: (_ for _ in ()).throw(error)
        skim = FakeAdapter("Skim", navigate=True)
        with self.assertRaisesRegex(ViewerUnavailable, "opened but could not navigate"):
            ViewerRegistry([preview, skim]).goto_page("/p.pdf", 3, preferred_viewer="Preview")
        self.assertEqual(skim.opened, [])

    def test_no_adapters_means_manual_mode_without_errors(self):
        registry = ViewerRegistry([], integration_enabled=True)
        result = registry.scan(["/known.pdf"])
        self.assertEqual((result.documents, result.notes), ([], []))
        self.assertFalse(registry.can_navigate())

    def test_denied_or_missing_viewers_are_skipped(self):
        denied = FakeAdapter("Preview", status=Status.NEEDS_PERMISSION, docs=["/a.pdf"], navigate=True)
        missing = FakeAdapter("Skim", status=Status.UNAVAILABLE, docs=["/b.pdf"], navigate=True)
        registry = ViewerRegistry([denied, missing])
        self.assertEqual(registry.scan([]).documents, [])
        self.assertFalse(registry.can_navigate())

    def test_scan_merges_dedupes_and_survives_failures(self):
        first = FakeAdapter("A", docs=["/docs/x.pdf", "/docs/y.pdf"])
        second = FakeAdapter("B", docs=["/docs/x.pdf", "/docs/z.pdf"])
        broken = FakeAdapter("C", fail_list="boom")
        result = ViewerRegistry([first, second, broken]).scan([])
        self.assertEqual([(d.path, d.viewer) for d in result.documents],
                         [("/docs/x.pdf", "A"), ("/docs/y.pdf", "A"), ("/docs/z.pdf", "B")])
        self.assertEqual(result.notes, ["C: boom"])

    def test_fallback_adapter_used_only_when_primary_cannot_list(self):
        primary = FakeAdapter("Acrobat", fail_list="no COM")
        fallback = FakeAdapter("Reader", docs=["/r.pdf"], fallback_only=True, scan_note="Reader note")
        result = ViewerRegistry([primary, fallback]).scan(["/r.pdf"])
        self.assertEqual([d.path for d in result.documents], ["/r.pdf"])
        self.assertEqual(result.notes, ["Reader note"])

        working = FakeAdapter("Acrobat", docs=["/a.pdf"])
        fallback2 = FakeAdapter("Reader", docs=["/r.pdf"], fallback_only=True)
        ViewerRegistry([working, fallback2]).scan([])
        self.assertEqual(fallback2.list_calls, 0)

    def test_goto_prefers_viewer_that_has_the_pdf_open_then_falls_back(self):
        skim = FakeAdapter("Skim", navigate=True)
        preview = FakeAdapter("Preview", navigate=True, fail_goto="window not found")
        registry = ViewerRegistry([skim, preview])
        self.assertEqual(registry.goto_page("/p.pdf", 4, preferred_viewer="Preview"), "Skim")
        self.assertEqual(skim.opened, [("/p.pdf", 4)])

    def test_goto_reports_all_failures(self):
        a = FakeAdapter("A", navigate=True, fail_goto="first failed")
        b = FakeAdapter("B", navigate=True, fail_goto="second failed")
        with self.assertRaisesRegex(ViewerUnavailable, "first failed second failed"):
            ViewerRegistry([a, b]).goto_page("/p.pdf", 1)


if __name__ == "__main__":
    unittest.main()
