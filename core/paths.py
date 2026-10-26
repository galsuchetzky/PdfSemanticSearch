"""Per-platform locations for indexes, the embedding model cache, and logs."""
from __future__ import annotations
import os
from pathlib import Path
import sys
import unicodedata

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

# The Windows installer, registry entry, and data folder already use the Acrobat
# name; macOS works with any viewer, so it uses the neutral product name.
APP_NAME = "Acrobat Semantic Search" if IS_WINDOWS else "PDF Semantic Search"
_WINDOWS_DIR_NAME = "AcrobatSemanticSearch"


def data_dir() -> Path:
    override = os.environ.get("ACROBAT_SEMANTIC_DATA_DIR")
    if override:
        return Path(override).expanduser()
    if IS_WINDOWS:
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) / _WINDOWS_DIR_NAME if local else Path.home() / ".acrobat_semantic_search"
    if IS_MACOS:
        return Path.home() / "Library" / "Application Support" / APP_NAME
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg) if xdg else Path.home() / ".local" / "share") / "pdf-semantic-search"


def model_dir() -> Path:
    return Path(os.environ.get("FASTEMBED_CACHE_PATH", str(data_dir() / "models")))


def log_dir(app_root: Path) -> Path:
    override = os.environ.get("ACROBAT_SEMANTIC_DATA_DIR")
    if override:
        return Path(override).expanduser() / "logs"
    if IS_WINDOWS:
        return app_root / "data" / "logs"
    if IS_MACOS:
        # The app bundle is read-only once signed; logs go where Console.app looks.
        return Path.home() / "Library" / "Logs" / APP_NAME
    return data_dir() / "logs"


def normalize_pdf_path(path: str | Path) -> str:
    """Canonical form used for index identity and open-document matching.

    macOS scripting can return decomposed (NFD) Unicode while file dialogs return
    composed text, so normalize to NFC there. Windows keeps its original form
    (normcase only) so existing Windows index IDs stay valid.
    """
    resolved = str(Path(path).expanduser().resolve())
    if not IS_WINDOWS:
        resolved = unicodedata.normalize("NFC", resolved)
    return os.path.normcase(resolved)
