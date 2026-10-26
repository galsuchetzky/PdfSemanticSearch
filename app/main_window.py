"""Desktop window: manual PDFs and search always work; viewer integration is optional."""
from __future__ import annotations
import logging
import os
from pathlib import Path
import shutil
import subprocess
import threading

from PySide6.QtCore import QEvent, QObject, QThread, QTimer, QUrl, Qt, Signal
from PySide6.QtGui import QAction, QDesktopServices, QGuiApplication, QIcon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QMenu, QMessageBox, QProgressBar, QPushButton, QSystemTrayIcon,
    QTabWidget, QVBoxLayout,
)

from core.paths import APP_NAME, IS_MACOS, IS_WINDOWS, data_dir, model_dir, normalize_pdf_path
from core.service import SearchService
from viewers.base import Status
from viewers.registry import ViewerRegistry, default_registry
from .hotkey import LABEL as HOTKEY_LABEL, GlobalHotkey
from .preferences import Preferences
from .single_instance import SingleInstance

logger = logging.getLogger("acrobat_semantic_search")

FILE_MANAGER = "Finder" if IS_MACOS else "Explorer" if IS_WINDOWS else "file manager"
STATUS_TEXT = {
    Status.AVAILABLE: "ready",
    Status.OFF: "off",
    Status.NEEDS_PERMISSION: "needs permission",
    Status.UNAVAILABLE: "not available",
}


class Worker(QThread):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str, int, int)

    def __init__(self, operation):
        super().__init__(); self.operation = operation; self.cancelled = False

    def run(self):
        try:
            self.done.emit(self.operation(lambda: self.cancelled, self.progress.emit))
        except Exception as exc:
            logger.exception("Background operation failed")
            self.failed.emit(str(exc))


def reveal_in_file_manager(path: str) -> None:
    if IS_MACOS:
        subprocess.Popen(["/usr/bin/open", "-R", path])
    elif IS_WINDOWS:
        subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(Path(path).parent)))


def _searchable(status) -> bool:
    """Indexed and not known to be changed (an unreadable file keeps its index)."""
    return bool(status) and status.get("stale") is not True


def _state_line(states) -> str:
    return "  ·  ".join(f"{name}: {STATUS_TEXT[state.status]}" for name, state in states)


STYLE = """
  QDialog{background:#F1F5F6;color:#183544;%(font)s}
  QLabel{color:#213D4B} QLabel#brand{font-size:22px;font-weight:700;color:#fff}
  QLabel#tagline{color:#D2E4E8;font-size:10pt} QFrame#hero{background:#123C50;border-radius:14px}
  QFrame#panel{background:#fff;border:1px solid #DCE6E9;border-radius:12px}
  QLabel#heading{font-size:15px;font-weight:700;color:#193C4B}
  QLabel#muted{color:#687F89} QTabWidget::pane{border:0;background:transparent}
  QTabBar::tab{background:#E1E9EC;color:#385562;padding:11px 20px;margin-right:5px;border-radius:7px;font-weight:600}
  QTabBar::tab:selected{background:#087E83;color:white}
  QListWidget{background:#FBFCFD;border:1px solid #DFE8EB;border-radius:8px;padding:4px;color:#203B49}
  QListWidget::item{padding:9px;border-bottom:1px solid #E8EFF1}
  QListWidget::item:selected{background:#DDF0ED;color:#153D43;border-radius:5px}
  QLineEdit{background:#FBFCFD;border:1px solid #CAD9DE;border-radius:8px;padding:12px;font-size:11pt;color:#183544}
  QLineEdit:focus{border:2px solid #078589}
  QPushButton{background:#E6EEF0;border:0;border-radius:8px;padding:10px 14px;color:#234956;font-weight:600}
  QPushButton:hover{background:#D6E5E8} QPushButton:disabled{background:#EDF1F2;color:#9AAAB0}
  QPushButton#primary{background:#087E83;color:white} QPushButton#primary:hover{background:#066B70}
  QPushButton#link{background:transparent;color:#087E83;padding:2px 4px;text-decoration:underline}
  QCheckBox{color:#213D4B}
  QProgressBar{height:10px;border:0;border-radius:5px;background:#DFE9EB}
  QProgressBar::chunk{background:#08A0A0;border-radius:5px}
""" % {"font": "font-family:'Segoe UI'" if IS_WINDOWS else ""}


