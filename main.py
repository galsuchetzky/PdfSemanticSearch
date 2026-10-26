"""Local semantic PDF search for Windows and macOS. Run with --help for CLI commands."""
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

from core.paths import APP_NAME, log_dir

_log_dir = log_dir(settings_root)
try:
    _log_dir.mkdir(parents=True, exist_ok=True)
    _handler = RotatingFileHandler(_log_dir / "app.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[_handler], format="%(asctime)s %(levelname)s %(name)s: %(message)s")
except OSError:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("acrobat_semantic_search")
logger.info("Application starting; executable=%s", sys.executable)

from core.service import SearchService


def _assets_dir() -> Path:
    candidates = []
    if hasattr(sys, "_MEIPASS"):  # PyInstaller (macOS .app)
        candidates.append(Path(sys._MEIPASS) / "assets")
    candidates += [settings_root / "assets", Path(__file__).resolve().parent / "install" / "assets"]
    return next((c for c in candidates if c.is_dir()), candidates[-1])


def _desktop() -> int:
    try:
        from app.main_window import run_desktop
    except ImportError as exc:
        print(f"The desktop app dependencies are incomplete: {exc}. Install requirements.txt.", file=sys.stderr)
        return 2
    return run_desktop(_assets_dir(), sys.argv)


def _viewers(enable: bool) -> dict:
    from app.preferences import Preferences
    from viewers.registry import default_registry
    from core.index_store import IndexStore
    registry = default_registry(enable or Preferences().viewer_integration)
    store = IndexStore()
    try:
        scan = registry.scan([row["path"] for row in store.list_documents()])
        return {
            "integration_enabled": registry.integration_enabled,
            "viewers": [{"name": name, "status": state.status.value, "detail": state.detail} for name, state in registry.states()],
            "can_open_pages": registry.can_navigate(),
            "open_pdfs": [{"path": doc.path, "viewer": doc.viewer} for doc in scan.documents],
            "notes": scan.notes,
        }
    finally:
        registry.close(); store.close()


def main() -> int:
    parser=argparse.ArgumentParser(description=f"{APP_NAME}: local semantic PDF search")
    parser.add_argument("--uninstall", action="store_true", help=argparse.SUPPRESS)
    sub=parser.add_subparsers(dest="command")
    ix=sub.add_parser("index",help="Index a PDF locally"); ix.add_argument("path"); ix.add_argument("--lexical-only",action="store_true")
    se=sub.add_parser("search",help="Search an indexed PDF"); se.add_argument("path"); se.add_argument("query"); se.add_argument("--limit",type=int,default=5)
    st=sub.add_parser("status",help="Show index status"); st.add_argument("path")
    de=sub.add_parser("delete",help="Delete a PDF index"); de.add_argument("path")
    vw=sub.add_parser("viewers",help="Show PDF viewer integration status and open PDFs")
    vw.add_argument("--enable",action="store_true",help="Use viewer integration even if it is switched off (may show macOS permission prompts)")
    # Older macOS versions pass a process serial number when launched from Finder.
    args=parser.parse_args([a for a in sys.argv[1:] if not a.startswith("-psn_")])
    if args.uninstall:
        from app.uninstall_windows import schedule_uninstall
        return schedule_uninstall()
    if not args.command:
        try:
            return _desktop()
        except Exception as exc:
            logger.exception("Desktop application failed during startup")
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                QMessageBox.critical(None, f"{APP_NAME} could not start",
                    f"The app failed during startup.\n\n{exc}\n\nSee app.log in {_log_dir}.")
            except Exception:
                pass
            return 1
    if args.command=="viewers":
        print(json.dumps(_viewers(args.enable),ensure_ascii=False,indent=2)); return 0
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

