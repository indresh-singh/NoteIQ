from app.config import ROOT


def test_note_heading_fallback_does_not_truncate_the_note_text():
    script = (ROOT / "web/app.js").read_text(encoding="utf-8")
    assert 'note.title || note.text || "Note"' in script
    assert '(note.text || "Note").slice(0, 60)' not in script
