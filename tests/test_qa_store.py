import tempfile
import unittest
from pathlib import Path

from qa_store import QAStore


class QAStoreTests(unittest.TestCase):
    def test_answers_round_trip_through_sqlite(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAStore(Path(root) / "jobs.db")
            store.put("Years of Python?", "3", "number", ["2", "3"])

            self.assertEqual(store.get("years of python", "number", ["2", "3"]), "3")
            self.assertEqual(len(store), 1)
            self.assertEqual(store.as_dict()["years of python"]["answer"], "3")

    def test_replace_rejects_non_object(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAStore(Path(root) / "jobs.db")
            with self.assertRaisesRegex(ValueError, "JSON object"):
                store.replace([])


if __name__ == "__main__":
    unittest.main()
