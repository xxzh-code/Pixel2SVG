#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixel2SVG · 核心模块
====================

把位图逐像素翻译为 SVG 正方形，不做轮廓拟合、颜色聚类或抗锯齿。

规则：
    * 默认 strict：一个不透明/半透明像素 -> 一个 1×1 的 <rect>
    * compact：每种 RGBA 颜色一条 <path>，每个像素仍是 1×1 子路径
    * 支持从整图中裁出矩形区域后再导出
    * 完全透明的像素          ->  不生成图形（画布保持透明）
    * 像素颜色按 sRGB 原样保存，透明度写入 fill-opacity
    * 坐标使用整数像素网格，shape-rendering="crispEdges"

图像读取优先使用 Pillow；Windows 上若没有 Pillow，则回退到系统
Windows Imaging Component (WIC)，因此本程序在 Windows 上无需安装
任何第三方库也能读取常见位图格式。
"""

from __future__ import annotations

import ctypes
import html
import math
import os
import struct
import sys
import tempfile
import threading
import uuid
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

try:  # Pillow 是可选依赖
    from PIL import Image, ImageOps

    HAS_PILLOW = True
except Exception:  # pragma: no cover - 取决于运行环境
    Image = None  # type: ignore[assignment]
    ImageOps = None  # type: ignore[assignment]
    HAS_PILLOW = False

IS_WINDOWS = sys.platform == "win32"

# 预览中透明区域的棋盘格颜色
CHECKER_LIGHT = (231, 227, 216)
CHECKER_DARK = (211, 206, 194)
PREVIEW_BG = (24, 34, 31)

# 单张图片最多允许的像素数，防止误加载超大文件耗尽内存
MAX_PIXELS = 180_000_000


class Pixel2SVGError(Exception):
    """所有可预期的错误基类。"""


class ImageLoadError(Pixel2SVGError):
    """图像读取失败。"""


class ExportCancelled(Pixel2SVGError):
    """用户取消了导出。"""


class CompactColorLimitError(Pixel2SVGError):
    """颜色数量过多，不适合紧凑路径模式。"""


@dataclass(slots=True)
class BitmapImage:
    """已解码的 RGBA 位图。"""

    width: int
    height: int
    rgba: bytes
    source: Path
    format: str
    frames: int = 1
    opaque_count: int = 0
    translucent_count: int = 0
    backend: str = ""
    # 如果这是从原图裁出的区域，记录 (left, top, width, height)
    region: Optional[tuple[int, int, int, int]] = None

    @property
    def total_pixels(self) -> int:
        return self.width * self.height

    @property
    def fully_opaque_count(self) -> int:
        return self.opaque_count - self.translucent_count

    @property
    def has_transparency(self) -> bool:
        """是否存在完全透明或半透明像素。"""
        return (
            self.opaque_count < self.total_pixels
            or self.translucent_count > 0
        )

    @property
    def transparent_count(self) -> int:
        """完全透明（alpha = 0）的像素数。"""
        return self.total_pixels - self.opaque_count

    @property
    def non_opaque_count(self) -> int:
        """非完全不透明（alpha < 255）的像素数。"""
        return self.total_pixels - self.fully_opaque_count


@dataclass(slots=True)
class ExportResult:
    path: Path
    rect_count: int
    file_size: int
    width: int
    height: int
    elapsed: float
    mode: str = "strict"
    color_paths: int = 0


# 紧凑模式最多容纳的颜色数。超过这个数量时，字典和 path 头部的开销
# 会抵消收益，因此拒绝导出并提示使用严格模式。
MAX_COMPACT_COLORS = 2048


# ---------------------------------------------------------------------------
# EXIF 方向（WIC 不会自动旋转，需要自己处理）
# ---------------------------------------------------------------------------

def _u16(data: bytes, offset: int, endian: str) -> int:
    if offset + 2 > len(data):
        raise ValueError("EXIF 数据不完整")
    return struct.unpack_from(endian + "H", data, offset)[0]


def _u32(data: bytes, offset: int, endian: str) -> int:
    if offset + 4 > len(data):
        raise ValueError("EXIF 数据不完整")
    return struct.unpack_from(endian + "I", data, offset)[0]


def _tiff_orientation(data: bytes, base: int) -> int:
    if base + 8 > len(data):
        return 1
    order = data[base:base + 2]
    if order == b"II":
        endian = "<"
    elif order == b"MM":
        endian = ">"
    else:
        return 1
    if _u16(data, base + 2, endian) != 42:
        return 1
    try:
        ifd_offset = _u32(data, base + 4, endian)
        if ifd_offset <= 0 or base + ifd_offset + 2 > len(data):
            return 1
        count = _u16(data, base + ifd_offset, endian)
        entry_base = base + ifd_offset + 2
        if count > 1024:
            return 1
        for index in range(count):
            entry = entry_base + index * 12
            if entry + 12 > len(data):
                break
            tag = _u16(data, entry, endian)
            if tag != 0x0112:
                continue
            value_type = _u16(data, entry + 2, endian)
            value_count = _u32(data, entry + 4, endian)
            if value_count < 1:
                return 1
            if value_type == 3:  # SHORT
                orientation = _u16(data, entry + 8, endian)
            elif value_type == 4:  # LONG
                orientation = _u32(data, entry + 8, endian)
            else:
                return 1
            return orientation if 1 <= orientation <= 8 else 1
    except (ValueError, struct.error):
        return 1
    return 1


def _jpeg_orientation(data: bytes) -> int:
    pos = 2
    length = len(data)
    while pos + 3 < length:
        if data[pos] != 0xFF:
            pos += 1
            continue
        while pos < length and data[pos] == 0xFF:
            pos += 1
        if pos >= length:
            break
        marker = data[pos]
        pos += 1
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            continue
        if pos + 2 > length:
            break
        segment_length = int.from_bytes(data[pos:pos + 2], "big")
        if segment_length < 2:
            break
        payload = pos + 2
        segment_end = pos + segment_length
        if segment_end > length:
            break
        if marker == 0xE1 and data[payload:payload + 6] == b"Exif\x00\x00":
            return _tiff_orientation(data, payload + 6)
        if marker == 0xDA:  # 图像数据开始，后面不会再有 EXIF
            break
        pos = segment_end
    return 1


def _png_orientation(data: bytes) -> int:
    """读取 PNG eXIf 块中的 TIFF Orientation。"""
    signature = b"\x89PNG\r\n\x1a\n"
    if not data.startswith(signature):
        return 1
    pos = len(signature)
    while pos + 12 <= len(data):
        length = int.from_bytes(data[pos:pos + 4], "big")
        chunk_type = data[pos + 4:pos + 8]
        payload_start = pos + 8
        chunk_end = payload_start + length
        if chunk_end + 4 > len(data):
            break
        if chunk_type == b"eXIf":
            base = payload_start
            if data[base:base + 6] == b"Exif\x00\x00":
                base += 6
            return _tiff_orientation(data, base)
        if chunk_type == b"IEND":
            break
        pos = chunk_end + 4
    return 1


def _tiff_orientation_from_file(path: Path) -> int:
    """直接从 TIFF 文件读取 IFD0，支持 IFD 位于文件末尾的大文件。"""
    try:
        with open(path, "rb") as handle:
            header = handle.read(8)
            if len(header) < 8:
                return 1
            order = header[:2]
            if order == b"II":
                endian = "<"
            elif order == b"MM":
                endian = ">"
            else:
                return 1
            if struct.unpack_from(endian + "H", header, 2)[0] != 42:
                return 1
            ifd_offset = struct.unpack_from(endian + "I", header, 4)[0]
            if ifd_offset < 8:
                return 1
            handle.seek(ifd_offset)
            count_data = handle.read(2)
            if len(count_data) < 2:
                return 1
            count = struct.unpack(endian + "H", count_data)[0]
            if count > 4096:
                return 1
            for _ in range(count):
                entry = handle.read(12)
                if len(entry) < 12:
                    break
                tag, value_type = struct.unpack_from(
                    endian + "HH", entry, 0
                )
                if tag != 0x0112:
                    continue
                value_count = struct.unpack_from(
                    endian + "I", entry, 4
                )[0]
                if value_count < 1:
                    return 1
                if value_type == 3:
                    orientation = struct.unpack_from(
                        endian + "H", entry, 8
                    )[0]
                elif value_type == 4:
                    orientation = struct.unpack_from(
                        endian + "I", entry, 8
                    )[0]
                else:
                    return 1
                return orientation if 1 <= orientation <= 8 else 1
    except (OSError, ValueError, struct.error):
        pass
    return 1


def read_exif_orientation(path: Path) -> int:
    """读取 PNG / JPEG / TIFF 的 EXIF Orientation；失败时返回 1。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read(4 * 1024 * 1024)
    except OSError:
        return 1
    try:
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return _png_orientation(data)
        if data[:2] == b"\xff\xd8":
            return _jpeg_orientation(data)
        if data[:2] in (b"II", b"MM"):
            return _tiff_orientation_from_file(path)
    except (ValueError, struct.error):
        pass
    return 1


