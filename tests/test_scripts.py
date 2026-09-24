import io
import sqlite3
import tarfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from scripts import configure, migrate_database, serve, vendor_assets


def test_environment_deploy_commands_generate_tags_internally():
    root = Path(__file__).resolve().parents[1]
    dev = (root / "scripts/deploy_dev.sh").read_text()
    prod = (root / "scripts/deploy_prod.sh").read_text()
    engine = (root / "scripts/deploy_daio.sh").read_text()

    assert 'NOTEIQ_EXPECTED_APP_ENV="dev"' in dev
    assert 'NOTEIQ_EXPECTED_APP_ENV="prod"' in prod
    assert '.env.dev' in dev
    assert '.env.prod' in prod
    assert "NOTEIQ_DEPLOY_GRAPH_CLIENT_ID" not in dev + prod
    assert "NOTEIQ_DEPLOY_SUBSCRIPTION_ID" in engine
    assert "NOTEIQ_DEPLOY_GRAPH_CLIENT_SECRET" in engine
    assert "NOTEIQ_DEPLOY_GRAPH_CLIENT_STATE" in engine
    assert "UNIQUE_TAG" not in dev + prod
    assert "dotenv_values" in engine
    assert 'readonly tag="release${timestamp}${commit}${nonce}"' in engine


def test_configure_writes_validated_single_line_environment(monkeypatch, tmp_path):
    answers = iter(
        [
            "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
            "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
            "https://noteiq.test/",
        ]
    )
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    monkeypatch.setattr("builtins.input", lambda prompt="": next(answers))
    monkeypatch.setattr(configure.getpass, "getpass", lambda prompt="": "secret'value")
    monkeypatch.setattr(configure.secrets, "token_urlsafe", lambda size: "generated-state")
    monkeypatch.setattr("sys.argv", ["configure"])

    configure.main()

    assert (tmp_path / ".env.dev").read_text().splitlines() == [
        "AZURE_TENANT_ID='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'",
        "GRAPH_CLIENT_ID='bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb'",
        "GRAPH_CLIENT_SECRET='secret\\'value'",
        "PUBLIC_BASE_URL='https://noteiq.test'",
        "GRAPH_CLIENT_STATE='generated-state'",
    ]


def test_configure_never_overwrites_existing_credentials(monkeypatch, tmp_path):
    path = tmp_path / ".env.dev"
    path.write_text("KEEP=me\n")
    monkeypatch.setattr(configure, "ROOT", tmp_path)
    monkeypatch.setattr("sys.argv", ["configure", "--app-env", "stg"])

    with pytest.raises(SystemExit, match="already exists"):
        configure.main()

    assert path.read_text() == "KEEP=me\n"


def test_serve_starts_uvicorn_without_access_logging(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    vendor = tmp_path / "web" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "teams.min.js").write_text("sdk")
    monkeypatch.setattr(
        serve, "settings", lambda: SimpleNamespace(public_url="https://noteiq.test")
    )
    configure = Mock()
    run = Mock()
    monkeypatch.setattr(serve, "configure_logging", configure)
    monkeypatch.setattr(serve.uvicorn, "run", run)
    monkeypatch.setenv("NOTEIQ_HOST", "0.0.0.0")
    monkeypatch.setenv("PORT", "9000")

    serve.main()

    configure.assert_called_once_with()
    run.assert_called_once_with(
        "app.web:app", host="0.0.0.0", port=9000, workers=1, access_log=False
    )


@pytest.mark.parametrize(
    "port,message",
    [
        ("abc", "PORT must be a number"),
        ("0", "PORT must be between"),
        ("65536", "PORT must be between"),
    ],
)
def test_serve_rejects_invalid_ports(monkeypatch, tmp_path, port, message):
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    vendor = tmp_path / "web" / "vendor"
    vendor.mkdir(parents=True)
    (vendor / "teams.min.js").write_text("sdk")
    monkeypatch.setattr(
        serve, "settings", lambda: SimpleNamespace(public_url="https://noteiq.test")
    )
    monkeypatch.setenv("PORT", port)

    with pytest.raises(SystemExit, match=message):
        serve.main()


