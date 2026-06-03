#!/usr/bin/env python3
"""Build a self-contained, uploadable Agent Skill bundle: dist/landmine-screen.zip

Mirrors the repo layout (so the CLI's default config/fixture paths resolve) with
SKILL.md at the archive root, the `landmine` package, config, and offline
fixtures vendored in — so the skill runs in a hosted sandbox with no repo
checkout and no SEC network egress. Run: `python scripts/build_skill.py`.
"""
from __future__ import annotations

import pathlib
import shutil
import zipfile

ROOT = pathlib.Path(__file__).resolve().parent.parent
SKILL_SRC = ROOT / ".claude" / "skills" / "landmine-screen"
OUT_DIR = ROOT / "dist" / "landmine-screen"
ZIP_PATH = ROOT / "dist" / "landmine-screen.zip"

_OLD_SETUP = ('## Setup\n\n```bash\n'
              'pip install -e .        # exposes the `landmine` command; '
              'or use `python -m landmine`\n```')
_NEW_SETUP = ('## Setup (self-contained bundle)\n\n'
              'This skill bundles the `landmine` package, `config/`, and offline data\n'
              "fixtures, so it runs with no repo checkout and no network. From this\n"
              "skill's directory:\n\n```bash\n"
              'pip install -e .            # installs the `landmine` CLI (only dep: PyYAML)\n'
              '# or, without installing:  export PYTHONPATH="$PWD"   '
              'then use `python -m landmine`\n'
              '```\n\n'
              'Default config/fixture paths resolve inside the bundle. Live SEC fetch\n'
              '(`--source claude`/`--filing-source edgar`/`--source sec`) needs network\n'
              'egress; without it, the bundled fixtures (a small demo universe) are used.')


def _clean_pycache(base: pathlib.Path) -> None:
    for p in base.rglob("__pycache__"):
        shutil.rmtree(p, ignore_errors=True)
    for p in base.rglob("*.pyc"):
        p.unlink(missing_ok=True)


def main() -> None:
    if OUT_DIR.exists():
        shutil.rmtree(OUT_DIR)
    OUT_DIR.mkdir(parents=True)
    shutil.copytree(ROOT / "landmine", OUT_DIR / "landmine")
    shutil.copytree(ROOT / "config", OUT_DIR / "config")
    shutil.copytree(ROOT / "tests" / "fixtures", OUT_DIR / "tests" / "fixtures")
    shutil.copy(ROOT / "pyproject.toml", OUT_DIR / "pyproject.toml")
    shutil.copy(SKILL_SRC / "reference.md", OUT_DIR / "reference.md")

    skill = (SKILL_SRC / "SKILL.md").read_text(encoding="utf-8")
    assert _OLD_SETUP in skill, "SKILL.md setup block changed; update build_skill.py"
    (OUT_DIR / "SKILL.md").write_text(skill.replace(_OLD_SETUP, _NEW_SETUP),
                                      encoding="utf-8")
    _clean_pycache(OUT_DIR)

    with zipfile.ZipFile(ZIP_PATH, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(OUT_DIR.rglob("*")):
            if p.is_file():
                z.write(p, p.relative_to(OUT_DIR))   # SKILL.md at archive root
    n = len(zipfile.ZipFile(ZIP_PATH).namelist())
    print(f"Wrote {ZIP_PATH} ({ZIP_PATH.stat().st_size // 1024} KB, {n} files)")


if __name__ == "__main__":
    main()
