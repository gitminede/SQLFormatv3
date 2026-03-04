from __future__ import annotations

import os
import tempfile
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

from sql_formatter import format_sql
from sql_formatter.formatter import decode_bytes_best_effort

APP_TITLE = "SQL Formatter (EBH style)"


def _log_path() -> str:
    return os.path.join(tempfile.gettempdir(), "sql_formatter_gui.log")


def _write_log(msg: str):
    try:
        with open(_log_path(), "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.geometry("1200x800")
        self._current_path: Path | None = None
        self._build_ui()

    def _build_ui(self):
        bar = tk.Frame(self, padx=8, pady=6)
        bar.pack(side=tk.TOP, fill=tk.X)

        tk.Button(bar, text="Beolvasás…", command=self.load_file).pack(side=tk.LEFT, padx=4)
        tk.Button(bar, text="Mentés…", command=self.save_file).pack(side=tk.LEFT, padx=4)
        tk.Button(bar, text="Formázás →", command=self.format_now).pack(side=tk.LEFT, padx=12)
        tk.Button(bar, text="Log megnyitása", command=self.open_log).pack(side=tk.LEFT, padx=12)

        pan = tk.PanedWindow(self, sashrelief=tk.RAISED, sashwidth=6, orient=tk.HORIZONTAL)
        pan.pack(fill=tk.BOTH, expand=True)

        left = tk.Frame(pan)
        right = tk.Frame(pan)
        pan.add(left)
        pan.add(right)

        tk.Label(left, text="Bemenet (eredeti SQL)").pack(anchor="w", padx=8, pady=(8, 0))
        self.in_text = tk.Text(left, wrap=tk.NONE, undo=True)
        self.in_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        tk.Label(right, text="Kimenet (formázott SQL)").pack(anchor="w", padx=8, pady=(8, 0))
        self.out_text = tk.Text(right, wrap=tk.NONE)
        self.out_text.pack(fill=tk.BOTH, expand=True, padx=8, pady=8)

        self.status = tk.StringVar(value="Készen")
        sb = tk.Label(self, textvariable=self.status, anchor="w", padx=8)
        sb.pack(side=tk.BOTTOM, fill=tk.X)

        self.bind_all("<Control-s>", lambda _e: self.save_file())
        self.bind_all("<Control-o>", lambda _e: self.load_file())
        self.bind_all("<Control-Return>", lambda _e: self.format_now())

    def open_log(self):
        p=_log_path()
        if not os.path.exists(p):
            messagebox.showinfo(APP_TITLE, f"Nincs log: {p}")
            return
        os.startfile(p)  # type: ignore[attr-defined]

    def set_status(self, msg: str):
        self.status.set(msg)
        self.update_idletasks()

    def load_file(self):
        path = filedialog.askopenfilename(
            title="SQL fájl kiválasztása",
            filetypes=[("SQL", "*.sql"), ("All", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        dec = decode_bytes_best_effort(p.read_bytes())
        self.in_text.delete("1.0", tk.END)
        self.in_text.insert("1.0", dec.text)
        self._current_path = p
        self.set_status(f"Beolvasva: {p.name} (encoding: {dec.encoding})")

    def save_file(self):
        default = None
        if self._current_path:
            default = self._current_path.with_name(self._current_path.stem + "_formatted.sql")

        path = filedialog.asksaveasfilename(
            title="Mentés",
            initialfile=(default.name if default else "formatted.sql"),
            defaultextension=".sql",
            filetypes=[("SQL", "*.sql"), ("All", "*.*")],
        )
        if not path:
            return
        p = Path(path)
        content = self.out_text.get("1.0", tk.END)
        p.write_text(content, encoding="utf-8")
        self.set_status(f"Mentve: {p.name}")

    def format_now(self):
        src = self.in_text.get("1.0", tk.END)
        if not src.strip():
            messagebox.showinfo(APP_TITLE, "Nincs bemeneti tartalom.")
            return
        try:
            res = format_sql(src)
        except Exception:
            _write_log("\n--- FORMAT ERROR ---\n" + traceback.format_exc())
            messagebox.showerror(APP_TITLE, f"Formázási hiba. Részletek: {_log_path()}")
            return
        self.out_text.delete("1.0", tk.END)
        self.out_text.insert("1.0", res)
        self.set_status(f"Kész (input: {len(src)} char, output: {len(res)} char)")


def main():
    _write_log("\n--- START ---")
    try:
        app = App()
        app.mainloop()
    except Exception:
        _write_log("\n--- STARTUP ERROR ---\n" + traceback.format_exc())
        raise


if __name__ == "__main__":
    main()