def test_serve_requires_vendored_browser_sdk(monkeypatch, tmp_path):
    monkeypatch.setattr(serve, "ROOT", tmp_path)
    monkeypatch.setattr(
        serve, "settings", lambda: SimpleNamespace(public_url="https://noteiq.test")
    )

    with pytest.raises(SystemExit, match="Browser SDK assets are missing"):
        serve.main()


def _package(files: dict[str, bytes]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as archive:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            archive.addfile(member, io.BytesIO(content))
    return stream.getvalue()


def test_vendor_assets_verifies_and_writes_package(monkeypatch, tmp_path):
    import base64
    import hashlib
    import json

    archive = _package({"package/dist/sdk.min.js": b"sdk", "package/LICENSE": b"license"})
    integrity = "sha512-" + base64.b64encode(hashlib.sha512(archive).digest()).decode()
    metadata = {"dist": {"tarball": "https://registry.test/sdk.tgz", "integrity": integrity}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            value = metadata if url.endswith("/1.0.0") else archive
            return SimpleNamespace(
                content=value if isinstance(value, bytes) else b"",
                json=lambda: value,
                raise_for_status=lambda: SimpleNamespace(
                    content=value if isinstance(value, bytes) else b"", json=lambda: value
                ),
            )

    monkeypatch.setattr(vendor_assets, "ROOT", tmp_path)
    monkeypatch.setattr(vendor_assets, "PACKAGES", [("sdk", "1.0.0", "sdk.min.js", "sdk.js")])
    monkeypatch.setattr(vendor_assets.httpx, "Client", Client)

    vendor_assets.main()

    directory = tmp_path / "web" / "vendor"
    assert (directory / "sdk.js").read_bytes() == b"sdk"
    assert (directory / "sdk.js.LICENSE").read_bytes() == b"license"
    records = json.loads((directory / "manifest.json").read_text())
    assert records[0]["sha256"] == hashlib.sha256(b"sdk").hexdigest()


def test_vendor_assets_rejects_integrity_mismatch(monkeypatch, tmp_path):
    metadata = {"dist": {"tarball": "https://registry.test/sdk.tgz", "integrity": "sha512-invalid"}}

    class Client:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, url):
            value = metadata if url.endswith("/1.0.0") else b"archive"
            return SimpleNamespace(
                content=value if isinstance(value, bytes) else b"",
                json=lambda: value,
                raise_for_status=lambda: SimpleNamespace(
                    content=value if isinstance(value, bytes) else b"", json=lambda: value
                ),
            )

    monkeypatch.setattr(vendor_assets, "ROOT", tmp_path)
    monkeypatch.setattr(vendor_assets, "PACKAGES", [("sdk", "1.0.0", "sdk.min.js", "sdk.js")])
    monkeypatch.setattr(vendor_assets.httpx, "Client", Client)

    with pytest.raises(ValueError, match="integrity check failed"):
        vendor_assets.main()


def test_migration_rejects_missing_source(tmp_path):
    with pytest.raises(ValueError, match="SQLite database not found"):
        migrate_database.migrate(tmp_path / "missing.sqlite3", "postgresql://test")


def test_migration_copies_only_existing_tables(monkeypatch, tmp_path):
    source = tmp_path / "source.sqlite3"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE users (id TEXT, name TEXT, status TEXT, enabled INTEGER)")
        db.execute("INSERT INTO users VALUES ('u', 'User', 'LISTENING', 1)")

    executed = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=()):
            executed.append((sql, params))
            return SimpleNamespace(fetchone=lambda: None)

        def executemany(self, sql, rows):
            executed.append((sql, list(rows)))

    target = SimpleNamespace(
        connect=lambda: Connection(), backfill_meeting_facts=Mock(), close=Mock()
    )
    monkeypatch.setattr(migrate_database, "Store", lambda *args, **kwargs: target)

    migrate_database.migrate(source, "postgresql://test")

    insert = next(item for item in executed if item[0].startswith("INSERT INTO users"))
    assert insert[1] == [("u", "User", "LISTENING", 1)]
    target.backfill_meeting_facts.assert_called_once_with()
    target.close.assert_called_once_with()
