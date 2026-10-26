"""Small per-user setup wizard bundled with the app payload."""
from __future__ import annotations
import os
import json
from pathlib import Path
import shutil
import sys
import tempfile
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import zipfile

PRODUCT = "Acrobat Semantic Search"
KEY = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\AcrobatSemanticSearch"
MARKER = ".acrobat_semantic_search_install"
DATA_MARKER = ".acrobat_semantic_search_data"
SETTINGS = "app-settings.json"


def payload_path() -> Path:
    return Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "payload.zip"


def safe_extract(archive: zipfile.ZipFile, root: Path) -> None:
    base = root.resolve()
    for info in archive.infolist():
        dest = (root / info.filename).resolve()
        if dest != base and base not in dest.parents:
            raise ValueError("The application package contains an invalid path.")
        if info.is_dir():
            dest.mkdir(parents=True, exist_ok=True)
        else:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as src, dest.open("wb") as dst:
                shutil.copyfileobj(src, dst)


def create_shortcut(path: Path, target: Path, args: str = "", icon: Path | None = None) -> None:
    import win32com.client
    shell = win32com.client.Dispatch("WScript.Shell")
    shortcut = shell.CreateShortcut(str(path))
    shortcut.TargetPath = str(target)
    shortcut.Arguments = args
    shortcut.WorkingDirectory = str(target.parent)
    shortcut.IconLocation = str(icon or target)
    shortcut.Save()


def install(root: Path, desktop: bool, progress) -> list[str]:
    payload = payload_path()
    if not payload.is_file():
        raise FileNotFoundError("The application files are missing from this installer.")
    root = root.expanduser().resolve()
    data_dir = root / "data"
    if root.exists() and not root.is_dir():
        raise FileExistsError(f"The application install path is a file, not a folder:\n{root}\nChoose a different parent folder.")
    if data_dir.exists() and not (data_dir / DATA_MARKER).is_file() and any(data_dir.iterdir()):
        raise FileExistsError(f"The data folder already contains files and is not managed by this app:\n{data_dir}\nMove or rename it before installing.")
    if root.exists() and any(root.iterdir()):
        if not (root / MARKER).is_file():
            raise FileExistsError(f"The application folder already exists and contains files:\n{root}\nChoose another installation location, or remove/rename that folder first.")
        preserve = None
        try:
            relative = data_dir.relative_to(root)
            if relative.parts:
                preserve = root / relative.parts[0]
        except ValueError:
            pass
        for child in list(root.iterdir()):
            if preserve is not None and child.resolve() == preserve.resolve():
                continue
            if child.is_dir(): shutil.rmtree(child)
            else: child.unlink()
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / DATA_MARKER).write_text("1", encoding="ascii")
    root.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(payload) as archive:
        total = max(1, len(archive.infolist()))
        for i, info in enumerate(archive.infolist(), 1):
            dest = (root / info.filename).resolve()
            base = root.resolve()
            if dest != base and base not in dest.parents:
                raise ValueError("The application package contains an invalid path.")
            if info.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(info) as src, dest.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
            progress(i, total)
    exe = root / "AcrobatSemanticSearch.exe"
    icon = root / "assets" / "acrobat-semantic-search.ico"
    if not exe.is_file():
        raise FileNotFoundError("The installer package is incomplete.")
    (root / MARKER).write_text("1", encoding="ascii")
    (root / SETTINGS).write_text(json.dumps({"data_dir": str(data_dir)}, indent=2), encoding="utf-8")
    appdata = Path(os.environ["APPDATA"])
    programs = appdata / "Microsoft" / "Windows" / "Start Menu" / "Programs" / PRODUCT
    programs.mkdir(parents=True, exist_ok=True)
    create_shortcut(programs / f"{PRODUCT}.lnk", exe, f'"{root / "main.py"}"', icon)
    desktop_dir = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "Desktop"
    warnings = []
    if desktop:
        try:
            import win32com.client
            desktop_dir = Path(win32com.client.Dispatch("WScript.Shell").SpecialFolders("Desktop"))
            desktop_dir.mkdir(parents=True, exist_ok=True)
            create_shortcut(desktop_dir / f"{PRODUCT}.lnk", exe, f'"{root / "main.py"}"', icon)
        except Exception as exc:
            warnings.append("Windows could not create a Desktop shortcut. The app is installed and available from the Start menu.")
    import winreg
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, KEY, 0, winreg.KEY_SET_VALUE) as key:
        values = {
            "DisplayName": PRODUCT,
            "DisplayVersion": "1.0.0",
            "Publisher": "Acrobat Semantic Search",
            "InstallLocation": str(root),
            "DisplayIcon": str(icon),
            "UninstallString": f'"{exe}" "{root / "main.py"}" --uninstall',
            "NoModify": 1,
            "NoRepair": 1,
        }
        for name, value in values.items():
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD if isinstance(value, int) else winreg.REG_SZ, value)
    return warnings


