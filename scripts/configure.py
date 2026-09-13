"""Create local configuration without putting the client secret in shell history."""

import getpass
import secrets
from uuid import UUID

from app.config import ROOT, Settings


def main():
    path = ROOT / ".env"
    if path.exists():
        raise SystemExit(
            ".env already exists. Edit it directly; existing credentials were preserved."
        )
    print("Use the note-iq app registration in your Microsoft 365 tenant.")
    tenant = UUID(input("Directory (tenant) ID: ").strip())
    client = UUID(input("Application (client) ID: ").strip())
    secret = getpass.getpass("Client secret VALUE (hidden): ").strip()
    url = (
        input("Public HTTPS origin (for example https://noteiq.yourdomain.ae): ")
        .strip()
        .rstrip("/")
    )
    state = secrets.token_urlsafe(48)
    Settings(
        tenant_id=tenant,
        graph_client_id=client,
        graph_secret=secret,
        public_url=url,
        client_state=state,
    )
    from urllib.parse import urlsplit

    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or not parts.hostname
        or parts.path
        or parts.query
        or parts.fragment
        or parts.username
        or parts.password
        or not secret
    ):
        raise SystemExit("Use a nonempty secret and an HTTPS origin without a path.")
    values = {
        "AZURE_TENANT_ID": str(tenant),
        "GRAPH_CLIENT_ID": str(client),
        "GRAPH_CLIENT_SECRET": secret,
        "PUBLIC_BASE_URL": url,
        "GRAPH_CLIENT_STATE": state,
    }
    if any("\n" in value or "\r" in value for value in values.values()):
        raise SystemExit("Configuration values must be single lines.")
    with path.open("x") as output:
        path.chmod(0o600)
        for key, value in values.items():
            quoted = value.replace("\\", "\\\\").replace("'", "\\'")
            output.write(f"{key}='{quoted}'\n")
    print("Saved .env. Add this Web redirect URI in Entra → note-iq → Authentication:")
    print(url + "/auth/callback")


if __name__ == "__main__":
    main()
