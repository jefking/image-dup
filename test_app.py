import tempfile
import unittest
from pathlib import Path


from app import DuplicateState


class DuplicateStateTests(unittest.TestCase):
    def _write(self, p: Path, content: bytes = b"x") -> None:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(content)

    def test_pairs_page_prefers_unsuffixed_as_base(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            year = root / "2024"
            self._write(year / "2E1B4361 (2).jpg")
            self._write(year / "2E1B4361 (3).jpg")
            self._write(year / "2E1B4361.jpg")
            self._write(year / "other.jpg")

            st = DuplicateState(root, permanent_delete=False)
            st.build_index()

            page = st.pairs_page(cursor=0, limit=10)
            # With limit > candidates, done may be True even on the first page.
            self.assertGreaterEqual(len(page["pairs"]), 2)
            self.assertEqual(page["total_candidate_pairs"], 2)

            left_names = {p["left"]["name"] for p in page["pairs"]}
            self.assertEqual(left_names, {"2E1B4361.jpg"})
            right_names = [p["right"]["name"] for p in page["pairs"]]
            self.assertIn("2E1B4361 (2).jpg", right_names)
            self.assertIn("2E1B4361 (3).jpg", right_names)

    def test_delete_moves_to_trash_and_pairs_skip_deleted(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            year = root / "2024"
            self._write(year / "A.jpg")
            self._write(year / "A (2).jpg")

            st = DuplicateState(root, permanent_delete=False)
            st.build_index()
            page1 = st.pairs_page(cursor=0, limit=10)
            self.assertEqual(len(page1["pairs"]), 1)
            right = page1["pairs"][0]["right"]
            rid = right["id"]

            res = st.delete_id(rid)
            self.assertTrue(res["ok"])
            trashed = root / ".image-dup-trash" / "2024" / right["name"]
            self.assertTrue(trashed.exists())

            page2 = st.pairs_page(cursor=0, limit=10)
            self.assertEqual(page2["pairs"], [])
            self.assertTrue(page2["done"])

    def test_hidden_dirs_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            self._write(root / ".hidden" / "X.jpg")
            self._write(root / ".hidden" / "X (2).jpg")
            st = DuplicateState(root, permanent_delete=False)
            st.build_index()
            page = st.pairs_page(cursor=0, limit=10)
            self.assertEqual(page["pairs"], [])

    def test_pairs_do_not_cross_folders(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            a = root / "2023"
            b = root / "2024"

            # Same normalized key exists in two different folders.
            self._write(a / "A.jpg")
            self._write(a / "A (2).jpg")
            self._write(b / "A.jpg")
            self._write(b / "A (2).jpg")

            st = DuplicateState(root, permanent_delete=False)
            st.build_index()
            page = st.pairs_page(cursor=0, limit=50)

            # Should yield 1 pair per folder, and never mix folders in a pair.
            self.assertEqual(page["total_candidate_pairs"], 2)
            for pair in page["pairs"]:
                left_dir = Path(pair["left"]["relpath"]).parent.as_posix()
                right_dir = Path(pair["right"]["relpath"]).parent.as_posix()
                self.assertEqual(left_dir, right_dir)


if __name__ == "__main__":
    unittest.main()
