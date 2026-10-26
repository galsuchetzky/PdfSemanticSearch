"""Assemble a portable application folder using a private copy of the build Python."""
from __future__ import annotations
from importlib import metadata
from pathlib import Path
import os
import re
import shutil
import struct
import sys
import zipfile

from packaging.requirements import Requirement

PROJECT = Path(__file__).resolve().parents[2]
DIST = Path(os.environ.get("ACROBAT_APP_BUILD_DIR", PROJECT / "build" / "portable" / "AcrobatSemanticSearch")).resolve()
ICON = PROJECT / "install" / "assets" / "acrobat-semantic-search.ico"


def embed_windows_icon(executable: Path) -> None:
    """Replace a copied interpreter's PE icon resources with the product icon."""
    import win32api
    raw = ICON.read_bytes()
    reserved, kind, count = struct.unpack_from("<HHH", raw, 0)
    if reserved != 0 or kind != 1 or count == 0:
        raise RuntimeError("The application icon is not a valid Windows .ico file.")
    entries = []
    offset = 6
    for index in range(count):
        width, height, colors, entry_reserved, planes, bits, length, image_offset = struct.unpack_from("<BBBBHHII", raw, offset)
        image = raw[image_offset:image_offset + length]
        if len(image) != length:
            raise RuntimeError("The application icon file is incomplete.")
        resource_id = index + 1
        entries.append(struct.pack("<BBBBHHIH", width, height, colors, entry_reserved, planes, bits, length, resource_id))
        offset += 16
    group = struct.pack("<HHH", 0, 1, count) + b"".join(entries)
    update = win32api.BeginUpdateResource(str(executable), False)
    try:
        # Store each image, then replace the executable's main icon group.
        for index in range(count):
            entry_offset = 6 + index * 16
            width, height, colors, entry_reserved, planes, bits, length, image_offset = struct.unpack_from("<BBBBHHII", raw, entry_offset)
            win32api.UpdateResource(update, 3, index + 1, raw[image_offset:image_offset + length], 0)
        win32api.UpdateResource(update, 14, 1, group, 0)
        win32api.EndUpdateResource(update, False)
    except Exception:
        win32api.EndUpdateResource(update, True)
        raise


def keep_package_file(relative: Path) -> bool:
    parts = relative.parts
    posix = relative.as_posix()
    if any(part.endswith(".dist-info") for part in parts):
        return True
    if posix.startswith("PySide6/"):
        name = parts[-1]
        if posix in {
            "PySide6/__init__.py", "PySide6/_config.py", "PySide6/_git_pyside_version.py",
            "PySide6/py.typed", "PySide6/PySide6_Essentials.json",
        }:
            return True
        if posix.startswith("PySide6/support/") and name.endswith(".py"):
            return True
        if posix.startswith("PySide6/plugins/platforms/") and name in {"qwindows.dll", "qoffscreen.dll"}:
            return True
    if posix.startswith("PySide6/plugins/imageformats/") and name == "qico.dll":
        # QIcon loads the product's Windows .ico through Qt's ICO image plugin.
        # Without it QIcon silently becomes null, and Windows gets an invisible
        # notification-area icon even though QSystemTrayIcon reports visible.
        return True
        if posix.startswith("PySide6/") and name in {
            "QtCore.pyd", "QtGui.pyd", "QtWidgets.pyd", "QtNetwork.pyd",
            "Qt6Core.dll", "Qt6Gui.dll", "Qt6Widgets.dll", "Qt6Network.dll",
            "pyside6.abi3.dll", "MSVCP140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll",
            "VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "concrt140.dll",
        }:
            return True
        return False
    if posix.startswith("shiboken6/"):
        return parts[-1].endswith((".py", ".pyd", ".dll")) or posix == "shiboken6/py.typed"
    if posix.startswith("onnxruntime/"):
        return posix == "onnxruntime/__init__.py" or posix.startswith("onnxruntime/capi/")
    if posix.startswith("pythonwin/"):
        return False
    if parts[0] in {"win32com", "win32comext"} and any(x in {"test", "tests", "demo", "demos"} for x in parts):
        return False
    if parts[0] in {"numpy", "PIL", "pymupdf", "fitz"} and any(
        x.lower() in {"test", "tests", "testing", "doc", "docs", "benchmarks", "examples"} for x in parts[1:]
    ):
        return False
    if parts[0] == "Lib":
        return False
    return True


