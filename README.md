# PDF Semantic Search

Local-first semantic search for PDFs on **Windows and macOS**, in Hebrew and English. Add PDFs, process them once, then search across all of them by meaning and by keyword. Nothing leaves the computer: text extraction, OCR, embeddings, and the index all run locally.

On Windows the app is distributed as **Acrobat Semantic Search**; on macOS as **PDF Semantic Search**. Both run the same code.

## What always works (no permissions needed)

- **Add PDFs…** or drag PDFs onto the window, then **Process selected PDFs**.
- Search every processed PDF at once, by meaning and by exact terms, in Hebrew or English.
- For each result: file, page, and excerpt, plus **Copy reference** and **Show in Finder / Explorer**.
- **Saved indexes…** lists indexes kept between sessions; delete one, all, or the downloaded model.

## Optional viewer integration

When available, the app also lists PDFs that are already open and opens a result at its page. If a viewer is missing or a permission is declined, the app quietly falls back to manual mode.

| | Find open PDFs | Open a result at its page |
|---|---|---|
| **Windows · Adobe Acrobat** | All open documents (COM) | Yes |
| **Windows · Adobe Reader** | Selected tab only, matched to saved indexes | Yes (DDE / command line) |
| **macOS · Skim** | Yes (Automation permission) | Yes (Automation permission) |
| **macOS · Preview** | Yes (Automation permission) | Yes, with Accessibility permission (uses Go ▸ Go to Page…) |
| **macOS · any other PDF app** | Yes (Accessibility permission) | No |

On Windows integration is on by default (it needs no prompts). On macOS it is **off until you switch it on** under **Viewer integration…**; only then does macOS ask for Automation / Accessibility permission. Declining keeps the app fully usable.

## Global shortcut

Ctrl+Alt+F on Windows, ⌘⌥F on macOS brings the window forward. Closing the window keeps the app in the system tray / menu bar; choose **Quit** there (or ⌘Q) to exit.

## Local development

Python 3.12 is recommended (the Windows build requires it).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt        # Windows: .venv\Scripts\pip
.venv/bin/python main.py                         # desktop app
.venv/bin/python main.py index some.pdf          # CLI: index, search, status, delete
.venv/bin/python main.py viewers                 # viewer integration status and open PDFs
.venv/bin/python -m unittest discover -s tests -t .
```

OCR for image-only pages needs Tesseract with Hebrew and English data: `brew install tesseract tesseract-lang` on macOS, or the UB Mannheim installer on Windows. The embedding model (about 220 MB) downloads on first use.

Data locations: `%LOCALAPPDATA%\AcrobatSemanticSearch` (or the folder chosen in the Windows installer) and `~/Library/Application Support/PDF Semantic Search` on macOS. Set `ACROBAT_SEMANTIC_DATA_DIR` to override.

## Build the Windows installer

From PowerShell in this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\install\windows\build.ps1
```

The distributable is `dist\AcrobatSemanticSearchSetup.exe`. Setup installs for the current Windows account without administrator rights, creates Start Menu (and optionally desktop) shortcuts, and registers the app in **Settings → Apps → Installed apps**. Uninstall removes the app and its managed data folder; PDF files are never copied or modified.

## Build the macOS app

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements-build.txt
install/macos/build_app.sh
```

This produces `dist/PDF Semantic Search.app` and `dist/PDF-Semantic-Search-1.0.0.dmg`. The app is ad-hoc signed, which is fine on your own Mac (first launch: right-click → Open). To share it, set `CODESIGN_IDENTITY` to a Developer ID certificate and notarize the DMG. To uninstall, drag the app to the Trash; use **Saved indexes… → Delete all / Delete downloaded model** first to remove its data, or delete the data folder above.

## Code layout

- `core/`: platform-independent extraction, page-aware chunking, SQLite FTS5 + vector index, hybrid ranking, and per-platform data paths.
- `viewers/`: optional viewer integrations behind one interface (`base.py`, `registry.py`), with `windows/` (Acrobat COM, Reader DDE) and `macos/` (Skim, Preview, Accessibility) adapters.
- `app/`: the Qt desktop window, preferences, global shortcut, single-instance handling, and the Windows uninstaller.
- `install/`: icon generation, `windows/` installer build, `macos/` app + DMG build.

See `SOLUTION_SPEC.md` for the original design.
