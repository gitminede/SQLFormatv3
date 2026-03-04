# SQL Formatter GUI (EBH style)

Kis, dependency-free (Tkinter) GUI alkalmazás, ami:

- inputként kap SQL-t **Ctrl+V**-vel vagy **fájl kiválasztással**
- lefuttat egy formázót (jelenleg: CREATE TABLE igazítás + pár alap szabály)
- megmutatja a kimenetet
- tud **menteni** `.sql` fájlba (UTF-8)

> A formázó modul szándékosan egyszerű és bővíthető.

## Futatás fejlesztés közben

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Linux/macOS: source .venv/bin/activate
pip install -e .
python src/app.py
```

## EXE build (lokálisan)

```bash
pip install -r requirements-dev.txt
pyinstaller --noconfirm --clean --onefile --windowed --name sql-formatter-gui src/app.py
```

Az EXE a `dist/` mappába kerül.

## GitHub Actions build

A repo tartalmaz egy workflow-t, ami Windows-on elkészíti az EXE-t és artifactként publikálja.

- `.github/workflows/build-windows.yml`

## Formázási szabályok (jelenleg)

- `CREATE TABLE` oszlopok igazítása (vessző bal oldalon)
- `VARCHAR (50)` és hasonló típusok zárójelezése: `VARCHAR (50)`
- `-------------------------------------------------------------------------------` szeparátor normalizálás
- blokk kommentek (`/* ... */`) megőrzése

A logika: `src/sql_formatter/formatter.py`.
