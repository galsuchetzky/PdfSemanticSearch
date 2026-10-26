"""Local Acrobat semantic search. Run with --help for CLI commands."""
from __future__ import annotations
import argparse
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import sys

settings_root = Path(sys.executable).resolve().parent if getattr(sys, "frozen", False) else Path(__file__).resolve().parent
settings_path = settings_root / "app-settings.json"
if settings_path.is_file():
    try:
        installed_settings = json.loads(settings_path.read_text(encoding="utf-8"))
        installed_data = str(Path(installed_settings["data_dir"]).resolve())
        os.environ.setdefault("ACROBAT_SEMANTIC_DATA_DIR", installed_data)
        os.environ.setdefault("FASTEMBED_CACHE_PATH", str(Path(installed_data) / "models"))
    except (OSError, ValueError, KeyError, TypeError):
        pass

_log_dir = Path(os.environ.get("ACROBAT_SEMANTIC_DATA_DIR", str(settings_root / "data"))) / "logs"
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _handler = RotatingFileHandler(_log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[_handler], format="%(asctime)s %(levelname)s %(name)s: %(message)s")
except OSError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("acrobat_semantic_search")
logger.info("Application starting; executable=%s", sys.executable)

from core.service import SearchService
from core.index_store import IndexStore


def _desktop_legacy() -> int:
    try:
        from PySide6.QtCore import QObject, QThread, Signal, Slot, Qt, QTimer
        from PySide6.QtGui import QKeySequence, QShortcut, QIcon
        from PySide6.QtWidgets import (QApplication, QDialog, QLabel,
            QLineEdit, QListWidget, QListWidgetItem, QProgressBar,
            QVBoxLayout, QMessageBox, QFileDialog, QPushButton, QHBoxLayout)
        from integration.acrobat import AcrobatController, AcrobatUnavailable, GlobalHotkey
    except ImportError as exc:
        print(f"Desktop dependencies are missing: {exc}. Install requirements.txt.", file=sys.stderr)
        return 2

    class Bridge(QObject):
        open_overlay = Signal()
        result = Signal(object)
        error = Signal(str)
        progress = Signal(str, int, int)

    class Worker(QThread):
        done = Signal(object)
        failed = Signal(str)
        progress = Signal(str, int, int)
        def __init__(self, operation):
            super().__init__(); self.operation=operation; self.cancelled=False
        def run(self):
            try: self.done.emit(self.operation(lambda: self.cancelled, self.progress.emit))
            except Exception as exc:
                logger.exception("Background operation failed")
                self.failed.emit(str(exc))

    class Overlay(QDialog):
        def __init__(self, service, acrobat):
            super().__init__(); self.service=service; self.acrobat=acrobat; self.path=None; self.open_paths=[]; self.worker=None
            self.setWindowTitle("Acrobat Semantic Search"); self.setMinimumSize(720, 520)
            self.setStyleSheet("QDialog{background:#202124;color:#eee} QLineEdit,QListWidget{background:#303134;color:#eee;border:1px solid #555;padding:10px;font-size:15px} QLabel{color:#ddd;padding:4px}")
            layout=QVBoxLayout(self); self.title=QLabel("Search the active Acrobat PDF"); self.input=QLineEdit(); self.input.setPlaceholderText("Search Hebrew or English terms…")
            self.list=QListWidget(); self.info=QLabel(""); self.progress=QProgressBar(); self.progress.hide(); self.cancel=QPushButton("Cancel indexing"); self.cancel.hide(); self.cancel.clicked.connect(self.cancel_index)
            actions=QHBoxLayout(); self.active_button=QPushButton("Search open Acrobat PDFs"); self.choose_button=QPushButton("Choose a PDF…"); self.manage_button=QPushButton("Manage saved indexes…")
            self.active_button.clicked.connect(self.search_open_documents); self.choose_button.clicked.connect(self.choose_pdf)
            self.manage_button.clicked.connect(self.manage_indexes)
            actions.addWidget(self.active_button); actions.addWidget(self.choose_button); actions.addWidget(self.manage_button)
            layout.addLayout(actions); layout.addWidget(self.title); layout.addWidget(self.input); layout.addWidget(self.list); layout.addWidget(self.info); layout.addWidget(self.progress); layout.addWidget(self.cancel)
            self.input.returnPressed.connect(self.search); self.list.itemActivated.connect(self.open_result)
            self._move_down=QShortcut(QKeySequence(Qt.Key_Down),self); self._move_down.activated.connect(lambda:self.move_result(1))
            self._move_up=QShortcut(QKeySequence(Qt.Key_Up),self); self._move_up.activated.connect(lambda:self.move_result(-1))
            self.input.setFocus()
        def show_for_active(self):
            self.show(); self.raise_(); self.activateWindow(); self.search_open_documents()
        def search_open_documents(self):
            if self.worker and self.worker.isRunning(): return
            self.input.setEnabled(False); self.info.setText("Finding PDFs open in Acrobat…")
            self.worker=Worker(lambda _cancel,_progress:self.acrobat.open_documents())
            self.worker.done.connect(self.open_documents_ready); self.worker.failed.connect(self.index_failed); self.worker.start()
        def open_documents_ready(self,paths):
            if not paths:
                self.input.setEnabled(True); self.info.setText("No open PDFs were found in Acrobat."); return
            self.info.setText(f"Preparing {len(paths)} open PDF(s)…")
            def prepare(cancel,progress):
                ready=[]; failures=[]
                for i,path in enumerate(paths,1):
                    if cancel(): raise InterruptedError("Indexing cancelled.")
                    try:
                        status=self.service.store.status(path)
                        if not status or status["stale"]:
                            self.service.index(path,lambda phase,current,total:progress(f"{Path(path).name}: {phase}",current,total),cancel,semantic=True)
                        ready.append(path)
                    except Exception as exc:
                        failures.append(f"{Path(path).name}: {exc}")
                    progress("Preparing open PDFs",i,len(paths))
                if not ready: raise RuntimeError("No open PDF could be prepared for searching. " + " ".join(failures))
                return ready,failures
            self.worker=Worker(prepare); self.worker.progress.connect(self.on_progress); self.worker.done.connect(self.open_documents_prepared); self.worker.failed.connect(self.index_failed); self.worker.start()
        def open_documents_prepared(self,result):
            self.open_paths,failures=result; self.path=None
            self.input.setEnabled(True); self.input.clear(); self.list.clear(); self.input.setFocus()
            detail=f"Ready to search {len(self.open_paths)} open PDF(s). Enter a query."
            if failures: detail+=f" {len(failures)} PDF(s) could not be indexed."
            self.info.setText(detail)
        def choose_pdf(self):
            file,_=QFileDialog.getOpenFileName(self,"Choose a PDF to index or search","","PDF files (*.pdf)")
            if file:
                self.path=file; self.open_paths=[file]; status=self.service.store.status(file)
                if not status or status["stale"]: self.start_index(file)
                else:
                    self.title.setText(Path(file).name); self.input.setEnabled(True); self.input.clear(); self.list.clear(); self.info.setText("Enter a search query."); self.input.setFocus()
        def manage_indexes(self):
            dialog=QDialog(self); dialog.setWindowTitle("Manage saved PDF indexes"); dialog.setMinimumSize(600,360)
            layout=QVBoxLayout(dialog); entries=QListWidget(); records=self.service.store.list_documents()
            for record in records:
                item=QListWidgetItem(f"{record['name']}  —  {record['page_count']} pages\n{record['path']}")
                item.setData(Qt.UserRole,record['path']); entries.addItem(item)
            layout.addWidget(QLabel("Saved indexes stay on this computer until you remove them or uninstall the app."))
            layout.addWidget(entries)
            buttons=QHBoxLayout(); delete=QPushButton("Delete selected index"); delete_all=QPushButton("Delete all saved indexes"); done=QPushButton("Close"); buttons.addWidget(delete); buttons.addWidget(delete_all); buttons.addStretch(1); buttons.addWidget(done); layout.addLayout(buttons)
            def remove_selected():
                item=entries.currentItem()
                if not item: return
                path=item.data(Qt.UserRole)
                if QMessageBox.question(dialog,"Delete saved index",f"Remove the saved index for {Path(path).name}? The PDF itself will not be changed.") != QMessageBox.Yes: return
                self.service.store.delete_document(path); entries.takeItem(entries.row(item))
                if path in self.open_paths: self.open_paths.remove(path)
                if self.path==path: self.path=None
            def remove_all():
                if not records: return
                if QMessageBox.question(dialog,"Delete all saved indexes",f"Remove all {len(records)} saved PDF indexes? The PDF files will not be changed.") != QMessageBox.Yes: return
                for record in records: self.service.store.delete_document(record["path"])
                self.open_paths=[]; self.path=None; entries.clear()
            delete.clicked.connect(remove_selected); delete_all.clicked.connect(remove_all); done.clicked.connect(dialog.accept); dialog.exec()
        def active_ready(self,path):
            try:
                self.path=path; status=self.service.store.status(self.path)
                self.title.setText(Path(self.path).name)
                if not status or status["stale"]:
                    text="This PDF is not indexed." if not status else "This PDF changed. Reindex before searching."
                    answer=QMessageBox.question(self,"Index PDF",text+"\nIndex it now?")
                    if answer != QMessageBox.Yes:
                        self.input.setEnabled(True); self.info.setText(text); return
                    self.start_index(self.path)
                else:
                    self.info.setText("Enter a search query."); self.input.setEnabled(True); self.input.clear(); self.list.clear(); self.input.setFocus()
            except Exception as exc: self.input.setEnabled(True); self.show_error(str(exc))
        def start_index(self,path):
            self.path=path; self.title.setText(Path(path).name); self.input.clear(); self.list.clear()
            self.show(); self.raise_(); self.activateWindow(); self.info.setText("Starting local indexing…")
            self.progress.setRange(0,0); self.progress.show(); self.cancel.show(); self.input.setEnabled(False)
            self.worker=Worker(lambda cancel, progress:self.service.index(path,lambda a,b,c:progress(a,b,c),cancel,semantic=True))
            self.worker.progress.connect(self.on_progress); self.worker.done.connect(self.index_ready); self.worker.failed.connect(self.index_failed); self.worker.start()
        def search(self):
            if not self.path and not self.open_paths: return
            query=self.input.text().strip()
            if not query: return
            if self.worker and self.worker.isRunning(): return
            self.info.setText("Searching locally…")
            paths=list(self.open_paths)
            def search_all(_cancel,_progress):
                hits=[]; errors=[]
                for path in paths:
                    try:
                        for hit in self.service.search(path,query,5):
                            hit["path"]=path; hit["filename"]=Path(path).name; hits.append(hit)
                    except Exception as exc: errors.append(f"{Path(path).name}: {exc}")
                return {"hits":sorted(hits,key=lambda hit:hit["score"],reverse=True)[:20],"errors":errors}
            self.worker=Worker(search_all)
            self.worker.done.connect(self.show_hits); self.worker.failed.connect(self.show_error); self.worker.start()
        def show_hits(self,result):
            hits=result.get("hits",[]) if isinstance(result,dict) else result
            self.list.clear()
            for hit in hits:
                excerpt=" ".join(hit["text"].split())
                item=QListWidgetItem(f"{hit.get('filename', Path(hit.get('path', self.path or '')).name)} — p. {hit['page']}  ·  {excerpt[:220]}"); item.setData(Qt.UserRole,hit); self.list.addItem(item)
            errors=result.get("errors",[]) if isinstance(result,dict) else []
            self.info.setText(f"{len(hits)} result(s)" + (f"; {len(errors)} PDF(s) could not be searched." if errors else ""))
            if hits:
                self.list.setCurrentRow(0); self.list.setFocus()
        def move_result(self,direction):
            if not self.list.count(): return
            row=self.list.currentRow()
            if row < 0: row=0
            self.list.setCurrentRow(max(0,min(self.list.count()-1,row+direction))); self.list.setFocus()
        def open_result(self,item):
            hit=item.data(Qt.UserRole)
            self.info.setText("Opening result in Acrobat…")
            self.worker=Worker(lambda _cancel,_progress:self.acrobat.goto_page(hit.get("path",self.path),hit["page"]))
            self.worker.done.connect(lambda _result:self.hide()); self.worker.failed.connect(self.show_error); self.worker.start()
        def show_error(self,message): self.info.setText(message)
        def on_progress(self,phase,current,total):
            self.info.setText(f"{phase}: {current} / {total}")
            if total:
                self.progress.setRange(0,total); self.progress.setValue(current)
        def index_ready(self,result):
            self.progress.hide(); self.cancel.hide(); self.input.setEnabled(True)
            mode="Semantic + keyword search ready." if result.get("semantic") else "Keyword search ready; semantic ranking is unavailable."
            if result.get("warning"): mode += " " + result["warning"]
            self.info.setText(mode)
        def index_failed(self,message):
            self.progress.hide(); self.cancel.hide(); self.input.setEnabled(True); self.show_error(message)
        def cancel_index(self):
            if self.worker:
                self.worker.cancelled=True; self.info.setText("Cancelling after the current page…")
        def closeEvent(self,event):
            if self.worker and self.worker.isRunning():
                self.worker.cancelled=True
                self.worker.wait()
            event.accept()

    app=QApplication(sys.argv); app.setQuitOnLastWindowClosed(True)
    icon_path = settings_root / "assets" / "acrobat-semantic-search.ico"
    if icon_path.is_file():
        app.setWindowIcon(QIcon(str(icon_path)))
    service=SearchService(); acrobat=AcrobatController(); overlay=Overlay(service,acrobat)
    bridge=Bridge(); bridge.open_overlay.connect(overlay.show_for_active, Qt.QueuedConnection)
    hotkey=GlobalHotkey(lambda:bridge.open_overlay.emit()); hotkey.start()
    overlay.show()
    QTimer.singleShot(750, lambda: QMessageBox.information(overlay,"Shortcut unavailable",hotkey.error) if hotkey.error else None)
    app.aboutToQuit.connect(hotkey.stop)
    app.aboutToQuit.connect(acrobat.close)
    app.aboutToQuit.connect(service.store.close)
    return app.exec()