def _apply_orientation(rgba: bytes, width: int, height: int, orientation: int):
    """按照 EXIF Orientation 旋转/镜像 RGBA 数据。"""
    if orientation <= 1 or orientation > 8:
        return rgba, width, height
    if orientation == 1:
        return rgba, width, height

    if orientation in (2, 3, 4):
        out = bytearray(len(rgba))
        stride = width * 4
        for y in range(height):
            if orientation == 4:
                sy = height - 1 - y
            elif orientation == 3:
                sy = height - 1 - y
            else:
                sy = y
            src_row = rgba[sy * stride:(sy + 1) * stride]
            dst_row = bytearray(stride)
            if orientation == 4:
                dst_row[:] = src_row
            else:
                for x in range(width):
                    sx = width - 1 - x
                    dst_row[x * 4:x * 4 + 4] = src_row[
                        sx * 4:sx * 4 + 4
                    ]
            out[y * stride:(y + 1) * stride] = dst_row
        return bytes(out), width, height

    new_width, new_height = height, width
    out = bytearray(len(rgba))
    src_stride = width * 4
    dst_stride = new_width * 4
    for y in range(new_height):
        for x in range(new_width):
            if orientation == 5:  # 主对角线翻转
                sx, sy = y, x
            elif orientation == 6:  # 顺时针 90°
                sx, sy = y, height - 1 - x
            elif orientation == 7:  # 副对角线翻转
                sx, sy = width - 1 - y, height - 1 - x
            else:  # 8，逆时针 90°
                sx, sy = width - 1 - y, x
            src = sy * src_stride + sx * 4
            dst = y * dst_stride + x * 4
            out[dst:dst + 4] = rgba[src:src + 4]
    return bytes(out), new_width, new_height


