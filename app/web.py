"""Teams tab, Microsoft sign-in, and Graph webhook in one local web server."""

import asyncio
import hashlib
import hmac
import html
import logging
import secrets
from contextlib import asynccontextmanager, suppress
from urllib.parse import urlencode
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.auth import identity_client
from app.clickup import ClickUp
from app.config import ROOT, Settings, settings
from app.graph_client import GraphClient
from app.models import parse_event
from app.notifications import validate_notifications
from app.store import Store, digest
from app.worker import run_worker

log = logging.getLogger(__name__)


class LoginStart(BaseModel):
    challenge: str = Field(pattern=r"^[a-f0-9]{64}$")
    in_teams: bool = False


class LoginComplete(BaseModel):
    code: str = Field(min_length=32, max_length=128)
    verifier: str = Field(pattern=r"^[A-Za-z0-9_-]{43,128}$")


class ClickUpList(BaseModel):
    list_id: str = Field(pattern=r"^[A-Za-z0-9]+$", max_length=64)


class ClickUpExport(BaseModel):
    list_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]+$", max_length=64)


class ClickUpConnect(BaseModel):
    in_teams: bool = False


class MeetingRecovery(BaseModel):
    meeting_url: str = Field(min_length=20, max_length=2000)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


def current_user(request: Request) -> dict:
    user = request.app.state.store.session_user(bearer(request))
    if not user:
        raise HTTPException(401, "Please connect your Microsoft 365 account.")
    return user


def auth_result(code: str = "", error: str = "", in_teams: bool = False) -> HTMLResponse:
    page = (ROOT / "web/auth-complete.html").read_text()
    return HTMLResponse(
        page.replace("{{CODE}}", html.escape(code, quote=True))
        .replace("{{ERROR}}", html.escape(error, quote=True))
        .replace("{{IN_TEAMS}}", "true" if in_teams else "false"),
        status_code=400 if error else 200,
    )


def clickup_result(error: str = "", in_teams: bool = False) -> HTMLResponse:
    page = (ROOT / "web/clickup-complete.html").read_text()
    return HTMLResponse(
        page.replace("{{ERROR}}", html.escape(error, quote=True)).replace(
            "{{IN_TEAMS}}", "true" if in_teams else "false"
        ),
        status_code=400 if error else 200,
    )


def actions(content: dict) -> list[dict]:
    insights = content.get("insights") or [{"insight": content.get("insight", {})}]
    return [
        action
        for item in insights
        for action in item.get("insight", {}).get("actionItems", [])
        if action.get("title") or action.get("text")
    ]


