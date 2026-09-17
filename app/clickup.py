"""Small ClickUp OAuth and task client. Tokens never leave the server."""

import httpx
from cryptography.fernet import Fernet, InvalidToken

from app.config import Settings

API = "https://api.clickup.com/api/v2"


class ClickUp:
    def __init__(self, config: Settings):
        self.config = config
        self.cipher = Fernet(config.clickup_token_key.get_secret_value())

    def encrypt(self, token: str) -> str:
        return self.cipher.encrypt(token.encode()).decode()

    def decrypt(self, value: str) -> str:
        try:
            return self.cipher.decrypt(value.encode()).decode()
        except InvalidToken as error:
            raise ValueError("ClickUp connection needs to be reconnected.") from error

    async def exchange(self, code: str) -> str:
        response = await self.request(
            "POST",
            "/oauth/token",
            json={
                "client_id": self.config.clickup_client_id,
                "client_secret": self.config.clickup_client_secret.get_secret_value(),
                "code": code,
            },
            auth=False,
        )
        token = response.get("access_token")
        if not isinstance(token, str) or not token:
            raise ValueError("ClickUp did not return an access token.")
        return token

    async def workspaces(self, token: str) -> list[dict]:
        result = await self.request("GET", "/team", token=token)
        return [
            {"id": str(item["id"]), "name": item.get("name") or "ClickUp Workspace"}
            for item in result.get("teams", [])
            if item.get("id") is not None
        ]

    async def list_name(self, token: str, list_id: str) -> str:
        result = await self.request("GET", f"/list/{list_id}", token=token)
        name = result.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("ClickUp did not return a List name.")
        return name

    async def create_task(self, token: str, list_id: str, name: str, description: str) -> dict:
        return await self.request(
            "POST",
            f"/list/{list_id}/task",
            token=token,
            json={"name": name[:250], "markdown_description": description[:20_000]},
        )

    async def request(
        self, method: str, path: str, *, token: str | None = None, auth: bool = True, **kwargs
    ):
        headers = kwargs.pop("headers", {})
        if auth:
            headers["Authorization"] = token or ""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.request(method, API + path, headers=headers, **kwargs)
            response.raise_for_status()
            return response.json()
        except httpx.HTTPStatusError as error:
            if error.response.status_code in {401, 403}:
                raise ValueError(
                    "ClickUp rejected this connection. Reconnect ClickUp and try again."
                ) from None
            raise ValueError("ClickUp could not complete this request. Try again.") from None
        except httpx.HTTPError:
            raise ValueError("Unable to reach ClickUp. Try again.") from None