def distribution_closure() -> list[metadata.Distribution]:
    pending: list[Requirement] = []
    for raw in (PROJECT / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        requirement = Requirement(line)
        if requirement.marker and not requirement.marker.evaluate():
            continue
        pending.append(requirement)
    selected: dict[str, metadata.Distribution] = {}
    while pending:
        req = pending.pop()
        key = re.sub(r"[-_.]+", "-", req.name).lower()
        if key in selected:
            continue
        try:
            dist = metadata.distribution(req.name)
        except metadata.PackageNotFoundError as exc:
            raise RuntimeError(f"Build dependency is missing: {req.name}") from exc
        selected[key] = dist
        for child in dist.requires or []:
            dependency = Requirement(child)
            if dependency.marker and not dependency.marker.evaluate({"extra": ""}):
                continue
            pending.append(dependency)
    return list(selected.values())


def copy_runtime() -> None:
    base = Path(sys.base_prefix)
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError("Build the portable runtime with Python 3.12.")
    if DIST.exists():
        shutil.rmtree(DIST)
    DIST.mkdir(parents=True)
    # Copy the interpreter and standard library into the install folder; do not
    # install, alter, or rely on a target user's system-wide Python.
    for name in ("python.exe", "pythonw.exe", "python312.dll", "python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        source = base / name
        if source.is_file():
            shutil.copy2(source, DIST / name)
    library = base / "Lib"
    with zipfile.ZipFile(DIST / "python312.zip", "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for source in library.rglob("*"):
            relative = source.relative_to(library)
            if source.is_file() and "site-packages" not in relative.parts and not any(
                part in {"__pycache__", "test", "tests", "idlelib", "tkinter", "turtledemo", "ensurepip"} for part in relative.parts
            ) and source.suffix not in {".pyc", ".pyo"}:
                archive.write(source, relative.as_posix())
    source_dlls = base / "DLLs"
    if source_dlls.is_dir():
        shutil.copytree(source_dlls, DIST / "DLLs", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    site = DIST / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    copied: set[str] = set()
    for dist in distribution_closure():
        for relative in dist.files or []:
            rel = Path(str(relative))
            if rel.is_absolute() or ".." in rel.parts:
                continue
            if not keep_package_file(rel):
                continue
            key = str(rel).lower()
            if key in copied:
                continue
            source = Path(dist.locate_file(relative))
            if not source.is_file():
                continue
            destination = site / rel
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            copied.add(key)
    # Isolate Python's module search path to the app folder and its private libs.
    (DIST / "python312._pth").write_text(
        "python312.zip\n.\nDLLs\nsite-packages\nsite-packages\\win32\n"
        "site-packages\\win32\\lib\nsite-packages\\Pythonwin\nimport site\n", encoding="ascii")
    shutil.copy2(PROJECT / "main.py", DIST / "main.py")
    shutil.copytree(PROJECT / "core", DIST / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    for package in ("viewers", "app"):
        shutil.copytree(PROJECT / package, DIST / package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(PROJECT / "install" / "assets", DIST / "assets")
    (site / "sitecustomize.py").write_text(
        "from pathlib import Path\n"
        "import runpy, sys\n"
        "try:\n"
        "    import pywin32_bootstrap\n"
        "except ImportError:\n"
        "    pass\n"
        "if Path(sys.executable).name.lower() == 'acrobatsemanticsearch.exe' and len(sys.argv) <= 1:\n"
        "    runpy.run_path(str(Path(sys.executable).resolve().parent / 'main.py'), run_name='__main__')\n"
        "    raise SystemExit(0)\n",
        encoding="utf-8",
    )
    # The product-facing executable is a private pythonw runtime renamed for the app.
    shutil.copy2(DIST / "pythonw.exe", DIST / "AcrobatSemanticSearch.exe")
    embed_windows_icon(DIST / "AcrobatSemanticSearch.exe")


if __name__ == "__main__":
    copy_runtime()
    print(f"Assembled portable app in {DIST}")
