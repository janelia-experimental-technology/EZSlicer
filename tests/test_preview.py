"""The preview pictures in .ctb and .goo files: what the printer's screen shows for a file."""
import struct
import tempfile
import time
import unittest
from pathlib import Path

from resin_slicer.config import SupportConfig, profile
from resin_slicer.mesh import cube_mesh
from resin_slicer.pipeline import SliceJob, slice_to_file
from resin_slicer.preview import BACKGROUND_RGB, Picture, blank, decode_ctb, encode_ctb, encode_goo, pictures, render, resized, rgb565

GOO_PREVIEW_AT = 194  # after the GOO header's names and three reserved words


def lit(picture: Picture) -> int:
    """How many pixels show something other than the background (after 16-bit rounding)."""
    bg = tuple(v >> 3 for v in BACKGROUND_RGB)
    return sum(
        1
        for i in range(picture.width * picture.height)
        if tuple(v >> 3 for v in picture.pixels[i * 3:i * 3 + 3]) != bg
    )


def ctb_previews(data: bytes) -> list[tuple[int, int, bytes, bytes]]:
    """(width, height, the 16 reserved bytes, picture bytes) of each preview in a CTB, found by its sizes."""
    out = []
    for width, height in ((400, 300), (200, 125)):
        at = data.find(struct.pack("<II", width, height))
        offset, size = struct.unpack_from("<II", data, at + 8)
        out.append((width, height, data[at + 16:at + 32], data[offset:offset + size]))
    return out


class PictureTests(unittest.TestCase):
    def test_a_cube_fills_the_middle_and_leaves_the_corners(self) -> None:
        picture = render(cube_mesh(10).triangles, 120, 90)
        self.assertNotEqual(picture.rgb(60, 45), BACKGROUND_RGB)
        for x, y in ((0, 0), (119, 0), (0, 89), (119, 89)):
            self.assertEqual(picture.rgb(x, y), BACKGROUND_RGB)
        # lit faces aren't all one shade: it looks solid
        shades = {picture.rgb(x, y) for x in range(120) for y in range(90)} - {BACKGROUND_RGB}
        self.assertGreaterEqual(len(shades), 2)

    def test_nothing_to_draw_is_a_blank_picture(self) -> None:
        self.assertEqual(render((), 20, 10).pixels, blank(20, 10).pixels)
        self.assertEqual([(p.width, p.height) for p in pictures((), [(40, 30), (20, 12)])], [(40, 30), (20, 12)])

    def test_a_smaller_picture_is_made_from_the_big_one(self) -> None:
        small, big = pictures(cube_mesh(10).triangles, [(200, 125), (400, 300)])
        self.assertEqual((small.width, small.height, big.width, big.height), (200, 125, 400, 300))
        self.assertGreater(lit(small), 200 * 125 // 10)
        half = resized(Picture(2, 2, bytearray([0, 0, 0, 255, 255, 255, 255, 255, 255, 0, 0, 0])), 1, 1)
        self.assertEqual(half.rgb(0, 0), (127, 127, 127))

    def test_ctb_coding_round_trips(self) -> None:
        picture = render(cube_mesh(10).triangles, 64, 48)
        back = decode_ctb(encode_ctb(picture), 64, 48)
        for i in range(64 * 48):
            r, g, b = picture.pixels[i * 3:i * 3 + 3]
            self.assertEqual(tuple(back.pixels[i * 3:i * 3 + 3]), ((r >> 3) << 3, (g >> 3) << 3, (b >> 3) << 3))
        # one colour all over: one run (pixel word with the 0x20 flag, then 0x3000 | count - 1)
        flat = encode_ctb(blank(10, 10, (0, 0, 0)))
        self.assertEqual(flat, struct.pack("<HH", 0x20, 0x3000 | 99))

    def test_goo_pixels_are_big_endian_rgb565(self) -> None:
        self.assertEqual(encode_goo(blank(1, 1, (255, 0, 0))), bytes([0xF8, 0x00]))
        self.assertEqual(encode_goo(blank(1, 1, (0, 0, 255))), bytes([0x00, 0x1F]))
        self.assertEqual(rgb565(255, 255, 255), 0xFFFF)

    def test_a_big_mesh_draws_quickly(self) -> None:
        # a 5 mm grid of small cubes: 12 triangles each, about 100 000 in all
        triangles = []
        for i in range(90):
            for j in range(90):
                triangles.extend(cube_mesh(0.5).transformed((i * 0.6, j * 0.6, 0)).triangles)
        start = time.perf_counter()
        pictures(triangles, [(400, 300), (200, 125)])
        self.assertLess(time.perf_counter() - start, 20)


class FileTests(unittest.TestCase):
    def test_ctb_and_goo_carry_a_picture_of_the_part(self) -> None:
        config = profile("small-test").with_overrides(layer_height_mm=2.0)
        job = SliceJob(cube_mesh(6), config, SupportConfig(enabled=False))
        with tempfile.TemporaryDirectory() as tmp:
            ctb = Path(tmp) / "cube.ctb"
            goo = Path(tmp) / "cube.goo"
            slice_to_file(job, ctb, "ctb")
            slice_to_file(job, goo, "goo")

            for width, height, reserved, coded in ctb_previews(ctb.read_bytes()):
                self.assertEqual(reserved, bytes(16))
                picture = decode_ctb(coded, width, height)
                self.assertGreater(lit(picture), width * height // 10, f"{width}x{height} is empty")

            data = goo.read_bytes()
            small = data[GOO_PREVIEW_AT:GOO_PREVIEW_AT + 116 * 116 * 2]
            after_small = GOO_PREVIEW_AT + 116 * 116 * 2
            self.assertEqual(data[after_small:after_small + 2], b"\r\n")
            big_at = after_small + 2
            self.assertEqual(data[big_at + 290 * 290 * 2:big_at + 290 * 290 * 2 + 2], b"\r\n")
            background = rgb565(*BACKGROUND_RGB).to_bytes(2, "big")
            self.assertGreater(sum(1 for i in range(0, len(small), 2) if small[i:i + 2] != background), 116 * 116 // 10)


if __name__ == "__main__":
    unittest.main()
