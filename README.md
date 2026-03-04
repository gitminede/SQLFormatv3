# SQL Formatter GUI (EBH style)

GUI (Tkinter) app:

- Input: **Ctrl+V** paste or **file open**
- Format
- Show output
- Save output as `.sql` (UTF-8)

## Local run

```bash
python -m venv .venv
.venv\Scripts\activate
pip install -e .
python src/app.py
```

## Build EXE (Windows)

```bash
pip install -r requirements-dev.txt
pyinstaller --noconfirm --clean --onefile --windowed --name sql-formatter-gui src/app.py
```

## GitHub Actions

Workflow builds Windows EXE and uploads artifact.

## Formatter rules (v0.3)

- separator normalization: `-------------------------------------------------------------------------------`
- block comments preserved as-is (`/* ... */`)
- line comments preserved (`-- ...`)
- `--/*----` style line comments are never split
- CREATE TABLE:
  - comma on the left
  - align column name + type + constraints (`NOT NULL`, `NULL`, etc.)
  - keep inline `-- ...` at end of line
  - keep comment-only lines inside table exactly
  - CONSTRAINT lines are kept on one line (no wrapping)
