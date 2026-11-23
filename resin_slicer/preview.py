"""Preview pictures for the printer's screen.

A ``.ctb`` or ``.goo`` file carries two small pictures of what it prints. The printer shows them in
its file list and while it prints (ChiTu boards, Elegoo's SDCP printers). This draws them: the parts
seen from the front right and a little above, lit from above, filling the picture.

It's plain Python (no packages), a small z-buffer rasterizer. Very big meshes are thinned, which is
fine at this size, so a picture takes about a second at most. Pictures are RGB, row by row from the
top: ``Picture.pixels`` is a bytearray of width * height * 3.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Iterable, Sequence

from .mesh import Triangle

MODEL_RGB = (146, 128, 244)  # a light violet that reads well on the printers' dark screens
BACKGROUND_RGB = (24, 26, 33)
MAX_TRIANGLES = 300_000  # more than this are thinned (every n-th one): small triangles cover each other
YAW_DEG = 35.0  # the view comes from the front right ...
PITCH_DEG = 32.0  # ... and from above
MARGIN = 0.08  # of the picture's size, kept clear around the parts


@dataclass(frozen=True)
class Picture:
    width: int
    height: int
    pixels: bytearray  # RGB, rows from the top

    def rgb(self, x: int, y: int) -> tuple[int, int, int]:
        i = (y * self.width + x) * 3
        return self.pixels[i], self.pixels[i + 1], self.pixels[i + 2]


def blank(width: int, height: int, rgb: tuple[int, int, int] = BACKGROUND_RGB) -> Picture:
    return Picture(width, height, bytearray(bytes(rgb) * (width * height)))


def _unit(v: Sequence[float]) -> tuple[float, float, float]:
    length = math.sqrt(v[0] * v[0] + v[1] * v[1] + v[2] * v[2]) or 1.0
    return v[0] / length, v[1] / length, v[2] / length


def render(
    triangles: Sequence[Triangle],
    width: int,
    height: int,
    *,
    color: tuple[int, int, int] = MODEL_RGB,
    background: tuple[int, int, int] = BACKGROUND_RGB,
) -> Picture:
    """The triangles (mm, Z up) drawn into a width x height picture."""
    picture = blank(width, height, background)
    if not triangles or width <= 0 or height <= 0:
        return picture
    yaw = math.radians(YAW_DEG)
    pitch = math.radians(PITCH_DEG)
    # the camera's direction from the parts, and its right and up on the screen
    eye = (math.sin(yaw) * math.cos(pitch), -math.cos(yaw) * math.cos(pitch), math.sin(pitch))
    right = _unit((math.cos(yaw), math.sin(yaw), 0.0))
    up = _unit((eye[1] * right[2] - eye[2] * right[1], eye[2] * right[0] - eye[0] * right[2], eye[0] * right[1] - eye[1] * right[0]))
    light = _unit((eye[0] - 0.35, eye[1] + 0.2, eye[2] + 0.9))
    rx, ry, rz = right
    ux, uy, uz = up
    ex, ey, ez = eye

    step = max(1, math.ceil(len(triangles) / MAX_TRIANGLES))
    tris = triangles[::step] if step > 1 else triangles

    # where everything lands on the screen, to fit it in
    min_sx = min_sy = math.inf
    max_sx = max_sy = -math.inf
    for tri in tris:
        for x, y, z in tri:
            sx = x * rx + y * ry + z * rz
            sy = x * ux + y * uy + z * uz
            if sx < min_sx:
                min_sx = sx
            if sx > max_sx:
                max_sx = sx
            if sy < min_sy:
                min_sy = sy
            if sy > max_sy:
                max_sy = sy
    span_x = max(max_sx - min_sx, 1e-6)
    span_y = max(max_sy - min_sy, 1e-6)
    scale = min(width * (1 - 2 * MARGIN) / span_x, height * (1 - 2 * MARGIN) / span_y)
    off_x = width / 2 - (min_sx + max_sx) / 2 * scale
    off_y = height / 2 + (min_sy + max_sy) / 2 * scale

    depth = [math.inf] * (width * height)  # nearer is smaller
    pixels = picture.pixels
    cr, cg, cb = color
    shades: dict[int, bytes] = {}

    def shade(level: int) -> bytes:
        rgb = shades.get(level)
        if rgb is None:
            k = level / 64.0
            rgb = bytes((min(255, int(cr * k)), min(255, int(cg * k)), min(255, int(cb * k))))
            shades[level] = rgb
        return rgb

    for (ax, ay, az), (bx, by, bz), (qx, qy, qz) in tris:
        # face normal (either side: some meshes are wound inside out), lit softly from above
        n1x, n1y, n1z = bx - ax, by - ay, bz - az
        n2x, n2y, n2z = qx - ax, qy - ay, qz - az
        nx = n1y * n2z - n1z * n2y
        ny = n1z * n2x - n1x * n2z
        nz = n1x * n2y - n1y * n2x
        length = math.sqrt(nx * nx + ny * ny + nz * nz)
        if length == 0:
            continue
        lambert = abs(nx * light[0] + ny * light[1] + nz * light[2]) / length
        rgb = shade(int((0.38 + 0.72 * lambert) * 64))
        # to the screen (pixels) and depth along the view
        p0x = (ax * rx + ay * ry + az * rz) * scale + off_x
        p0y = off_y - (ax * ux + ay * uy + az * uz) * scale
        p1x = (bx * rx + by * ry + bz * rz) * scale + off_x
        p1y = off_y - (bx * ux + by * uy + bz * uz) * scale
        p2x = (qx * rx + qy * ry + qz * rz) * scale + off_x
        p2y = off_y - (qx * ux + qy * uy + qz * uz) * scale
        d0 = -(ax * ex + ay * ey + az * ez)
        d1 = -(bx * ex + by * ey + bz * ez)
        d2 = -(qx * ex + qy * ey + qz * ez)
        x0 = max(0, int(min(p0x, p1x, p2x)))
        x1 = min(width - 1, int(max(p0x, p1x, p2x)))
        y0 = max(0, int(min(p0y, p1y, p2y)))
        y1 = min(height - 1, int(max(p0y, p1y, p2y)))
        if x0 > x1 or y0 > y1:
            continue
        area = (p1x - p0x) * (p2y - p0y) - (p2x - p0x) * (p1y - p0y)
        if abs(area) < 1.0:
            # smaller than a pixel: its middle
            px = int((p0x + p1x + p2x) / 3)
            py = int((p0y + p1y + p2y) / 3)
            if 0 <= px < width and 0 <= py < height:
                d = (d0 + d1 + d2) / 3
                i = py * width + px
                if d < depth[i]:
                    depth[i] = d
                    pixels[i * 3:i * 3 + 3] = rgb
            continue
        inv = 1.0 / area
        for py in range(y0, y1 + 1):
            cy = py + 0.5
            row = py * width
            for px in range(x0, x1 + 1):
                cx = px + 0.5
                w0 = ((p1x - cx) * (p2y - cy) - (p2x - cx) * (p1y - cy)) * inv
                w1 = ((p2x - cx) * (p0y - cy) - (p0x - cx) * (p2y - cy)) * inv
                w2 = 1.0 - w0 - w1
                if w0 < -1e-6 or w1 < -1e-6 or w2 < -1e-6:
                    continue
                d = w0 * d0 + w1 * d1 + w2 * d2
                i = row + px
                if d < depth[i]:
                    depth[i] = d
                    pixels[i * 3:i * 3 + 3] = rgb
    return picture


def resized(picture: Picture, width: int, height: int) -> Picture:
    """The picture at another size, each new pixel the average of the ones it covers."""
    out = bytearray(width * height * 3)
    sx = picture.width / width
    sy = picture.height / height
    src = picture.pixels
    for y in range(height):
        ya = int(y * sy)
        yb = max(ya + 1, min(picture.height, int(math.ceil((y + 1) * sy))))
        for x in range(width):
            xa = int(x * sx)
            xb = max(xa + 1, min(picture.width, int(math.ceil((x + 1) * sx))))
            r = g = b = 0
            count = 0
            for yy in range(ya, yb):
                base = (yy * picture.width) * 3
                for xx in range(xa, xb):
                    i = base + xx * 3
                    r += src[i]
                    g += src[i + 1]
                    b += src[i + 2]
                    count += 1
            o = (y * width + x) * 3
            out[o] = r // count
            out[o + 1] = g // count
            out[o + 2] = b // count
    return Picture(width, height, out)


def pictures(triangles: Sequence[Triangle], sizes: Iterable[tuple[int, int]]) -> list[Picture]:
    """One picture per size: the biggest is drawn, the others are made from it.

    Never fails a slice: if drawing goes wrong, the pictures are left blank."""
    sizes = list(sizes)
    try:
        big_w, big_h = max(sizes, key=lambda s: s[0] * s[1])
        big = render(triangles, big_w, big_h)
        return [big if (w, h) == (big_w, big_h) else _fit(big, w, h) for w, h in sizes]
    except Exception:  # noqa: BLE001 - a picture must never stop a print file from being written
        return [blank(w, h) for w, h in sizes]


def _fit(picture: Picture, width: int, height: int) -> Picture:
    """Resized to width x height; a different shape is cropped from the middle first (no stretching)."""
    want = width / height
    have = picture.width / picture.height
    if abs(want - have) < 0.01:
        return resized(picture, width, height)
    if have > want:
        crop_w, crop_h = int(round(picture.height * want)), picture.height
    else:
        crop_w, crop_h = picture.width, int(round(picture.width / want))
    x0 = (picture.width - crop_w) // 2
    y0 = (picture.height - crop_h) // 2
    rows = bytearray()
    for y in range(y0, y0 + crop_h):
        start = (y * picture.width + x0) * 3
        rows += picture.pixels[start:start + crop_w * 3]
    return resized(Picture(crop_w, crop_h, rows), width, height)


# ---- the printers' pixel formats ------------------------------------------------------------------

def rgb565(r: int, g: int, b: int) -> int:
    return ((r >> 3) << 11) | ((g >> 2) << 5) | (b >> 3)


def encode_goo(picture: Picture) -> bytes:
    """GOO: 16-bit RGB565 pixels, big-endian, row by row (UVtools GooFile: RGB565_BE)."""
    out = bytearray(picture.width * picture.height * 2)
    p = picture.pixels
    for i in range(picture.width * picture.height):
        value = rgb565(p[i * 3], p[i * 3 + 1], p[i * 3 + 2])
        out[i * 2] = value >> 8
        out[i * 2 + 1] = value & 0xFF
    return bytes(out)


CTB_REPEAT = 0x20  # a pixel word with this bit set is followed by a run length (its 12 low bits + 1)
CTB_MAX_RUN = 0xFFF


def encode_ctb(picture: Picture) -> bytes:
    """CTB: run-length coded 16-bit pixels, little-endian, row by row (ChiTuBox's RGB15 RLE).

    A pixel is RGB565 with bit 0x20 (green's lowest) used as the "a run follows" flag; a run word
    is 0x3000 | (count - 1)."""
    out = bytearray()
    p = picture.pixels
    total = picture.width * picture.height

    def put(value: int, count: int) -> None:
        if count <= 2:
            word = value & ~CTB_REPEAT
            for _ in range(count):
                out.extend(word.to_bytes(2, "little"))
        else:
            out.extend((value | CTB_REPEAT).to_bytes(2, "little"))
            out.extend(((count - 1) | 0x3000).to_bytes(2, "little"))

    current = -1
    count = 0
    for i in range(total):
        value = rgb565(p[i * 3], p[i * 3 + 1], p[i * 3 + 2]) & ~CTB_REPEAT
        if value == current and count < CTB_MAX_RUN:
            count += 1
            continue
        if count:
            put(current, count)
        current = value
        count = 1
    if count:
        put(current, count)
    return bytes(out)


def decode_ctb(data: bytes, width: int, height: int) -> Picture:
    """The other way (for tests and checks): CTB preview bytes back to RGB."""
    pixels = bytearray()
    i = 0
    while i + 1 < len(data) and len(pixels) < width * height * 3:
        word = data[i] | (data[i + 1] << 8)
        i += 2
        count = 1
        if word & CTB_REPEAT:
            count += (data[i] | (data[i + 1] << 8)) & 0xFFF
            i += 2
        rgb = bytes((((word >> 11) & 0x1F) << 3, ((word >> 6) & 0x1F) << 3, (word & 0x1F) << 3))
        pixels += rgb * count
    pixels = pixels[: width * height * 3]
    pixels += bytes(width * height * 3 - len(pixels))
    return Picture(width, height, pixels)
