#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Pixel2SVG 核心行为测试（只用标准库 + 可选 Pillow）。"""

from __future__ import annotations

import re
import sys
import struct
import unittest
import uuid
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import pixel2svg_core as core  # noqa: E402

SVG_NS = "{http://www.w3.org/2000/svg}"


def make_rgba(path: Path, width: int, height: int, pixels) -> None:
    """不依赖 Pillow 写一个简单的 32 位 BMP（WIC 可读）。"""
    # 32bpp BI_RGB，自上而下用负高度，BGRA 顺序
    row_size = width * 4
    pixel_data = bytearray()
    for y in range(height):
        for x in range(width):
            r, g, b, a = pixels(x, y)
            pixel_data.extend((b, g, r, a))
    file_header = (
        b"BM"
        + (14 + 40 + len(pixel_data)).to_bytes(4, "little")
        + b"\x00\x00\x00\x00"
        + (54).to_bytes(4, "little")
    )
    info_header = (
        (40).to_bytes(4, "little")
        + width.to_bytes(4, "little", signed=True)
        + (-height).to_bytes(4, "little", signed=True)
        + (1).to_bytes(2, "little")
        + (32).to_bytes(2, "little")
        + (0).to_bytes(4, "little")
        + len(pixel_data).to_bytes(4, "little")
        + (2835).to_bytes(4, "little")
        + (2835).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + (0).to_bytes(4, "little")
    )
    path.write_bytes(file_header + info_header + bytes(pixel_data))


def make_png(path: Path, width: int, height: int, pixels) -> None:
    """不依赖 Pillow 写一个 RGBA PNG，用于精确验证 alpha。"""
    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            len(payload).to_bytes(4, "big")
            + kind
            + payload
            + zlib.crc32(kind + payload).to_bytes(4, "big")
        )

    raw = bytearray()
    for y in range(height):
        raw.append(0)
        for x in range(width):
            raw.extend(pixels(x, y))
    ihdr = (
        struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    )
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + chunk(b"IEND", b"")
    )