def create_app(
    config: Settings | None = None, graph: GraphClient | None = None, background: bool = True
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        app.state.config = config or settings()
        app.state.store = Store(
            app.state.config.database,
            app.state.config.backup_database,
            app.state.config.database_url,
        )
        app.state.graph = graph or GraphClient()
        app.state.clickup = ClickUp(app.state.config) if app.state.config.clickup_enabled else None
        app.state.repair = asyncio.Event()
        task = (
            asyncio.create_task(run_worker(app.state.store, app.state.graph, app.state.repair))
            if background
            else None
        )
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        app.state.store.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method in ("POST", "DELETE"):
            origin = request.headers.get("origin")
            if origin and origin != request.app.state.config.public_url:
                return PlainTextResponse("Invalid origin", status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Tabs are embedded by Teams; do not set X-Frame-Options: DENY/SAMEORIGIN.
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
            "frame-ancestors https://teams.microsoft.com https://*.teams.microsoft.com "
            "https://*.cloud.microsoft https://*.office.com https://*.microsoft365.com"
        )
        return response

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return FileResponse(ROOT / "web/index.html")

    @app.get("/healthz")
    async def health(request: Request):
        if not request.app.state.store.healthy():
            raise HTTPException(503, "Storage unavailable")
        return {"status": "ok"}

    @app.post("/api/auth/start")
    async def login_start(body: LoginStart, request: Request):
        config = request.app.state.config
        # MSAL generates OAuth state, nonce and the PKCE verifier. Keep that flow server-side.
        try:
            flow = await asyncio.to_thread(
                lambda: identity_client().initiate_auth_code_flow(
                    scopes=["User.Read"],
                    redirect_uri=config.redirect_uri,
                    prompt="select_account",
                )
            )
        except Exception:
            raise HTTPException(
                503, "Unable to reach Microsoft sign-in. Check the server connection."
            ) from None
        if "auth_uri" not in flow:
            raise HTTPException(503, "Unable to start Microsoft sign-in. Check app configuration.")
        request.app.state.store.put(
            "flow",
            flow["state"],
            {
                "flow": flow,
                "challenge": body.challenge,
                "in_teams": body.in_teams,
            },
        )
        return {"url": config.public_url + "/auth/launch?" + urlencode({"state": flow["state"]})}

    @app.get("/auth/launch")
    async def login_launch(state: str, request: Request):
        item = request.app.state.store.get("flow", state)
        if not item:
            return auth_result(error="Sign-in expired. Close this window and connect again.")
        return RedirectResponse(item["flow"]["auth_uri"])

    @app.get("/auth/callback")
    async def login_callback(request: Request):
        store = request.app.state.store
        item = store.pop("flow", request.query_params.get("state", ""))
        if not item:
            return auth_result(error="Sign-in expired. Close this window and connect again.")
        try:
            result = await asyncio.to_thread(
                lambda: identity_client().acquire_token_by_auth_code_flow(
                    item["flow"], dict(request.query_params)
                )
            )
            claims = result.get("id_token_claims") or {}
            user_id = str(UUID(claims["oid"]))
            if UUID(claims["tid"]) != request.app.state.config.tenant_id:
                raise ValueError("Wrong tenant")
        except Exception:
            return auth_result(
                error="Microsoft sign-in failed. Use your organization's work account.",
                in_teams=item["in_teams"],
            )
        code = secrets.token_urlsafe(32)
        store.put(
            "handoff",
            digest(code),
            {
                "challenge": item["challenge"],
                "user_id": user_id,
                "name": claims.get("name") or "Microsoft 365 user",
            },
            ttl=60,
        )
        # Never return Microsoft's access or refresh tokens to the tab.
        return auth_result(code=code, in_teams=item["in_teams"])

    @app.post("/api/auth/complete")
    async def login_complete(body: LoginComplete, request: Request):
        store = request.app.state.store
        item = store.get("handoff", digest(body.code))
        if not item or not hmac.compare_digest(item["challenge"], digest(body.verifier)):
            raise HTTPException(401, "Sign-in verification failed. Please connect again.")
        item = store.pop("handoff", digest(body.code))
        if not item:
            raise HTTPException(401, "This sign-in has already been used.")
        store.enroll(item["user_id"], item["name"])
        request.app.state.repair.set()
        return {"token": store.session(item["user_id"])}

    @app.get("/api/me")
    async def me(request: Request, user: dict = Depends(current_user)):
        with request.app.state.store.connect() as db:
            failed = db.execute(
                "SELECT 1 FROM activity_outbox WHERE user_id=? AND "
                "(status='failed' OR (status='pending' AND attempts>0)) LIMIT 1",
                (user["id"],),
            ).fetchone()
        return {
            **{key: user[key] for key in ("id", "name", "status")},
            "notifications": "DELIVERY_ERROR" if failed else "READY",
            "clickup": {
                "available": request.app.state.clickup is not None,
                "connected": bool(request.app.state.store.clickup(user["id"])),
            },
        }

    @app.get("/api/clickup")
    async def clickup_status(request: Request, user: dict = Depends(current_user)):
        connection = request.app.state.store.clickup(user["id"])
        default_id = connection.get("list_id") if connection else None
        lists = request.app.state.store.clickup_lists(user["id"]) if connection else []
        return {
            "available": request.app.state.clickup is not None,
            "connected": bool(connection),
            "list_id": default_id,
            "list_name": connection.get("list_name") if connection else None,
            "lists": [{**item, "is_default": item["list_id"] == default_id} for item in lists],
            "workspaces": connection.get("workspaces", []) if connection else [],
        }

    @app.get("/api/clickup/available-lists")
    async def clickup_available_lists(request: Request, user: dict = Depends(current_user)):
        client = request.app.state.clickup
        connection = request.app.state.store.clickup(user["id"])
        if not client or not connection:
            raise HTTPException(409, "Connect ClickUp first.")
        try:
            token = client.decrypt(connection["token"])
            lists = await client.available_lists(token, connection["workspaces"])
        except ValueError as error:
            raise HTTPException(502, str(error)) from None
        return {"lists": lists}

    @app.post("/api/clickup/connect")
    async def clickup_connect(
        body: ClickUpConnect, request: Request, user: dict = Depends(current_user)
    ):
        client = request.app.state.clickup
        if not client:
            raise HTTPException(503, "ClickUp is not configured on this NoteIQ server.")
        state = secrets.token_urlsafe(32)
        request.app.state.store.put(
            "clickup", state, {"user_id": user["id"], "in_teams": body.in_teams}
        )
        return {
            "url": request.app.state.config.public_url
            + "/clickup/authorize?"
            + urlencode({"state": state})
        }

    @app.get("/clickup/authorize")
    async def clickup_authorize(request: Request, state: str = ""):
        client = request.app.state.clickup
        item = request.app.state.store.get("clickup", state)
        if not client or not item:
            return clickup_result("ClickUp connection expired. Return to NoteIQ and try again.")
        query = urlencode(
            {
                "client_id": client.config.clickup_client_id,
                "redirect_uri": client.config.clickup_redirect_uri,
                "state": state,
            }
        )
        return RedirectResponse("https://app.clickup.com/api?" + query)

    @app.get("/clickup/callback")
    async def clickup_callback(request: Request, state: str = "", code: str = ""):
        item = request.app.state.store.pop("clickup", state)
        client = request.app.state.clickup
        if not item or not code or not client:
            return clickup_result("ClickUp connection expired. Return to NoteIQ and try again.")
        in_teams = bool(item.get("in_teams"))
        try:
            token = await client.exchange(code)
            workspaces = await client.workspaces(token)
            request.app.state.store.save_clickup(item["user_id"], client.encrypt(token), workspaces)
        except ValueError as error:
            return clickup_result(str(error), in_teams)
        return clickup_result(in_teams=in_teams)

    @app.post("/api/clickup/lists")
    async def clickup_add_list(
        body: ClickUpList, request: Request, user: dict = Depends(current_user)
    ):
        client = request.app.state.clickup
        store = request.app.state.store
        connection = store.clickup(user["id"])
        if not client or not connection:
            raise HTTPException(409, "Connect ClickUp first.")
        try:
            token = client.decrypt(connection["token"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        try:
            name = await client.list_name(token, body.list_id)
        except ValueError:
            raise HTTPException(
                400, "That ClickUp List couldn't be found. Check the List ID."
            ) from None
        store.add_clickup_list(user["id"], body.list_id, name)
        if not connection.get("list_id"):
            store.set_clickup_list(user["id"], body.list_id, name)
        return {"list_id": body.list_id, "list_name": name}

    @app.post("/api/clickup/lists/default")
    async def clickup_default_list(
        body: ClickUpList, request: Request, user: dict = Depends(current_user)
    ):
        store = request.app.state.store
        lists = {item["list_id"]: item["list_name"] for item in store.clickup_lists(user["id"])}
        if body.list_id not in lists:
            raise HTTPException(404, "Add this ClickUp List before setting it as default.")
        store.set_clickup_list(user["id"], body.list_id, lists[body.list_id])
        return {"list_id": body.list_id, "list_name": lists[body.list_id]}

    @app.delete("/api/clickup/lists/{list_id}")
    async def clickup_remove_list(
        list_id: str, request: Request, user: dict = Depends(current_user)
    ):
        request.app.state.store.remove_clickup_list(user["id"], list_id)
        return {"status": "removed"}

    @app.post("/api/clickup/disconnect")
    async def clickup_disconnect(request: Request, user: dict = Depends(current_user)):
        with request.app.state.store.connect() as db:
            db.execute("DELETE FROM clickup_connections WHERE user_id=?", (user["id"],))
            db.execute("DELETE FROM clickup_lists WHERE user_id=?", (user["id"],))
            db.execute("DELETE FROM clickup_tasks WHERE user_id=?", (user["id"],))
        return {"status": "disconnected"}

    @app.post("/api/meetings/{meeting_id}/clickup")
    async def export_clickup(
        meeting_id: int,
        body: ClickUpExport,
        request: Request,
        user: dict = Depends(current_user),
    ):
        client = request.app.state.clickup
        store = request.app.state.store
        connection = store.clickup(user["id"])
        meeting = store.meeting(user["id"], meeting_id)
        if not client or not connection:
            raise HTTPException(409, "Connect ClickUp first.")
        list_id = body.list_id or connection.get("list_id")
        if not list_id:
            raise HTTPException(409, "Choose a ClickUp List first.")
        if body.list_id and body.list_id not in {
            item["list_id"] for item in store.clickup_lists(user["id"])
        }:
            raise HTTPException(400, "Unknown ClickUp List. Add it in Account settings first.")
        if not meeting:
            raise HTTPException(404, "Meeting not found.")
        try:
            token = client.decrypt(connection["token"])
            created = skipped = 0
            for action in actions(meeting["content"]):
                raw = "|".join(
                    (
                        list_id,
                        meeting["content"].get("meeting_id") or "",
                        action.get("title") or "",
                        action.get("text") or "",
                        action.get("ownerDisplayName") or "",
                    )
                )
                action_key = hashlib.sha256(raw.encode()).hexdigest()
                if store.clickup_task(user["id"], action_key):
                    skipped += 1
                    continue
                name = action.get("title") or action.get("text")
                detail = action.get("text") or ""
                owner = action.get("ownerDisplayName") or "Owner not specified"
                task = await client.create_task(
                    token,
                    list_id,
                    name,
                    f"{detail}\n\n**Owner:** {owner}\n\n_Source: NoteIQ — {meeting['subject']}_",
                )
                store.save_clickup_task(user["id"], action_key, task)
                created += 1
        except ValueError as error:
            raise HTTPException(502, str(error)) from None
        return {"created": created, "skipped": skipped}

    @app.post("/api/notifications/retry")
    async def retry_notifications(request: Request, user: dict = Depends(current_user)):
        with request.app.state.store.connect() as db:
            db.execute(
                "UPDATE activity_outbox SET status='pending', attempts=0, due=0 "
                "WHERE user_id=? AND status IN ('failed', 'pending')",
                (user["id"],),
            )
        return {"status": "pending"}

    @app.get("/api/meetings")
    async def meetings(request: Request, user: dict = Depends(current_user)):
        return request.app.state.store.meetings(user["id"])

    @app.post("/api/sync")
    async def sync(request: Request, user: dict = Depends(current_user)):
        from app.sync import queue_sync

        return {"queued": queue_sync(request.app.state.store, user["id"])}

    @app.post("/api/recover-meeting")
    async def recover_meeting(
        body: MeetingRecovery, request: Request, user: dict = Depends(current_user)
    ):
        from app.sync import recover_from_link

        try:
            result = await recover_from_link(
                request.app.state.store, request.app.state.graph, user["id"], body.meeting_url
            )
        except ValueError as error:
            raise HTTPException(400, str(error)) from None
        if not result["found"]:
            raise HTTPException(404, "Meeting not found for your organizer account.")
        return result

    @app.get("/api/transcripts/{transcript_id}")
    async def transcript(transcript_id: int, request: Request, user: dict = Depends(current_user)):
        text = request.app.state.store.transcript(user["id"], transcript_id)
        if text is None:
            raise HTTPException(404, "Transcript not found.")
        return PlainTextResponse(text)

    @app.post("/api/reconnect")
    async def reconnect(request: Request, user: dict = Depends(current_user)):
        request.app.state.store.status(user["id"], "CONNECTING")
        request.app.state.repair.set()
        return {"status": "CONNECTING"}

    @app.post("/api/logout")
    async def logout(request: Request, user: dict = Depends(current_user)):
        request.app.state.store.logout(bearer(request))
        return {"status": "signed_out"}

    @app.post("/api/disconnect")
    async def disconnect(request: Request, user: dict = Depends(current_user)):
        request.app.state.store.disconnect(user["id"])
        request.app.state.repair.set()
        return {"status": "disconnected"}

    async def accept(request: Request, lifecycle: bool = False):
        if "validationToken" in request.query_params:
            return PlainTextResponse(request.query_params["validationToken"])
        try:
            payload = await request.json()
            messages = validate_notifications(payload, request.app.state.config, lifecycle)
        except PermissionError:
            raise HTTPException(403, "Invalid notification") from None
        except (ValueError, TypeError):
            raise HTTPException(400, "Invalid notification") from None
        if lifecycle:
            if messages:
                request.app.state.repair.set()
            if "missed" in messages:
                for user_id in request.app.state.store.users():
                    request.app.state.store.status(user_id, "MISSED_EVENTS")
                log.warning("Missed Graph events; use the recovery command for affected meetings")
        else:
            users = set(request.app.state.store.users())
            accepted = [
                message for message in messages if str(parse_event(message).user_id) in users
            ]
            request.app.state.store.enqueue(accepted)
            log.info("Graph webhook received=%s enrolled=%s", len(messages), len(accepted))
        return PlainTextResponse("", status_code=202)

    @app.post("/api/graph/notifications")
    async def notifications(request: Request):
        return await accept(request)

    @app.post("/api/graph/lifecycle")
    async def lifecycle(request: Request):
        return await accept(request, lifecycle=True)

    app.mount("/static", StaticFiles(directory=ROOT / "web"), name="static")
    return app


app = create_app()
