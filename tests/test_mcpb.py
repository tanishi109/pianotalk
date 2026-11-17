"""The .mcpb bundle: layout, manifest and version consistency."""
import importlib.util
import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_builder():
    spec = importlib.util.spec_from_file_location("build_mcpb", ROOT / "scripts" / "build-mcpb.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_bundle_layout_and_manifest(tmp_path):
    out = load_builder().build(tmp_path)
    names = set(zipfile.ZipFile(out).namelist())
    assert {"manifest.json", "mcpb_main.py", "pyproject.toml", "uv.lock", "pianotalk/server.py"} <= names
    assert not any(n.startswith((".venv", "tests/", "dist/")) or "__pycache__" in n for n in names)

    m = json.loads(zipfile.ZipFile(out).read("manifest.json"))
    assert m["manifest_version"] == "0.4" and m["server"]["type"] == "uv"
    assert out.name == f"pianotalk-{m['version']}.mcpb"
    # the manifest lists exactly the tools the server provides
    import asyncio
    from pianotalk.server import mcp
    served = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {t["name"] for t in m["tools"]} == served
