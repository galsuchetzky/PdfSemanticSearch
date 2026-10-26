# Acrobat Semantic Search

Local-first semantic search for PDFs opened in Adobe Acrobat. The Windows desktop app runs only while its window is open. Closing the window stops the app and any active indexing operation.

## Build the Windows installer

From PowerShell in this folder:

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-build.txt
.\install\build.ps1
```

The distributable is `dist\AcrobatSemanticSearchSetup.exe`. Send that file to the user. Setup lets them choose the application folder and data location, installs for the current Windows account without administrator rights, creates a Start Menu shortcut and optionally a desktop shortcut, and can launch the app when setup finishes. Data is stored in an `AcrobatSemanticSearch` subfolder under the chosen data location. The app is registered in **Settings → Apps → Installed apps** and can be removed there.

## App use

- **Search open Acrobat PDFs** finds every PDF currently open in Acrobat, indexes any missing or changed documents, and searches them together. Selecting a result focuses the matching PDF in Acrobat and moves to its page.
- **Choose a PDF** indexes or searches a PDF selected from a file picker.
- Select a result to navigate Acrobat to that page.
- Closing the window stops the app. Indexing is cancelled at a page boundary.

Closing the app preserves indexes and the embedding model in the chosen data folder. Use **Manage saved indexes** to delete an individual PDF index or clear all saved indexes. Uninstall removes the app's managed data folder, including indexes and model cache, and leaves system-wide software such as Python untouched. The installer packages the app and its private runtime so it does not need to change a system Python installation. PDF files are never copied or modified.

## Local development

```powershell
.\.venv\Scripts\python.exe main.py
```

Core implementation: page-preserving PDF chunks, local SQLite FTS5 retrieval, optional local FastEmbed vectors, hybrid ranking, and an Acrobat COM adapter. OCR for image-only pages requires Tesseract with Hebrew and English language data. Acrobat COM behavior varies by Acrobat edition and version.
