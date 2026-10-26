"""Best-effort Acrobat COM access, isolated to a dedicated STA worker thread."""
from __future__ import annotations
import os
import queue
import threading
import importlib.util
import logging
from typing import Any

from ..base import AdapterState, Status, ViewerAdapter

logger = logging.getLogger("acrobat_semantic_search.acrobat")


class AcrobatUnavailable(RuntimeError):
    pass


class AcrobatController:
    def __init__(self):
        self._jobs: queue.Queue = queue.Queue()
        self._available = os.name == "nt" and importlib.util.find_spec("pythoncom") is not None \
            and importlib.util.find_spec("win32com") is not None
        self._startup_error: str | None = None
        self._thread = threading.Thread(target=self._run, name="Acrobat-STA", daemon=True)
        if self._available:
            self._thread.start()

    def _call(self, method: str, *args: Any) -> Any:
        done = threading.Event()
        box: list[Any] = []
        self._jobs.put((method, args, box, done))
        if not done.wait(10):
            raise AcrobatUnavailable("Acrobat did not respond to automation.")
        if isinstance(box[0], Exception):
            raise AcrobatUnavailable(str(box[0])) from box[0]
        return box[0]

    def _run(self) -> None:
        try:
            import pythoncom  # type: ignore[import-not-found]
            import win32com.client  # type: ignore[import-not-found]
        except Exception as exc:
            self._startup_error = f"The bundled Acrobat connector could not load ({exc}). Reinstall the application."
            return
        try:
            pythoncom.CoInitialize()
            app = None
            while True:
                job = self._jobs.get()
                if job is None:
                    break
                method, args, box, done = job
                stage = "creating the Acrobat automation object"
                try:
                    if app is None:
                        app = win32com.client.Dispatch("AcroExch.App")
                    if method == "documents":
                        stage = "reading the number of open Acrobat documents"
                        value = []
                        count = int(app.GetNumAVDocs())
                        for i in range(count):
                            stage = f"accessing Acrobat document {i + 1} of {count}"
                            av_doc = app.GetAVDoc(i)
                            if av_doc is None:
                                continue
                            stage = f"reading Acrobat document {i + 1} of {count}"
                            pd_doc = av_doc.GetPDDoc()
                            if pd_doc is not None:
                                stage = f"reading the file path for Acrobat document {i + 1} of {count}"
                                filename = str(pd_doc.GetFileName() or "").strip()
                                if not filename or not filename.lower().endswith(".pdf"):
                                    continue  # Ignore unsaved documents and non-PDF Acrobat tabs.
                                path = os.path.normcase(os.path.abspath(filename))
                                if path not in value:
                                    value.append(path)
                    elif method == "path":
                        stage = "reading the active Acrobat document"
                        active = app.GetActiveDoc()
                        if active is None:
                            raise AcrobatUnavailable("No active PDF was found in Acrobat.")
                        stage = "reading the active PDF file path"
                        pd_doc = active.GetPDDoc()
                        if pd_doc is None:
                            raise AcrobatUnavailable("Could not access the active Acrobat PDF.")
                        value = os.path.normcase(os.path.abspath(pd_doc.GetFileName()))
                    elif method == "goto":
                        stage = "reading the open Acrobat documents"
                        expected = os.path.normcase(os.path.abspath(args[0]))
                        value = False
                        count = int(app.GetNumAVDocs())
                        for i in range(count):
                            stage = f"checking Acrobat document {i + 1} of {count}"
                            av_doc = app.GetAVDoc(i)
                            if av_doc is None:
                                continue
                            pd_doc = av_doc.GetPDDoc()
                            if pd_doc is None:
                                continue
                            actual = os.path.normcase(os.path.abspath(pd_doc.GetFileName()))
                            if actual == expected:
                                stage = f"bringing {os.path.basename(args[0])} to the front"
                                av_doc.BringToFront()
                                stage = f"navigating to page {args[1]}"
                                pd_doc.GetJSObject().pageNum = int(args[1]) - 1
                                value = True
                                break
                        if not value:
                            raise AcrobatUnavailable("That PDF is no longer open in Acrobat. Open it and try again.")
                    else:
                        raise ValueError("Unknown Acrobat operation")
                    box.append(value)
                except Exception as exc:
                    logger.exception("Acrobat COM call failed (method=%s, stage=%s)", method, stage)
                    detail = str(exc)
                    if "no such interface supported" in detail.lower() or "-2147467262" in detail:
                        if method == "goto":
                            try:
                                from .reader_dde import goto_page
                                goto_page(args[0], int(args[1]))
                                logger.info("Navigated with Reader DDE fallback: %s page %s", args[0], args[1])
                                box.append(True)
                                continue
                            except Exception as dde_exc:
                                logger.exception("Reader DDE navigation failed")
                                box.append(AcrobatUnavailable(str(dde_exc)))
                                continue
                        if method == "documents":
                            box.append(AcrobatUnavailable(
                                "This Adobe installation does not expose Acrobat's open-file list. "
                                "In free Reader, use Add PDFs to choose files to process. Search results can still open in Reader at the matching page."
                            ))
                            continue
                        box.append(AcrobatUnavailable(
                            f"Acrobat rejected its automation interface while {stage}. "
                            "Close Acrobat completely, reopen it, then choose Refresh list. "
                            "See data\\logs\\app.log in the app folder for the diagnostic details."
                        ))
                        continue
                    if method == "documents" and any(token in detail.lower() for token in ("invalid class", "class not registered", "class string")):
                        box.append(AcrobatUnavailable(
                            "Could not connect to desktop Acrobat. Make sure Adobe Acrobat (not a browser PDF viewer) is installed and open."
                        ))
                    else:
                        box.append(exc)
                finally:
                    done.set()
        finally:
            try:
                pythoncom.CoUninitialize()
            except Exception:
                pass

    def _ensure_available(self) -> None:
        if not self._available:
            raise AcrobatUnavailable(
                "Acrobat control is available only in the installed Windows desktop app."
            )
        if not self._thread.is_alive():
            detail = self._startup_error or "The Acrobat connector stopped during startup."
            raise AcrobatUnavailable(f"{detail} If Acrobat is already open, close and reopen this app after reinstalling.")

    def active_path(self) -> str:
        self._ensure_available()
        return self._call("path")

    def open_documents(self) -> list[str]:
        self._ensure_available()
        return self._call("documents")

    def goto_page(self, path: str, page: int) -> None:
        if page < 1:
            raise ValueError("Page number must be 1 or greater.")
        self._ensure_available()
        self._call("goto", path, page)

    def close(self) -> None:
        if self._available and self._thread.is_alive():
            self._jobs.put(None)
            self._thread.join(timeout=3)


class AcrobatAdapter(ViewerAdapter):
    """Licensed Acrobat: lists every open document and navigates over COM."""
    name = "Adobe Acrobat"

    def __init__(self):
        self.controller = AcrobatController()

    def state(self) -> AdapterState:
        if not self.controller._available:
            return AdapterState(Status.UNAVAILABLE, "Acrobat automation (pywin32) is not available.")
        if not self.controller._thread.is_alive():
            return AdapterState(Status.UNAVAILABLE, self.controller._startup_error or "The Acrobat connector stopped.")
        return AdapterState(Status.AVAILABLE)

    def open_documents(self, known_paths: list[str]) -> list[str]:
        return self.controller.open_documents()

    def can_navigate(self) -> bool:
        return True

    def goto_page(self, path: str, page: int) -> None:
        self.controller.goto_page(path, page)

    def close(self) -> None:
        self.controller.close()