# ---------------------------------------------------------------------------
# Windows Imaging Component 回退解码器
# ---------------------------------------------------------------------------

if IS_WINDOWS:
    from ctypes import wintypes

    HRESULT = ctypes.c_long
    UINT = ctypes.c_uint
    BYTE = ctypes.c_ubyte

    class _GUID(ctypes.Structure):
        _fields_ = [
            ("Data1", ctypes.c_ulong),
            ("Data2", ctypes.c_ushort),
            ("Data3", ctypes.c_ushort),
            ("Data4", ctypes.c_ubyte * 8),
        ]

    def _guid(text: str) -> _GUID:
        value = uuid.UUID(text)
        return _GUID(
            value.time_low,
            value.time_mid,
            value.time_hi_version,
            (ctypes.c_ubyte * 8)(*value.bytes[8:]),
        )

    _CLSID_WICImagingFactory = _guid("{CACAF262-9370-4615-A13B-9F5539DA4C0A}")
    _IID_IWICImagingFactory = _guid("{EC5EC8A9-C395-4314-9C77-54D7A935FF70}")
    _GUID_WICPixelFormat32bppRGBA = _guid(
        "{F5C7AD2D-6A8D-43DD-A7A8-A29935261AE9}"
    )

    def _com_method(pointer, index: int, restype, *argtypes):
        vtable = ctypes.cast(
            pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))
        )[0]
        prototype = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
        return prototype(vtable[index])

    def _check_hresult(hresult: int, action: str) -> None:
        if hresult < 0:
            raise OSError(
                f"WIC {action} 失败 (HRESULT 0x{hresult & 0xFFFFFFFF:08X})"
            )

    def _wic_decode(path: Path):
        ole32 = ctypes.WinDLL("ole32")
        coinit = ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
        # RPC_E_CHANGED_MODE 表示当前线程已经以其它单元模型初始化过 COM，
        # 这时仍然可以使用 WIC，只是不能再调用 CoUninitialize。
        changed_mode = coinit == -2147417850
        if coinit < 0 and not changed_mode:
            raise OSError(f"COM 初始化失败 (0x{coinit & 0xFFFFFFFF:08X})")

        objects: list[ctypes.c_void_p] = []
        try:
            factory = ctypes.c_void_p()
            hresult = ole32.CoCreateInstance(
                ctypes.byref(_CLSID_WICImagingFactory),
                None,
                1,  # CLSCTX_INPROC_SERVER
                ctypes.byref(_IID_IWICImagingFactory),
                ctypes.byref(factory),
            )
            _check_hresult(hresult, "创建图像工厂")
            objects.append(factory)

            decoder = ctypes.c_void_p()
            create_decoder = _com_method(
                factory,
                3,
                HRESULT,
                ctypes.c_wchar_p,
                ctypes.POINTER(_GUID),
                wintypes.DWORD,
                ctypes.c_int,
                ctypes.POINTER(ctypes.c_void_p),
            )
            hresult = create_decoder(
                factory,
                str(path.resolve()),
                None,
                0x80000000,  # GENERIC_READ
                1,  # WICDecodeMetadataCacheOnLoad
                ctypes.byref(decoder),
            )
            _check_hresult(hresult, "打开图像")
            objects.append(decoder)

            frame_count = UINT(0)
            get_frame_count = _com_method(
                decoder, 12, HRESULT, ctypes.POINTER(UINT)
            )
            if get_frame_count(decoder, ctypes.byref(frame_count)) < 0:
                frame_count.value = 1

            frame = ctypes.c_void_p()
            get_frame = _com_method(
                decoder, 13, HRESULT, UINT, ctypes.POINTER(ctypes.c_void_p)
            )
            hresult = get_frame(decoder, 0, ctypes.byref(frame))
            _check_hresult(hresult, "读取图像帧")
            objects.append(frame)

            converter = ctypes.c_void_p()
            create_converter = _com_method(
                factory, 10, HRESULT, ctypes.POINTER(ctypes.c_void_p)
            )
            hresult = create_converter(factory, ctypes.byref(converter))
            _check_hresult(hresult, "创建像素格式转换器")
            objects.append(converter)

            initialize = _com_method(
                converter,
                8,
                HRESULT,
                ctypes.c_void_p,
                ctypes.POINTER(_GUID),
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_double,
                ctypes.c_int,
            )
            hresult = initialize(
                converter,
                frame,
                ctypes.byref(_GUID_WICPixelFormat32bppRGBA),
                0,  # WICBitmapDitherTypeNone
                None,
                0.0,
                0,  # WICBitmapPaletteTypeCustom
            )
            _check_hresult(hresult, "转换像素格式")

            width = UINT(0)
            height = UINT(0)
            get_size = _com_method(
                converter,
                3,
                HRESULT,
                ctypes.POINTER(UINT),
                ctypes.POINTER(UINT),
            )
            hresult = get_size(
                converter, ctypes.byref(width), ctypes.byref(height)
            )
            _check_hresult(hresult, "读取图像尺寸")
            if width.value <= 0 or height.value <= 0:
                raise OSError("图像尺寸无效")
            if width.value * height.value > MAX_PIXELS:
                raise OSError(
                    f"图像过大（{width.value} × {height.value}），"
                    "超出可处理范围"
                )

            stride = width.value * 4
            buffer_size = stride * height.value
            buffer = (BYTE * buffer_size)()
            copy_pixels = _com_method(
                converter,
                7,
                HRESULT,
                ctypes.c_void_p,
                UINT,
                UINT,
                ctypes.POINTER(BYTE),
            )
            hresult = copy_pixels(
                converter, None, stride, buffer_size, buffer
            )
            _check_hresult(hresult, "复制像素")
            rgba = bytes(buffer)
            w, h = width.value, height.value
            orientation = read_exif_orientation(path)
            rgba, w, h = _apply_orientation(rgba, w, h, orientation)
            frames = max(1, int(frame_count.value))
            return w, h, rgba, frames
        finally:
            for obj in reversed(objects):
                try:
                    if obj:
                        _com_method(obj, 2, ctypes.c_ulong)(obj)
                except Exception:
                    pass
            if coinit >= 0:
                ole32.CoUninitialize()


