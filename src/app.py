from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox
from pathlib import Path

from sql_formatter import format_sql
from sql_formatter.formatter import decode_bytes_best_effort

APP_TITLE = "SQL Formatter (EBH style)"

class App(tk.Tk):
    ...
