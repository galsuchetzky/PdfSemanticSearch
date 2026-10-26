import tempfile
import unittest
from pathlib import Path

from core.chunker import Chunk, chunk_pages
from core.extractor import PageText, normalize_text
from core.index_store import IndexStore, document_id


class TextPipelineTests(unittest.TestCase):
    def test_normalize_unicode_and_whitespace(self):
        self.assertEqual(normalize_text("  שלום\r\n\r\n\n world  "), "שלום\n\nworld")

    def test_chunks_keep_page_and_split_long_paragraph(self):
        text = ("מילה " * 1400).strip()
        chunks = chunk_pages([PageText(8, text, "native", len(text))], max_chars=700, overlap=80)
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(c.page == 8 and c.method == "native" for c in chunks))
        self.assertTrue(all(len(c.text) <= 700 for c in chunks))


class IndexStoreTests(unittest.TestCase):
    def test_index_lexical_search_stale_and_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            pdf = Path(tmp) / "study.pdf"
            pdf.write_bytes(b"first version")
            store = IndexStore(Path(tmp) / "index.sqlite3")
            chunks = [Chunk(5, 0, "Atrial fibrillation treatment and stroke prevention", "native"),
                      Chunk(19, 0, "Respiratory acidosis and hypercapnia", "native")]
            from core.index_store import fingerprint
            store.replace_document(pdf, fingerprint(pdf), 20, chunks)
            self.assertEqual(document_id(pdf), store.status(pdf)["id"])
            self.assertEqual(store.list_documents()[0]["path"], str(pdf.resolve()))
            hits = store.search(pdf, "hypercapnia")
            self.assertEqual(hits[0]["page"], 19)
            phrase_hits = store.search(pdf, "atrial fibrillation")
            self.assertEqual(phrase_hits[0]["page"], 5)
            self.assertEqual(len({hit["page"] for hit in phrase_hits}), len(phrase_hits))
            semantic_pdf = Path(tmp) / "semantic.pdf"
            semantic_pdf.write_bytes(b"semantic fixture")
            store.replace_document(semantic_pdf, fingerprint(semantic_pdf), 2,
                [Chunk(1, 0, "heart rhythm topic", "native"), Chunk(2, 0, "lung topic", "native")],
                [[1.0, 0.0], [0.0, 1.0]], "test-model")
            self.assertEqual(store.search(semantic_pdf, "unrelated phrase", query_vector=[0.9, 0.1])[0]["page"], 1)
            pdf.write_bytes(b"changed version")
            self.assertTrue(store.status(pdf)["stale"])
            store.delete_document(pdf)
            self.assertIsNone(store.status(pdf))
            store.close()


if __name__ == "__main__":
    unittest.main()