# ---------------------------------------------------------------------------
# 图像加载
# ---------------------------------------------------------------------------

def _count_alpha(rgba: bytes) -> tuple[int, int]:
    """返回 (不透明像素数, 半透明像素数)。"""
    opaque = 0
    translucent = 0
    alpha = memoryview(rgba)[3::4]
    for value in alpha:
        if value:
            opaque += 1
            if value < 255:
                translucent += 1
    return opaque, translucent


_PILLOW_LOAD_LOCK = threading.RLock()


def _identity_exif_transpose(image, in_place: bool = False):
    return image


def _load_with_pillow(path: Path) -> BitmapImage:
    assert Image is not None and ImageOps is not None
    with Image.open(path) as source:
        image_format = source.format or path.suffix.lstrip(".").upper() or "IMAGE"
        frames = max(1, int(getattr(source, "n_frames", 1) or 1))
        if source.size[0] * source.size[1] > MAX_PIXELS:
            raise ImageLoadError(
                f"图像过大（{source.size[0]} × {source.size[1]}），"
                "超出可处理范围。"
            )
        raw_size = None
        if image_format == "TIFF":
            try:
                raw_size = (
                    int(source.tag_v2[256]),
                    int(source.tag_v2[257]),
                )
            except Exception:
                raw_size = None
        try:
            source.seek(0)
        except Exception:
            pass

        if image_format == "TIFF" and raw_size is not None:
            # Pillow 12 的 TIFF 加载器会在 load_end 里自动做 EXIF 旋转，
            # 但 RGBA / L 等模式在 90° / 270° 时会得到错误结果。
            # 这里在解码阶段临时关闭它的自动旋转，拿到原始像素后，
            # 由 _apply_orientation 统一只旋转一次。
            try:
                orientation = int(source.tag_v2.get(274, 1))
            except Exception:
                try:
                    orientation = int(source.getexif().get(0x0112, 1))
                except Exception:
                    orientation = 1
            original_transpose = ImageOps.exif_transpose
            with _PILLOW_LOAD_LOCK:
                ImageOps.exif_transpose = _identity_exif_transpose
                try:
                    # load_end 会根据 tag 交换 size；先恢复成原始 TIFF
                    # 尺寸，确保解码缓冲区与尺寸一致，再统一旋转。
                    source._size = raw_size  # type: ignore[attr-defined]
                    source.load()
                    rgba_image = source.convert("RGBA")
                finally:
                    ImageOps.exif_transpose = original_transpose
            width, height = rgba_image.size
            rgba = rgba_image.tobytes()
            if orientation > 1:
                rgba, width, height = _apply_orientation(
                    rgba, width, height, orientation
                )
                rgba_image = Image.frombytes(
                    "RGBA", (width, height), rgba
                )
        else:
            try:
                transposed = ImageOps.exif_transpose(source)
            except Exception:
                transposed = source
            rgba_image = transposed.convert("RGBA")
            width, height = rgba_image.size
            rgba = rgba_image.tobytes()

        if width <= 0 or height <= 0:
            raise ImageLoadError("图像尺寸无效。")
        if width * height > MAX_PIXELS:
            raise ImageLoadError(
                f"图像过大（{width} × {height}），超出可处理范围。"
            )
        histogram = rgba_image.getchannel("A").histogram()
        total = width * height
        opaque = total - histogram[0]
        translucent = total - histogram[0] - histogram[255]
    return BitmapImage(
        width=width,
        height=height,
        rgba=rgba,
        source=path,
        format=image_format.upper(),
        frames=frames,
        opaque_count=opaque,
        translucent_count=translucent,
        backend="Pillow",
    )