class IntegrationDialog(QDialog):
    """Explains and toggles the optional viewer integration."""

    def __init__(self, parent: "MainWindow", registry: ViewerRegistry, prefs: Preferences):
        super().__init__(parent)
        self.owner = parent; self.registry = registry; self.prefs = prefs
        parent.permissions_done.connect(self.refresh_states)
        self.setWindowTitle("Viewer integration"); self.setMinimumWidth(560); self.setStyleSheet(STYLE)
        layout = QVBoxLayout(self); layout.setSpacing(10)
        intro = QLabel(
            "Adding PDFs and searching them never needs any permission. Viewer integration is an optional extra: "
            "it lists PDFs that are already open and opens a search result at its page."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        if IS_MACOS:
            mac = QLabel(
                "On macOS it uses Preview, Skim, and other PDF apps through Automation and Accessibility permissions. "
                "macOS asks once; if you decline, the app keeps working with manually added PDFs."
            )
            mac.setObjectName("muted"); mac.setWordWrap(True); layout.addWidget(mac)
        self.enabled = QCheckBox("Find open PDFs and open results in my PDF viewer")
        self.enabled.setChecked(registry.integration_enabled); self.enabled.toggled.connect(self.toggle)
        layout.addWidget(self.enabled)
        self.states = QLabel(); self.states.setWordWrap(True); self.states.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.states)
        buttons = QHBoxLayout()
        if IS_MACOS:
            settings = QPushButton("Open Privacy & Security settings"); settings.clicked.connect(self.open_privacy_settings)
            buttons.addWidget(settings)
            self.ask = QPushButton("Ask for permissions again"); self.ask.clicked.connect(self.request_access)
            buttons.addWidget(self.ask)
        buttons.addStretch(1)
        done = QPushButton("Done"); done.setObjectName("primary"); done.clicked.connect(self.accept); buttons.addWidget(done)
        layout.addLayout(buttons)
        self.refresh_states()

    def refresh_states(self):
        rows = []
        for name, state in self.registry.states():
            line = f"<b>{name}</b>: {STATUS_TEXT[state.status]}"
            if state.detail and state.status not in (Status.AVAILABLE, Status.OFF):
                line += f"<br><span style='color:#687F89'>{state.detail}</span>"
            rows.append(line)
        self.states.setText("<br>".join(rows) or "No viewer integrations exist for this platform.")
        if IS_MACOS:
            self.ask.setEnabled(self.registry.integration_enabled and not self.owner.requesting_permissions)

    def toggle(self, checked: bool):
        self.prefs.viewer_integration = checked
        self.registry.integration_enabled = checked
        if checked and self.registry.has_permission_adapters:
            self.request_access()
        else:
            self.refresh_states()

    def request_access(self):
        if self.owner.request_permissions():
            self.states.setText("Waiting for your answer to the macOS permission prompts…")
            if IS_MACOS:
                self.ask.setEnabled(False)

    def open_privacy_settings(self):
        QDesktopServices.openUrl(QUrl("x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility"))


