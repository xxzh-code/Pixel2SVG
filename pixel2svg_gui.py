#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Pixel2SVG · Tkinter 图形界面

界面刻意保持“像素 / 网格”的视觉语言：硬边框、等宽数字、棋盘格预览。
窗口逻辑与转换逻辑分离，核心转换在 pixel2svg_core.py。
"""

from __future__ import annotations

import ctypes
import math
import os
import queue
import subprocess
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from typing import Optional

import pixel2svg_core as core

APP_TITLE = "Pixel2SVG · 像素矢量工坊"
APP_VERSION = "1.1"

# 色彩系统：暖纸色 + 墨绿 + 珊瑚色
BG = "#F3F0E8"
PANEL = "#FFFFFF"
PANEL_SOFT = "#F7F5EE"
INK = "#13201D"
INK_2 = "#23322E"
MUTED = "#6B7873"
MUTED_LIGHT = "#97A19C"
BORDER = "#DDD7CA"
BORDER_STRONG = "#C8C0B0"
ACCENT = "#0E7C66"
ACCENT_DARK = "#0A5F4E"
ACCENT_SOFT = "#E6F2ED"
ACCENT_LINE = "#C8DED5"
CORAL = "#E45C3A"
CORAL_DARK = "#C44728"
GOLD = "#C69A3F"
ERROR = "#B94332"

FONT_UI = "Microsoft YaHei UI"
FONT_MONO = "Consolas"

SCALE_FACTOR = 1.0
SYSTEM_DPI = 96.0


def px(value: float) -> int:
    return int(round(value * SCALE_FACTOR))


def setup_dpi() -> None:
    """在创建 Tk 根窗口之前启用 DPI 感知并读取系统 DPI。"""
    global SCALE_FACTOR, SYSTEM_DPI
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass
        try:
            SYSTEM_DPI = float(ctypes.windll.user32.GetDpiForSystem())
        except Exception:
            try:
                hdc = ctypes.windll.user32.GetDC(0)
                SYSTEM_DPI = float(
                    ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)
                )
                ctypes.windll.user32.ReleaseDC(0, hdc)
            except Exception:
                SYSTEM_DPI = 96.0
    SCALE_FACTOR = max(0.8, min(3.0, SYSTEM_DPI / 96.0))


def shorten_middle(text: str, limit: int = 42) -> str:
    text = str(text)
    if len(text) <= limit:
        return text
    keep = max(4, limit - 1)
    left = keep // 2
    right = keep - left
    return text[:left] + "…" + text[-right:]


def make_app_icon() -> tk.PhotoImage:
    """用 PPM 数据生成 32×32 像素图标。"""
    pattern = [
        "1111100000000000",
        "1000010000000000",
        "1000010000000000",
        "1000010000000000",
        "1111100000000000",
        "1000000000000000",
        "1000000000000000",
        "1000000000000000",
        "1000000000000000",
        "0000000000000000",
    ]
    size = 32
    rows = []
    for y in range(size):
        row = bytearray()
        for x in range(size):
            sy = min(len(pattern) - 1, y // 2)
            sx = min(15, x // 2)
            if sy < len(pattern) and pattern[sy][sx] == "1":
                color = (14, 124, 102)
            else:
                color = (19, 32, 29)
            if 22 <= x <= 27 and 6 <= y <= 11:
                color = (228, 92, 58)
            row.extend(color)
        rows.append(bytes(row))
    data = (
        f"P6\n{size} {size}\n255\n".encode("ascii") + b"".join(rows)
    )
    return tk.PhotoImage(data=data, format="PPM")


def is_descendant(widget, ancestor) -> bool:
    current = widget
    while current is not None:
        if current is ancestor:
            return True
        try:
            current = current.master
        except Exception:
            return False
    return False


class ScrollFrame(tk.Frame):
    """带自动隐藏滚动条的左栏容器。"""

    def __init__(self, parent, bg: str, width: int):
        super().__init__(parent, bg=bg, width=width)
        self.pack_propagate(False)
        self.canvas = tk.Canvas(
            self, bg=bg, highlightthickness=0, bd=0, width=width
        )
        self.inner = tk.Frame(self.canvas, bg=bg)
        self.window = self.canvas.create_window(
            (0, 0), window=self.inner, anchor="nw"
        )
        self.scrollbar = ttk.Scrollbar(
            self,
            orient="vertical",
            command=self.canvas.yview,
            style="P2S.Vertical.TScrollbar",
        )
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self._scrollbar_visible = False
        self.inner.bind("<Configure>", self._refresh)
        self.canvas.bind("<Configure>", self._refresh)

    def _refresh(self, _event=None) -> None:
        self.canvas.configure(scrollregion=self.canvas.bbox("all"))
        width = self.canvas.winfo_width()
        if width > 1:
            self.canvas.itemconfigure(self.window, width=width)
        needed = self.inner.winfo_reqheight() > self.canvas.winfo_height() + 1
        if needed and not self._scrollbar_visible:
            self.scrollbar.pack(
                side="right", fill="y", before=self.canvas
            )
            self._scrollbar_visible = True
        elif not needed and self._scrollbar_visible:
            self.scrollbar.pack_forget()
            self._scrollbar_visible = False

    def scroll_wheel(self, event) -> None:
        if self.inner.winfo_reqheight() <= self.canvas.winfo_height():
            return
        delta = getattr(event, "delta", 0)
        number = getattr(event, "num", 0)
        if number in (4, 5):
            steps = -3 if number == 4 else 3
        elif delta:
            steps = int(-delta / 120)
            if steps == 0:
                steps = -1 if delta > 0 else 1
        else:
            return
        self.canvas.yview_scroll(steps, "units")

    def reset_scroll(self) -> None:
        self.canvas.yview_moveto(0)


class ExportProgressDialog(tk.Toplevel):
    def __init__(self, parent, path: Path, total_rects: int, on_cancel):
        super().__init__(parent)
        self.root = parent
        self.on_cancel = on_cancel
        self.cancelled = False
        self.title("正在生成 SVG")
        self.configure(bg=PANEL)
        self.resizable(False, False)
        self.transient(parent)
        self.protocol("WM_DELETE_WINDOW", self.cancel)
        width, height = px(470), px(236)
        self.geometry(self._center_geometry(parent, width, height))
        try:
            icon = getattr(parent, "_app_icon", None)
            if icon is not None:
                self.iconphoto(False, icon)
        except Exception:
            pass

        header = tk.Frame(self, bg=INK, height=px(54))
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="正在生成 SVG",
            bg=INK,
            fg="#F6F2E9",
            font=(FONT_UI, 12, "bold"),
        ).pack(side="left", padx=px(20))
        tk.Label(
            header,
            text="STRICT PIXEL MODE",
            bg=INK,
            fg="#8FB7AA",
            font=(FONT_MONO, 8),
        ).pack(side="right", padx=px(20))

        body = tk.Frame(self, bg=PANEL)
        body.pack(fill="both", expand=True, padx=px(20), pady=px(16))
        tk.Label(
            body,
            text=shorten_middle(str(path), 54),
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            font=(FONT_MONO, 9),
        ).pack(fill="x")
        self.detail_var = tk.StringVar(
            value=f"0 / {total_rects:,} 个正方形"
        )
        tk.Label(
            body,
            textvariable=self.detail_var,
            bg=PANEL,
            fg=INK,
            anchor="w",
            font=(FONT_UI, 10),
        ).pack(fill="x", pady=(px(10), px(6)))
        self.bar = ttk.Progressbar(
            body,
            style="P2S.Horizontal.TProgressbar",
            mode="determinate",
            maximum=100,
            value=0,
        )
        self.bar.pack(fill="x")
        footer = tk.Frame(body, bg=PANEL)
        footer.pack(fill="x", pady=(px(12), 0))
        self.percent_var = tk.StringVar(value="0%")
        tk.Label(
            footer,
            textvariable=self.percent_var,
            bg=PANEL,
            fg=ACCENT,
            font=(FONT_MONO, 11, "bold"),
        ).pack(side="left")
        self.cancel_button = tk.Button(
            footer,
            text="取消",
            command=self.cancel,
            bg=PANEL_SOFT,
            fg=INK,
            activebackground="#EAE6DC",
            activeforeground=INK,
            font=(FONT_UI, 9),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(14),
            pady=px(5),
            cursor="hand2",
        )
        self.cancel_button.pack(side="right")
        self.bind("<Escape>", lambda _event: self.cancel())
        self.update_idletasks()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    @staticmethod
    def _center_geometry(parent, width: int, height: int) -> str:
        x = parent.winfo_rootx() + (parent.winfo_width() - width) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - height) // 2
        return f"{width}x{height}+{max(0, x)}+{max(0, y)}"

    def set_progress(self, fraction: float, done_rects: int) -> None:
        fraction = max(0.0, min(1.0, fraction))
        self.bar["value"] = fraction * 100.0
        self.percent_var.set(f"{fraction * 100:.0f}%")
        self.detail_var.set(
            f"{done_rects:,} 个正方形 / 约 {fraction * 100:.0f}%"
        )

    def set_indeterminate_note(self, text: str) -> None:
        self.detail_var.set(text)

    def cancel(self) -> None:
        if self.cancelled:
            return
        self.cancelled = True
        self.cancel_button.configure(state="disabled")
        self.detail_var.set("正在取消，等待当前行写完…")
        try:
            self.on_cancel()
        except Exception:
            pass

    def close(self) -> None:
        try:
            self.grab_release()
        except tk.TclError:
            pass
        self.destroy()


class Pixel2SVGApp:
    def __init__(self, initial_image: Optional[str] = None):
        self.root = tk.Tk()
        self.root.title(APP_TITLE)
        self.root.configure(bg=BG)
        try:
            self.root.tk.call("tk", "scaling", SYSTEM_DPI / 72.0)
        except tk.TclError:
            pass
        self._app_icon = make_app_icon()
        try:
            self.root.iconphoto(True, self._app_icon)
        except tk.TclError:
            pass

        self.image: Optional[core.BitmapImage] = None
        self.source_path: Optional[Path] = None
        self.output_path: Optional[Path] = None
        self.last_output: Optional[Path] = None
        self.zoom = 1.0
        self.offset_x = 0.0
        self.offset_y = 0.0
        self.fit_mode = True
        self._preview_photo: Optional[tk.PhotoImage] = None
        self._thumb_photo: Optional[tk.PhotoImage] = None
        self._preview_job: Optional[str] = None
        self._thumb_job: Optional[str] = None
        self._toast_job: Optional[str] = None
        self._toast_label: Optional[tk.Label] = None
        self._drag_start = None
        self._dragged = False
        self.crop_box: Optional[tuple[int, int, int, int]] = None
        self.cropped_image: Optional[core.BitmapImage] = None
        self._crop_anchor: Optional[tuple[int, int]] = None
        self._crop_current: Optional[tuple[int, int]] = None
        self._crop_dragging = False
        self._color_count_cache: Optional[int] = None
        self._color_count_ready = False
        self.loading = False
        self.exporting = False
        self._pending_close = False
        self._load_seq = 0
        self._load_queue: queue.Queue = queue.Queue()
        self._export_queue: queue.Queue = queue.Queue()
        self._export_cancel: Optional[threading.Event] = None
        self._progress_dialog: Optional[ExportProgressDialog] = None
        self._export_buttons: list[tk.Button] = []

        self.scale_var = tk.StringVar(value="1")
        self.scale_var.trace_add("write", self._on_scale_changed)
        self.display_size_var = tk.StringVar(value="SVG 尺寸 —")
        self.name_var = tk.StringVar(value="还没有打开图片")
        self.meta_var = tk.StringVar(value="支持 PNG / JPG / BMP / GIF / WebP / TIFF")
        self.output_path_var = tk.StringVar(value="导出时选择位置")
        self.rect_var = tk.StringVar(value="—")
        self.alpha_var = tk.StringVar(value="—")
        self.size_var = tk.StringVar(value="—")
        self.zoom_var = tk.StringVar(value="—")
        self.grid_var = tk.BooleanVar(value=True)
        self.crop_mode_var = tk.BooleanVar(value=False)
        self.mode_var = tk.StringVar(value="strict")
        self.mode_note_var = tk.StringVar(
            value="不合并同色像素 · 不做轮廓拟合 · 不使用抗锯齿"
        )
        self.status_var = tk.StringVar(value="就绪 · 打开一张位图开始翻译")

        self._configure_styles()
        self._build_window()
        self._bind_events()
        self._set_status("就绪 · 打开一张位图开始翻译", "ready")
        self._refresh_action_states()
        self._draw_empty_preview()

        if initial_image:
            self.root.after(
                180, lambda: self._begin_load(Path(initial_image))
            )

    # ------------------------------------------------------------------
    # 样式与布局
    # ------------------------------------------------------------------

    def _configure_styles(self) -> None:
        style = ttk.Style(self.root)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure(
            "P2S.Horizontal.TProgressbar",
            troughcolor="#E8E3D8",
            background=ACCENT,
            bordercolor="#E8E3D8",
            lightcolor=ACCENT,
            darkcolor=ACCENT,
            thickness=px(9),
        )
        style.configure(
            "P2S.Vertical.TScrollbar",
            background="#D8D2C4",
            troughcolor=BG,
            bordercolor=BG,
            arrowcolor=MUTED,
            lightcolor="#D8D2C4",
            darkcolor="#D8D2C4",
            arrowsize=px(12),
        )
        style.configure(
            "P2S.TSpinbox",
            fieldbackground=PANEL_SOFT,
            background=PANEL_SOFT,
            foreground=INK,
            bordercolor=BORDER,
            lightcolor=BORDER,
            darkcolor=BORDER,
            arrowcolor=MUTED,
            padding=px(3),
        )

    def _build_window(self) -> None:
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        width = min(px(1280), max(px(960), screen_w - px(48)))
        height = min(px(900), max(px(700), screen_h - px(90)))
        x = max(0, (screen_w - width) // 2)
        y = max(0, (screen_h - height) // 2 - px(12))
        self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.root.minsize(
            min(px(1080), screen_w - px(32)),
            min(px(740), screen_h - px(48)),
        )
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self._build_header()
        body = tk.Frame(self.root, bg=BG)
        body.pack(fill="both", expand=True, padx=px(22), pady=(px(16), px(8)))
        self.sidebar = ScrollFrame(body, bg=BG, width=px(368))
        self.sidebar.pack(side="left", fill="y")
        self.preview_area = tk.Frame(body, bg=BG)
        self.preview_area.pack(
            side="left", fill="both", expand=True, padx=(px(16), 0)
        )
        self._build_sidebar(self.sidebar.inner)
        self._build_preview(self.preview_area)
        self._build_footer()

    def _build_header(self) -> None:
        header = tk.Frame(self.root, bg=INK, height=px(84))
        header.pack(fill="x")
        header.pack_propagate(False)
        inner = tk.Frame(header, bg=INK)
        inner.pack(fill="both", expand=True, padx=px(24), pady=px(14))

        logo = tk.Canvas(
            inner,
            width=px(44),
            height=px(44),
            bg=INK,
            highlightthickness=0,
        )
        logo.pack(side="left", pady=px(2))
        pattern = [
            "1111000",
            "1000100",
            "1000100",
            "1111000",
            "1000000",
            "1000000",
            "1000000",
        ]
        cell = px(5)
        gap = px(1)
        for row, bits in enumerate(pattern):
            for col, bit in enumerate(bits):
                if bit != "1":
                    continue
                x = col * (cell + gap)
                y = row * (cell + gap)
                logo.create_rectangle(
                    x, y, x + cell, y + cell, fill=ACCENT, outline=""
                )
        logo.create_rectangle(
            px(36), px(2), px(41), px(7), fill=CORAL, outline=""
        )

        titles = tk.Frame(inner, bg=INK)
        titles.pack(side="left", padx=(px(12), 0))
        tk.Label(
            titles,
            text="Pixel2SVG",
            bg=INK,
            fg="#F8F4EC",
            font=(FONT_MONO, 19, "bold"),
        ).pack(anchor="w")
        tk.Label(
            titles,
            text="像素矢量工坊   /   一个像素，一个正方形",
            bg=INK,
            fg="#91A69F",
            font=(FONT_UI, 9),
        ).pack(anchor="w", pady=(px(1), 0))

        about = tk.Button(
            inner,
            text="?",
            command=self._show_about,
            bg=INK_2,
            fg="#B8C8C2",
            activebackground="#33443E",
            activeforeground="#FFFFFF",
            font=(FONT_UI, 10, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(11),
            pady=px(7),
            cursor="hand2",
        )
        about.pack(side="right")
        export_button = tk.Button(
            inner,
            text="导出 SVG",
            command=self.export_svg,
            bg=CORAL,
            fg="#FFFFFF",
            activebackground=CORAL_DARK,
            activeforeground="#FFFFFF",
            disabledforeground="#B9AAA4",
            font=(FONT_UI, 10, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(18),
            pady=px(8),
            cursor="hand2",
        )
        export_button.pack(side="right", padx=(0, px(10)))
        open_button = tk.Button(
            inner,
            text="打开图片",
            command=self.open_image_dialog,
            bg=INK_2,
            fg="#F0ECE3",
            activebackground="#33443E",
            activeforeground="#FFFFFF",
            font=(FONT_UI, 10),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(18),
            pady=px(8),
            cursor="hand2",
        )
        open_button.pack(side="right", padx=(0, px(10)))
        self._header_export_button = export_button
        self._header_open_button = open_button
        self._export_buttons.append(export_button)

    def _build_sidebar(self, parent: tk.Frame) -> None:
        self._build_source_card(parent)
        self._build_output_card(parent)
        self._build_export_card(parent)

    def _card(self, parent, bg: str = PANEL, border: str = BORDER):
        return tk.Frame(
            parent, bg=bg, highlightbackground=border, highlightthickness=1
        )

    def _section(
        self,
        parent,
        number: str,
        title: str,
        tag: str = "",
        on_soft: bool = False,
    ):
        row = tk.Frame(parent, bg=parent.cget("bg"))
        row.pack(fill="x")
        chip_bg = "#D7E9E2" if on_soft else ACCENT_SOFT
        tk.Label(
            row,
            text=number,
            bg=chip_bg,
            fg=ACCENT_DARK,
            font=(FONT_MONO, 8, "bold"),
            padx=px(6),
            pady=px(2),
        ).pack(side="left")
        tk.Label(
            row,
            text=title,
            bg=parent.cget("bg"),
            fg=INK,
            font=(FONT_UI, 11, "bold"),
        ).pack(side="left", padx=(px(8), 0))
        if tag:
            tk.Label(
                row,
                text=tag,
                bg=parent.cget("bg"),
                fg=MUTED_LIGHT,
                font=(FONT_MONO, 7),
            ).pack(side="right")
        return row

    def _link(self, parent, text: str, command, bg: str = PANEL):
        label = tk.Label(
            parent,
            text=text,
            bg=bg,
            fg=ACCENT,
            font=(FONT_UI, 9),
            cursor="hand2",
        )
        label.bind("<Button-1>", lambda _event: command())
        label.bind(
            "<Enter>", lambda _event: label.configure(fg=ACCENT_DARK)
        )
        label.bind("<Leave>", lambda _event: label.configure(fg=ACCENT))
        return label

    def _build_source_card(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(0, px(12)))
        box = tk.Frame(card, bg=PANEL)
        box.pack(fill="x", padx=px(15), pady=px(13))
        self._section(box, "01", "源图像", "BITMAP")

        self.source_canvas = tk.Canvas(
            box,
            height=px(124),
            bg="#18221F",
            highlightthickness=1,
            highlightbackground=BORDER_STRONG,
            highlightcolor=ACCENT,
            bd=0,
            cursor="hand2",
        )
        self.source_canvas.pack(fill="x", pady=(px(10), px(9)))
        self.source_canvas.bind("<Configure>", self._schedule_thumb)
        self.source_canvas.bind("<Button-1>", lambda _event: self.open_image_dialog())
        self.source_canvas.bind(
            "<Enter>", lambda _event: self.source_canvas.configure(
                highlightbackground=ACCENT
            )
        )
        self.source_canvas.bind(
            "<Leave>", lambda _event: self.source_canvas.configure(
                highlightbackground=BORDER_STRONG
            )
        )

        name_row = tk.Frame(box, bg=PANEL)
        name_row.pack(fill="x")
        tk.Label(
            name_row,
            textvariable=self.name_var,
            bg=PANEL,
            fg=INK,
            anchor="w",
            font=(FONT_UI, 10, "bold"),
        ).pack(side="left", fill="x", expand=True)
        self.change_link = self._link(
            name_row, "更换", self.open_image_dialog
        )
        self.change_link.pack(side="right", padx=(px(8), 0))
        tk.Label(
            box,
            textvariable=self.meta_var,
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=px(310),
            font=(FONT_MONO, 8),
        ).pack(fill="x", pady=(px(4), 0))

    def _build_output_card(self, parent: tk.Frame) -> None:
        card = self._card(parent)
        card.pack(fill="x", pady=(0, px(12)))
        box = tk.Frame(card, bg=PANEL)
        box.pack(fill="x", padx=px(15), pady=px(13))
        self._section(box, "02", "输出规则", "MODE")

        rule = tk.Frame(box, bg=PANEL_SOFT)
        rule.pack(fill="x", pady=(px(10), 0))
        rule_inner = tk.Frame(rule, bg=PANEL_SOFT)
        rule_inner.pack(fill="x", padx=px(10), pady=px(8))
        marker = tk.Canvas(
            rule_inner,
            width=px(13),
            height=px(13),
            bg=PANEL_SOFT,
            highlightthickness=0,
        )
        marker.pack(side="left")
        marker.create_rectangle(
            px(1),
            px(1),
            px(12),
            px(12),
            fill=ACCENT,
            outline=ACCENT_DARK,
        )
        tk.Label(
            rule_inner,
            text="1 像素   →   1 个 1×1 矢量正方形",
            bg=PANEL_SOFT,
            fg=INK,
            font=(FONT_UI, 9, "bold"),
        ).pack(side="left", padx=(px(7), 0))
        tk.Label(
            box,
            textvariable=self.mode_note_var,
            bg=PANEL,
            fg=MUTED,
            anchor="w",
            justify="left",
            wraplength=px(315),
            font=(FONT_UI, 8),
        ).pack(fill="x", pady=(px(6), px(10)))

        scale_row = tk.Frame(box, bg=PANEL)
        scale_row.pack(fill="x")
        tk.Label(
            scale_row,
            text="SVG 显示倍数",
            bg=PANEL,
            fg=INK,
            font=(FONT_UI, 9),
        ).pack(side="left")
        spin = tk.Spinbox(
            scale_row,
            from_=1,
            to=100,
            textvariable=self.scale_var,
            width=5,
            font=(FONT_MONO, 9),
            bg=PANEL_SOFT,
            fg=INK,
            buttonbackground="#E9E4D8",
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=BORDER,
            highlightcolor=ACCENT,
            justify="center",
            command=self._on_scale_changed,
        )
        spin.pack(side="right")
        spin.bind("<FocusOut>", self._normalize_scale)
        spin.bind("<Return>", self._normalize_scale)
        self.scale_spin = spin
        tk.Label(
            scale_row,
            text="×",
            bg=PANEL,
            fg=MUTED,
            font=(FONT_MONO, 9),
        ).pack(side="right", padx=(0, px(6)))

        structure_row = tk.Frame(box, bg=PANEL)
        structure_row.pack(fill="x", pady=(px(7), 0))
        tk.Label(
            structure_row,
            text="SVG 结构",
            bg=PANEL,
            fg=INK,
            font=(FONT_UI, 9),
        ).pack(side="left")
        mode_box = tk.Frame(structure_row, bg=PANEL)
        mode_box.pack(side="right")
        for value, text in (
            ("strict", "严格 rect"),
            ("compact", "紧凑 path"),
        ):
            button = tk.Radiobutton(
                mode_box,
                text=text,
                variable=self.mode_var,
                value=value,
                command=self._on_mode_changed,
                bg=PANEL_SOFT,
                fg=INK,
                activebackground="#E9E4D8",
                activeforeground=INK,
                selectcolor=ACCENT_SOFT,
                font=(FONT_UI, 8),
                bd=0,
                highlightthickness=0,
                indicatoron=False,
                padx=px(7),
                pady=px(3),
                cursor="hand2",
            )
            button.pack(side="left", padx=(px(3), 0))
        tk.Label(
            box,
            textvariable=self.display_size_var,
            bg=PANEL,
            fg=MUTED_LIGHT,
            anchor="w",
            font=(FONT_MONO, 8),
        ).pack(fill="x", pady=(px(5), px(9)))

        stats = tk.Frame(
            box,
            bg=PANEL_SOFT,
            highlightbackground="#E8E3D7",
            highlightthickness=1,
        )
        stats.pack(fill="x")
        stats.columnconfigure(0, weight=1)
        stats.columnconfigure(1, weight=1)
        stats.columnconfigure(2, weight=1)
        values = [self.rect_var, self.alpha_var, self.size_var]
        captions = ["正方形", "透明 / 半透明", "预计体积"]
        for index in range(3):
            cell = tk.Frame(stats, bg=PANEL_SOFT)
            cell.grid(row=0, column=index, sticky="nsew", pady=px(8))
            tk.Label(
                cell,
                textvariable=values[index],
                bg=PANEL_SOFT,
                fg=INK,
                font=(FONT_MONO, 11, "bold"),
            ).pack()
            tk.Label(
                cell,
                text=captions[index],
                bg=PANEL_SOFT,
                fg=MUTED,
                font=(FONT_UI, 8),
            ).pack(pady=(px(1), 0))
            if index < 2:
                tk.Frame(
                    stats, bg="#E4DED1", width=1, height=px(28)
                ).grid(row=0, column=index, sticky="e", pady=px(9))
        tk.Label(
            box,
            text="大图生成的 SVG 会很大；导出前会再次确认。",
            bg=PANEL,
            fg=MUTED_LIGHT,
            anchor="w",
            justify="left",
            wraplength=px(315),
            font=(FONT_UI, 8),
        ).pack(fill="x", pady=(px(7), 0))

    def _build_export_card(self, parent: tk.Frame) -> None:
        card = self._card(parent, bg=ACCENT_SOFT, border=ACCENT_LINE)
        card.pack(fill="x")
        box = tk.Frame(card, bg=ACCENT_SOFT)
        box.pack(fill="x", padx=px(15), pady=px(13))
        self._section(box, "03", "导出 SVG", "OUTPUT", on_soft=True)

        tk.Label(
            box,
            text="目标文件",
            bg=ACCENT_SOFT,
            fg=ACCENT_DARK,
            anchor="w",
            font=(FONT_UI, 8, "bold"),
        ).pack(fill="x", pady=(px(9), px(2)))
        tk.Label(
            box,
            textvariable=self.output_path_var,
            bg=ACCENT_SOFT,
            fg=INK,
            anchor="w",
            justify="left",
            wraplength=px(315),
            font=(FONT_MONO, 8),
        ).pack(fill="x")

        self._sidebar_export_button = tk.Button(
            box,
            text="选择位置并导出 SVG",
            command=self.export_svg,
            bg=ACCENT,
            fg="#FFFFFF",
            activebackground=ACCENT_DARK,
            activeforeground="#FFFFFF",
            disabledforeground="#A8B3AC",
            font=(FONT_UI, 10, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(12),
            pady=px(9),
            cursor="hand2",
        )
        self._sidebar_export_button.pack(fill="x", pady=(px(10), px(7)))
        self._export_buttons.append(self._sidebar_export_button)

        bottom = tk.Frame(box, bg=ACCENT_SOFT)
        bottom.pack(fill="x")
        self.folder_link = self._link(
            bottom,
            "在文件夹中显示",
            self.open_output_folder,
            bg=ACCENT_SOFT,
        )
        self.folder_link.pack(side="left")
        self.folder_link.unbind("<Button-1>")
        self.folder_link.bind(
            "<Button-1>", lambda _event: self.open_output_folder()
        )
        tk.Label(
            bottom,
            text="写入完成后再替换目标文件",
            bg=ACCENT_SOFT,
            fg="#6E8B81",
            font=(FONT_UI, 8),
        ).pack(side="right")

    def _build_preview(self, parent: tk.Frame) -> None:
        card = tk.Frame(
            parent, bg=PANEL, highlightbackground=BORDER, highlightthickness=1
        )
        card.pack(fill="both", expand=True)
        self.preview_card = card

        top = tk.Frame(card, bg=PANEL)
        top.pack(fill="x", padx=px(16), pady=(px(12), px(7)))
        title_box = tk.Frame(top, bg=PANEL)
        title_box.pack(side="left")
        tk.Label(
            title_box,
            text="预览",
            bg=PANEL,
            fg=INK,
            font=(FONT_UI, 12, "bold"),
        ).pack(side="left")
        tk.Label(
            title_box,
            text="LIVE / CHECKERBOARD",
            bg=PANEL,
            fg=MUTED_LIGHT,
            font=(FONT_MONO, 7),
        ).pack(side="left", padx=(px(9), 0), pady=(px(3), 0))

        toolbar = tk.Frame(top, bg=PANEL)
        toolbar.pack(side="right")
        self._zoom_label = tk.Label(
            toolbar,
            textvariable=self.zoom_var,
            bg=PANEL,
            fg=MUTED,
            width=7,
            anchor="e",
            font=(FONT_MONO, 8),
        )
        self._zoom_label.pack(side="right", padx=(px(7), 0))
        self._tool_button(
            toolbar, "1:1", self.actual_size, padx=px(7)
        ).pack(side="right")
        self._tool_button(toolbar, "+", lambda: self.zoom_by(1.25)).pack(
            side="right", padx=(px(3), 0)
        )
        self._tool_button(toolbar, "适合", self.fit_to_window, padx=px(8)).pack(
            side="right", padx=(px(3), 0)
        )
        self._tool_button(toolbar, "−", lambda: self.zoom_by(1 / 1.25)).pack(
            side="right", padx=(px(3), 0)
        )
        grid_toggle = tk.Checkbutton(
            toolbar,
            text="网格",
            variable=self.grid_var,
            command=self._schedule_preview,
            bg=PANEL,
            fg=MUTED,
            activebackground=PANEL,
            activeforeground=INK,
            selectcolor=ACCENT_SOFT,
            font=(FONT_UI, 8),
            bd=0,
            highlightthickness=0,
            indicatoron=False,
            padx=px(8),
            pady=px(4),
            cursor="hand2",
        )
        grid_toggle.pack(side="right", padx=(0, px(4)))
        self.crop_toggle = tk.Checkbutton(
            toolbar,
            text="裁剪",
            variable=self.crop_mode_var,
            command=self._toggle_crop_mode,
            bg=PANEL,
            fg=MUTED,
            activebackground=PANEL,
            activeforeground=INK,
            selectcolor=ACCENT_SOFT,
            font=(FONT_UI, 8),
            bd=0,
            highlightthickness=0,
            indicatoron=False,
            padx=px(8),
            pady=px(4),
            cursor="hand2",
        )
        self.crop_toggle.pack(side="right", padx=(0, px(4)))
        self.crop_clear_button = self._tool_button(
            toolbar, "清除", self.clear_crop, padx=px(7)
        )
        self.crop_clear_button.pack(side="right", padx=(0, px(2)))
        self.crop_clear_button.configure(state="disabled")
        tk.Frame(
            toolbar, bg=BORDER, width=1, height=px(18)
        ).pack(side="right", padx=(px(4), px(6)))

        canvas_frame = tk.Frame(card, bg="#18221F")
        canvas_frame.pack(
            fill="both", expand=True, padx=px(12), pady=(0, px(6))
        )
        self.canvas = tk.Canvas(
            canvas_frame,
            bg="#18221F",
            highlightthickness=0,
            bd=0,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Configure>", self._on_canvas_configure)
        self.canvas.bind("<MouseWheel>", self._on_mousewheel)
        self.canvas.bind("<Button-4>", self._on_mousewheel_linux)
        self.canvas.bind("<Button-5>", self._on_mousewheel_linux)
        self.canvas.bind("<ButtonPress-1>", self._on_pan_start)
        self.canvas.bind("<B1-Motion>", self._on_pan_move)
        self.canvas.bind("<ButtonRelease-1>", self._on_pan_end)
        self.canvas.bind("<ButtonPress-3>", self._on_force_pan_start)
        self.canvas.bind("<B3-Motion>", self._on_force_pan_move)
        self.canvas.bind("<ButtonRelease-3>", self._on_force_pan_end)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)

        hint = tk.Frame(card, bg=PANEL)
        hint.pack(fill="x", padx=px(16), pady=(px(2), px(9)))
        tk.Label(
            hint,
            text="滚轮缩放 · 拖动平移 · 双击适合窗口",
            bg=PANEL,
            fg=MUTED_LIGHT,
            font=(FONT_UI, 8),
        ).pack(side="left")
        tk.Label(
            hint,
            text="1 px = 1 方格",
            bg=PANEL,
            fg=MUTED_LIGHT,
            font=(FONT_MONO, 8),
        ).pack(side="right")

    def _tool_button(self, parent, text: str, command, padx: int = px(8)):
        return tk.Button(
            parent,
            text=text,
            command=command,
            bg=PANEL_SOFT,
            fg=INK,
            activebackground="#E9E4D8",
            activeforeground=INK,
            font=(FONT_UI, 8),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=padx,
            pady=px(4),
            cursor="hand2",
        )

    def _build_footer(self) -> None:
        tk.Frame(self.root, bg=BORDER, height=1).pack(fill="x")
        footer = tk.Frame(self.root, bg=BG, height=px(36))
        footer.pack(fill="x")
        footer.pack_propagate(False)
        self.status_dot = tk.Canvas(
            footer,
            width=px(10),
            height=px(10),
            bg=BG,
            highlightthickness=0,
        )
        self.status_dot.pack(side="left", padx=(px(24), px(8)))
        self.status_dot_id = self.status_dot.create_rectangle(
            px(1), px(1), px(9), px(9), fill=ACCENT, outline=""
        )
        tk.Label(
            footer,
            textvariable=self.status_var,
            bg=BG,
            fg=MUTED,
            font=(FONT_UI, 8),
        ).pack(side="left")
        tk.Label(
            footer,
            text=f"严格逐像素 · v{APP_VERSION}",
            bg=BG,
            fg=MUTED_LIGHT,
            font=(FONT_MONO, 7),
        ).pack(side="right", padx=px(24))

    # ------------------------------------------------------------------
    # 事件绑定
    # ------------------------------------------------------------------

    def _bind_events(self) -> None:
        self.root.bind("<Control-o>", lambda _event: self.open_image_dialog())
        self.root.bind("<Control-s>", lambda _event: self.export_svg())
        self.root.bind("<Control-0>", lambda _event: self.fit_to_window())
        self.root.bind("<Control-1>", lambda _event: self.actual_size())
        self.root.bind("<Escape>", self._on_escape)
        self.root.bind_all("<MouseWheel>", self._on_global_wheel, add="+")
        self.root.bind_all("<Button-4>", self._on_global_wheel_linux, add="+")
        self.root.bind_all("<Button-5>", self._on_global_wheel_linux, add="+")

    def _on_global_wheel(self, event) -> None:
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return
        if widget is not None and is_descendant(widget, self.sidebar.inner):
            self.sidebar.scroll_wheel(event)

    def _on_global_wheel_linux(self, event) -> None:
        try:
            widget = self.root.winfo_containing(event.x_root, event.y_root)
        except Exception:
            return
        if widget is not None and is_descendant(widget, self.sidebar.inner):
            self.sidebar.scroll_wheel(event)

    def _on_escape(self, _event=None) -> None:
        if self._progress_dialog is not None:
            self._progress_dialog.cancel()
            return
        if self.crop_mode_var.get():
            self.crop_mode_var.set(False)
            self._toggle_crop_mode()
            return
        if self.crop_box is not None:
            self.clear_crop()

    # ------------------------------------------------------------------
    # 打开与读取
    # ------------------------------------------------------------------

    def open_image_dialog(self) -> None:
        if self.loading or self.exporting:
            return
        filetypes = [
            (
                "常见位图",
                "*.png *.jpg *.jpeg *.jfif *.bmp *.gif *.webp "
                "*.tif *.tiff",
            ),
            ("PNG", "*.png"),
            ("JPEG", "*.jpg *.jpeg *.jfif"),
            ("GIF", "*.gif"),
            ("BMP", "*.bmp"),
            ("WebP", "*.webp"),
            ("TIFF", "*.tif *.tiff"),
            (
                "Pillow 扩展格式（需安装 Pillow）",
                "*.ico *.tga *.ppm *.pgm *.pbm *.pcx *.dds",
            ),
            ("所有文件", "*.*"),
        ]
        initial_dir = (
            str(self.source_path.parent)
            if self.source_path is not None
            else str(Path.home())
        )
        path = filedialog.askopenfilename(
            parent=self.root,
            title="选择一张位图",
            initialdir=initial_dir,
            filetypes=filetypes,
        )
        if path:
            self._begin_load(Path(path))

    def _begin_load(self, path: Path) -> None:
        if self.loading or self.exporting:
            return
        if not path.exists():
            messagebox.showerror(
                "无法打开", f"文件不存在：\n{path}", parent=self.root
            )
            return
        self.loading = True
        self._load_seq += 1
        sequence = self._load_seq
        self._refresh_action_states()
        self._set_status(f"正在读取 {shorten_middle(path.name, 30)} …", "busy")
        self.source_canvas.configure(cursor="watch")
        self._draw_source_loading(path.name)
        try:
            self.root.configure(cursor="watch")
        except tk.TclError:
            pass
        thread = threading.Thread(
            target=self._load_worker,
            args=(sequence, path),
            daemon=True,
        )
        thread.start()
        self.root.after(40, self._poll_load)

    def _load_worker(self, sequence: int, path: Path) -> None:
        try:
            image = core.load_bitmap(path)
        except Exception as exc:  # noqa: BLE001 - 传回主线程展示
            self._load_queue.put((sequence, "error", exc))
        else:
            self._load_queue.put((sequence, "ok", image))

    def _poll_load(self) -> None:
        handled = False
        while True:
            try:
                sequence, kind, payload = self._load_queue.get_nowait()
            except queue.Empty:
                break
            if sequence != self._load_seq:
                continue
            handled = True
            if kind == "ok":
                self._finish_load(payload)
            else:
                self._finish_load_error(payload)
        if self.loading and not handled:
            self.root.after(40, self._poll_load)

    def _finish_load(self, image: core.BitmapImage) -> None:
        self.loading = False
        self.image = image
        self.source_path = image.source
        self.output_path = image.source.with_suffix(".svg")
        self.last_output = None
        self.fit_mode = True
        self.crop_mode_var.set(False)
        self.crop_box = None
        self.cropped_image = None
        self._crop_anchor = None
        self._crop_current = None
        self._crop_dragging = False
        self._color_count_cache = None
        self._color_count_ready = False
        self.canvas.configure(cursor="")
        self._refresh_action_states()
        try:
            self.root.configure(cursor="")
        except tk.TclError:
            pass
        self.source_canvas.configure(cursor="hand2")
        self.root.title(f"{APP_TITLE} — {image.source.name}")
        self.name_var.set(shorten_middle(image.source.name, 32))
        bits = [
            image.format,
            f"{image.width} × {image.height}",
            "含透明像素" if image.has_transparency else "不透明",
        ]
        if image.frames > 1:
            bits.append(f"动画 {image.frames} 帧（取第 1 帧）")
        self.meta_var.set(" · ".join(bits))
        self._update_output_path_label()
        self._update_stats()
        self._normalize_scale()
        self._draw_source_thumb()
        self._redraw_preview()
        self.sidebar.reset_scroll()
        if image.opaque_count == 0:
            self._set_status("已载入 · 图像完全透明，将输出空白 SVG", "done")
        else:
            self._set_status(
                f"已载入 {image.width} × {image.height} · "
                f"{image.opaque_count:,} 个待翻译像素",
                "done",
            )
        self._refresh_action_states()

    def _finish_load_error(self, error: Exception) -> None:
        self.loading = False
        try:
            self.root.configure(cursor="")
        except tk.TclError:
            pass
        self.source_canvas.configure(cursor="hand2")
        self._refresh_action_states()
        if self.image is None:
            self._draw_source_empty()
        else:
            self._draw_source_thumb()
        self._set_status("读取失败", "error")
        messagebox.showerror("无法读取图片", str(error), parent=self.root)

    # ------------------------------------------------------------------
    # 界面数据刷新
    # ------------------------------------------------------------------

    def _get_scale(self) -> int:
        try:
            value = int(float(self.scale_var.get().strip()))
        except (TypeError, ValueError):
            return 1
        return max(1, min(100, value))

    def _normalize_scale(self, _event=None) -> None:
        value = self._get_scale()
        self.scale_var.set(str(value))
        self._on_scale_changed()

    def _effective_image(self) -> Optional[core.BitmapImage]:
        return self.cropped_image or self.image

    def _on_scale_changed(self, *_args) -> None:
        image = self._effective_image()
        if image is None:
            self.display_size_var.set("SVG 尺寸 —")
            return
        try:
            value = int(float(self.scale_var.get().strip()))
        except (TypeError, ValueError):
            return
        if not 1 <= value <= 100:
            return
        prefix = (
            f"选区 {image.width} × {image.height} · "
            if self.cropped_image is not None
            else ""
        )
        self.display_size_var.set(
            f"{prefix}SVG {image.width * value} × "
            f"{image.height * value}"
        )

    def _get_color_count(self) -> Optional[int]:
        if self._color_count_ready:
            return self._color_count_cache
        image = self._effective_image()
        if image is None:
            return None
        count = core.count_visible_colors(
            image, core.MAX_COMPACT_COLORS + 1
        )
        if count is not None and count > core.MAX_COMPACT_COLORS:
            count = None
        self._color_count_cache = count
        self._color_count_ready = True
        return self._color_count_cache

    def _on_mode_changed(self) -> None:
        if self.image is None:
            self.mode_note_var.set(
                "不合并同色像素 · 不做轮廓拟合 · 不使用抗锯齿"
                if self.mode_var.get() == "strict"
                else "每色一条 path · 像素仍为 1×1 方格 · 文件更小"
            )
            return
        self._update_stats()
        self._on_scale_changed()

    def _update_stats(self) -> None:
        image = self._effective_image()
        if image is None:
            self.rect_var.set("—")
            self.alpha_var.set("—")
            self.size_var.set("—")
            self.mode_note_var.set(
                "不合并同色像素 · 不做轮廓拟合 · 不使用抗锯齿"
                if self.mode_var.get() == "strict"
                else "每色一条 path · 像素仍为 1×1 方格 · 文件更小"
            )
            return
        self.rect_var.set(f"{image.opaque_count:,}")
        transparent_ratio = (
            image.non_opaque_count / image.total_pixels * 100.0
            if image.total_pixels
            else 0.0
        )
        if transparent_ratio == 0:
            self.alpha_var.set("0%")
        elif transparent_ratio >= 10:
            self.alpha_var.set(f"{transparent_ratio:.0f}%")
        else:
            self.alpha_var.set(f"{transparent_ratio:.1f}%")
        if self.mode_var.get() == "compact":
            colors = self._get_color_count()
            if colors is None:
                self.size_var.set("颜色过多")
                self.mode_note_var.set(
                    f"颜色超过 {core.MAX_COMPACT_COLORS} 种，"
                    "紧凑路径不划算，建议改用严格 rect"
                )
            else:
                self.size_var.set(
                    core.human_size(
                        core.estimate_svg_size(
                            image, mode="compact", color_count=colors
                        )
                    )
                )
                self.mode_note_var.set(
                    f"每色一条 path · 共 {colors:,} 种颜色 · "
                    "像素仍为 1×1 方格"
                )
        else:
            self.size_var.set(core.human_size(core.estimate_svg_size(image)))
            self.mode_note_var.set(
                "不合并同色像素 · 不做轮廓拟合 · 不使用抗锯齿"
            )

    def _update_output_path_label(self) -> None:
        path = self.last_output or self.output_path
        if path is None:
            self.output_path_var.set("导出时选择位置")
        else:
            self.output_path_var.set(shorten_middle(str(path), 52))

    def _refresh_action_states(self) -> None:
        has_image = self.image is not None
        busy = self.loading or self.exporting
        header_enabled = has_image and not busy
        self._header_export_button.configure(
            state="normal" if header_enabled else "disabled",
            bg=CORAL if header_enabled else "#4B3B35",
            activebackground=CORAL_DARK,
            cursor="hand2" if header_enabled else "arrow",
        )
        sidebar_enabled = has_image and not busy
        self._sidebar_export_button.configure(
            state="normal" if sidebar_enabled else "disabled",
            bg=ACCENT if sidebar_enabled else "#CBD8D1",
            activebackground=ACCENT_DARK,
            cursor="hand2" if sidebar_enabled else "arrow",
        )
        if busy:
            self._header_open_button.configure(
                state="disabled",
                bg="#2A3733",
                cursor="arrow",
            )
        else:
            self._header_open_button.configure(
                state="normal",
                bg=INK_2,
                cursor="hand2",
            )
        self.change_link.configure(
            fg=ACCENT if not busy else MUTED_LIGHT,
            cursor="hand2" if not busy else "arrow",
        )
        if self.last_output is not None and not busy:
            self.folder_link.configure(fg=ACCENT, cursor="hand2")
        else:
            self.folder_link.configure(fg="#8CA69C", cursor="arrow")
        if hasattr(self, "crop_toggle"):
            crop_enabled = has_image and not busy
            self.crop_toggle.configure(
                state="normal" if crop_enabled else "disabled",
                cursor="hand2" if crop_enabled else "arrow",
            )
            self._refresh_crop_controls()

    def _refresh_crop_controls(self) -> None:
        if not hasattr(self, "crop_clear_button"):
            return
        enabled = (
            self.crop_box is not None
            and not self.loading
            and not self.exporting
        )
        self.crop_clear_button.configure(
            state="normal" if enabled else "disabled",
            bg=PANEL_SOFT if enabled else "#F0EDE5",
            cursor="hand2" if enabled else "arrow",
        )

    def _set_status(self, text: str, kind: str = "ready") -> None:
        colors = {
            "ready": ACCENT,
            "busy": GOLD,
            "done": ACCENT,
            "error": ERROR,
        }
        try:
            self.status_dot.itemconfigure(
                self.status_dot_id, fill=colors.get(kind, ACCENT)
            )
        except tk.TclError:
            pass
        self.status_var.set(text)

    def _toast(self, text: str) -> None:
        if self._toast_label is not None:
            try:
                self._toast_label.destroy()
            except tk.TclError:
                pass
        if self._toast_job is not None:
            try:
                self.root.after_cancel(self._toast_job)
            except tk.TclError:
                pass
        label = tk.Label(
            self.preview_card,
            text=f"  ✓  {text}  ",
            bg=ACCENT,
            fg="#FFFFFF",
            font=(FONT_UI, 9, "bold"),
            padx=px(8),
            pady=px(7),
        )
        label.place(relx=0.985, rely=0.94, anchor="se")
        label.lift()
        self._toast_label = label
        self._toast_job = self.root.after(4200, self._hide_toast)

    def _hide_toast(self) -> None:
        self._toast_job = None
        if self._toast_label is not None:
            try:
                self._toast_label.destroy()
            except tk.TclError:
                pass
            self._toast_label = None

    # ------------------------------------------------------------------
    # 缩略图与预览
    # ------------------------------------------------------------------

    def _schedule_thumb(self, _event=None) -> None:
        if self._thumb_job is not None:
            try:
                self.root.after_cancel(self._thumb_job)
            except tk.TclError:
                pass
        self._thumb_job = self.root.after(40, self._draw_source_thumb)

    def _schedule_preview(self, _event=None) -> None:
        if self._preview_job is not None:
            try:
                self.root.after_cancel(self._preview_job)
            except tk.TclError:
                pass
        self._preview_job = self.root.after(25, self._redraw_preview)

    def _draw_source_loading(self, name: str) -> None:
        canvas = self.source_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.create_text(
            width / 2,
            height / 2 - px(7),
            text="正在读取…",
            fill="#D9E5DF",
            font=(FONT_UI, 10, "bold"),
        )
        canvas.create_text(
            width / 2,
            height / 2 + px(12),
            text=shorten_middle(name, 34),
            fill="#7F978F",
            font=(FONT_MONO, 8),
        )

    def _draw_source_empty(self) -> None:
        canvas = self.source_canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.create_rectangle(
            px(9),
            px(9),
            max(px(10), width - px(9)),
            max(px(10), height - px(9)),
            outline="#3D4D47",
            dash=(px(3), px(3)),
            width=1,
        )
        cx, cy = width / 2, height / 2
        cell = px(9)
        gap = px(3)
        for row in range(3):
            for col in range(3):
                if (row, col) in ((0, 0), (0, 1), (1, 0), (1, 1), (2, 2)):
                    color = ACCENT if (row + col) % 2 == 0 else "#2C8C77"
                    canvas.create_rectangle(
                        cx - px(18) + col * (cell + gap),
                        cy - px(30) + row * (cell + gap),
                        cx - px(18) + col * (cell + gap) + cell,
                        cy - px(30) + row * (cell + gap) + cell,
                        fill=color,
                        outline="",
                    )
                else:
                    canvas.create_rectangle(
                        cx - px(18) + col * (cell + gap),
                        cy - px(30) + row * (cell + gap),
                        cx - px(18) + col * (cell + gap) + cell,
                        cy - px(30) + row * (cell + gap) + cell,
                        outline="#3D4D47",
                    )
        canvas.create_text(
            cx,
            cy + px(14),
            text="点击选择一张图片",
            fill="#E7EEE9",
            font=(FONT_UI, 9, "bold"),
        )
        canvas.create_text(
            cx,
            cy + px(32),
            text="PNG · JPG · BMP · GIF · WEBP · TIFF",
            fill="#7F978F",
            font=(FONT_MONO, 7),
        )

    def _draw_source_thumb(self) -> None:
        self._thumb_job = None
        if self.image is None:
            self._draw_source_empty()
            return
        canvas = self.source_canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 10 or height < 10:
            return
        zoom, ox, oy = core.fit_viewport(
            self.image.width,
            self.image.height,
            width,
            height,
            margin=px(7),
            max_zoom=10.0,
        )
        try:
            ppm = core.render_preview_ppm(
                self.image, width, height, zoom, ox, oy
            )
            photo = tk.PhotoImage(data=ppm, format="PPM")
        except (tk.TclError, ValueError):
            return
        canvas.delete("all")
        self._thumb_photo = photo
        canvas.create_image(0, 0, anchor="nw", image=photo)

    def _draw_empty_preview(self) -> None:
        canvas = self.canvas
        canvas.delete("all")
        width = max(1, canvas.winfo_width())
        height = max(1, canvas.winfo_height())
        canvas.create_rectangle(
            0, 0, width, height, fill="#18221F", outline=""
        )
        step = px(48)
        for x in range(0, width, step):
            canvas.create_line(
                x, 0, x, height, fill="#22302B", stipple="gray25"
            )
        for y in range(0, height, step):
            canvas.create_line(
                0, y, width, y, fill="#22302B", stipple="gray25"
            )
        cx, cy = width / 2, height / 2
        unit = px(18)
        gap = px(5)
        pattern = [
            [1, 1, 0, 1],
            [1, 0, 0, 1],
            [0, 0, 1, 1],
            [0, 1, 1, 0],
        ]
        start_x = cx - (4 * unit + 3 * gap) / 2
        start_y = cy - px(92)
        for row, bits in enumerate(pattern):
            for col, bit in enumerate(bits):
                x0 = start_x + col * (unit + gap)
                y0 = start_y + row * (unit + gap)
                if bit:
                    color = ACCENT if (row + col) % 3 else CORAL
                    canvas.create_rectangle(
                        x0, y0, x0 + unit, y0 + unit, fill=color, outline=""
                    )
                else:
                    canvas.create_rectangle(
                        x0,
                        y0,
                        x0 + unit,
                        y0 + unit,
                        outline="#34443E",
                        width=1,
                    )
        canvas.create_text(
            cx,
            start_y + 4 * unit + 3 * gap + px(32),
            text="打开一张图片，开始逐像素翻译",
            fill="#EDF3EF",
            font=(FONT_UI, 13, "bold"),
        )
        canvas.create_text(
            cx,
            start_y + 4 * unit + 3 * gap + px(54),
            text="每个像素都会成为一个独立的 1×1 矢量正方形",
            fill="#8FA49C",
            font=(FONT_UI, 9),
        )
        canvas.create_text(
            cx,
            start_y + 4 * unit + 3 * gap + px(76),
            text="Ctrl+O 打开   ·   Ctrl+S 导出",
            fill="#647B73",
            font=(FONT_MONO, 8),
        )

    def _on_canvas_configure(self, _event=None) -> None:
        self._schedule_preview()

    def _redraw_preview(self) -> None:
        self._preview_job = None
        if self.image is None:
            self.zoom_var.set("—")
            self._draw_empty_preview()
            return
        canvas = self.canvas
        width = canvas.winfo_width()
        height = canvas.winfo_height()
        if width < 20 or height < 20:
            return
        if self.fit_mode:
            self.zoom, self.offset_x, self.offset_y = core.fit_viewport(
                self.image.width,
                self.image.height,
                width,
                height,
                margin=px(28),
                max_zoom=32.0,
            )
        try:
            ppm = core.render_preview_ppm(
                self.image,
                width,
                height,
                self.zoom,
                self.offset_x,
                self.offset_y,
            )
            photo = tk.PhotoImage(data=ppm, format="PPM")
        except (tk.TclError, ValueError):
            return
        self._preview_photo = photo
        canvas.delete("all")
        canvas.create_image(0, 0, anchor="nw", image=photo)
        self._draw_grid(width, height)
        self._draw_crop_overlay()
        self._update_zoom_label()

    def _draw_grid(self, canvas_width: int, canvas_height: int) -> None:
        if self.image is None or not self.grid_var.get():
            return
        zoom = self.zoom
        if zoom < 7.0:
            return
        left = max(0.0, self.offset_x)
        top = max(0.0, self.offset_y)
        right = min(float(canvas_width), self.offset_x + self.image.width * zoom)
        bottom = min(
            float(canvas_height), self.offset_y + self.image.height * zoom
        )
        if right <= left or bottom <= top:
            return
        start_x = max(0, int(math.ceil((left - self.offset_x) / zoom)))
        end_x = min(
            self.image.width, int(math.floor((right - self.offset_x) / zoom))
        )
        start_y = max(0, int(math.ceil((top - self.offset_y) / zoom)))
        end_y = min(
            self.image.height, int(math.floor((bottom - self.offset_y) / zoom))
        )
        if (end_x - start_x) <= 420:
            for x in range(start_x, end_x + 1):
                screen_x = self.offset_x + x * zoom
                if left <= screen_x <= right:
                    self.canvas.create_line(
                        screen_x + 0.5,
                        top,
                        screen_x + 0.5,
                        bottom,
                        fill="#D4E2DB",
                        stipple="gray12",
                    )
        if (end_y - start_y) <= 420:
            for y in range(start_y, end_y + 1):
                screen_y = self.offset_y + y * zoom
                if top <= screen_y <= bottom:
                    self.canvas.create_line(
                        left,
                        screen_y + 0.5,
                        right,
                        screen_y + 0.5,
                        fill="#D4E2DB",
                        stipple="gray12",
                    )

    def _draw_crop_label(
        self, x: float, y: float, text: str, anchor: str = "nw"
    ) -> None:
        canvas = self.canvas
        text_id = canvas.create_text(
            x,
            y,
            text=text,
            anchor=anchor,
            fill="#FFEDE7",
            font=(FONT_MONO, 8),
            tags=("crop",),
        )
        bbox = canvas.bbox(text_id)
        if bbox is None:
            return
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        move_x = 0
        move_y = 0
        if bbox[2] > cw - px(4):
            move_x = cw - px(4) - bbox[2]
        if bbox[0] + move_x < px(4):
            move_x = px(4) - bbox[0]
        if bbox[1] < px(4):
            move_y = px(4) - bbox[1]
        if bbox[3] + move_y > ch - px(4):
            move_y = ch - px(4) - bbox[3]
        if move_x or move_y:
            canvas.move(text_id, move_x, move_y)
            bbox = canvas.bbox(text_id)
        canvas.create_rectangle(
            bbox[0] - px(5),
            bbox[1] - px(3),
            bbox[2] + px(5),
            bbox[3] + px(3),
            fill="#101A16",
            outline="#FF7657",
            width=1,
            tags=("crop",),
        )
        canvas.tag_raise(text_id)

    def _draw_crop_overlay(self) -> None:
        canvas = self.canvas
        canvas.delete("crop")
        if self.image is None:
            return
        if self.crop_box is None:
            if self.crop_mode_var.get():
                self._draw_crop_label(
                    px(12),
                    px(12),
                    "拖动预览选择导出区域 · 再次点“裁剪”退出",
                    anchor="nw",
                )
            return

        x0, y0, x1, y1 = self.crop_box
        sx0 = self.offset_x + x0 * self.zoom
        sy0 = self.offset_y + y0 * self.zoom
        sx1 = self.offset_x + x1 * self.zoom
        sy1 = self.offset_y + y1 * self.zoom
        cw = canvas.winfo_width()
        ch = canvas.winfo_height()
        if cw < 5 or ch < 5:
            return

        top = max(0.0, min(float(ch), sy0))
        bottom = max(0.0, min(float(ch), sy1))
        left = max(0.0, min(float(cw), sx0))
        right = max(0.0, min(float(cw), sx1))
        dim_color = "#050B09"

        def dim_rect(xa, ya, xb, yb):
            if xb - xa > 0 and yb - ya > 0:
                canvas.create_rectangle(
                    xa,
                    ya,
                    xb,
                    yb,
                    fill=dim_color,
                    outline="",
                    stipple="gray75",
                    tags=("crop",),
                )

        dim_rect(0, 0, cw, top)
        dim_rect(0, bottom, cw, ch)
        dim_rect(0, top, left, bottom)
        dim_rect(right, top, cw, bottom)

        canvas.create_rectangle(
            sx0,
            sy0,
            sx1,
            sy1,
            outline="#FF7657",
            width=2,
            tags=("crop",),
        )
        handle = px(4)
        for hx, hy in ((sx0, sy0), (sx1, sy0), (sx0, sy1), (sx1, sy1)):
            if 0 <= hx <= cw and 0 <= hy <= ch:
                canvas.create_rectangle(
                    hx - handle,
                    hy - handle,
                    hx + handle,
                    hy + handle,
                    fill=CORAL,
                    outline="#FFF3EE",
                    width=1,
                    tags=("crop",),
                )

        width = x1 - x0
        height = y1 - y0
        if self.cropped_image is not None and not self._crop_dragging:
            label = (
                f"{width} × {height} px · "
                f"{self.cropped_image.opaque_count:,} 正方形"
            )
        else:
            label = f"{width} × {height} px"
        label_y = sy0 - px(8)
        if label_y < px(28):
            label_y = sy0 + px(20)
            anchor = "nw"
        else:
            anchor = "sw"
        label_x = max(float(px(6)), min(sx0 + px(4), cw - px(10)))
        self._draw_crop_label(label_x, label_y, label, anchor=anchor)

    def _toggle_crop_mode(self) -> None:
        if self.image is None:
            self.crop_mode_var.set(False)
            return
        active = self.crop_mode_var.get()
        if active:
            self.canvas.configure(cursor="crosshair")
            self._set_status(
                "裁剪模式 · 左键拖出区域 · 右键拖动平移 · Esc 退出",
                "busy",
            )
        else:
            if self._crop_anchor is not None:
                # 拖拽中途退出：丢弃这次未完成的选区，避免显示一个
                # 并未真正应用到导出数据的边框。
                self.crop_box = None
                self.cropped_image = None
                self._color_count_cache = None
                self._color_count_ready = False
                self._update_stats()
                self._on_scale_changed()
                self._refresh_crop_controls()
            self._crop_anchor = None
            self._crop_current = None
            self._crop_dragging = False
            self.canvas.configure(cursor="")
            self._set_status("裁剪模式已关闭", "ready")
        self._draw_crop_overlay()

    def clear_crop(self) -> None:
        if self.crop_box is None and self.cropped_image is None:
            return
        self.crop_box = None
        self.cropped_image = None
        self._crop_anchor = None
        self._crop_current = None
        self._crop_dragging = False
        self._color_count_cache = None
        self._color_count_ready = False
        self._refresh_crop_controls()
        self._update_stats()
        self._on_scale_changed()
        self._draw_crop_overlay()
        if self.crop_mode_var.get():
            self._set_status(
                "裁剪模式 · 拖动选择新的区域 · Esc 退出", "busy"
            )
        else:
            self._set_status("已清除裁剪选区 · 恢复导出整张图", "ready")

    def _event_to_pixel(
        self, event, clamp: bool = False
    ) -> Optional[tuple[int, int]]:
        if self.image is None or self.zoom <= 0:
            return None
        image_x = (event.x - self.offset_x) / self.zoom
        image_y = (event.y - self.offset_y) / self.zoom
        if clamp:
            image_x = max(0.0, min(self.image.width - 0.001, image_x))
            image_y = max(0.0, min(self.image.height - 0.001, image_y))
        elif not (
            0 <= image_x < self.image.width
            and 0 <= image_y < self.image.height
        ):
            return None
        ix = max(0, min(self.image.width - 1, int(round(image_x))))
        iy = max(0, min(self.image.height - 1, int(round(image_y))))
        return ix, iy

    def _crop_box_from_cells(
        self, anchor: tuple[int, int], current: tuple[int, int]
    ) -> tuple[int, int, int, int]:
        left = min(anchor[0], current[0])
        top = min(anchor[1], current[1])
        right = max(anchor[0], current[0]) + 1
        bottom = max(anchor[1], current[1]) + 1
        return left, top, right, bottom

    def _start_crop(self, event) -> None:
        cell = self._event_to_pixel(event)
        if cell is None:
            return
        self._crop_anchor = cell
        self._crop_current = cell
        self._crop_dragging = True
        self.crop_box = self._crop_box_from_cells(cell, cell)
        self._draw_crop_overlay()

    def _update_crop(self, event) -> None:
        if self._crop_anchor is None:
            return
        cell = self._event_to_pixel(event, clamp=True)
        if cell is None:
            return
        self._crop_current = cell
        self.crop_box = self._crop_box_from_cells(
            self._crop_anchor, cell
        )
        self._draw_crop_overlay()

    def _finish_crop(self) -> None:
        if self._crop_anchor is None or self.crop_box is None:
            self._crop_anchor = None
            self._crop_current = None
            self._crop_dragging = False
            return
        box = self.crop_box
        self._crop_anchor = None
        self._crop_current = None
        self._crop_dragging = False
        image = self.image
        if image is None:
            return
        width = box[2] - box[0]
        height = box[3] - box[1]
        if width <= 0 or height <= 0:
            self.crop_box = None
            self._draw_crop_overlay()
            return
        if box == (0, 0, image.width, image.height):
            self.clear_crop()
            return
        try:
            cropped = core.crop_bitmap(image, box)
        except Exception as exc:  # noqa: BLE001
            self.crop_box = None
            self._draw_crop_overlay()
            messagebox.showerror(
                "裁剪失败", f"无法裁剪选区：\n\n{exc}", parent=self.root
            )
            return
        self.crop_box = box
        self.cropped_image = cropped
        self._color_count_cache = None
        self._color_count_ready = False
        self._refresh_crop_controls()
        self._update_stats()
        self._on_scale_changed()
        self._draw_crop_overlay()
        self._set_status(
            f"裁剪选区 {cropped.width} × {cropped.height} · "
            f"{cropped.opaque_count:,} 个正方形",
            "done",
        )

    def _update_zoom_label(self) -> None:
        if self.image is None:
            self.zoom_var.set("—")
            return
        percent = self.zoom * 100.0
        if percent >= 1000:
            text = f"{percent:,.0f}%"
        elif percent >= 100:
            text = f"{percent:.0f}%"
        else:
            text = f"{percent:.1f}%"
        self.zoom_var.set(text)

    # ------------------------------------------------------------------
    # 缩放 / 平移
    # ------------------------------------------------------------------

    def zoom_by(self, factor: float, anchor=None) -> None:
        if self.image is None:
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        if width < 10 or height < 10:
            return
        if anchor is None:
            anchor_x, anchor_y = width / 2.0, height / 2.0
        else:
            anchor_x, anchor_y = anchor
        old_zoom = self.zoom
        new_zoom = max(0.02, min(128.0, old_zoom * factor))
        if abs(new_zoom - old_zoom) < 1e-9:
            return
        image_x = (anchor_x - self.offset_x) / old_zoom
        image_y = (anchor_y - self.offset_y) / old_zoom
        self.zoom = new_zoom
        self.offset_x = anchor_x - image_x * new_zoom
        self.offset_y = anchor_y - image_y * new_zoom
        self.fit_mode = False
        self._update_zoom_label()
        self._schedule_preview()

    def _on_mousewheel(self, event) -> None:
        if self.image is None:
            return
        factor = 1.2 if event.delta > 0 else 1 / 1.2
        self.zoom_by(factor, (event.x, event.y))
        return "break"

    def _on_mousewheel_linux(self, event) -> None:
        if self.image is None:
            return
        factor = 1.2 if event.num == 4 else 1 / 1.2
        self.zoom_by(factor, (event.x, event.y))
        return "break"

    def fit_to_window(self) -> None:
        if self.image is None:
            return
        self.fit_mode = True
        self._redraw_preview()

    def actual_size(self) -> None:
        if self.image is None:
            return
        width = self.canvas.winfo_width()
        height = self.canvas.winfo_height()
        self.zoom = 1.0
        self.offset_x = (width - self.image.width * self.zoom) / 2.0
        self.offset_y = (height - self.image.height * self.zoom) / 2.0
        self.fit_mode = False
        self._update_zoom_label()
        self._redraw_preview()

    def _on_double_click(self, _event) -> None:
        if self.crop_mode_var.get():
            return
        self.fit_to_window()

    def _on_pan_start(self, event) -> None:
        if self.image is None or self.loading or self.exporting:
            return
        if self.crop_mode_var.get():
            self._start_crop(event)
            return
        self._drag_start = (
            event.x,
            event.y,
            self.offset_x,
            self.offset_y,
        )
        self._dragged = False
        self.canvas.configure(cursor="fleur")

    def _on_pan_move(self, event) -> None:
        if self.image is None or self.loading or self.exporting:
            return
        if self.crop_mode_var.get():
            self._update_crop(event)
            return
        if self._drag_start is None:
            return
        start_x, start_y, offset_x, offset_y = self._drag_start
        delta_x = event.x - start_x
        delta_y = event.y - start_y
        if abs(delta_x) + abs(delta_y) > 2:
            self._dragged = True
        self.offset_x = offset_x + delta_x
        self.offset_y = offset_y + delta_y
        self.fit_mode = False
        self._schedule_preview()

    def _on_pan_end(self, _event) -> None:
        if self.crop_mode_var.get():
            self._finish_crop()
            return
        if self._drag_start is not None:
            self._drag_start = None
            self.canvas.configure(cursor="")
            if self._dragged:
                self._redraw_preview()

    def _on_force_pan_start(self, event) -> None:
        """右键拖动：裁剪模式下也能平移画布。"""
        if self.image is None or self.loading or self.exporting:
            return
        self._drag_start = (
            event.x,
            event.y,
            self.offset_x,
            self.offset_y,
        )
        self._dragged = False
        self.canvas.configure(cursor="fleur")

    def _on_force_pan_move(self, event) -> None:
        if self._drag_start is None or self.image is None:
            return
        start_x, start_y, offset_x, offset_y = self._drag_start
        delta_x = event.x - start_x
        delta_y = event.y - start_y
        if abs(delta_x) + abs(delta_y) > 2:
            self._dragged = True
        self.offset_x = offset_x + delta_x
        self.offset_y = offset_y + delta_y
        self.fit_mode = False
        self._schedule_preview()

    def _on_force_pan_end(self, _event) -> None:
        if self._drag_start is not None:
            self._drag_start = None
            self.canvas.configure(
                cursor="crosshair" if self.crop_mode_var.get() else ""
            )
            if self._dragged:
                self._redraw_preview()

    # ------------------------------------------------------------------
    # 导出
    # ------------------------------------------------------------------

    def export_svg(self) -> None:
        if self.image is None or self.loading or self.exporting:
            return
        self._normalize_scale()
        image = self._effective_image()
        if image is None:
            return
        scale = self._get_scale()
        mode = self.mode_var.get()
        if mode == "compact":
            colors = self._get_color_count()
            if colors is None:
                switch = messagebox.askyesno(
                    "紧凑模式不适合这张图",
                    f"这张图的颜色超过 {core.MAX_COMPACT_COLORS} 种。"
                    "紧凑路径需要为每种颜色单独记录位置，颜色太多时"
                    "文件不一定更小。\n\n"
                    "是否改用严格 rect 模式继续导出？",
                    parent=self.root,
                )
                if not switch:
                    return
                self.mode_var.set("strict")
                self._on_mode_changed()
                mode = "strict"
                colors = None
        else:
            colors = None

        source = self.source_path or Path("pixel2svg")
        initial = self.output_path or source.with_suffix(".svg")
        path = filedialog.asksaveasfilename(
            parent=self.root,
            title="导出逐像素 SVG",
            initialdir=str(initial.parent),
            initialfile=initial.name,
            defaultextension=".svg",
            filetypes=[("SVG 矢量图", "*.svg"), ("所有文件", "*.*")],
        )
        if not path:
            return
        destination = Path(path)
        if destination.suffix.lower() != ".svg":
            destination = destination.with_suffix(".svg")
        if image.opaque_count > 250_000:
            estimated = core.human_size(
                core.estimate_svg_size(
                    image, mode=mode, color_count=colors
                )
            )
            shape_word = "矩形" if mode == "strict" else "正方形子路径"
            mode_word = "严格逐像素" if mode == "strict" else "紧凑路径"
            area_word = "当前选区" if self.cropped_image else "这张图"
            confirmed = messagebox.askyesno(
                "SVG 会比较大",
                f"{area_word}有 {image.opaque_count:,} 个待翻译像素，"
                f"将生成同样数量的{shape_word}，预计约 {estimated}。\n\n"
                f"{mode_word}模式不会合并像素。仍然继续吗？",
                parent=self.root,
            )
            if not confirmed:
                return
        self._start_export(destination, scale, image, mode)

    def _start_export(
        self,
        destination: Path,
        scale: int,
        image: Optional[core.BitmapImage] = None,
        mode: str = "strict",
    ) -> None:
        if image is None:
            image = self._effective_image()
        if image is None:
            return
        self.exporting = True
        self._export_cancel = threading.Event()
        self._export_queue = queue.Queue()
        self._refresh_action_states()
        self._set_status(
            f"正在逐像素生成 {image.opaque_count:,} 个正方形…",
            "busy",
        )
        self._progress_dialog = ExportProgressDialog(
            self.root,
            destination,
            image.opaque_count,
            self._cancel_export,
        )
        cancel_event = self._export_cancel
        output_queue = self._export_queue

        def progress(done: int, total: int) -> None:
            fraction = done / total if total else 0.0
            output_queue.put(
                (
                    "progress",
                    fraction,
                    int(image.opaque_count * fraction),
                )
            )

        def worker() -> None:
            try:
                result = core.export_svg(
                    image,
                    destination,
                    scale=scale,
                    progress=progress,
                    cancel=cancel_event.is_set,
                    mode=mode,
                )
            except core.ExportCancelled:
                output_queue.put(("cancelled", None))
            except Exception as exc:  # noqa: BLE001
                output_queue.put(("error", exc))
            else:
                output_queue.put(("done", result))

        threading.Thread(target=worker, daemon=True).start()
        self.root.after(50, self._poll_export)

    def _cancel_export(self) -> None:
        if self._export_cancel is not None:
            self._export_cancel.set()

    def _poll_export(self) -> None:
        try:
            while True:
                kind, *payload = self._export_queue.get_nowait()
                if kind == "progress":
                    fraction, done = payload
                    if self._progress_dialog is not None:
                        self._progress_dialog.set_progress(fraction, done)
                elif kind == "done":
                    self._finish_export(payload[0])
                    return
                elif kind == "cancelled":
                    self._finish_export_cancelled()
                    return
                elif kind == "error":
                    self._finish_export_error(payload[0])
                    return
        except queue.Empty:
            pass
        if self.exporting:
            self.root.after(50, self._poll_export)

    def _close_progress_dialog(self) -> None:
        if self._progress_dialog is not None:
            try:
                self._progress_dialog.close()
            except tk.TclError:
                pass
            self._progress_dialog = None

    def _finish_export(self, result: core.ExportResult) -> None:
        self.exporting = False
        self._close_progress_dialog()
        self.last_output = result.path
        self.output_path = result.path
        self._update_output_path_label()
        self._refresh_action_states()
        mode_text = (
            f"紧凑 path · {result.color_paths:,} 条"
            if result.mode == "compact"
            else "严格 rect"
        )
        self._set_status(
            f"已导出 {result.rect_count:,} 个正方形 · {mode_text} · "
            f"{core.human_size(result.file_size)} · {result.elapsed:.1f}s",
            "done",
        )
        self._toast(
            f"SVG 已保存 · {result.rect_count:,} 个正方形 · "
            f"{core.human_size(result.file_size)}"
        )
        self._schedule_thumb()
        if self._pending_close:
            self.root.after(120, self.root.destroy)

    def _finish_export_cancelled(self) -> None:
        self.exporting = False
        self._close_progress_dialog()
        self._refresh_action_states()
        self._set_status("已取消导出，未写入目标文件", "ready")
        if self._pending_close:
            self.root.destroy()

    def _finish_export_error(self, error: Exception) -> None:
        self.exporting = False
        self._close_progress_dialog()
        self._refresh_action_states()
        self._set_status("导出失败", "error")
        if self._pending_close:
            self.root.destroy()
            return
        messagebox.showerror(
            "导出失败", f"无法写入 SVG：\n\n{error}", parent=self.root
        )

    def open_output_folder(self) -> None:
        if self.last_output is None:
            return
        folder = self.last_output.parent
        try:
            if sys.platform == "win32":
                if self.last_output.exists():
                    subprocess.Popen(
                        ["explorer", "/select,", str(self.last_output)]
                    )
                else:
                    os.startfile(str(folder))  # type: ignore[attr-defined]
            elif sys.platform == "darwin":
                if self.last_output.exists():
                    subprocess.Popen(["open", "-R", str(self.last_output)])
                else:
                    subprocess.Popen(["open", str(folder)])
            else:
                subprocess.Popen(["xdg-open", str(folder)])
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror(
                "无法打开文件夹", str(exc), parent=self.root
            )

    # ------------------------------------------------------------------
    # 关于 / 关闭
    # ------------------------------------------------------------------

    def _show_about(self) -> None:
        dialog = tk.Toplevel(self.root)
        dialog.title("关于 Pixel2SVG")
        dialog.configure(bg=BG)
        dialog.resizable(False, False)
        dialog.transient(self.root)
        width, height = px(540), px(408)
        x = self.root.winfo_rootx() + (
            self.root.winfo_width() - width
        ) // 2
        y = self.root.winfo_rooty() + (
            self.root.winfo_height() - height
        ) // 2
        dialog.geometry(f"{width}x{height}+{max(0, x)}+{max(0, y)}")
        try:
            dialog.iconphoto(False, self._app_icon)
        except tk.TclError:
            pass

        header = tk.Frame(dialog, bg=INK, height=px(76))
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(
            header,
            text="Pixel2SVG",
            bg=INK,
            fg="#F8F4EC",
            font=(FONT_MONO, 18, "bold"),
        ).pack(anchor="w", padx=px(22), pady=(px(13), 0))
        tk.Label(
            header,
            text="把位图翻译成矢量方格，而不是猜测轮廓。",
            bg=INK,
            fg="#91A69F",
            font=(FONT_UI, 9),
        ).pack(anchor="w", padx=px(22))

        body = tk.Frame(dialog, bg=BG)
        body.pack(fill="both", expand=True, padx=px(22), pady=px(15))
        tk.Label(
            body,
            text=(
                "市面上的矢量转换工具通常会拟合曲线、合并色块、简化细节。"
                "Pixel2SVG 不这样做：它逐个读取像素，把每个不透明像素"
                "翻译成一个 1×1 的矢量正方形。"
            ),
            bg=BG,
            fg=INK,
            justify="left",
            anchor="w",
            wraplength=px(470),
            font=(FONT_UI, 9),
        ).pack(fill="x")
        code = tk.Label(
            body,
            text=(
                '<rect x="12" y="7" width="1" height="1"\n'
                '       fill="#e45c3a" fill-opacity="0.72"/>'
            ),
            bg=INK,
            fg="#C7DDD3",
            justify="left",
            anchor="w",
            font=(FONT_MONO, 9),
            padx=px(12),
            pady=px(9),
        )
        code.pack(fill="x", pady=(px(11), px(10)))
        tk.Label(
            body,
            text=(
                "完全透明的像素不会生成图形；半透明像素保留 alpha，"
                "写入 fill-opacity。输出根节点使用 viewBox 与 "
                'shape-rendering="crispEdges"，放大后边缘依然干净。'
            ),
            bg=BG,
            fg=MUTED,
            justify="left",
            anchor="w",
            wraplength=px(470),
            font=(FONT_UI, 8),
        ).pack(fill="x")

        shortcuts = tk.Frame(body, bg=PANEL, highlightbackground=BORDER, highlightthickness=1)
        shortcuts.pack(fill="x", pady=(px(12), 0))
        shortcut_text = (
            "Ctrl+O  打开图片      Ctrl+S  导出 SVG\n"
            "Ctrl+0  适合窗口      Ctrl+1  1:1 显示\n"
            "滚轮缩放  拖动平移  双击预览  适合窗口"
        )
        tk.Label(
            shortcuts,
            text=shortcut_text,
            bg=PANEL,
            fg=MUTED,
            justify="left",
            anchor="w",
            font=(FONT_MONO, 8),
            padx=px(12),
            pady=px(8),
        ).pack(fill="x")

        if core.HAS_PILLOW:
            try:
                from PIL import __version__ as pillow_version

                backend = f"Pillow {pillow_version}"
            except Exception:
                backend = "Pillow"
        else:
            backend = "Windows WIC" if core.IS_WINDOWS else "不可用"
        tk.Label(
            body,
            text=f"解码器：{backend}   ·   版本 {APP_VERSION}",
            bg=BG,
            fg=MUTED_LIGHT,
            anchor="w",
            font=(FONT_MONO, 7),
        ).pack(fill="x", pady=(px(9), 0))

        close = tk.Button(
            dialog,
            text="关闭",
            command=dialog.destroy,
            bg=ACCENT,
            fg="#FFFFFF",
            activebackground=ACCENT_DARK,
            activeforeground="#FFFFFF",
            font=(FONT_UI, 9, "bold"),
            relief="flat",
            bd=0,
            highlightthickness=0,
            padx=px(20),
            pady=px(7),
            cursor="hand2",
        )
        close.pack(side="right", padx=px(22), pady=(0, px(16)))
        dialog.bind("<Escape>", lambda _event: dialog.destroy())
        dialog.update_idletasks()
        try:
            dialog.grab_set()
        except tk.TclError:
            pass

    def _on_close(self) -> None:
        if self.exporting:
            close = messagebox.askyesno(
                "正在导出",
                "SVG 还没有写完。取消导出并退出吗？",
                parent=self.root,
            )
            if not close:
                return
            self._pending_close = True
            self._set_status("正在取消导出…", "busy")
            self._cancel_export()
            return
        self.root.destroy()

    def run(self) -> None:
        self.root.mainloop()