def _load_with_wic(path: Path) -> BitmapImage:
    if not IS_WINDOWS:
        raise ImageLoadError("WIC 解码仅在 Windows 上可用。")
    width, height, rgba, frames = _wic_decode(path)
    opaque, translucent = _count_alpha(rgba)
    return BitmapImage(
        width=width,
        height=height,
        rgba=rgba,
        source=path,
        format=path.suffix.lstrip(".").upper() or "IMAGE",
        frames=frames,
        opaque_count=opaque,
        translucent_count=translucent,
        backend="WIC",
    )


def load_bitmap(path: str | Path, backend: str = "auto") -> BitmapImage:
    """读取位图并返回 RGBA 数据。

    backend: "auto" | "pillow" | "wic"
    """
    source = Path(path).expanduser()
    try:
        source = source.resolve()
    except OSError:
        pass
    if not source.exists():
        raise ImageLoadError(f"文件不存在：{source}")
    if not source.is_file():
        raise ImageLoadError(f"不是文件：{source}")
    if backend not in ("auto", "pillow", "wic"):
        raise ValueError("backend 必须是 auto、pillow 或 wic")
    if backend == "pillow" and not HAS_PILLOW:
        raise ImageLoadError(
            "当前 Python 没有安装 Pillow，无法使用 pillow 后端。\n\n"
            "请先安装：\n\n    python -m pip install Pillow\n\n"
            "或改用 --backend auto / --backend wic。"
        )
    if backend == "wic" and not IS_WINDOWS:
        raise ImageLoadError(
            "WIC 解码器只存在于 Windows 系统中。\n\n"
            "请安装 Pillow 后使用 --backend pillow 或留空使用 auto。"
        )

    errors: list[Exception] = []
    if backend in ("auto", "pillow") and HAS_PILLOW:
        try:
            # TIFF 分支会临时替换 Pillow 的 EXIF 自动旋转；串行化所有
            # Pillow 解码，避免另一个线程在补丁生效期间解码别的格式。
            with _PILLOW_LOAD_LOCK:
                return _load_with_pillow(source)
        except ImageLoadError:
            raise
        except Exception as exc:
            errors.append(exc)
            if backend == "pillow":
                raise ImageLoadError(f"无法读取图片：{exc}") from exc

    if backend in ("auto", "wic") and IS_WINDOWS:
        try:
            return _load_with_wic(source)
        except Exception as exc:
            errors.append(exc)
            if backend == "wic":
                raise ImageLoadError(f"无法读取图片：{exc}") from exc

    if not HAS_PILLOW and not IS_WINDOWS:
        raise ImageLoadError(
            "缺少图像解码能力。请先安装 Pillow：\n\n"
            "    python -m pip install Pillow"
        )
    detail = str(errors[-1]) if errors else "不支持的格式或文件已损坏"
    raise ImageLoadError(
        f"无法读取这张图片。\n\n{detail}\n\n"
        "可以尝试将其另存为 PNG 后再打开。"
    )


# ---------------------------------------------------------------------------
# SVG 导出
# ---------------------------------------------------------------------------

def human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def estimate_svg_size(
    image: BitmapImage,
    mode: str = "strict",
    color_count: Optional[int] = None,
) -> int:
    """估算 SVG 文件大小，用于界面提示。"""
    if mode == "compact":
        colors = max(1, color_count or 1)
        return 1200 + image.opaque_count * 12 + colors * 95
    translucent = min(image.translucent_count, image.opaque_count)
    return 900 + image.opaque_count * 57 + translucent * 26


def crop_bitmap(
    image: BitmapImage,
    box: tuple[int, int, int, int],
) -> BitmapImage:
    """从图中裁出 box=(left, top, right, bottom)，坐标为半开区间。

    返回的图像坐标从 (0, 0) 开始，并在 region 中记录它在原图中的位置。
    """
    left, top, right, bottom = (int(value) for value in box)
    left = max(0, min(image.width, left))
    top = max(0, min(image.height, top))
    right = max(left, min(image.width, right))
    bottom = max(top, min(image.height, bottom))
    if right <= left or bottom <= top:
        raise ValueError("裁剪区域为空")
    if left == 0 and top == 0 and right == image.width and bottom == image.height:
        return image

    width, height = right - left, bottom - top
    stride = image.width * 4
    row_bytes = width * 4
    out = bytearray(row_bytes * height)
    for y in range(height):
        source_start = (top + y) * stride + left * 4
        target_start = y * row_bytes
        out[target_start:target_start + row_bytes] = image.rgba[
            source_start:source_start + row_bytes
        ]
    rgba = bytes(out)
    opaque, translucent = _count_alpha(rgba)
    if image.region is None:
        region = (left, top, width, height)
    else:
        region = (
            image.region[0] + left,
            image.region[1] + top,
            width,
            height,
        )
    return BitmapImage(
        width=width,
        height=height,
        rgba=rgba,
        source=image.source,
        format=image.format,
        frames=image.frames,
        opaque_count=opaque,
        translucent_count=translucent,
        backend=image.backend,
        region=region,
    )