class CoreTests(unittest.TestCase):
    def setUp(self):
        # 直接使用工作区内的共享临时目录，避免新建子目录触发 Windows 沙箱限制
        self.dir = ROOT / "tests" / ".tmp"
        self.dir.mkdir(exist_ok=True)
        self.prefix = uuid.uuid4().hex + "_"

    def tearDown(self):
        for path in self.dir.glob(self.prefix + "*"):
            try:
                path.unlink()
            except OSError:
                pass
        for path in self.dir.glob("." + self.prefix + "*"):
            try:
                path.unlink()
            except OSError:
                pass

    def path(self, name: str) -> Path:
        return self.dir / (self.prefix + name)

    def test_every_non_transparent_pixel_is_one_rect(self):
        image_path = self.path("tiny.bmp")

        def pixel(_x, _y):
            return (255, 0, 0, 255)

        make_rgba(image_path, 3, 2, pixel)
        image = core.load_bitmap(image_path, backend="wic" if core.IS_WINDOWS else "auto")
        self.assertEqual(image.width, 3)
        self.assertEqual(image.height, 2)
        self.assertEqual(image.opaque_count, 6)
        output = self.path("tiny.svg")
        result = core.export_svg(image, output)
        self.assertEqual(result.rect_count, 6)
        root = ET.parse(output).getroot()
        rects = root.findall(f".//{SVG_NS}rect")
        self.assertEqual(len(rects), 6)
        for rect in rects:
            self.assertEqual(rect.get("width"), "1")
            self.assertEqual(rect.get("height"), "1")

    def test_pixels_are_not_merged(self):
        image_path = self.path("same.bmp")

        def pixel(_x, _y):
            return (10, 20, 30, 255)

        make_rgba(image_path, 4, 1, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("same.svg")
        core.export_svg(image, output)
        root = ET.parse(output).getroot()
        rects = root.findall(f".//{SVG_NS}rect")
        self.assertEqual(len(rects), 4)
        self.assertEqual(
            [(r.get("x"), r.get("y")) for r in rects],
            [("0", "0"), ("1", "0"), ("2", "0"), ("3", "0")],
        )

    def test_alpha_and_transparency(self):
        image_path = self.path("alpha.png")

        def pixel(x, y):
            if (x, y) == (0, 0):
                return (255, 0, 0, 255)
            if (x, y) == (1, 0):
                return (0, 255, 0, 128)
            if (x, y) == (2, 0):
                return (0, 0, 255, 0)
            return (255, 255, 255, 255)

        make_png(image_path, 3, 2, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        self.assertEqual(image.opaque_count, 5)
        self.assertEqual(image.translucent_count, 1)
        self.assertEqual(image.fully_opaque_count, 4)
        self.assertEqual(image.non_opaque_count, 2)
        self.assertTrue(image.has_transparency)
        output = self.path("alpha.svg")
        core.export_svg(image, output)
        root = ET.parse(output).getroot()
        rects = root.findall(f".//{SVG_NS}rect")
        self.assertEqual(len(rects), 5)
        opacities = [r.get("fill-opacity") for r in rects]
        self.assertIn("0.501961", opacities)

    def test_svg_reconstructs_source_pixels_exactly(self):
        image_path = self.path("reconstruct.png")

        def pixel(x, y):
            if (x + y) % 3 == 0:
                return (0, 0, 0, 0)
            return (
                (x * 67 + 11) % 256,
                (y * 91 + 23) % 256,
                (x * 37 + y * 53) % 256,
                (x * 41 + y * 73 + 1) % 256,
            )

        make_png(image_path, 7, 5, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("reconstruct.svg")
        core.export_svg(image, output)
        root = ET.parse(output).getroot()
        rebuilt = bytearray(image.width * image.height * 4)
        for rect in root.findall(f".//{SVG_NS}rect"):
            x = int(rect.get("x"))
            y = int(rect.get("y"))
            fill = rect.get("fill").lstrip("#")
            red, green, blue = (
                int(fill[0:2], 16),
                int(fill[2:4], 16),
                int(fill[4:6], 16),
            )
            alpha = round(float(rect.get("fill-opacity", "1")) * 255)
            offset = (y * image.width + x) * 4
            rebuilt[offset:offset + 4] = bytes((red, green, blue, alpha))
        self.assertEqual(bytes(rebuilt), image.rgba)

    def test_crop_bitmap_slices_region_and_keeps_metadata(self):
        image_path = self.path("crop.png")

        def pixel(x, y):
            return (x * 30, y * 40, (x + y) * 10, 255 - x * 10)

        make_png(image_path, 5, 4, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        crop = core.crop_bitmap(image, (1, 1, 4, 3))
        self.assertEqual((crop.width, crop.height), (3, 2))
        self.assertEqual(crop.region, (1, 1, 3, 2))
        expected = bytearray()
        for y in range(1, 3):
            for x in range(1, 4):
                expected.extend(pixel(x, y))
        self.assertEqual(crop.rgba, bytes(expected))
        output = self.path("crop_strict.svg")
        result = core.export_svg(crop, output)
        root = ET.parse(output).getroot()
        self.assertEqual(root.get("viewBox"), "0 0 3 2")
        self.assertEqual(result.rect_count, crop.opaque_count)
        self.assertIn("选区 1,1", root.find(f".//{SVG_NS}desc").text)

    def test_compact_svg_reconstructs_pixels_exactly(self):
        image_path = self.path("compact.png")

        def pixel(x, y):
            if (x * 3 + y) % 11 == 0:
                return (0, 0, 0, 0)
            return (
                (x * 71 + 7) % 256,
                (y * 53 + 19) % 256,
                (x * 13 + y * 29) % 256,
                (x * 37 + y * 61 + 1) % 256,
            )

        make_png(image_path, 9, 6, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("compact.svg")
        colors = core.count_visible_colors(image)
        self.assertIsNotNone(colors)
        result = core.export_svg(image, output, mode="compact")
        root = ET.parse(output).getroot()
        self.assertEqual(root.get("data-pixel-mode"), "compact")
        self.assertEqual(result.mode, "compact")
        paths = root.findall(f".//{SVG_NS}path")
        self.assertEqual(len(paths), result.color_paths)
        self.assertEqual(colors, result.color_paths)
        rebuilt = bytearray(image.width * image.height * 4)
        for path in paths:
            fill = path.get("fill").lstrip("#")
            red, green, blue = (
                int(fill[0:2], 16),
                int(fill[2:4], 16),
                int(fill[4:6], 16),
            )
            alpha = round(float(path.get("fill-opacity", "1")) * 255)
            for sx, sy in re.findall(
                r"M(\d+) (\d+)h1v1h-1z", path.get("d")
            ):
                x, y = int(sx), int(sy)
                offset = (y * image.width + x) * 4
                rebuilt[offset:offset + 4] = bytes(
                    (red, green, blue, alpha)
                )
        self.assertEqual(bytes(rebuilt), image.rgba)

    def test_compact_rejects_too_many_colors(self):
        image_path = self.path("too_many_colors.png")

        def pixel(x, y):
            index = y * 46 + x
            return (index & 0xFF, (index >> 8) & 0xFF, 0, 255)

        make_png(image_path, 46, 46, pixel)
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("too_many_colors.svg")
        self.assertIsNone(
            core.count_visible_colors(image, core.MAX_COMPACT_COLORS + 1)
        )
        with self.assertRaises(core.CompactColorLimitError):
            core.export_svg(image, output, mode="compact")
        self.assertFalse(output.exists())

    def test_alpha_text_round_trips_to_8bit(self):
        for alpha in range(1, 255):
            text = core._opacity_text(alpha)
            restored = round(float(text) * 255)
            self.assertEqual(restored, alpha, f"alpha={alpha} text={text}")

    def test_scale_only_changes_display_size(self):
        image_path = self.path("scale.bmp")
        make_rgba(image_path, 2, 2, lambda _x, _y: (1, 2, 3, 255))
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("scale.svg")
        core.export_svg(image, output, scale=4)
        root = ET.parse(output).getroot()
        self.assertEqual(root.get("width"), "8")
        self.assertEqual(root.get("height"), "8")
        self.assertEqual(root.get("viewBox"), "0 0 2 2")

    def test_cancel_leaves_no_files(self):
        image_path = self.path("cancel.bmp")
        make_rgba(image_path, 40, 40, lambda _x, _y: (0, 0, 0, 255))
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        output = self.path("cancel.svg")

        def cancel():
            return True

        with self.assertRaises(core.ExportCancelled):
            core.export_svg(image, output, cancel=cancel)
        self.assertFalse(output.exists())
        self.assertEqual(list(self.dir.glob("." + self.prefix + "*.tmp")), [])

    def test_preview_ppm_header(self):
        image_path = self.path("preview.bmp")
        make_rgba(image_path, 2, 2, lambda _x, _y: (255, 0, 0, 255))
        image = core.load_bitmap(
            image_path, backend="wic" if core.IS_WINDOWS else "auto"
        )
        ppm = core.render_preview_ppm(image, 80, 60, 10, 20, 10)
        self.assertTrue(ppm.startswith(b"P6\n80 60\n255\n"))
        self.assertEqual(len(ppm), len(b"P6\n80 60\n255\n") + 80 * 60 * 3)

    def test_fit_viewport_centers_image(self):
        zoom, offset_x, offset_y = core.fit_viewport(10, 10, 100, 100, 10, 32)
        self.assertEqual(zoom, 8.0)
        self.assertAlmostEqual(offset_x, 10.0)
        self.assertAlmostEqual(offset_y, 10.0)

    @unittest.skipUnless(core.HAS_PILLOW, "需要 Pillow")
    def test_png_exif_orientation_matches_wic(self):
        if not core.IS_WINDOWS:
            self.skipTest("WIC 仅 Windows")
        from PIL import Image

        for orientation in (3, 6, 8):
            path = self.path(f"exif_{orientation}.png")
            base = Image.new("RGB", (3, 2))
            for y in range(2):
                for x in range(3):
                    base.putpixel((x, y), (x * 80, y * 100, 50))
            exif = Image.Exif()
            exif[0x0112] = orientation
            base.save(path, exif=exif)
            with_pillow = core.load_bitmap(path, backend="pillow")
            with_wic = core.load_bitmap(path, backend="wic")
            self.assertEqual(
                (with_pillow.width, with_pillow.height, with_pillow.rgba),
                (with_wic.width, with_wic.height, with_wic.rgba),
                f"orientation={orientation}",
            )

    @unittest.skipUnless(core.HAS_PILLOW, "需要 Pillow")
    def test_tiff_exif_orientation_matches_wic(self):
        if not core.IS_WINDOWS:
            self.skipTest("WIC 仅 Windows")
        from PIL import Image

        for mode in ("RGB", "RGBA", "LA", "L"):
            for orientation in range(1, 9):
                path = self.path(f"tiff_exif_{mode}_{orientation}.tif")
                base = Image.new(mode, (3, 2))
                for y in range(2):
                    for x in range(3):
                        if mode == "RGBA":
                            color = (x * 80, y * 100, 50, 120 + x * 20)
                        elif mode == "RGB":
                            color = (x * 80, y * 100, 50)
                        elif mode == "LA":
                            color = (x * 50, 120 + x * 20)
                        else:
                            color = (x * 50,)
                        base.putpixel((x, y), color)
                exif = Image.Exif()
                exif[0x0112] = orientation
                base.save(path, exif=exif)
                with_pillow = core.load_bitmap(path, backend="pillow")
                with_wic = core.load_bitmap(path, backend="wic")
                self.assertEqual(
                    (with_pillow.width, with_pillow.height, with_pillow.rgba),
                    (with_wic.width, with_wic.height, with_wic.rgba),
                    f"mode={mode} orientation={orientation}",
                )

    @unittest.skipUnless(core.HAS_PILLOW, "需要 Pillow")
    def test_pillow_matches_wic_with_alpha(self):
        if not core.IS_WINDOWS:
            self.skipTest("WIC 仅 Windows")
        from PIL import Image

        path = self.path("alpha.png")
        image = Image.new("RGBA", (3, 2), (0, 0, 0, 0))
        image.putpixel((0, 0), (255, 0, 0, 255))
        image.putpixel((1, 0), (0, 255, 0, 128))
        image.putpixel((2, 1), (0, 0, 255, 77))
        image.save(path)
        with_pillow = core.load_bitmap(path, backend="pillow")
        with_wic = core.load_bitmap(path, backend="wic")
        self.assertEqual(
            (with_pillow.width, with_pillow.height, with_pillow.rgba),
            (with_wic.width, with_wic.height, with_wic.rgba),
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
