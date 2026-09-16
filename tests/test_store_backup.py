from app.store import Store


def test_store_restores_and_saves_mounted_snapshot(tmp_path):
    backup = tmp_path / "mounted" / "noteiq.sqlite3"
    first = Store(tmp_path / "first.sqlite3", backup)
    first.enroll("user", "Demo user")

    restored = Store(tmp_path / "new-container.sqlite3", backup)
    assert restored.user("user")["name"] == "Demo user"
