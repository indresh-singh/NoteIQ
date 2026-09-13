"""Refresh the pinned browser SDK files; no Node installation or runtime CDN needed."""

import base64
import hashlib
import io
import json
import tarfile

import httpx

from app.config import ROOT

PACKAGES = [
    ("@microsoft/teams-js", "2.56.0", "MicrosoftTeams.min.js", "teams.min.js"),
    ("adaptivecards", "3.0.6", "adaptivecards.min.js", "adaptivecards.min.js"),
]


def main():
    directory = ROOT / "web/vendor"
    directory.mkdir(parents=True, exist_ok=True)
    records = []
    with httpx.Client(timeout=60) as client:
        for package, version, filename, output in PACKAGES:
            metadata = (
                client.get(f"https://registry.npmjs.org/{package}/{version}")
                .raise_for_status()
                .json()
            )
            content = client.get(metadata["dist"]["tarball"]).raise_for_status().content
            algorithm, expected = metadata["dist"]["integrity"].split("-", 1)
            actual = base64.b64encode(hashlib.new(algorithm, content).digest()).decode()
            if actual != expected:
                raise ValueError(f"Package integrity check failed: {package}")
            with tarfile.open(fileobj=io.BytesIO(content), mode="r:gz") as archive:
                matches = [
                    item for item in archive.getmembers() if item.name.endswith("/" + filename)
                ]
                if len(matches) != 1:
                    raise ValueError(
                        f"Unexpected package layout: {[i.name for i in archive.getmembers() if i.name.endswith('.min.js')]}"
                    )
                member = matches[0]
                asset = archive.extractfile(member).read()
                (directory / output).write_bytes(asset)
                for item in archive.getmembers():
                    if item.name.lower() in {
                        "package/license",
                        "package/license.txt",
                        "package/license.md",
                    }:
                        (directory / (output + ".LICENSE")).write_bytes(
                            archive.extractfile(item).read()
                        )
            records.append(
                {
                    "package": package,
                    "version": version,
                    "file": output,
                    "sha256": hashlib.sha256(asset).hexdigest(),
                    "source": metadata["dist"]["tarball"],
                    "integrity": metadata["dist"]["integrity"],
                }
            )
            print(f"Bundled {package} {version}")
    (directory / "manifest.json").write_text(json.dumps(records, indent=2) + "\n")


if __name__ == "__main__":
    main()
