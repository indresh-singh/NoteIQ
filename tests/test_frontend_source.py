import shutil
import subprocess

import pytest

from app.config import ROOT


def test_javascript_sources_parse():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is not installed")
    for script in (ROOT / "web").glob("*.js"):
        subprocess.run([node, "--check", script], check=True, capture_output=True, text=True)


def test_note_heading_fallback_does_not_truncate_the_note_text():
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'optionalText(note.title) || optionalText(note.text) || "Note"' in script
    assert 'value.trim().toLowerCase() === "null"' in script
    assert '(note.text || "Note").slice(0, 60)' not in script


def test_action_exports_use_separate_native_destination_selects():
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'createDestinationSelect(\n      lists, "list_id", "list_name"' in script
    assert 'createDestinationSelect(\n      plans, "plan_id", "plan_name"' in script
    assert "createOptionPicker" not in script
    assert "plans.length > 1" not in script
    assert "lists.length > 1" not in script