def _desktop() -> int:
    """Two-tab desktop workflow with open-PDF processing and tray lifecycle."""
    try:
        from PySide6.QtCore import QObject, QThread, Signal, Qt, QTimer
        from PySide6.QtGui import QAction, QIcon
        from PySide6.QtWidgets import (
            QApplication, QDialog, QFileDialog, QFrame, QHBoxLayout, QLabel,
            QLineEdit, QListWidget, QListWidgetItem, QMenu, QMessageBox,
            QProgressBar, QPushButton, QTabWidget, QSystemTrayIcon, QVBoxLayout,
        )
        from integration.acrobat import AcrobatController, GlobalHotkey
    except ImportError as exc:
        print(f"The desktop app dependencies are incomplete: {exc}. Reinstall Acrobat Semantic Search.", file=sys.stderr)
        return 2

    class Worker(QThread):
        done = Signal(object)
        failed = Signal(str)
        progress = Signal(str, int, int)
        def __init__(self, operation):
            super().__init__(); self.operation=operation; self.cancelled=False
        def run(self):
            try: self.done.emit(self.operation(lambda: self.cancelled, self.progress.emit))
            except Exception as exc:
                logger.exception("Background operation failed")
                self.failed.emit(str(exc))

    class HotkeyBridge(QObject):
        show_requested=Signal()

    app=QApplication(sys.argv)
    icon_path=settings_root / "assets" / "acrobat-semantic-search.ico"
    app_icon=QIcon(str(icon_path)) if icon_path.is_file() else QIcon()
    if app_icon.isNull():
        logger.error("Product icon could not be loaded from %s; tray icon may not render", icon_path)
    else:
        logger.info("Product icon loaded with %s size(s)", len(app_icon.availableSizes()))
    app.setWindowIcon(app_icon)
    # Never hide the only window unless Qt confirms that a notification area
    # exists. Treating every Windows host as tray-capable can strand the app:
    # close hides the window while Explorer has not accepted a tray icon.
    tray_available=QSystemTrayIcon.isSystemTrayAvailable()
    app.setQuitOnLastWindowClosed(not tray_available)
    service=SearchService(); acrobat=AcrobatController()

    class MainWindow(QDialog):
        def __init__(self):
            super().__init__(); self.service=service; self.acrobat=acrobat; self.worker=None; self.allow_quit=False; self.doc_items=[]; self.ready_paths=[]
            self.setWindowTitle("Acrobat Semantic Search"); self.setWindowIcon(app_icon); self.resize(970,700); self.setMinimumSize(800,590)
            self.setStyleSheet("""
              QDialog{background:#F1F5F6;color:#183544;font-family:'Segoe UI'}
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
              QProgressBar{height:10px;border:0;border-radius:5px;background:#DFE9EB}
              QProgressBar::chunk{background:#08A0A0;border-radius:5px}
            """)
            outer=QVBoxLayout(self); outer.setContentsMargins(20,18,20,16); outer.setSpacing(13)
            hero=QFrame(); hero.setObjectName("hero"); hl=QHBoxLayout(hero); hl.setContentsMargins(20,15,20,15)
            brandbox=QVBoxLayout(); brand=QLabel("Acrobat Semantic Search"); brand.setObjectName("brand")
            tagline=QLabel("Find the right passage. Jump to the page in Acrobat."); tagline.setObjectName("tagline")
            brandbox.addWidget(brand); brandbox.addWidget(tagline); hl.addLayout(brandbox); hl.addStretch(1)
            outer.addWidget(hero)
            self.tabs=QTabWidget(); outer.addWidget(self.tabs,1)
            self.pdf_tab=QFrame(); self.pdf_tab.setObjectName("panel"); pl=QVBoxLayout(self.pdf_tab); pl.setContentsMargins(16,15,16,15); pl.setSpacing(10)
            head=QHBoxLayout(); heading=QLabel("PDFs in Adobe"); heading.setObjectName("heading"); head.addWidget(heading); head.addStretch(1)
            self.add_pdfs=QPushButton("Add PDFs…"); self.add_pdfs.clicked.connect(self.add_pdf_files); head.addWidget(self.add_pdfs)
            self.refresh=QPushButton("Refresh list"); self.refresh.clicked.connect(self.refresh_pdfs); head.addWidget(self.refresh); pl.addLayout(head)
            self.pdf_hint=QLabel("Checking Adobe for open PDFs… You can also add files directly."); self.pdf_hint.setObjectName("muted"); self.pdf_hint.setWordWrap(True); pl.addWidget(self.pdf_hint)
            self.pdf_list=QListWidget(); self.pdf_list.setMinimumHeight(330); self.pdf_list.setWordWrap(True); self.pdf_list.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); pl.addWidget(self.pdf_list,1)
            actions=QHBoxLayout(); self.select_all=QPushButton("Select all"); self.select_all.clicked.connect(self.check_all); actions.addWidget(self.select_all)
            self.process=QPushButton("Process selected PDFs"); self.process.setObjectName("primary"); self.process.clicked.connect(self.process_selected); actions.addStretch(1); actions.addWidget(self.process); pl.addLayout(actions)
            self.tabs.addTab(self.pdf_tab,"PDFs")
            self.search_tab=QFrame(); self.search_tab.setObjectName("panel"); sl=QVBoxLayout(self.search_tab); sl.setContentsMargins(16,15,16,15); sl.setSpacing(10)
            s_title=QLabel("Search processed PDFs"); s_title.setObjectName("heading"); sl.addWidget(s_title)
            self.scope=QLabel("Search includes processed PDFs and saved indexes from previous sessions."); self.scope.setObjectName("muted"); self.scope.setWordWrap(True); sl.addWidget(self.scope)
            queryrow=QHBoxLayout(); self.query=QLineEdit(); self.query.setPlaceholderText("Search in Hebrew or English…"); self.query.setEnabled(False); queryrow.addWidget(self.query,1)
            self.search_button=QPushButton("Search"); self.search_button.setObjectName("primary"); self.search_button.setEnabled(False); self.search_button.clicked.connect(self.search); queryrow.addWidget(self.search_button); sl.addLayout(queryrow)
            self.results=QListWidget(); self.results.setMinimumHeight(360); self.results.setWordWrap(True); self.results.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff); self.results.itemClicked.connect(self.open_result); sl.addWidget(self.results,1)
            self.tabs.addTab(self.search_tab,"Search")
            self.query.returnPressed.connect(self.search)
            footer=QHBoxLayout(); self.status=QLabel("Ready"); self.status.setObjectName("muted"); self.status.setWordWrap(True); footer.addWidget(self.status,1)
            self.progress=QProgressBar(); self.progress.setFixedWidth(190); self.progress.hide(); footer.addWidget(self.progress)
            self.cancel=QPushButton("Cancel"); self.cancel.hide(); self.cancel.clicked.connect(self.cancel_work); footer.addWidget(self.cancel); outer.addLayout(footer)
            self.refresh_pdfs()

        def refresh_pdfs(self):
            if self.worker and self.worker.isRunning(): return
            self.pdf_list.clear(); self.doc_items=[]
            self.ready_paths=[row["path"] for row in service.store.list_documents()
                              if (service.store.status(row["path"]) or {}).get("stale") is False]
            self.search_button.setEnabled(bool(self.ready_paths)); self.query.setEnabled(bool(self.ready_paths))
            self.pdf_hint.setText("Looking for open PDFs in Acrobat…"); self.status.setText("Checking Acrobat…"); self.refresh.setEnabled(False); self.process.setEnabled(False)
            def scan(_cancel,_progress):
                try:
                    found=self.acrobat.open_documents()
                    return [(path,self.service_status(path)) for path in found], None
                except Exception as exc:
                    # Free Adobe Reader does not expose Acrobat's COM document
                    # collection. Its visible window title identifies the
                    # selected tab; match that against saved index paths so the
                    # user can still see a useful open/processed state.
                    from integration.reader_dde import visible_reader_titles, match_reader_titles_to_paths
                    known=[row["path"] for row in service.store.list_documents()]
                    found=match_reader_titles_to_paths(visible_reader_titles(), known)
                    if found:
                        return [(path,self.service_status(path)) for path in found], (
                            "Reader only exposes the selected PDF tab here. Showing indexed PDFs matched to its window title; "
                            "use Add PDFs for other open tabs."
                        )
                    raise exc
            self.worker=Worker(scan); self.worker.done.connect(self.show_open_pdfs); self.worker.failed.connect(self.pdf_scan_failed); self.worker.start()

        def service_status(self,path): return service.store.status(path)

        def show_open_pdfs(self,result):
            documents,scan_note=result
            self.refresh.setEnabled(True); self.pdf_list.clear(); self.doc_items=[]
            if not documents:
                self.pdf_hint.setText("No open PDFs were detected. Choose Add PDFs to select files. Search results can still open in Reader or Acrobat at the matching page.")
                self.status.setText("Add PDFs to process, or search saved indexes."); self.process.setEnabled(False); return
            for path,status in documents:
                if status and status.get("stale"): state="Changed · process again"; icon="↻"
                elif status: state="Processed · ready to search"; icon="✓"
                else: state="Not processed yet"; icon="＋"
                item=QListWidgetItem(f"{icon}  {Path(path).name}\n     {state}")
                item.setData(Qt.UserRole,path); item.setData(Qt.UserRole+1,status); item.setToolTip(path)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked); self.pdf_list.addItem(item); self.doc_items.append(item)
            pending=sum(1 for _,s in documents if not s or s.get("stale"))
            hint=f"Found {len(documents)} open PDF(s). {pending} need processing. Select the files you want available in Search."
            if scan_note: hint=scan_note
            self.pdf_hint.setText(hint)
            self.status.setText("Select PDFs, then process them. Already processed files are kept between app sessions."); self.process.setEnabled(True)

        def add_pdf_files(self):
            files,_=QFileDialog.getOpenFileNames(self,"Add PDFs to search","","PDF files (*.pdf)")
            if not files: return
            existing={os.path.normcase(os.path.abspath(i.data(Qt.UserRole))) for i in self.doc_items}
            added=0
            for path in files:
                path=os.path.normcase(os.path.abspath(path))
                if path in existing: continue
                status=service.store.status(path)
                if status and not status.get("stale"):
                    state="Processed · ready to search"; icon="✓"
                elif status:
                    state="Changed · process again"; icon="↻"
                else:
                    state="Not processed yet"; icon="＋"
                item=QListWidgetItem(f"{icon}  {Path(path).name}\n     {state}")
                item.setData(Qt.UserRole,path); item.setData(Qt.UserRole+1,status); item.setToolTip(path)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked)
                self.pdf_list.addItem(item); self.doc_items.append(item); existing.add(path); added+=1
                if status and not status.get("stale") and path not in self.ready_paths:
                    self.ready_paths.append(path)
            self.process.setEnabled(bool(self.doc_items)); self.search_button.setEnabled(bool(self.ready_paths)); self.query.setEnabled(bool(self.ready_paths))
            self.pdf_hint.setText(f"Added {added} PDF(s). Select files to process; saved files remain available in Search.")
            self.status.setText(f"{len(self.ready_paths)} processed PDF(s) available to search.")

        def pdf_scan_failed(self,message):
            self.refresh.setEnabled(True); self.process.setEnabled(bool(self.doc_items))
            self.pdf_hint.setText("Adobe could not list its open PDFs. Choose Add PDFs to select files manually; searches and page jumps still work in Reader.")
            self.status.setText(message)

        def check_all(self):
            checked=all(item.checkState()==Qt.Checked for item in self.doc_items)
            for item in self.doc_items: item.setCheckState(Qt.Unchecked if checked else Qt.Checked)

        def process_selected(self):
            paths=[i.data(Qt.UserRole) for i in self.doc_items if i.checkState()==Qt.Checked]
            if not paths: self.status.setText("Select at least one PDF first."); return
            self.process.setEnabled(False); self.refresh.setEnabled(False); self.cancel.show(); self.progress.setRange(0,0); self.progress.show(); self.status.setText("Processing selected PDFs locally…")
            def run(cancel,progress):
                ready=[]; failures=[]
                for i,path in enumerate(paths,1):
                    if cancel(): raise InterruptedError("Processing cancelled.")
                    current=service.store.status(path)
                    if not current or current.get("stale"):
                        try: service.index(path,lambda phase,a,b:progress(f"{Path(path).name}: {phase}",a,b),cancel,semantic=True)
                        except Exception as exc: failures.append((path,str(exc))); continue
                    ready.append(path); progress("Checking selected PDFs",i,len(paths))
                return ready,failures
            self.worker=Worker(run); self.worker.progress.connect(self.update_progress); self.worker.done.connect(self.process_done); self.worker.failed.connect(self.work_failed); self.worker.start()

        def process_done(self,result):
            processed,failures=result; self.ready_paths=list(dict.fromkeys(self.ready_paths+processed)); self.finish_work()
            for item in self.doc_items:
                path=item.data(Qt.UserRole)
                if path in self.ready_paths:
                    item.setData(Qt.UserRole+1,service.store.status(path))
                    item.setText(f"✓  {Path(path).name}\n     Processed · ready to search")
                elif any(f[0]==path for f in failures):
                    detail=next(f[1] for f in failures if f[0]==path)
                    item.setText(f"!  {Path(path).name}\n     Could not process: {detail}")
            self.query.setEnabled(bool(self.ready_paths)); self.search_button.setEnabled(bool(self.ready_paths)); self.scope.setText(f"Search will look through {len(self.ready_paths)} saved, processed PDF(s).")
            self.status.setText(f"Ready to search {len(self.ready_paths)} PDF(s)." + (f" {len(failures)} could not be processed." if failures else ""))
            if self.ready_paths: self.tabs.setCurrentWidget(self.search_tab); self.query.setFocus()

        def search(self):
            q=self.query.text().strip()
            if not q or not self.ready_paths or (self.worker and self.worker.isRunning()): return
            paths=list(self.ready_paths); self.search_button.setEnabled(False); self.status.setText("Searching locally…")
            def run(_cancel,_progress):
                hits=[]; failures=[]
                for path in paths:
                    try:
                        for hit in service.search(path,q,8): hit["path"]=path; hit["filename"]=Path(path).name; hits.append(hit)
                    except Exception as exc: failures.append(f"{Path(path).name}: {exc}")
                hits.sort(key=lambda h:h.get("score",0),reverse=True)
                return hits[:30],failures
            self.worker=Worker(run); self.worker.done.connect(self.show_results); self.worker.failed.connect(self.work_failed); self.worker.start()

        def show_results(self,result):
            hits,failures=result; self.results.clear(); self.search_button.setEnabled(bool(self.ready_paths))
            for hit in hits:
                excerpt=" ".join(hit["text"].split())
                item=QListWidgetItem(f"{hit['filename']}  ·  Page {hit['page']}\n{excerpt[:320]}")
                item.setData(Qt.UserRole,hit); item.setToolTip("Click to open this PDF at the matching page in Acrobat."); self.results.addItem(item)
            self.status.setText(f"Found {len(hits)} result(s). Click a result to jump to that page." + (f" {len(failures)} file(s) skipped." if failures else ""))

        def open_result(self,item):
            hit=item.data(Qt.UserRole); self.status.setText(f"Opening {hit['filename']} at page {hit['page']}…"); self.search_button.setEnabled(False)
            self.worker=Worker(lambda _cancel,_progress:acrobat.goto_page(hit["path"],hit["page"]))
            def navigated(_result):
                self.status.setText(f"Opened {hit['filename']} at page {hit['page']}.")
                self.search_button.setEnabled(True)
                self.hide()
            self.worker.done.connect(navigated)
            self.worker.failed.connect(self.work_failed); self.worker.start()

        def update_progress(self,phase,current,total):
            self.status.setText(f"{phase}  ·  {current}/{total}" if total else phase)
            if total: self.progress.setRange(0,total); self.progress.setValue(current)

        def finish_work(self):
            self.progress.hide(); self.cancel.hide(); self.process.setEnabled(True); self.refresh.setEnabled(True); self.search_button.setEnabled(bool(self.ready_paths))

        def work_failed(self,message):
            self.finish_work(); self.status.setText(message); self.search_button.setEnabled(bool(self.ready_paths))

        def cancel_work(self):
            if self.worker and self.worker.isRunning(): self.worker.cancelled=True; self.status.setText("Cancelling after the current page…")

        def manage_indexes(self):
            dialog=QDialog(self); dialog.setWindowTitle("Saved indexes"); dialog.resize(650,400); layout=QVBoxLayout(dialog)
            entries=QListWidget(); records=service.store.list_documents()
            for row in records:
                it=QListWidgetItem(f"{row['name']} · {row['page_count']} pages\n{row['path']}"); it.setData(Qt.UserRole,row['path']); entries.addItem(it)
            layout.addWidget(QLabel("Indexes stay on this computer until deleted here or the app is uninstalled.")); layout.addWidget(entries)
            buttons=QHBoxLayout(); delete=QPushButton("Delete selected"); delete_all=QPushButton("Delete all"); done=QPushButton("Done")
            buttons.addWidget(delete); buttons.addWidget(delete_all); buttons.addStretch(1); buttons.addWidget(done); layout.addLayout(buttons)
            def remove_selected():
                it=entries.currentItem()
                if not it: return
                path=it.data(Qt.UserRole)
                if QMessageBox.question(dialog,"Delete saved index",f"Delete the index for {Path(path).name}? The PDF file will stay untouched.")!=QMessageBox.Yes:return
                service.store.delete_document(path); entries.takeItem(entries.row(it))
                self.ready_paths=[p for p in self.ready_paths if p!=path]; self.search_button.setEnabled(bool(self.ready_paths)); self.query.setEnabled(bool(self.ready_paths))
            def remove_all():
                if not records:return
                if QMessageBox.question(dialog,"Delete all indexes",f"Delete all {len(records)} saved indexes? PDF files will stay untouched.")!=QMessageBox.Yes:return
                for row in records: service.store.delete_document(row['path'])
                self.ready_paths=[]; self.search_button.setEnabled(False); self.query.setEnabled(False); self.results.clear(); entries.clear()
            delete.clicked.connect(remove_selected); delete_all.clicked.connect(remove_all); done.clicked.connect(dialog.accept); dialog.exec()

        def closeEvent(self,event):
            if tray_available and not self.allow_quit:
                self.hide(); event.ignore(); return
            if self.worker and self.worker.isRunning(): self.worker.cancelled=True; self.worker.wait()
            event.accept()
            if not tray_available: QApplication.quit()

    window=MainWindow(); tray=QSystemTrayIcon(app_icon,app); tray.setToolTip("Acrobat Semantic Search")
    menu=QMenu(); show_action=QAction("Open Acrobat Semantic Search",menu); quit_action=QAction("Quit",menu)
    show_action.triggered.connect(lambda:(window.show(),window.raise_(),window.activateWindow()))
    hotkey_bridge=HotkeyBridge()
    def show_from_hotkey():
        window.show(); window.raise_(); window.activateWindow()
    hotkey_bridge.show_requested.connect(show_from_hotkey, Qt.QueuedConnection)
    hotkey=GlobalHotkey(lambda:hotkey_bridge.show_requested.emit())
    hotkey.start()
    def quit_app():
        window.allow_quit=True
        window.close()
        tray.hide()
        app.quit()
    quit_action.triggered.connect(quit_app); menu.addAction(show_action); menu.addSeparator(); menu.addAction(quit_action); tray.setContextMenu(menu)
    tray.activated.connect(lambda reason: (window.show(),window.raise_(),window.activateWindow()) if reason in (QSystemTrayIcon.DoubleClick,QSystemTrayIcon.Trigger) else None)
    if tray_available:
        tray.show()
        if tray.isVisible():
            logger.info("System tray icon registered")
        else:
            logger.error("Qt reported a system tray but did not show the application icon")
            tray_available=False
            app.setQuitOnLastWindowClosed(True)
            window.status.setText("Windows did not accept the tray icon. Closing this window will exit the app.")
    else:
        logger.warning("No system tray is available; the app will exit when its window closes")
        window.status.setText("System tray is unavailable; closing the window will exit the app.")
    window.show()
    QTimer.singleShot(1500, lambda: window.status.setText(hotkey.error) if hotkey.error else None)
    app.aboutToQuit.connect(hotkey.stop); app.aboutToQuit.connect(tray.hide); app.aboutToQuit.connect(acrobat.close); app.aboutToQuit.connect(service.store.close)
    return app.exec()