class MainWindow(QDialog):
    permissions_done = Signal()

    def __init__(self, service: SearchService, registry: ViewerRegistry, prefs: Preferences, icon: QIcon):
        super().__init__()
        self.requesting_permissions = False
        self.service = service; self.registry = registry; self.prefs = prefs
        self.worker = None; self.allow_quit = False; self.tray_available = False
        self.doc_items: list[QListWidgetItem] = []; self.ready_paths: list[str] = []
        self.manual_paths: list[str] = []; self.viewer_for: dict[str, str] = {}; self.can_navigate = False
        self.setWindowTitle(APP_NAME); self.setWindowIcon(icon); self.resize(970, 720); self.setMinimumSize(800, 600)
        self.setStyleSheet(STYLE); self.setAcceptDrops(True)
        outer = QVBoxLayout(self); outer.setContentsMargins(20, 18, 20, 16); outer.setSpacing(13)
        hero = QFrame(); hero.setObjectName("hero"); hl = QHBoxLayout(hero); hl.setContentsMargins(20, 15, 20, 15)
        brandbox = QVBoxLayout(); brand = QLabel(APP_NAME); brand.setObjectName("brand")
        tagline = QLabel("Find the right passage in your PDFs, in Hebrew or English."); tagline.setObjectName("tagline")
        brandbox.addWidget(brand); brandbox.addWidget(tagline); hl.addLayout(brandbox); hl.addStretch(1)
        outer.addWidget(hero)
        self.tabs = QTabWidget(); outer.addWidget(self.tabs, 1)

        self.pdf_tab = QFrame(); self.pdf_tab.setObjectName("panel")
        pl = QVBoxLayout(self.pdf_tab); pl.setContentsMargins(16, 15, 16, 15); pl.setSpacing(10)
        head = QHBoxLayout(); heading = QLabel("PDFs"); heading.setObjectName("heading"); head.addWidget(heading); head.addStretch(1)
        self.add_pdfs = QPushButton("Add PDFs…"); self.add_pdfs.clicked.connect(self.add_pdf_files); head.addWidget(self.add_pdfs)
        self.refresh = QPushButton("Refresh list"); self.refresh.clicked.connect(self.refresh_pdfs); head.addWidget(self.refresh)
        self.manage = QPushButton("Saved indexes…"); self.manage.clicked.connect(self.manage_indexes); head.addWidget(self.manage)
        pl.addLayout(head)
        integration = QHBoxLayout()
        self.integration_label = QLabel(); self.integration_label.setObjectName("muted"); self.integration_label.setWordWrap(True)
        integration.addWidget(self.integration_label, 1)
        self.integration_button = QPushButton("Viewer integration…"); self.integration_button.setObjectName("link")
        self.integration_button.clicked.connect(self.edit_integration); integration.addWidget(self.integration_button)
        pl.addLayout(integration)
        self.pdf_hint = QLabel("Add PDFs or drag them onto this window."); self.pdf_hint.setObjectName("muted"); self.pdf_hint.setWordWrap(True)
        pl.addWidget(self.pdf_hint)
        self.pdf_list = QListWidget(); self.pdf_list.setMinimumHeight(300); self.pdf_list.setWordWrap(True)
        self.pdf_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); pl.addWidget(self.pdf_list, 1)
        actions = QHBoxLayout(); self.select_all = QPushButton("Select all"); self.select_all.clicked.connect(self.check_all)
        actions.addWidget(self.select_all)
        self.process = QPushButton("Process selected PDFs"); self.process.setObjectName("primary"); self.process.setEnabled(False)
        self.process.clicked.connect(self.process_selected); actions.addStretch(1); actions.addWidget(self.process)
        pl.addLayout(actions)
        self.tabs.addTab(self.pdf_tab, "PDFs")

        self.search_tab = QFrame(); self.search_tab.setObjectName("panel")
        sl = QVBoxLayout(self.search_tab); sl.setContentsMargins(16, 15, 16, 15); sl.setSpacing(10)
        s_title = QLabel("Search processed PDFs"); s_title.setObjectName("heading"); sl.addWidget(s_title)
        self.scope = QLabel("Search includes processed PDFs and saved indexes from previous sessions.")
        self.scope.setObjectName("muted"); self.scope.setWordWrap(True); sl.addWidget(self.scope)
        queryrow = QHBoxLayout(); self.query = QLineEdit(); self.query.setPlaceholderText("Search in Hebrew or English…")
        self.query.setEnabled(False); queryrow.addWidget(self.query, 1)
        self.search_button = QPushButton("Search"); self.search_button.setObjectName("primary"); self.search_button.setEnabled(False)
        self.search_button.clicked.connect(self.search); queryrow.addWidget(self.search_button); sl.addLayout(queryrow)
        self.results = QListWidget(); self.results.setMinimumHeight(330); self.results.setWordWrap(True)
        self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.results.itemActivated.connect(self.result_activated)
        self.results.currentItemChanged.connect(lambda *_: self.update_result_actions()); sl.addWidget(self.results, 1)
        result_actions = QHBoxLayout()
        self.open_button = QPushButton("Open at page"); self.open_button.clicked.connect(self.open_current_result)
        self.copy_button = QPushButton("Copy reference"); self.copy_button.clicked.connect(self.copy_reference)
        self.reveal_button = QPushButton(f"Show in {FILE_MANAGER}"); self.reveal_button.clicked.connect(self.reveal_result)
        for button in (self.open_button, self.copy_button, self.reveal_button):
            result_actions.addWidget(button)
        result_actions.addStretch(1); sl.addLayout(result_actions)
        self.tabs.addTab(self.search_tab, "Search")
        self.query.returnPressed.connect(self.search)

        footer = QHBoxLayout(); self.status = QLabel("Ready"); self.status.setObjectName("muted"); self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.progress = QProgressBar(); self.progress.setFixedWidth(190); self.progress.hide(); footer.addWidget(self.progress)
        self.cancel = QPushButton("Cancel"); self.cancel.hide(); self.cancel.clicked.connect(self.cancel_work); footer.addWidget(self.cancel)
        outer.addLayout(footer)
        self.update_result_actions()
        self.refresh_pdfs()

    # ---- PDF list -------------------------------------------------------
    def busy(self) -> bool:
        return bool(self.worker and self.worker.isRunning())

    def refresh_pdfs(self):
        if self.busy(): return
        self.refresh.setEnabled(False); self.process.setEnabled(False)
        self.status.setText("Looking for open PDFs…" if self.registry.integration_enabled else "Loading saved indexes…")
        manual = list(self.manual_paths)

        def scan(_cancel, _progress):
            store = self.service.store

            def status(path):
                # One unreadable file must not stop the rest of the list.
                try:
                    return store.status(path)
                except Exception:
                    logger.exception("Could not check index status for %s", path)
                    return None

            known = [row["path"] for row in store.list_documents()]
            ready = [p for p in known if _searchable(status(p))]
            result = self.registry.scan(known)
            rows = [(doc.path, doc.viewer, status(doc.path)) for doc in result.documents]
            listed = {normalize_pdf_path(p) for p, _v, _s in rows}
            rows += [(p, None, status(p)) for p in manual if normalize_pdf_path(p) not in listed and Path(p).is_file()]
            return ready, rows, result.notes, self.registry.states(), self.registry.can_navigate()

        self.worker = Worker(scan); self.worker.done.connect(self.show_pdfs); self.worker.failed.connect(self.pdf_scan_failed)
        self.worker.start()

    def show_pdfs(self, result):
        ready, rows, notes, states, can_navigate = result
        self.ready_paths = ready; self.can_navigate = can_navigate
        self.viewer_for = {normalize_pdf_path(p): v for p, v, _s in rows if v}
        self.refresh.setEnabled(True); self.pdf_list.clear(); self.doc_items = []
        for path, viewer, status in rows:
            self._add_item(path, viewer, status)
        self.process.setEnabled(bool(self.doc_items))
        self.query.setEnabled(bool(self.ready_paths)); self.search_button.setEnabled(bool(self.ready_paths))
        self.show_integration_state(states)
        open_count = sum(1 for _p, v, _s in rows if v)
        if not self.registry.integration_enabled:
            hint = "Add PDFs or drag them onto this window, then process them to search."
        elif open_count:
            pending = sum(1 for _p, _v, s in rows if not _searchable(s))
            hint = f"Found {open_count} open PDF(s). {pending} need processing. Add more PDFs or drag them here."
        else:
            hint = "No open PDFs were detected. Add PDFs or drag them onto this window."
        if notes:
            hint += " " + " ".join(notes)
        self.pdf_hint.setText(hint)
        self.status.setText(f"{len(self.ready_paths)} processed PDF(s) available to search." if self.ready_paths
                            else "Add PDFs, then process them to search.")
        self.update_result_actions()

    def show_integration_state(self, states):
        if not self.registry.integration_enabled:
            self.integration_label.setText("Viewer integration: off. Results can be copied or shown in " + FILE_MANAGER + ".")
        elif states:
            self.integration_label.setText(_state_line(states))
        else:
            self.integration_label.setText("No PDF viewer integration is available on this platform.")

    def pdf_scan_failed(self, message):
        self.refresh.setEnabled(True); self.process.setEnabled(bool(self.doc_items))
        self.query.setEnabled(bool(self.ready_paths)); self.search_button.setEnabled(bool(self.ready_paths))
        self.pdf_hint.setText("Open PDFs could not be listed. Add PDFs manually; searching works the same.")
        self.status.setText(message)

    def _status_text(self, viewer, status):
        if status and status.get("stale"): state, icon = "Changed · process again", "↻"
        elif status and status.get("stale") is None: state, icon = "Processed · file could not be re-checked", "✓"
        elif status: state, icon = "Processed · ready to search", "✓"
        else: state, icon = "Not processed yet", "＋"
        source = f"open in {viewer}" if viewer else "added manually"
        return icon, f"{state}  ·  {source}"

    def _add_item(self, path, viewer, status):
        icon, state = self._status_text(viewer, status)
        item = QListWidgetItem(f"{icon}  {Path(path).name}\n     {state}")
        item.setData(Qt.UserRole, path); item.setData(Qt.UserRole + 1, status); item.setData(Qt.UserRole + 2, viewer)
        item.setToolTip(path); item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked)
        self.pdf_list.addItem(item); self.doc_items.append(item)

    def add_paths(self, files):
        existing = {normalize_pdf_path(i.data(Qt.UserRole)) for i in self.doc_items}
        added = 0
        for raw in files:
            if not raw.lower().endswith(".pdf") or not os.path.isfile(raw):
                continue
            path = os.path.abspath(raw); key = normalize_pdf_path(path)
            if key in existing: continue
            status = self.service.store.status(path)
            self._add_item(path, None, status); existing.add(key); added += 1
            if key not in {normalize_pdf_path(p) for p in self.manual_paths}:
                self.manual_paths.append(path)
            if _searchable(status):
                self._mark_ready([path])
        self.process.setEnabled(bool(self.doc_items)); self.search_button.setEnabled(bool(self.ready_paths))
        self.query.setEnabled(bool(self.ready_paths))
        self.pdf_hint.setText(f"Added {added} PDF(s). Select files to process; processed files stay available in Search.")
        self.status.setText(f"{len(self.ready_paths)} processed PDF(s) available to search.")

    def add_pdf_files(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Add PDFs to search", "", "PDF files (*.pdf)")
        if files: self.add_paths(files)

    def dragEnterEvent(self, event):
        if any(url.toLocalFile().lower().endswith(".pdf") for url in event.mimeData().urls()):
            event.acceptProposedAction()

    def dropEvent(self, event):
        self.add_paths([url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()])
        self.tabs.setCurrentWidget(self.pdf_tab)

    def check_all(self):
        checked = all(item.checkState() == Qt.Checked for item in self.doc_items)
        for item in self.doc_items: item.setCheckState(Qt.Unchecked if checked else Qt.Checked)

    def process_selected(self):
        paths = [i.data(Qt.UserRole) for i in self.doc_items if i.checkState() == Qt.Checked]
        if not paths: self.status.setText("Select at least one PDF first."); return
        if self.busy(): return
        self.process.setEnabled(False); self.refresh.setEnabled(False); self.cancel.show()
        self.progress.setRange(0, 0); self.progress.show(); self.status.setText("Processing selected PDFs locally…")
        service = self.service

        def run(cancel, progress):
            ready = []; failures = []
            for i, path in enumerate(paths, 1):
                if cancel(): raise InterruptedError("Processing cancelled.")
                current = service.store.status(path)
                if not _searchable(current):
                    try: service.index(path, lambda phase, a, b: progress(f"{Path(path).name}: {phase}", a, b), cancel, semantic=True)
                    except Exception as exc: failures.append((path, str(exc))); continue
                ready.append(path); progress("Checking selected PDFs", i, len(paths))
            return ready, failures

        self.worker = Worker(run); self.worker.progress.connect(self.update_progress); self.worker.done.connect(self.process_done)
        self.worker.failed.connect(self.work_failed); self.worker.start()

    def process_done(self, result):
        processed, failures = result
        self._mark_ready(processed); self.finish_work()
        ready_keys = {normalize_pdf_path(p) for p in self.ready_paths}
        for item in self.doc_items:
            path = item.data(Qt.UserRole)
            if normalize_pdf_path(path) in ready_keys:
                status = self.service.store.status(path); item.setData(Qt.UserRole + 1, status)
                icon, state = self._status_text(item.data(Qt.UserRole + 2), status)
                item.setText(f"{icon}  {Path(path).name}\n     {state}")
            elif any(f[0] == path for f in failures):
                detail = next(f[1] for f in failures if f[0] == path)
                item.setText(f"!  {Path(path).name}\n     Could not process: {detail}")
        self.query.setEnabled(bool(self.ready_paths)); self.search_button.setEnabled(bool(self.ready_paths))
        self.scope.setText(f"Search will look through {len(self.ready_paths)} saved, processed PDF(s).")
        self.status.setText(f"Ready to search {len(self.ready_paths)} PDF(s)." + (f" {len(failures)} could not be processed." if failures else ""))
        if self.ready_paths: self.tabs.setCurrentWidget(self.search_tab); self.query.setFocus()

    # ---- Search ---------------------------------------------------------
    def search(self):
        q = self.query.text().strip()
        if not q or not self.ready_paths or self.busy(): return
        paths = list(self.ready_paths); self.search_button.setEnabled(False); self.status.setText("Searching locally…")
        service = self.service

        def run(_cancel, _progress):
            hits = []; failures = []
            for path in paths:
                try:
                    for hit in service.search(path, q, 8): hit["path"] = path; hit["filename"] = Path(path).name; hits.append(hit)
                except Exception as exc: failures.append(f"{Path(path).name}: {exc}")
            hits.sort(key=lambda h: h.get("score", 0), reverse=True)
            return hits[:30], failures

        self.worker = Worker(run); self.worker.done.connect(self.show_results); self.worker.failed.connect(self.work_failed)
        self.worker.start()

    def show_results(self, result):
        hits, failures = result; self.results.clear(); self.search_button.setEnabled(bool(self.ready_paths))
        tip = "Double-click to open this PDF at the matching page." if self.can_navigate else "Copy the page reference or show the file."
        for hit in hits:
            excerpt = " ".join(hit["text"].split())
            item = QListWidgetItem(f"{hit['filename']}  ·  Page {hit['page']}\n{excerpt[:320]}")
            item.setData(Qt.UserRole, hit); item.setToolTip(tip); self.results.addItem(item)
        action = "Double-click a result to jump to that page." if self.can_navigate else "Select a result to copy its page reference."
        self.status.setText(f"Found {len(hits)} result(s). {action}" + (f" {len(failures)} file(s) skipped." if failures else ""))
        if hits: self.results.setCurrentRow(0)
        self.update_result_actions()

    def current_hit(self):
        item = self.results.currentItem()
        return item.data(Qt.UserRole) if item else None

    def update_result_actions(self):
        has_hit = self.current_hit() is not None
        self.open_button.setVisible(self.can_navigate)
        self.open_button.setEnabled(has_hit and self.can_navigate)
        self.copy_button.setEnabled(has_hit); self.reveal_button.setEnabled(has_hit)

    def result_activated(self, item):
        if self.can_navigate: self.open_result(item.data(Qt.UserRole))

    def open_current_result(self):
        hit = self.current_hit()
        if hit and self.can_navigate: self.open_result(hit)

    def open_result(self, hit):
        if self.busy(): return
        self.status.setText(f"Opening {hit['filename']} at page {hit['page']}…"); self.search_button.setEnabled(False)
        preferred = self.viewer_for.get(normalize_pdf_path(hit["path"]))
        self.worker = Worker(lambda _c, _p: self.registry.goto_page(hit["path"], hit["page"], preferred))

        def navigated(viewer):
            self.status.setText(f"Opened {hit['filename']} at page {hit['page']} in {viewer}.")
            self.search_button.setEnabled(True)
            if self.tray_available:
                self.hide()

        self.worker.done.connect(navigated); self.worker.failed.connect(self.work_failed); self.worker.start()

    def copy_reference(self):
        hit = self.current_hit()
        if not hit: return
        QGuiApplication.clipboard().setText(f"{hit['filename']}, page {hit['page']}\n{hit['path']}")
        self.status.setText(f"Copied: {hit['filename']}, page {hit['page']}.")

    def reveal_result(self):
        hit = self.current_hit()
        if hit: reveal_in_file_manager(hit["path"])

    # ---- Shared work state ---------------------------------------------
    def update_progress(self, phase, current, total):
        self.status.setText(f"{phase}  ·  {current}/{total}" if total else phase)
        if total: self.progress.setRange(0, total); self.progress.setValue(current)

    def finish_work(self):
        self.progress.hide(); self.cancel.hide(); self.process.setEnabled(bool(self.doc_items)); self.refresh.setEnabled(True)
        self.search_button.setEnabled(bool(self.ready_paths))

    def work_failed(self, message):
        self.finish_work(); self.status.setText(message); self.search_button.setEnabled(bool(self.ready_paths))

    def cancel_work(self):
        if self.busy(): self.worker.cancelled = True; self.status.setText("Cancelling after the current page…")

    # ---- Dialogs --------------------------------------------------------
    def edit_integration(self):
        # Keep the dialog (and any permission request still waiting on a macOS
        # prompt) alive after it closes; its worker must not be destroyed mid-run.
        self._integration_dialog = IntegrationDialog(self, self.registry, self.prefs)
        self._integration_dialog.exec()
        if not self.registry.integration_enabled:
            self.can_navigate = False; self.viewer_for = {}
            self.show_integration_state([]); self.update_result_actions()
        self.refresh_pdfs()

    def request_permissions(self) -> bool:
        """Ask for macOS permissions on a daemon thread; prompts can wait for minutes.

        A plain daemon thread (not QThread) is safe to abandon if the app quits
        while a system prompt is still open.
        """
        if self.requesting_permissions:
            return False
        self.requesting_permissions = True

        def run():
            try:
                self.registry.request_access()
            finally:
                self.requesting_permissions = False
                self.permissions_done.emit()  # queued to the GUI thread

        threading.Thread(target=run, name="Permission-Request", daemon=True).start()
        return True

    def _mark_ready(self, paths):
        keys = {normalize_pdf_path(p) for p in self.ready_paths}
        for path in paths:
            key = normalize_pdf_path(path)
            if key not in keys:
                keys.add(key); self.ready_paths.append(path)

    def manage_indexes(self):
        dialog = QDialog(self); dialog.setWindowTitle("Saved indexes"); dialog.resize(650, 420); dialog.setStyleSheet(STYLE)
        layout = QVBoxLayout(dialog)
        entries = QListWidget(); records = self.service.store.list_documents()
        for row in records:
            it = QListWidgetItem(f"{row['name']} · {row['page_count']} pages\n{row['path']}"); it.setData(Qt.UserRole, row['path']); entries.addItem(it)
        note = QLabel(f"Indexes and the downloaded model stay on this computer in {data_dir()} until you delete them here"
                      + (" or uninstall the app." if IS_WINDOWS else "."))
        note.setWordWrap(True); note.setObjectName("muted"); layout.addWidget(note); layout.addWidget(entries)
        buttons = QHBoxLayout(); delete = QPushButton("Delete selected"); delete_all = QPushButton("Delete all")
        delete_model = QPushButton("Delete downloaded model"); show_folder = QPushButton("Show data folder")
        done = QPushButton("Done"); done.setObjectName("primary")
        for button in (delete, delete_all, delete_model, show_folder): buttons.addWidget(button)
        buttons.addStretch(1); buttons.addWidget(done); layout.addLayout(buttons)

        def remove_selected():
            it = entries.currentItem()
            if not it: return
            path = it.data(Qt.UserRole)
            if QMessageBox.question(dialog, "Delete saved index", f"Delete the index for {Path(path).name}? The PDF file will stay untouched.") != QMessageBox.Yes: return
            self.service.store.delete_document(path); entries.takeItem(entries.row(it))
            self.ready_paths = [p for p in self.ready_paths if p != path]
            self.search_button.setEnabled(bool(self.ready_paths)); self.query.setEnabled(bool(self.ready_paths))

        def remove_all():
            if not records: return
            if QMessageBox.question(dialog, "Delete all indexes", f"Delete all {len(records)} saved indexes? PDF files will stay untouched.") != QMessageBox.Yes: return
            for row in records: self.service.store.delete_document(row['path'])
            self.ready_paths = []; self.search_button.setEnabled(False); self.query.setEnabled(False); self.results.clear(); entries.clear()

        def remove_model():
            if self.busy():
                QMessageBox.information(dialog, "Busy", "Wait for the current processing or search to finish."); return
            target = model_dir()
            if not target.exists():
                QMessageBox.information(dialog, "No model", "The embedding model has not been downloaded."); return
            if QMessageBox.question(dialog, "Delete downloaded model",
                                    "Delete the local embedding model? It downloads again the next time a PDF is processed. "
                                    "Until then, search uses keywords only.") != QMessageBox.Yes: return
            self.service.reset_embedder()
            shutil.rmtree(target, ignore_errors=True)

        delete.clicked.connect(remove_selected); delete_all.clicked.connect(remove_all); delete_model.clicked.connect(remove_model)
        show_folder.clicked.connect(lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(str(data_dir()))))
        done.clicked.connect(dialog.accept); dialog.exec()
        self.refresh_pdfs()

    def show_window(self):
        self.show(); self.raise_(); self.activateWindow()

    def closeEvent(self, event):
        if self.tray_available and not self.allow_quit:
            self.hide(); event.ignore(); return
        if self.busy():
            self.worker.cancelled = True
            self.registry.cancel_pending()  # don't wait out a viewer timeout
            self.worker.wait()
        event.accept()
        if not self.tray_available: QApplication.quit()


