"""Pack dist/pianotalk.mcpb (a zip with manifest.json at the root).

uv-type bundle: ships source + pyproject.toml + uv.lock, never a .venv.
"""
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "dist" / "pianotalk.mcpb"
FILES = ["pyproject.toml", "uv.lock", ".python-version", "README.md", "LICENSE"]

OUT.parent.mkdir(exist_ok=True)
with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
    for name in ("manifest.json", "mcpb_main.py"):
        z.write(ROOT / "mcpb" / name, name)
    for name in FILES:
        z.write(ROOT / name, name)
    for p in sorted((ROOT / "pianotalk").rglob("*.py")):
        z.write(p, p.relative_to(ROOT).as_posix())
print(OUT)
for i in zipfile.ZipFile(OUT).infolist():
    print(f"  {i.file_size:7d}  {i.filename}")