class Setup:
    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"Install {PRODUCT}")
        self.root.resizable(False, False)
        self.root.geometry("590x315")
        self.root.configure(padx=22, pady=18)
        icon_file = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "acrobat-semantic-search.ico"
        if icon_file.is_file():
            try:
                self.root.iconbitmap(str(icon_file))
            except tk.TclError:
                pass
        ttk.Label(self.root, text=PRODUCT, font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(self.root, text="Install the app for your Windows account.", font=("Segoe UI", 10)).pack(anchor="w", pady=(4, 10))
        self.path = tk.StringVar(value=str(Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Programs"))
        ttk.Label(self.root, text="Install under this location (an AcrobatSemanticSearch folder will be created)").pack(anchor="w")
        row=ttk.Frame(self.root); row.pack(fill="x", pady=(3, 9)); ttk.Entry(row, textvariable=self.path).pack(side="left", fill="x", expand=True); ttk.Button(row,text="Browse…",command=self.choose_install).pack(side="left",padx=(6,0))
        ttk.Label(self.root, text="Saved indexes and models will be stored in the app's data subfolder.", wraplength=540).pack(anchor="w", pady=(0, 7))
        self.desktop = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.root, text="Create a desktop shortcut", variable=self.desktop).pack(anchor="w")
        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=(12, 8))
        self.button = ttk.Button(self.root, text="Install", command=self.start_install)
        self.button.pack(anchor="e")

    def choose_install(self):
        selected=filedialog.askdirectory(parent=self.root,title="Choose parent location for the application",initialdir=self.path.get(),mustexist=False)
        if selected:
            self.path.set(selected)

    def start_install(self):
        self.button.configure(state="disabled")
        try:
            root = Path(self.path.get()).expanduser() / "AcrobatSemanticSearch"
            if root.exists() and root.is_dir() and any(root.iterdir()):
                managed = (root / MARKER).is_file()
                if not managed:
                    messagebox.showwarning("Installation folder is not empty",
                        f"The folder already exists and contains files:\n\n{root}\n\nChoose another parent folder or move its contents first.", parent=self.root)
                    self.button.configure(state="normal")
                    return
                if not messagebox.askyesno("Replace existing installation?",
                    f"An existing Acrobat Semantic Search installation was found here:\n\n{root}\n\nReplace its application files? Your saved data will be kept.", parent=self.root):
                    self.button.configure(state="normal")
                    return
            def update_progress(i,n):
                self.progress.configure(value=i * 100 / n); self.root.update_idletasks()
            warnings = install(root, self.desktop.get(), update_progress)
        except Exception as exc:
            messagebox.showerror("Installation failed", str(exc), parent=self.root)
            self.button.configure(state="normal")
            return
        self.show_completion(root, warnings)
        self.root.destroy()

    def show_completion(self, install_root: Path, warnings: list[str]) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("Installation complete")
        dialog.resizable(False, False)
        dialog.transient(self.root)
        icon_file = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent)) / "assets" / "acrobat-semantic-search.ico"
        if icon_file.is_file():
            try: dialog.iconbitmap(str(icon_file))
            except tk.TclError: pass
        dialog.geometry("510x260")
        frame=ttk.Frame(dialog,padding=22); frame.pack(fill="both",expand=True)
        ttk.Label(frame,text="You’re all set",font=("Segoe UI",16,"bold")).pack(anchor="w")
        ttk.Label(frame,text=f"{PRODUCT} is installed. Open it from the Start menu or desktop shortcut.",wraplength=460).pack(anchor="w",pady=(12,8))
        if warnings:
            ttk.Label(frame,text="\n".join(warnings),wraplength=460,foreground="#9B4D00").pack(anchor="w",pady=(0,8))
        action={"launch":False}
        buttons=ttk.Frame(frame); buttons.pack(side="bottom",fill="x",pady=(12,0))
        def finish(launch: bool):
            action["launch"]=launch; dialog.destroy()
        ttk.Button(buttons,text="Close",command=lambda:finish(False)).pack(side="right",padx=(8,0))
        ttk.Button(buttons,text="Close and launch",command=lambda:finish(True)).pack(side="right")
        dialog.protocol("WM_DELETE_WINDOW",lambda:finish(False))
        dialog.grab_set(); self.root.wait_window(dialog)
        if action["launch"]:
            import subprocess
            exe=install_root / "AcrobatSemanticSearch.exe"
            subprocess.Popen([str(exe), str(install_root / "main.py")], cwd=str(install_root), close_fds=True)

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    Setup().run()
