import os
import tempfile
import unicodedata
import unittest
from pathlib import Path
from unittest.mock import patch

from core import paths
from core.index_store import document_id, fingerprint


class PathTests(unittest.TestCase):
    def test_env_override_wins_on_every_platform(self):
        with patch.dict(os.environ, {"ACROBAT_SEMANTIC_DATA_DIR": "/tmp/pss-data"}):
            self.assertEqual(paths.data_dir(), Path("/tmp/pss-data"))
            self.assertEqual(paths.log_dir(Path("/app")), Path("/tmp/pss-data/logs"))

    def test_macos_uses_library_folders(self):
        env = {k: v for k, v in os.environ.items() if k not in ("ACROBAT_SEMANTIC_DATA_DIR", "FASTEMBED_CACHE_PATH")}
        with patch.dict(os.environ, env, clear=True), patch.object(paths, "IS_WINDOWS", False), \
                patch.object(paths, "IS_MACOS", True):
            self.assertEqual(paths.data_dir(), Path.home() / "Library" / "Application Support" / paths.APP_NAME)
            self.assertEqual(paths.log_dir(Path("/Applications/x.app/Contents/MacOS")),
                             Path.home() / "Library" / "Logs" / paths.APP_NAME)
            self.assertEqual(paths.model_dir(), paths.data_dir() / "models")

    def test_windows_keeps_existing_locations(self):
        env = {"LOCALAPPDATA": r"C:\Users\me\AppData\Local"}
        with patch.dict(os.environ, env, clear=True), patch.object(paths, "IS_WINDOWS", True), \
                patch.object(paths, "IS_MACOS", False):
            self.assertEqual(paths.data_dir(), Path(r"C:\Users\me\AppData\Local") / "AcrobatSemanticSearch")
            self.assertEqual(paths.log_dir(Path("/install")), Path("/install") / "data" / "logs")

    def test_nfc_and_nfd_paths_share_one_document_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            name = "résumé סיכום.pdf"
            nfc = Path(tmp) / unicodedata.normalize("NFC", name)
            nfc.write_bytes(b"%PDF")
            nfd = Path(tmp) / unicodedata.normalize("NFD", name)
            self.assertEqual(document_id(nfc), document_id(nfd))
            self.assertEqual(paths.normalize_pdf_path(nfd), paths.normalize_pdf_path(nfc))

    def test_windows_document_ids_are_unchanged_from_the_original_formula(self):
        import hashlib
        name = "/tmp/" + unicodedata.normalize("NFD", "בּ résumé.pdf")  # not NFC
        with patch.object(paths, "IS_WINDOWS", True):
            original = hashlib.sha256(os.path.normcase(str(Path(name).resolve())).encode("utf-8")).hexdigest()
            self.assertEqual(document_id(name), original)

    def test_unreadable_pdf_keeps_its_index_searchable(self):
        from core.chunker import Chunk
        from core.index_store import IndexStore
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "a.pdf"; pdf.write_bytes(b"content")
            store = IndexStore(Path(tmp) / "i.sqlite3")
            store.replace_document(pdf, fingerprint(pdf), 1, [Chunk(1, 0, "hypercapnia", "native")])
            with patch("core.index_store.fingerprint", side_effect=PermissionError("denied")):
                self.assertIsNone(store.status(pdf)["stale"])
            pdf.unlink()
            self.assertTrue(store.status(pdf)["stale"])
            store.close()

    def test_fingerprint_changes_with_content(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "a.pdf"
            pdf.write_bytes(b"one")
            first = fingerprint(pdf)
            self.assertEqual(fingerprint(pdf), first)
            pdf.write_bytes(b"two!")
            self.assertNotEqual(fingerprint(pdf), first)
            # Same size and restored mtime: the inode/ctime part of the cache key still catches it.
            stat = pdf.stat(); pdf.write_bytes(b"TWO!"); os.utime(pdf, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            self.assertEqual(fingerprint(pdf, cached=False), fingerprint(pdf))


if __name__ == "__main__":
    unittest.main()
