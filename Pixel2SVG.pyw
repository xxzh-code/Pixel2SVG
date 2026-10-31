#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无控制台窗口的启动器：双击即可打开 Pixel2SVG。"""

from __future__ import annotations

import sys
import traceback


def _show_fatal_error(message: str) -> None:
    try:
        import tkinter as tk
        from tkinter import messagebox

        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Pixel2SVG 启动失败", message, parent=root)
        root.destroy()
    except Exception:
        traceback.print_exc()


def _main() -> int:
    try:
        from pixel2svg import main

        return main()
    except SystemExit:
        raise
    except Exception:
        _show_fatal_error(traceback.format_exc())
        return 1


if __name__ == "__main__":
    raise SystemExit(_main())