class _QuitFilter(QObject):
    """macOS ⌘Q sends a Quit event; let it close the window instead of hiding it."""

    def __init__(self, window: MainWindow):
        super().__init__(); self.window = window

    def eventFilter(self, obj, event):
        if event.type() == QEvent.Quit:
            self.window.allow_quit = True
        return False


def _load_icon(assets: Path) -> QIcon:
    for name in (("acrobat-semantic-search.ico", "acrobat-semantic-search.png") if IS_WINDOWS
                 else ("acrobat-semantic-search.png", "acrobat-semantic-search.ico")):
        icon = QIcon(str(assets / name))
        if not icon.isNull():
            logger.info("Product icon loaded from %s", assets / name)
            return icon
    logger.error("Product icon could not be loaded from %s; tray icon may not render", assets)
    return QIcon()


def run_desktop(assets: Path, argv: list[str]) -> int:
    app = QApplication(argv)
    app.setApplicationName(APP_NAME)
    instance = SingleInstance()
    if instance.notify_running_instance():
        logger.info("Another instance is running; asked it to show its window")
        return 0
    if not instance.listen():
        logger.warning("Single-instance server could not listen: %s", instance.server.errorString() if instance.server else "")
    app_icon = _load_icon(assets); app.setWindowIcon(app_icon)
    # Never hide the only window unless Qt confirms a notification area / menu bar
    # exists; otherwise closing the window could strand an invisible process.
    tray_available = QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray_available)
    prefs = Preferences()
    service = SearchService(); registry = default_registry(prefs.viewer_integration)
    window = MainWindow(service, registry, prefs, app_icon); window.tray_available = tray_available
    quit_filter = _QuitFilter(window); app.installEventFilter(quit_filter)
    instance.show_requested.connect(window.show_window)

    tray = QSystemTrayIcon(app_icon, app); tray.setToolTip(APP_NAME)
    menu = QMenu(); show_action = QAction(f"Open {APP_NAME}", menu); quit_action = QAction("Quit", menu)
    show_action.triggered.connect(window.show_window)

    def quit_app():
        window.allow_quit = True
        window.close()
        tray.hide()
        app.quit()

    quit_action.triggered.connect(quit_app); menu.addAction(show_action); menu.addSeparator(); menu.addAction(quit_action)
    tray.setContextMenu(menu)
    if not IS_MACOS:
        # On macOS a click opens the menu-bar menu; elsewhere it shows the window.
        tray.activated.connect(lambda reason: window.show_window() if reason in (QSystemTrayIcon.DoubleClick, QSystemTrayIcon.Trigger) else None)
    if tray_available:
        tray.show()
        if tray.isVisible():
            logger.info("System tray icon registered")
        else:
            logger.error("Qt reported a system tray but did not show the application icon")
            tray_available = False; window.tray_available = False; app.setQuitOnLastWindowClosed(True)
            window.status.setText("The tray icon could not be shown. Closing this window will exit the app.")
    else:
        logger.warning("No system tray is available; the app will exit when its window closes")
        window.status.setText("No tray or menu bar is available; closing the window will exit the app.")

    hotkey_bridge = _HotkeyBridge(); hotkey_bridge.show_requested.connect(window.show_window, Qt.QueuedConnection)
    hotkey = GlobalHotkey(hotkey_bridge.show_requested.emit); hotkey.start()
    if IS_MACOS:
        # Clicking the Dock icon re-activates the app; bring a hidden window back.
        app.applicationStateChanged.connect(
            lambda state: window.show_window() if state == Qt.ApplicationActive and not window.isVisible() else None)
    window.show()
    QTimer.singleShot(1500, lambda: window.status.setText(hotkey.error) if hotkey.error else None)
    for cleanup in (hotkey.stop, tray.hide, registry.close, service.store.close, instance.close):
        app.aboutToQuit.connect(cleanup)
    logger.info("Desktop window ready; hotkey %s", HOTKEY_LABEL)
    return app.exec()


class _HotkeyBridge(QObject):
    show_requested = Signal()