def main() -> int:
    parser=argparse.ArgumentParser(description="Local PDF search for Adobe Acrobat")
    parser.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    sub=parser.add_subparsers(dest="command")
    ix=sub.add_parser("index",help="Index a PDF locally"); ix.add_argument("path"); ix.add_argument("--lexical-only",action="store_true")
    se=sub.add_parser("search",help="Search an indexed PDF"); se.add_argument("path"); se.add_argument("query"); se.add_argument("--limit",type=int,default=5)
    st=sub.add_parser("status",help="Show index status"); st.add_argument("path")
    de=sub.add_parser("delete",help="Delete a PDF index"); de.add_argument("path")
    args=parser.parse_args()
    if args.uninstall:
        from integration.uninstall import schedule_uninstall
        return schedule_uninstall()
    if not args.command:
        try:
            return _desktop()
        except Exception as exc:
            logger.exception("Desktop application failed during startup")
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                QMessageBox.critical(None, "Acrobat Semantic Search could not start",
                    f"The app failed during startup.\n\n{exc}\n\nSee data\\logs\\app.log in the application folder.")
            except Exception:
                pass
            return 1
    service=SearchService()
    try:
        if args.command=="index": result=service.index(args.path,semantic=not args.lexical_only)
        elif args.command=="search": result=service.search(args.path,args.query,args.limit)
        elif args.command=="status": result=service.store.status(args.path)
        else: service.store.delete_document(args.path); result={"deleted":True}
        print(json.dumps(result,ensure_ascii=False,indent=2,default=str)); return 0
    except Exception as exc:
        print(str(exc),file=sys.stderr); return 1
    finally: service.store.close()


if __name__=="__main__": raise SystemExit(main())

