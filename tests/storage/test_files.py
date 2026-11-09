import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from app.shared.errors import Conflict, InvalidInput, NotFound
from app.storage.files import FileStore

NOW = datetime(2026, 11, 9, tzinfo=timezone.utc)


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "assets"
        self.store = FileStore(self.root, max_bytes=16)

    def tearDown(self):
        self.temp.cleanup()

    def write(self, **updates):
        values = {
            "business_id": "business-1",
            "filename": "picture.png",
            "content": b"image-data",
            "mime_type": "image/png",
            "now": NOW,
        }
        values.update(updates)
        return self.store.write(**values)

    def test_write_read_and_restart_preserve_checksum(self):
        asset = self.write(is_demo=True)
        self.assertTrue(asset.is_demo)
        self.assertEqual(self.store.read(asset), b"image-data")
        self.assertEqual(FileStore(self.root, max_bytes=16).read(asset), b"image-data")
        self.assertEqual([path.name for path in self.root.iterdir()], [f"{asset.id}.blob"])

    def test_user_filename_and_business_id_never_become_paths(self):
        asset = self.write(filename="../../outside.png", business_id="../../outside")
        self.assertEqual(asset.filename, "outside.png")
        self.assertEqual(self.store.read(asset), b"image-data")
        self.assertFalse((Path(self.temp.name) / "outside.png").exists())
        with self.assertRaises(InvalidInput):
            self.store.read(asset.model_copy(update={"id": "../../outside"}))

    def test_empty_large_and_naive_inputs_are_rejected_without_partial_files(self):
        for content in (b"", b"x" * 17):
            with self.assertRaises(InvalidInput):
                self.write(content=content)
        with self.assertRaises(InvalidInput):
            self.write(now=datetime(2026, 11, 9))
        self.assertEqual(list(self.root.iterdir()), [])

    def test_same_size_tampering_is_detected(self):
        asset = self.write()
        (self.root / f"{asset.id}.blob").write_bytes(b"evil--data")
        with self.assertRaises(Conflict):
            self.store.read(asset)

    def test_size_tampering_is_detected_before_read(self):
        asset = self.write()
        (self.root / f"{asset.id}.blob").write_bytes(b"x" * 17)
        with self.assertRaises(Conflict):
            self.store.read(asset)

    def test_missing_asset_is_not_found(self):
        asset = self.write()
        (self.root / f"{asset.id}.blob").unlink()
        with self.assertRaises(NotFound):
            self.store.read(asset)

    def test_symlink_asset_cannot_read_outside_root(self):
        asset = self.write()
        path = self.root / f"{asset.id}.blob"
        outside = Path(self.temp.name) / "outside.bin"
        outside.write_bytes(b"image-data")
        path.unlink()
        try:
            path.symlink_to(outside)
        except OSError:
            self.skipTest("Creating symlinks requires Windows Developer Mode or elevated rights")
        with self.assertRaises(InvalidInput):
            self.store.read(asset)

    def test_symlink_root_is_rejected(self):
        outside = Path(self.temp.name) / "outside-dir"
        outside.mkdir()
        linked = Path(self.temp.name) / "linked-assets"
        try:
            linked.symlink_to(outside, target_is_directory=True)
        except OSError:
            self.skipTest("Creating symlinks requires Windows Developer Mode or elevated rights")
        with self.assertRaises(InvalidInput):
            FileStore(linked)


if __name__ == "__main__":
    unittest.main()
