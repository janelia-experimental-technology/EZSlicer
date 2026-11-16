import tempfile
import unittest
from pathlib import Path

from resin_slicer.config import SupportConfig, profile
from resin_slicer.material_tags import materials_match, read_file_material
from resin_slicer.mesh import cube_mesh
from resin_slicer.pipeline import SliceJob, slice_to_file


class FileMaterialTests(unittest.TestCase):
    def _slice(self, tmp: str, fmt: str, resin: str) -> Path:
        config = profile("small-test").with_overrides(layer_height_mm=2.0, resin_name=resin)
        path = Path(tmp) / f"cube.{fmt}"
        slice_to_file(SliceJob(cube_mesh(6), config, SupportConfig(enabled=False)), path, fmt)
        return path

    def test_reads_resin_name_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            for fmt in ("goo", "ctb"):
                with self.subTest(fmt=fmt):
                    self.assertEqual(read_file_material(self._slice(tmp, fmt, "Siraya Fast Grey")), "Siraya Fast Grey")

    def test_long_name_cut_by_goo_still_matches(self) -> None:
        long_name = "Resione K+ Tough Grey - Elegoo Mars 5 Ultra 9K"
        with tempfile.TemporaryDirectory() as tmp:
            stored = read_file_material(self._slice(tmp, "goo", long_name))
            self.assertEqual(stored, long_name[:32])
            self.assertTrue(materials_match(long_name, stored))
            self.assertEqual(read_file_material(self._slice(tmp, "ctb", long_name)), long_name)

    def test_unknown_file_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "x.ctb"
            path.write_bytes(b"\0" * 64)
            self.assertIsNone(read_file_material(path))
            self.assertIsNone(read_file_material(Path(tmp) / "missing.goo"))

    def test_match_rules(self) -> None:
        self.assertTrue(materials_match("Siraya Fast  grey", "siraya fast-Grey"))
        self.assertFalse(materials_match("Siraya Fast Grey", "Elegoo ABS-like Clear"))
        self.assertIsNone(materials_match("", "Elegoo ABS-like Clear"))
        self.assertIsNone(materials_match("Standard", None))


if __name__ == "__main__":
    unittest.main()