def count_visible_colors(
    image: BitmapImage, limit: int = MAX_COMPACT_COLORS + 1
) -> Optional[int]:
    """统计非透明像素的颜色数（RGBA 完全相同算一种）。

    超过 limit 时返回 None。Pillow 可用时走 C 层 getcolors，速度很快。
    """
    limit = max(1, int(limit))
    if HAS_PILLOW:
        try:
            assert Image is not None
            pixels = Image.frombytes(
                "RGBA", (image.width, image.height), image.rgba
            )
            colors = pixels.getcolors(maxcolors=limit)
            if colors is None:
                return None
            return sum(1 for _count, color in colors if color[3] != 0)
        except Exception:
            pass
    seen: set[int] = set()
    rgba = image.rgba
    for offset in range(0, len(rgba), 4):
        alpha = rgba[offset + 3]
        if alpha == 0:
            continue
        key = (
            (rgba[offset] << 24)
            | (rgba[offset + 1] << 16)
            | (rgba[offset + 2] << 8)
            | alpha
        )
        seen.add(key)
        if len(seen) > limit:
            return None
    return len(seen)


def _opacity_text(alpha: int) -> str:
    text = f"{alpha / 255:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _collect_compact_positions(
    image: BitmapImage,
    progress: Optional[Callable[[int, int], None]],
    cancel: Optional[Callable[[], bool]],
) -> tuple[dict[int, array], int]:
    """按 RGBA 对像素分组，每个值记录 y*width+x 的位置。

    位置用 array('I') 存储，避免 Python int 列表带来的额外内存。
    颜色超过 MAX_COMPACT_COLORS 时抛出 CompactColorLimitError。
    """
    width = image.width
    rgba = image.rgba
    groups: dict[int, array] = {}
    total_steps = image.height + image.opaque_count
    for y in range(image.height):
        if cancel is not None and cancel():
            raise ExportCancelled("导出已取消")
        row_start = y * width * 4
        for x in range(width):
            offset = row_start + x * 4
            alpha = rgba[offset + 3]
            if alpha == 0:
                continue
            key = (
                (rgba[offset] << 24)
                | (rgba[offset + 1] << 16)
                | (rgba[offset + 2] << 8)
                | alpha
            )
            bucket = groups.get(key)
            if bucket is None:
                if len(groups) >= MAX_COMPACT_COLORS:
                    raise CompactColorLimitError(
                        f"颜色超过 {MAX_COMPACT_COLORS} 种，"
                        "紧凑路径模式不再划算"
                    )
                bucket = array("I")
                groups[key] = bucket
            bucket.append(y * width + x)
        if progress is not None and (y % 8 == 0 or y + 1 == image.height):
            progress(y + 1, total_steps)
    return groups, total_steps


