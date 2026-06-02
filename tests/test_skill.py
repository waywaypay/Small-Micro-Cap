"""Validate the packaged Claude skill is well-formed and points at real commands."""
import os
import re

import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SKILL_DIR = os.path.join(ROOT, ".claude", "skills", "landmine-screen")


def _skill_text():
    with open(os.path.join(SKILL_DIR, "SKILL.md"), encoding="utf-8") as fh:
        return fh.read()


def _frontmatter(text):
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    assert m, "SKILL.md must start with a YAML frontmatter block"
    return yaml.safe_load(m.group(1))


def test_frontmatter_has_name_and_trigger_description():
    fm = _frontmatter(_skill_text())
    assert fm["name"] == "landmine-screen"
    desc = fm["description"].lower()
    assert len(desc) > 80                      # a real, specific trigger description
    assert "landmine" in desc and "sec" in desc
    # mentions what it screens for (trigger coverage)
    assert any(k in desc for k in ("distress", "going-concern", "dilution",
                                   "cash runway"))


def test_body_points_at_real_cli_commands():
    body = _skill_text()
    for cmd in ("landmine run", "landmine universe", "landmine language-batch"):
        assert cmd in body, f"SKILL.md should document `{cmd}`"
    # progressive-disclosure reference exists
    assert os.path.exists(os.path.join(SKILL_DIR, "reference.md"))


def test_documented_subcommands_exist_in_the_cli():
    from landmine.cli import build_parser
    sub = next(a for a in build_parser()._actions
               if a.__class__.__name__ == "_SubParsersAction")
    for cmd in ("run", "universe", "calibrate", "backtest", "language",
                "language-batch"):
        assert cmd in sub.choices, f"CLI is missing documented subcommand `{cmd}`"
