from app.config import ROOT


def test_note_heading_fallback_does_not_truncate_the_note_text():
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'note.title || note.text || "Note"' in script
    assert '(note.text || "Note").slice(0, 60)' not in script


def test_action_exports_use_separate_native_destination_selects():
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'createDestinationSelect(\n      lists, "list_id", "list_name"' in script
    assert 'createDestinationSelect(\n      plans, "plan_id", "plan_name"' in script
    assert "createOptionPicker" not in script
    assert "plans.length > 1" not in script
    assert "lists.length > 1" not in script