def export_svg(
    image: BitmapImage,
    path: str | Path,
    scale: int = 1,
    progress: Optional[Callable[[int, int], None]] = None,
    cancel: Optional[Callable[[], bool]] = None,
    mode: str = "strict",
) -> ExportResult:
    """把图片逐像素写成一个 SVG 文件。

    mode="strict" : 每个非全透明像素生成一个独立的 1×1 <rect>（默认）。
    mode="compact": 每种 RGBA 生成一条 <path>，路径由多个 1×1
                    正方形子路径组成；视觉结果相同，颜色少的图像更小。

    progress(done, total) 是单调递增的工作量回调；cancel() 返回 True 时取消。
    """
    import time

    if mode not in ("strict", "compact"):
        raise ValueError("mode 必须是 strict 或 compact")
    destination = Path(path).expanduser()
    if destination.suffix.lower() != ".svg":
        destination = destination.with_suffix(".svg")
    if not (1 <= int(scale) <= 100):
        raise ValueError("显示倍数必须在 1 到 100 之间")
    scale = int(scale)
    started = time.perf_counter()

    destination.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=f".{destination.stem}.", suffix=".tmp", dir=destination.parent
    )
    temp_path = Path(temp_name)
    rect_count = 0
    color_paths = 0
    width, height = image.width, image.height
    display_width = width * scale
    display_height = height * scale
    stride = width * 4
    rgba = image.rgba
    title = html.escape(f"{image.source.name} · 像素矢量")
    region_text = ""
    if image.region is not None:
        left, top, region_w, region_h = image.region
        region_text = (
            f"选区 {left},{top} 起，{region_w}×{region_h} 像素；"
        )
    if mode == "compact":
        description = html.escape(
            f"Pixel2SVG 紧凑逐像素转换：{region_text}"
            f"{image.opaque_count} 个不透明或半透明像素，每种 RGBA 颜色"
            "生成一条 path，路径由多个 1×1 正方形子路径组成；"
            "透明像素保持透明。"
        )
    else:
        description = html.escape(
            f"Pixel2SVG 严格逐像素转换：{region_text}"
            f"{image.opaque_count} 个不透明或半透明像素，每个对应一个 "
            "1×1 矢量正方形；透明像素保持透明。"
        )

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write('<?xml version="1.0" encoding="UTF-8"?>\n')
            handle.write(
                f'<svg xmlns="http://www.w3.org/2000/svg" '
                f'width="{display_width}" height="{display_height}" '
                f'viewBox="0 0 {width} {height}" version="1.1" '
                f'data-pixel-mode="{mode}" '
                f'shape-rendering="crispEdges">\n'
            )
            handle.write(f"  <title>{title}</title>\n")
            handle.write(f"  <desc>{description}</desc>\n")
            handle.write('  <g id="pixels">\n')

            if mode == "strict":
                for y in range(height):
                    if cancel is not None and cancel():
                        raise ExportCancelled("导出已取消")
                    row_start = y * stride
                    parts: list[str] = []
                    for x in range(width):
                        offset = row_start + x * 4
                        alpha = rgba[offset + 3]
                        if alpha == 0:
                            continue
                        red = rgba[offset]
                        green = rgba[offset + 1]
                        blue = rgba[offset + 2]
                        color = f"#{red:02x}{green:02x}{blue:02x}"
                        if alpha == 255:
                            parts.append(
                                f'<rect x="{x}" y="{y}" width="1" '
                                f'height="1" fill="{color}"/>'
                            )
                        else:
                            parts.append(
                                f'<rect x="{x}" y="{y}" width="1" '
                                f'height="1" fill="{color}" '
                                f'fill-opacity="{_opacity_text(alpha)}"/>'
                            )
                        rect_count += 1
                    if parts:
                        handle.write("    " + "".join(parts) + "\n")
                    if progress is not None and (
                        y % 8 == 0 or y + 1 == height
                    ):
                        progress(y + 1, height)
            else:
                groups, total_steps = _collect_compact_positions(
                    image, progress, cancel
                )
                color_paths = len(groups)
                written = 0
                for key in sorted(groups):
                    if cancel is not None and cancel():
                        raise ExportCancelled("导出已取消")
                    red = (key >> 24) & 0xFF
                    green = (key >> 16) & 0xFF
                    blue = (key >> 8) & 0xFF
                    alpha = key & 0xFF
                    color = f"#{red:02x}{green:02x}{blue:02x}"
                    opacity = (
                        ""
                        if alpha == 255
                        else f' fill-opacity="{_opacity_text(alpha)}"'
                    )
                    handle.write(
                        f'    <path fill="{color}"{opacity} d="'
                    )
                    parts = []
                    for position in groups[key]:
                        x = position % width
                        y = position // width
                        parts.append(f"M{x} {y}h1v1h-1z")
                        written += 1
                        if len(parts) >= 4096:
                            handle.write("".join(parts))
                            parts.clear()
                            if cancel is not None and cancel():
                                raise ExportCancelled("导出已取消")
                            if progress is not None:
                                progress(
                                    height + written, total_steps
                                )
                    handle.write("".join(parts))
                    handle.write('"/>\n')
                    rect_count = written
                    if progress is not None:
                        progress(height + written, total_steps)

            handle.write("  </g>\n")
            handle.write("</svg>\n")
        os.replace(temp_path, destination)
    except ExportCancelled:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise
    except Exception:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    elapsed = time.perf_counter() - started
    try:
        file_size = destination.stat().st_size
    except OSError:
        file_size = 0
    return ExportResult(
        path=destination,
        rect_count=rect_count,
        file_size=file_size,
        width=width,
        height=height,
        elapsed=elapsed,
        mode=mode,
        color_paths=color_paths,
    )


# ---------------------------------------------------------------------------
# 预览渲染（与 GUI 解耦，方便测试）
# ---------------------------------------------------------------------------

def fit_viewport(
    image_width: int,
    image_height: int,
    canvas_width: int,
    canvas_height: int,
    margin: int = 24,
    max_zoom: float = 32.0,
) -> tuple[float, float, float]:
    """计算适合窗口的缩放与偏移。返回 (zoom, offset_x, offset_y)。"""
    if image_width <= 0 or image_height <= 0:
        return 1.0, 0.0, 0.0
    usable_w = max(1, canvas_width - margin * 2)
    usable_h = max(1, canvas_height - margin * 2)
    zoom = min(usable_w / image_width, usable_h / image_height)
    zoom = max(0.01, min(zoom, max_zoom))
    offset_x = (canvas_width - image_width * zoom) / 2.0
    offset_y = (canvas_height - image_height * zoom) / 2.0
    return zoom, offset_x, offset_y


