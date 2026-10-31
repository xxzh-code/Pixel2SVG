#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixel2SVG · 像素矢量工坊
========================

把位图逐像素翻译成 SVG。每个不透明像素 = 一个 1×1 矢量正方形，
透明像素保持透明。不做轮廓拟合、颜色聚类或抗锯齿。

用法：
    python pixel2svg.py [图片路径]

无界面导出：
    python pixel2svg.py 图片.png --export 输出.svg [--scale 2]
    python pixel2svg.py 图片.png --export 输出.svg --mode compact
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pixel2svg",
        description="把位图逐像素翻译为 SVG 正方形（默认每个像素一个 1×1 rect）。",
    )
    parser.add_argument(
        "image",
        nargs="?",
        help="打开图片；留空则启动空白界面",
    )
    parser.add_argument(
        "--export",
        metavar="SVG",
        help="不打开界面，直接把图片导出为指定 SVG 文件",
    )
    parser.add_argument(
        "--scale",
        type=int,
        default=1,
        help="SVG 显示倍数，1-100（默认 1）",
    )
    parser.add_argument(
        "--mode",
        choices=("strict", "compact"),
        default="strict",
        help="SVG 结构：strict=每像素一个 rect（默认）；"
        "compact=每色一条 path",
    )
    parser.add_argument(
        "--backend",
        choices=("auto", "pillow", "wic"),
        default="auto",
        help="图像解码后端（默认自动选择）",
    )
    return parser


def headless_export(
    image_path: str,
    output_path: str,
    scale: int,
    backend: str,
    mode: str,
) -> int:
    import pixel2svg_core as core

    try:
        image = core.load_bitmap(image_path, backend=backend)
    except Exception as exc:  # noqa: BLE001
        print(f"读取失败：{exc}", file=sys.stderr)
        return 1
    try:
        result = core.export_svg(
            image, output_path, scale=scale, mode=mode
        )
    except Exception as exc:  # noqa: BLE001
        print(f"导出失败：{exc}", file=sys.stderr)
        return 1
    mode_text = (
        f"紧凑 path / {result.color_paths} 条"
        if result.mode == "compact"
        else "严格 rect"
    )
    print(
        f"已导出 {result.path}  ({result.width} × {result.height} 像素, "
        f"{result.rect_count:,} 个正方形, {mode_text}, "
        f"{core.human_size(result.file_size)}, {result.elapsed:.2f}s)"
    )
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    args = build_parser().parse_args(argv)
    if not 1 <= args.scale <= 100:
        print("--scale 必须在 1 到 100 之间", file=sys.stderr)
        return 2

    if args.export:
        if not args.image:
            print("使用 --export 时必须提供图片路径", file=sys.stderr)
            return 2
        return headless_export(
            args.image, args.export, args.scale, args.backend, args.mode
        )

    if args.image and not Path(args.image).exists():
        print(f"文件不存在：{args.image}", file=sys.stderr)
        return 2

    try:
        import tkinter  # noqa: F401
    except Exception as exc:  # noqa: BLE001
        print(f"无法启动图形界面：{exc}", file=sys.stderr)
        print("请安装带 Tk 的 Python，或使用 --export 命令行模式。", file=sys.stderr)
        return 1

    from pixel2svg_gui import Pixel2SVGApp, setup_dpi

    setup_dpi()
    app = Pixel2SVGApp(initial_image=args.image)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
