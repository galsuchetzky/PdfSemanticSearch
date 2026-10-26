import os
import sys
import threading
import types
import unittest
from unittest.mock import patch

from integration.acrobat import AcrobatController, AcrobatUnavailable


class _Pdf:
    def __init__(self, path):
        self.path = path
        self.js = types.SimpleNamespace(pageNum=-1)

    def GetFileName(self):
        return self.path

    def GetJSObject(self):
        return self.js


class _AvDoc:
    def __init__(self, pdf):
        self.pdf = pdf
        self.front = False

    def GetPDDoc(self):
        return self.pdf

    def BringToFront(self):
        self.front = True


class _App:
    def __init__(self, avdocs):
        self.avdocs = avdocs

    def GetNumAVDocs(self):
        return len(self.avdocs)

    def GetAVDoc(self, index):
        return self.avdocs[index]


class AcrobatAutomationTests(unittest.TestCase):
    def setUp(self):
        self.old_modules = {name: sys.modules.get(name) for name in ("pythoncom", "win32com", "win32com.client")}
        self.pythoncom = types.ModuleType("pythoncom")
        self.pythoncom.CoInitialize = lambda: None
        self.pythoncom.CoUninitialize = lambda: None

    def tearDown(self):
        for name, module in self.old_modules.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def _controller(self, app):
        client = types.ModuleType("win32com.client")
        client.Dispatch = lambda _prog_id: app
        win32com = types.ModuleType("win32com")
        win32com.client = client
        sys.modules.update({"pythoncom": self.pythoncom, "win32com": win32com, "win32com.client": client})
        controller = AcrobatController.__new__(AcrobatController)
        controller._jobs = __import__("queue").Queue()
        controller._available = True
        controller._startup_error = None
        controller._thread = threading.Thread(target=controller._run, daemon=True)
        controller._thread.start()
        self.addCleanup(controller.close)
        return controller

    def test_enumerates_open_pdf_and_navigates_to_requested_page(self):
        pdf = _Pdf(r"C:\Docs\sample.pdf")
        avdoc = _AvDoc(pdf)
        controller = self._controller(_App([avdoc]))

        self.assertEqual(controller.open_documents(), [r"c:\docs\sample.pdf"])
        controller.goto_page(r"C:\Docs\sample.pdf", 7)
        self.assertTrue(avdoc.front)
        self.assertEqual(pdf.js.pageNum, 6)

    def test_interface_error_has_actionable_operation_context(self):
        class BrokenApp:
            def GetNumAVDocs(self):
                raise RuntimeError("(-2147467262, 'No such interface supported', None, None)")

        controller = self._controller(BrokenApp())
        with self.assertRaisesRegex(AcrobatUnavailable, "use Add PDFs"):
            controller.open_documents()

    def test_reader_interface_error_falls_back_to_reader_navigation(self):
        class NoReaderCom:
            def Dispatch(self, _prog_id):
                raise RuntimeError("(-2147467262, 'No such interface supported', None, None)")

        win32com = types.ModuleType("win32com")
        win32com.client = NoReaderCom()
        sys.modules["win32com"] = win32com
        sys.modules["win32com.client"] = win32com.client
        controller = self._controller_with_modules()
        from integration import reader_dde
        with patch.object(reader_dde, "goto_page") as navigate:
            controller.goto_page(r"C:\Docs\sample.pdf", 9)
        navigate.assert_called_once_with(r"C:\Docs\sample.pdf", 9)

    def _controller_with_modules(self):
        sys.modules["pythoncom"] = self.pythoncom
        controller = AcrobatController.__new__(AcrobatController)
        controller._jobs = __import__("queue").Queue()
        controller._available = True
        controller._startup_error = None
        controller._thread = threading.Thread(target=controller._run, daemon=True)
        controller._thread.start()
        self.addCleanup(controller.close)
        return controller


class ReaderDdeTests(unittest.TestCase):
    def test_navigation_command_uses_reader_commands_and_zero_based_page(self):
        from integration.reader_dde import build_navigation_command
        self.assertEqual(
            build_navigation_command(r"C:\Docs\sample.pdf", 9),
            '[DocOpen("C:\\Docs\\sample.pdf")][FileOpen("C:\\Docs\\sample.pdf")][DocGoTo("C:\\Docs\\sample.pdf",8)]',
        )

    def test_reader_cli_arguments_preserve_unicode_filename_and_one_based_page(self):
        from integration.reader_dde import build_reader_arguments
        args=build_reader_arguments(r"D:\docs\סיכום מבחן.pdf", 12)
        self.assertEqual(args[0:2], ["/A", "page=12"])
        self.assertTrue(args[2].endswith("סיכום מבחן.pdf"))

    def test_unicode_path_uses_reader_cli_fallback_when_dde_cannot_encode(self):
        from integration import reader_dde
        with patch.object(reader_dde.Path, "is_file", return_value=True), \
             patch.object(reader_dde, "build_navigation_command", side_effect=reader_dde.ReaderDdeError("no ANSI alias")), \
             patch.object(reader_dde, "_open_at_page") as launch:
            reader_dde.goto_page(r"D:\docs\סיכום מבחן.pdf", 12)
        launch.assert_called_once()
        self.assertTrue(launch.call_args.args[0].endswith("סיכום מבחן.pdf"))
        self.assertEqual(launch.call_args.args[1], 12)

    def test_reader_window_title_matches_known_indexed_pdf(self):
        from integration.reader_dde import match_reader_titles_to_paths
        paths=[r"D:\docs\report.pdf", r"D:\other\notes.pdf"]
        found=match_reader_titles_to_paths(
            ["report.pdf - Adobe Acrobat Reader (64-bit)"], paths,
        )
        self.assertEqual(found, [os.path.normcase(os.path.abspath(paths[0]))])


if __name__ == "__main__":
    unittest.main()