def _checker_row(
    width: int, start_x: int, start_y: int, cell: int = 12
) -> bytes:
    """生成屏幕空间中的一行棋盘格 RGB 数据。"""
    result = bytearray()
    x = start_x
    remaining = width
    row_parity = (start_y // cell) & 1
    while remaining > 0:
        cell_x = x // cell
        take = min(cell - (x % cell), remaining)
        color = (
            CHECKER_LIGHT
            if ((cell_x + row_parity) & 1) == 0
            else CHECKER_DARK
        )
        result.extend(bytes(color) * take)
        x += take
        remaining -= take
    return bytes(result)


def _render_preview_with_pillow(
    image: BitmapImage,
    canvas_width: int,
    canvas_height: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
) -> bytes:
    assert Image is not None
    rgba = Image.frombytes(
        "RGBA", (image.width, image.height), image.rgba
    )
    canvas = Image.new("RGB", (canvas_width, canvas_height), PREVIEW_BG)

    display_w = image.width * zoom
    display_h = image.height * zoom
    dx0 = max(0, int(math.floor(offset_x)))
    dy0 = max(0, int(math.floor(offset_y)))
    dx1 = min(canvas_width, int(math.ceil(offset_x + display_w)))
    dy1 = min(canvas_height, int(math.ceil(offset_y + display_h)))
    if dx1 > dx0 and dy1 > dy0:
        sx0 = max(0, min(image.width - 1, int((dx0 - offset_x) / zoom)))
        sy0 = max(0, min(image.height - 1, int((dy0 - offset_y) / zoom)))
        sx1 = max(sx0 + 1, min(image.width, math.ceil((dx1 - offset_x) / zoom)))
        sy1 = max(sy0 + 1, min(image.height, math.ceil((dy1 - offset_y) / zoom)))
        crop = rgba.crop((sx0, sy0, sx1, sy1))
        target_w = max(1, dx1 - dx0)
        target_h = max(1, dy1 - dy0)
        resample = (
            Image.Resampling.NEAREST
            if zoom >= 1.0
            else Image.Resampling.LANCZOS
        )
        scaled = crop.resize((target_w, target_h), resample)
        checker_data = b"".join(
            _checker_row(target_w, dx0, dy0 + row)
            for row in range(target_h)
        )
        checker = Image.frombytes("RGB", (target_w, target_h), checker_data)
        checker.paste(scaled, (0, 0), scaled)
        canvas.paste(checker, (dx0, dy0))
    return canvas.tobytes()


def _render_preview_python(
    image: BitmapImage,
    canvas_width: int,
    canvas_height: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
) -> bytes:
    """没有 Pillow 时的纯 Python 最近邻渲染。"""
    output = bytearray(bytes(PREVIEW_BG) * (canvas_width * canvas_height))
    display_w = image.width * zoom
    display_h = image.height * zoom
    dx0 = max(0, int(math.floor(offset_x)))
    dy0 = max(0, int(math.floor(offset_y)))
    dx1 = min(canvas_width, int(math.ceil(offset_x + display_w)))
    dy1 = min(canvas_height, int(math.ceil(offset_y + display_h)))
    if dx1 <= dx0 or dy1 <= dy0:
        return bytes(output)
    stride = image.width * 4
    rgba = image.rgba
    for screen_y in range(dy0, dy1):
        src_y = int((screen_y + 0.5 - offset_y) / zoom)
        if src_y < 0 or src_y >= image.height:
            continue
        src_row = src_y * stride
        checker = _checker_row(dx1 - dx0, dx0, screen_y)
        dest = screen_y * canvas_width * 3 + dx0 * 3
        for screen_x in range(dx0, dx1):
            src_x = int((screen_x + 0.5 - offset_x) / zoom)
            if src_x < 0 or src_x >= image.width:
                continue
            offset = src_row + src_x * 4
            alpha = rgba[offset + 3]
            checker_offset = (screen_x - dx0) * 3
            if alpha == 255:
                output[dest] = rgba[offset]
                output[dest + 1] = rgba[offset + 1]
                output[dest + 2] = rgba[offset + 2]
            elif alpha:
                factor = alpha / 255.0
                inverse = 1.0 - factor
                output[dest] = int(
                    rgba[offset] * factor
                    + checker[checker_offset] * inverse
                )
                output[dest + 1] = int(
                    rgba[offset + 1] * factor
                    + checker[checker_offset + 1] * inverse
                )
                output[dest + 2] = int(
                    rgba[offset + 2] * factor
                    + checker[checker_offset + 2] * inverse
                )
            else:
                output[dest] = checker[checker_offset]
                output[dest + 1] = checker[checker_offset + 1]
                output[dest + 2] = checker[checker_offset + 2]
            dest += 3
    return bytes(output)


def render_preview_ppm(
    image: BitmapImage,
    canvas_width: int,
    canvas_height: int,
    zoom: float,
    offset_x: float,
    offset_y: float,
) -> bytes:
    """渲染带棋盘格背景的预览图，返回完整 PPM 数据（Tk 可直接读取）。"""
    canvas_width = max(1, int(canvas_width))
    canvas_height = max(1, int(canvas_height))
    if image.width <= 0 or image.height <= 0:
        raise ValueError("图像尺寸无效")
    if HAS_PILLOW:
        pixels = _render_preview_with_pillow(
            image,
            canvas_width,
            canvas_height,
            zoom,
            offset_x,
            offset_y,
        )
    else:
        pixels = _render_preview_python(
            image,
            canvas_width,
            canvas_height,
            zoom,
            offset_x,
            offset_y,
        )
    header = f"P6\n{canvas_width} {canvas_height}\n255\n".encode("ascii")
    return header + pixels
