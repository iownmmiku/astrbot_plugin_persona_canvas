"""Build an installable source ZIP without runtime data or credentials."""
from __future__ import annotations

import argparse
import re
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "__init__.py", "main.py", "active.py", "dialogue.py", "intent.py", "companion.py",
    "moderation.py", "storage.py", "page_api.py", "webui_server.py",
    "metadata.yaml", "_conf_schema.json", "requirements.txt", "README.md", "CHANGELOG.md", "logo.png",
    "providers/__init__.py", "providers/base.py",
    "pages/canvas/index.html", "pages/canvas/app.js", "pages/canvas/app.css",
)


def build(output: Path | None = None) -> Path:
    metadata = (ROOT / "metadata.yaml").read_text(encoding="utf-8")
    match = re.search(r"^version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$", metadata, re.MULTILINE)
    if not match:
        raise ValueError("metadata.yaml must declare a semantic version")
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    if not re.search(r"^##\s+" + re.escape(match[1]) + r"(?:\s|$)", changelog, re.MULTILINE):
        raise ValueError(f"CHANGELOG.md must include an entry for version {match[1]}")
    # AstrBot derives the installed Python package directory from the ZIP name.
    # Keep it stable and identifier-safe; the release version lives in metadata.
    output = (output or ROOT.parent / f"{ROOT.name}.zip").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    for relative in SOURCE_FILES:
        source = (ROOT / relative).resolve()
        if not source.is_relative_to(ROOT) or not source.is_file():
            raise ValueError(f"Missing source file: {relative}")
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="persona-canvas-", suffix=".zip", dir=output.parent, delete=False) as handle:
            temporary = Path(handle.name)
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            # Older AstrBot installers expect a GitHub-style enclosing directory
            # as the first entry, then flatten its contents into the plugin path.
            prefix = ROOT.name + "/"
            directory = zipfile.ZipInfo(prefix, date_time=(2020, 1, 1, 0, 0, 0))
            directory.external_attr = (0o40755 << 16) | 0x10
            archive.writestr(directory, b"")
            for relative in SOURCE_FILES:
                entry = zipfile.ZipInfo(prefix + relative, date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = zipfile.ZIP_DEFLATED
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, (ROOT / relative).read_bytes())
        with zipfile.ZipFile(temporary) as archive:
            expected = {prefix, *(prefix + relative for relative in SOURCE_FILES)}
            if archive.testzip() is not None or archive.namelist()[0] != prefix or set(archive.namelist()) != expected:
                raise ValueError("ZIP integrity check failed")
        temporary.replace(output)
        return output
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    package = build(args.output)
    print(f"{package} ({package.stat().st_size} bytes; {len(SOURCE_FILES)} source files)")
