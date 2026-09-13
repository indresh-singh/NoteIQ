import json
import struct
from zipfile import ZipFile

from scripts.package_teams import main


def test_package_uses_public_tab_and_contains_no_bot_or_secrets(config, tmp_path, monkeypatch):
    package = tmp_path / "noteiq.zip"
    monkeypatch.setattr("sys.argv", ["package_teams", "--output", str(package)])
    main()
    with ZipFile(package) as archive:
        assert set(archive.namelist()) == {"manifest.json", "color.png", "outline.png"}
        content = archive.read("manifest.json").decode()
        manifest = json.loads(content)
        assert "bots" not in manifest
        assert "packageName" not in manifest
        assert manifest["webApplicationInfo"]["id"] == str(config.graph_client_id)
        assert manifest["authorization"]["permissions"]["resourceSpecific"] == [
            {"name": "TeamsActivity.Send.User", "type": "Application"}
        ]
        assert {item["type"] for item in manifest["activities"]["activityTypes"]} == {
            "transcriptReady",
            "insightsReady",
        }
        assert "${" not in content
        assert manifest["$schema"].startswith("https://developer.microsoft.com/")
        assert manifest["staticTabs"][0]["contentUrl"] == config.public_url + "/"
        assert manifest["validDomains"] == ["noteiq.test"]
        assert config.graph_secret.get_secret_value() not in content
        for name, size in (("color.png", 192), ("outline.png", 32)):
            assert struct.unpack(">II", archive.read(name)[16:24]) == (size, size)
