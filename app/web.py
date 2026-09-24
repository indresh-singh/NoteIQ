"""Teams tab, Microsoft sign-in, and Graph webhook in one local web server."""

import asyncio
import functools
import hashlib
import hmac
import html
import logging
import math
import secrets
import time
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timezone
from typing import Literal
from urllib.parse import urlencode
from uuid import UUID, uuid4

import httpx
import msal
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from app.adaptive_cards import build_card
from app.auth import identity_client
from app.clickup import ClickUp
from app.config import ROOT, Settings, settings
from app.graph_client import GraphBusy, GraphClient, busy_from, interactive_requests
from app.models import InsightEvent, TranscriptEvent, UserSync, parse_event
from app.notifications import validate_notifications
from app.observability import log_context, safe_correlation_id
from app.occurrences import present_meeting, select_session, transcript_version
from app.openai import OpenAI, OpenAIProviderError
from app.openrouter import MAX_TRANSCRIPT_CHARS, OpenRouter
from app.planner import Planner
from app.planner_identity import PLANNER_SCOPES, DelegatedGraph, cache_cipher, delegated_token
from app.store import Store, digest
from app.subscriptions import resource_owner
from app.sync import REFRESH_GRAPH_SLOTS, REFRESH_LOCK_SECONDS
from app.transcripts import meeting_transcript_text, summarize_with_ai
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
    occurrence_id: str | None = Field(default=None, max_length=2048)
    list_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9]+$", max_length=64)
    # "openai" was missing here even after OpenAI became a summary provider;
    # filtering an export by it would 422 despite such insights existing.
    provider: Literal["copilot", "openrouter", "openai"] | None = None


class ClickUpConnect(BaseModel):
    in_teams: bool = False


# Graph object IDs are opaque, provider-issued strings (letters, digits, "-", "_"),
# unlike ClickUp's numeric-only List IDs -- hence the different pattern.
class PlannerPlan(BaseModel):
    plan_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,100}$")


class PlannerExport(BaseModel):
    occurrence_id: str | None = Field(default=None, max_length=2048)
    plan_id: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]{1,100}$")
    provider: Literal["copilot", "openrouter", "openai"] | None = None


class CustomTranscript(BaseModel):
    subject: str = Field(min_length=1, max_length=200)
    filename: str = Field(min_length=1, max_length=255)
    text: str = Field(min_length=1, max_length=MAX_TRANSCRIPT_CHARS)


class RegenerateInsight(BaseModel):
    occurrence_id: str | None = Field(default=None, max_length=2048)
    provider: Literal["openrouter", "openai"] | None = None


class MeetingRecovery(BaseModel):
    meeting_url: str = Field(min_length=20, max_length=2000)


def bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:] if header.startswith("Bearer ") else ""


def request_repair(request: Request):
    """Ask the worker to re-check subscriptions, wherever it is running.

    The event reaches a worker inside this process immediately; the stored flag
    is what reaches it when the roles are split across containers, or when
    another web replica served the request.
    """
    request.app.state.repair.set()
    request.app.state.store.request_repair()


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


def actions(content: dict, provider: str | None = None) -> list[dict]:
    insights = content.get("insights") or [{"insight": content.get("insight", {})}]
    if provider:
        insights = [
            item
            for item in insights
            if (item.get("insight", {}).get("provider") or "copilot") == provider
        ]
    return [
        action
        for item in insights
        for action in item.get("insight", {}).get("actionItems", [])
        if action.get("title") or action.get("text")
    ]


def scoped_meeting(meeting: dict | None, occurrence_id: str | None) -> dict:
    if not meeting:
        raise HTTPException(404, "Meeting not found.")
    try:
        content = select_session(meeting["content"], occurrence_id)
    except KeyError:
        raise HTTPException(404, "Meeting session not found.") from None
    except ValueError as error:
        raise HTTPException(409, str(error)) from None
    subject = meeting["subject"]
    if content.get("started_at"):
        subject += " — " + content["started_at"]
    return {**meeting, "subject": subject, "content": content}


def export_identity(content: dict) -> str:
    identity = content.get("meeting_id") or ""
    if content.get("occurrence_id"):
        identity += ":" + content["occurrence_id"]
    return identity


def create_app(
    config: Settings | None = None, graph: GraphClient | None = None, background: bool = True
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app: FastAPI):
        startup_started = time.monotonic()
        task = None
        try:
            app.state.config = config or settings()
            app.state.store = Store(
                app.state.config.database,
                app.state.config.backup_database,
                app.state.config.database_url,
            )
            app.state.graph = graph or GraphClient()
            if isinstance(app.state.graph, GraphClient):
                # Every 429, from any caller, pauses the tenant's Graph work;
                # see Store.pause_graph.
                app.state.graph.on_throttle = app.state.store.pause_graph
            app.state.clickup = (
                ClickUp(app.state.config) if app.state.config.clickup_enabled else None
            )
            # Always on: Planner needs no separate consent flow of its own to
            # gate, unlike ClickUp -- it rides the same app-only Graph
            # credentials already required for everything else. Whether the
            # tenant has actually granted the two extra Graph permissions is
            # a property of the tenant, not something this process can check
            # in advance; a call made without them fails with a clear error
            # at the point of use instead.
            app.state.planner = Planner(app.state.config, app.state.graph)
            app.state.repair = asyncio.Event()
            # Shared by every Refresh on this replica: see REFRESH_GRAPH_SLOTS.
            app.state.refresh_slots = asyncio.Semaphore(REFRESH_GRAPH_SLOTS)
            task = (
                asyncio.create_task(
                    run_worker(app.state.store, app.state.graph, app.state.repair),
                    name="noteiq-worker",
                )
                if background and app.state.config.runs_worker
                else None
            )
            log.info(
                "Application startup complete role=%s background_requested=%s worker_started=%s "
                "storage=%s clickup_enabled=%s summary_providers=%s duration_ms=%d",
                app.state.config.role,
                background,
                task is not None,
                "postgresql" if app.state.config.database_url else "sqlite",
                app.state.config.clickup_enabled,
                ",".join(app.state.config.summary_providers),
                (time.monotonic() - startup_started) * 1000,
            )
            yield
        except Exception as error:
            log.exception(
                "Application lifespan failed phase=%s duration_ms=%d error_type=%s error=%s",
                "startup" if not hasattr(app.state, "store") else "running",
                (time.monotonic() - startup_started) * 1000,
                type(error).__name__,
                error,
            )
            raise
        finally:
            shutdown_started = time.monotonic()
            if task:
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
            if hasattr(app.state, "graph"):
                await app.state.graph.aclose()
            if hasattr(app.state, "store"):
                app.state.store.close()
            log.info(
                "Application shutdown complete duration_ms=%d",
                (time.monotonic() - shutdown_started) * 1000,
            )

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        started = time.monotonic()
        request_id = safe_correlation_id(request.headers.get("x-request-id"), str(uuid4()))
        query_keys = sorted(set(request.query_params.keys()))
        client_host = request.client.host if request.client else "unknown"
        with log_context(request_id=request_id):
            log.info(
                "HTTP request started method=%s path=%s query_keys=%s client=%s "
                "content_type=%s content_length=%s user_agent=%r",
                request.method,
                request.url.path,
                ",".join(query_keys) or "-",
                client_host,
                request.headers.get("content-type", "-"),
                request.headers.get("content-length", "-"),
                request.headers.get("user-agent", "-")[:300],
            )
            try:
                if request.url.path.startswith("/api/") and request.method in ("POST", "DELETE"):
                    origin = request.headers.get("origin")
                    if origin and origin != request.app.state.config.public_url:
                        log.warning(
                            "HTTP request rejected reason=invalid_origin method=%s path=%s "
                            "origin=%r expected_origin=%r",
                            request.method,
                            request.url.path,
                            origin,
                            request.app.state.config.public_url,
                        )
                        response = PlainTextResponse("Invalid origin", status_code=403)
                    else:
                        response = await call_next(request)
                else:
                    response = await call_next(request)
            except Exception as error:
                log.exception(
                    "HTTP request crashed method=%s path=%s duration_ms=%d error_type=%s error=%s",
                    request.method,
                    request.url.path,
                    (time.monotonic() - started) * 1000,
                    type(error).__name__,
                    error,
                )
                raise
            response.headers["Cache-Control"] = "no-store"
            response.headers["Referrer-Policy"] = "no-referrer"
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Request-ID"] = request_id
            # Tabs are embedded by Teams; do not set X-Frame-Options: DENY/SAMEORIGIN.
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; base-uri 'none'; "
                "frame-ancestors https://teams.microsoft.com https://*.teams.microsoft.com "
                "https://*.cloud.microsoft https://*.office.com https://*.microsoft365.com"
            )
            route = request.scope.get("route")
            log_method = log.warning if response.status_code >= 400 else log.info
            log_method(
                "HTTP request completed method=%s path=%s route=%s status=%s duration_ms=%d "
                "response_content_type=%s response_content_length=%s",
                request.method,
                request.url.path,
                getattr(route, "path", "unmatched"),
                response.status_code,
                (time.monotonic() - started) * 1000,
                response.headers.get("content-type", "-"),
                response.headers.get("content-length", "-"),
            )
            return response

    @app.exception_handler(GraphBusy)
    async def graph_busy(request: Request, error: GraphBusy):
        log.warning(
            "Graph busy response method=%s path=%s retry_after_s=%s",
            request.method,
            request.url.path,
            error.retry_after,
        )
        return JSONResponse(
            {"detail": str(error)},
            status_code=429,
            headers={"Retry-After": str(error.retry_after)},
        )

    @app.exception_handler(HTTPException)
    async def log_http_exception(request: Request, error: HTTPException):
        log.warning(
            "HTTP handled error method=%s path=%s status=%s detail=%r headers=%s",
            request.method,
            request.url.path,
            error.status_code,
            error.detail,
            sorted((error.headers or {}).keys()),
        )
        return await http_exception_handler(request, error)

    @app.exception_handler(RequestValidationError)
    async def log_validation_exception(request: Request, error: RequestValidationError):
        diagnostics = [
            {"location": list(item["loc"]), "type": item["type"], "message": item["msg"]}
            for item in error.errors()
        ]
        log.warning(
            "HTTP validation failed method=%s path=%s error_count=%s errors=%s",
            request.method,
            request.url.path,
            len(diagnostics),
            diagnostics,
        )
        return await request_validation_exception_handler(request, error)

    @app.get("/", response_class=HTMLResponse)
    async def home():
        return FileResponse(ROOT / "web/index.html")

    @app.get("/healthz")
    async def health(request: Request):
        if not request.app.state.store.healthy():
            raise HTTPException(503, "Storage unavailable")
        return {"status": "ok"}

    async def start_login(body: LoginStart, request: Request, planner_user=None):
        config = request.app.state.config
        # MSAL generates OAuth state, nonce and the PKCE verifier. Keep that flow server-side.
        try:
            flow = await asyncio.to_thread(
                lambda: identity_client().initiate_auth_code_flow(
                    scopes=["User.Read", *PLANNER_SCOPES] if planner_user else ["User.Read"],
                    redirect_uri=config.redirect_uri,
                    prompt="select_account",
                )
            )
        except Exception as error:
            log.exception(
                "Microsoft sign-in initialization failed redirect_uri=%s error_type=%s error=%s",
                config.redirect_uri,
                type(error).__name__,
                error,
            )
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
                "planner_user": planner_user,
                "challenge": body.challenge,
                "in_teams": body.in_teams,
            },
        )
        return {"url": config.public_url + "/auth/launch?" + urlencode({"state": flow["state"]})}

    @app.post("/api/auth/start")
    async def login_start(body: LoginStart, request: Request):
        return await start_login(body, request)

    @app.post("/api/planner/connect")
    async def planner_connect(
        body: LoginStart, request: Request, user: dict = Depends(current_user)
    ):
        return await start_login(body, request, user["id"])

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
        cache = msal.SerializableTokenCache() if item.get("planner_user") else None
        try:
            result = await asyncio.to_thread(
                lambda: identity_client(token_cache=cache).acquire_token_by_auth_code_flow(
                    item["flow"], dict(request.query_params)
                )
            )
            if "error" in result:
                raise ValueError(result.get("error_description") or result["error"])
            claims = result.get("id_token_claims") or {}
            user_id = str(UUID(claims["oid"]))
            if UUID(claims["tid"]) != request.app.state.config.tenant_id:
                raise ValueError("Wrong tenant")
            if item.get("planner_user") and user_id != item["planner_user"]:
                raise ValueError("Use the same Microsoft account you connected to NoteIQ.")
            if cache is not None:
                scopes = {
                    scope.rsplit("/", 1)[-1].lower() for scope in result.get("scope", "").split()
                }
                if not result.get("access_token") or "tasks.readwrite" not in scopes:
                    raise ValueError(
                        "Microsoft did not grant Tasks.ReadWrite. Ask your administrator to allow Planner consent."
                    )
        except Exception as error:
            log.exception(
                "Microsoft sign-in callback failed in_teams=%s has_state=%s query_keys=%s "
                "error_type=%s error=%s",
                item["in_teams"],
                bool(request.query_params.get("state")),
                sorted(request.query_params.keys()),
                type(error).__name__,
                error,
            )
            # DEV ONLY: surfaces the real MSAL/Graph error for pilot testing.
            # Replace with a generic message before wider release.
            return auth_result(
                error=f"Microsoft sign-in failed: {error}",
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
                "planner_cache": cache_cipher(request.app.state.config, user_id)
                .encrypt(cache.serialize().encode())
                .decode()
                if cache is not None
                else None,
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
        if item.get("planner_cache"):
            store.save_planner_cache(item["user_id"], item["planner_cache"])
            log.info("Planner delegated connection saved user=%s", item["user_id"])
        request_repair(request)
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
            # Seconds left on a tenant-wide Graph throttle pause, 0 when none:
            # the tab shows that updates are delayed, and re-reads when it ends.
            "graph_throttled_seconds": math.ceil(
                max(request.app.state.store.graph_paused_until() - time.time(), 0)
            ),
            "summary_provider": request.app.state.config.summary_provider,
            "summary_providers": request.app.state.config.summary_providers,
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
            log.warning(
                "ClickUp list discovery failed user=%s workspace_count=%s error=%s",
                user["id"],
                len(connection.get("workspaces") or []),
                error,
                exc_info=True,
            )
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
            log.warning(
                "ClickUp OAuth callback failed user=%s error=%s",
                item["user_id"],
                error,
                exc_info=True,
            )
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
            log.warning(
                "ClickUp token decryption failed user=%s error=%s",
                user["id"],
                error,
                exc_info=True,
            )
            raise HTTPException(409, str(error)) from None
        try:
            name = await client.list_name(token, body.list_id)
        except ValueError as error:
            log.warning(
                "ClickUp list lookup failed user=%s list_id=%s error=%s",
                user["id"],
                body.list_id,
                error,
                exc_info=True,
            )
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
        meeting = scoped_meeting(meeting, body.occurrence_id)
        created = skipped = 0
        try:
            token = client.decrypt(connection["token"])
            for action in actions(meeting["content"], body.provider):
                raw = "|".join(
                    (
                        list_id,
                        export_identity(meeting["content"]),
                        action.get("title") or "",
                        action.get("text") or "",
                        action.get("ownerDisplayName") or "",
                    )
                )
                action_key = hashlib.sha256(raw.encode()).hexdigest()
                existing_task_id = store.clickup_task_id(user["id"], action_key)
                if existing_task_id:
                    # The local record doesn't know if the task was deleted on
                    # ClickUp's side (e.g. from the ClickUp UI), so confirm
                    # with the API before trusting it as "already sent".
                    if await client.task_exists(token, existing_task_id):
                        skipped += 1
                        continue
                    store.forget_clickup_task(user["id"], action_key)
                # Reserve the row before calling ClickUp: this is a single atomic
                # statement, so concurrent exports of the same action item can't
                # both pass the "already sent?" check and both create a task.
                if not store.reserve_clickup_task(user["id"], action_key):
                    skipped += 1
                    continue
                name = action.get("title") or action.get("text")
                detail = action.get("text") or ""
                owner = action.get("ownerDisplayName") or "Owner not specified"
                try:
                    task = await client.create_task(
                        token,
                        list_id,
                        name,
                        f"{detail}\n\n**Owner:** {owner}\n\n_Source: NoteIQ — {meeting['subject']}_",
                    )
                except Exception as error:
                    store.release_clickup_task(user["id"], action_key)
                    log.exception(
                        "ClickUp task creation failed user=%s meeting_row=%s list_id=%s "
                        "action_key=%s error_type=%s error=%s",
                        user["id"],
                        meeting_id,
                        list_id,
                        action_key[:12],
                        type(error).__name__,
                        error,
                    )
                    raise
                store.save_clickup_task(user["id"], action_key, task)
                created += 1
        except ValueError as error:
            log.warning(
                "ClickUp export failed user=%s meeting_row=%s list_id=%s created=%s "
                "skipped=%s error=%s",
                user["id"],
                meeting_id,
                list_id,
                created,
                skipped,
                error,
                exc_info=True,
            )
            raise HTTPException(502, str(error)) from None
        return {"created": created, "skipped": skipped}

    async def planner_client(request, user):
        store = request.app.state.store
        if not store.planner_cache(user["id"]):
            return request.app.state.planner
        try:
            token = await delegated_token(request.app.state.config, store, user["id"])
        except ValueError as error:
            raise HTTPException(409, str(error)) from None
        return Planner(
            request.app.state.config,
            DelegatedGraph(
                request.app.state.graph, token, request.app.state.config.planner_graph_version
            ),
            delegated=True,
        )

    @app.post("/api/planner/disconnect")
    async def planner_disconnect(request: Request, user: dict = Depends(current_user)):
        # Clear targets too: disconnected personal plans must never be sent via app-only access.
        with request.app.state.store.connect() as db:
            for table in ("planner_connections", "planner_defaults", "planner_plans"):
                db.execute(f"DELETE FROM {table} WHERE user_id=?", (user["id"],))
        return {"status": "disconnected"}

    @app.get("/api/planner")
    async def planner_status(request: Request, user: dict = Depends(current_user)):
        store = request.app.state.store
        default = store.planner_default(user["id"])
        default_id = default.get("plan_id") if default else None
        plans = store.planner_plans(user["id"])
        return {
            "delegated_connected": bool(store.planner_cache(user["id"])),
            "plan_id": default_id,
            "plan_name": default.get("plan_name") if default else None,
            "plans": [{**item, "is_default": item["plan_id"] == default_id} for item in plans],
        }

    def graph_action(endpoint):
        """For a button that calls Graph while the person waits.

        While Graph is throttling the tenant, answer at once with "try again
        in N seconds" instead of adding to the throttle; otherwise let a 429
        wait only a few seconds before saying the same.
        """

        @functools.wraps(endpoint)
        async def wrapper(*args, **kwargs):
            store = kwargs["request"].app.state.store
            wait = store.graph_paused_until() - time.time()
            if wait > 0:
                raise GraphBusy(wait)
            with interactive_requests():
                return await endpoint(*args, **kwargs)

        return wrapper

    @app.get("/api/planner/available-plans")
    @graph_action
    async def planner_available_plans(request: Request, user: dict = Depends(current_user)):
        client = await planner_client(request, user)
        try:
            plans = await client.available_plans(user["id"])
        except ValueError as error:
            log.warning(
                "Planner plan discovery failed user=%s error=%s", user["id"], error, exc_info=True
            )
            raise HTTPException(502, str(error)) from None
        return {"plans": plans}

    @app.post("/api/planner/plans")
    @graph_action
    async def planner_add_plan(
        body: PlannerPlan, request: Request, user: dict = Depends(current_user)
    ):
        client = await planner_client(request, user)
        store = request.app.state.store
        try:
            name = await client.plan_name(body.plan_id)
        except ValueError as error:
            log.warning(
                "Planner plan lookup failed user=%s plan_id=%s error=%s",
                user["id"],
                body.plan_id,
                error,
                exc_info=True,
            )
            raise HTTPException(400, str(error)) from None
        store.add_planner_plan(user["id"], body.plan_id, name)
        if not store.planner_default(user["id"]):
            store.set_planner_default(user["id"], body.plan_id, name)
        return {"plan_id": body.plan_id, "plan_name": name}

    @app.post("/api/planner/plans/default")
    async def planner_default_plan(
        body: PlannerPlan, request: Request, user: dict = Depends(current_user)
    ):
        store = request.app.state.store
        plans = {item["plan_id"]: item["plan_name"] for item in store.planner_plans(user["id"])}
        if body.plan_id not in plans:
            raise HTTPException(404, "Add this Planner plan before setting it as default.")
        store.set_planner_default(user["id"], body.plan_id, plans[body.plan_id])
        return {"plan_id": body.plan_id, "plan_name": plans[body.plan_id]}

    @app.delete("/api/planner/plans/{plan_id}")
    async def planner_remove_plan(
        plan_id: str, request: Request, user: dict = Depends(current_user)
    ):
        request.app.state.store.remove_planner_plan(user["id"], plan_id)
        return {"status": "removed"}

    @app.get("/api/planner/tasks")
    @graph_action
    async def planner_tasks_list(
        plan_id: str, request: Request, user: dict = Depends(current_user)
    ):
        client = await planner_client(request, user)
        store = request.app.state.store
        if plan_id not in {item["plan_id"] for item in store.planner_plans(user["id"])}:
            raise HTTPException(400, "Unknown Planner plan. Add it in Account settings first.")
        try:
            tasks = await client.list_tasks(plan_id)
        except ValueError as error:
            log.warning(
                "Planner task listing failed user=%s plan_id=%s error=%s",
                user["id"],
                plan_id,
                error,
                exc_info=True,
            )
            raise HTTPException(502, str(error)) from None
        return {"tasks": tasks}

    @app.post("/api/meetings/{meeting_id}/planner")
    @graph_action
    async def export_planner(
        meeting_id: int,
        body: PlannerExport,
        request: Request,
        user: dict = Depends(current_user),
    ):
        client = await planner_client(request, user)
        store = request.app.state.store
        meeting = store.meeting(user["id"], meeting_id)
        default = store.planner_default(user["id"])
        plan_id = body.plan_id or (default.get("plan_id") if default else None)
        if not plan_id:
            raise HTTPException(409, "Choose a Planner plan first.")
        if body.plan_id and body.plan_id not in {
            item["plan_id"] for item in store.planner_plans(user["id"])
        }:
            raise HTTPException(400, "Unknown Planner plan. Add it in Account settings first.")
        if not meeting:
            raise HTTPException(404, "Meeting not found.")
        meeting = scoped_meeting(meeting, body.occurrence_id)
        created = skipped = 0
        try:
            for action in actions(meeting["content"], body.provider):
                raw = "|".join(
                    (
                        plan_id,
                        export_identity(meeting["content"]),
                        action.get("title") or "",
                        action.get("text") or "",
                        action.get("ownerDisplayName") or "",
                    )
                )
                action_key = hashlib.sha256(raw.encode()).hexdigest()
                existing_task_id = store.planner_task_id(user["id"], action_key)
                if existing_task_id:
                    # The local record doesn't know if the task was deleted on
                    # Planner's side (e.g. from the Planner app), so confirm
                    # with the API before trusting it as "already sent".
                    if await client.task_exists(existing_task_id):
                        skipped += 1
                        continue
                    store.forget_planner_task(user["id"], action_key)
                # Reserve the row before calling Planner: this is a single atomic
                # statement, so concurrent exports of the same action item can't
                # both pass the "already sent?" check and both create a task.
                if not store.reserve_planner_task(user["id"], action_key):
                    skipped += 1
                    continue
                name = action.get("title") or action.get("text")
                detail = action.get("text") or ""
                owner = action.get("ownerDisplayName") or "Owner not specified"
                try:
                    task = await client.create_task(
                        plan_id,
                        name,
                        f"{detail}\n\n**Owner:** {owner}\n\n_Source: NoteIQ — {meeting['subject']}_",
                    )
                except Exception as error:
                    store.release_planner_task(user["id"], action_key)
                    log.exception(
                        "Planner task creation failed user=%s meeting_row=%s plan_id=%s "
                        "action_key=%s error_type=%s error=%s",
                        user["id"],
                        meeting_id,
                        plan_id,
                        action_key[:12],
                        type(error).__name__,
                        error,
                    )
                    raise
                store.save_planner_task(user["id"], action_key, task)
                created += 1
        except GraphBusy as busy:
            if not created:
                raise
            # Exports are de-duplicated per action item, so trying again
            # sends only what is left.
            raise GraphBusy(
                busy.retry_after,
                before=f"Sent {created} task{'' if created == 1 else 's'} to Planner. ",
                after=" Tasks already sent won't be sent twice.",
            ) from None
        except ValueError as error:
            log.warning(
                "Planner export failed user=%s meeting_row=%s plan_id=%s created=%s "
                "skipped=%s error=%s",
                user["id"],
                meeting_id,
                plan_id,
                created,
                skipped,
                error,
                exc_info=True,
            )
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
        return [present_meeting(m) for m in request.app.state.store.meetings(user["id"])]

    @app.post("/api/transcripts/upload")
    async def upload_transcript(
        body: CustomTranscript, request: Request, user: dict = Depends(current_user)
    ):
        config = settings()
        if not config.external_ai_enabled:
            raise HTTPException(
                409, "Configure the selected AI provider to analyze uploaded transcripts."
            )
        if not body.filename.lower().endswith((".docx", ".txt", ".vtt", ".srt")):
            raise HTTPException(400, "Upload a Teams .docx or UTF-8 .txt, .vtt or .srt file.")
        if not body.subject.strip() or not body.text.strip() or "\x00" in body.text:
            raise HTTPException(400, "Provide a title and a non-empty text transcript.")
        meeting_key = "upload:" + secrets.token_hex(16)
        provider_name = "OpenAI"
        provider_id = config.summary_provider
        try:
            if config.summary_provider == "openai":
                try:
                    insight = await OpenAI(config).summarize(
                        meeting_key, body.subject.strip(), body.text
                    )
                except ValueError as openai_error:
                    if not config.openrouter_enabled:
                        raise
                    log.warning(
                        "Transcript upload OpenAI analysis failed; falling back to OpenRouter "
                        "user=%s filename_extension=%s error=%s",
                        user["id"],
                        body.filename.rsplit(".", 1)[-1].lower(),
                        openai_error,
                    )
                    insight = await OpenRouter(config).summarize(
                        meeting_key, body.subject.strip(), body.text
                    )
                    provider_name = "OpenRouter"
                    provider_id = "openrouter"
            else:
                insight = await OpenRouter(config).summarize(
                    meeting_key, body.subject.strip(), body.text
                )
        except ValueError as error:
            log.warning(
                "Transcript upload analysis failed user=%s filename_extension=%s "
                "transcript_chars=%s subject_chars=%s error=%s",
                user["id"],
                body.filename.rsplit(".", 1)[-1].lower(),
                len(body.text),
                len(body.subject.strip()),
                error,
                exc_info=True,
            )
            raise HTTPException(502, str(error)) from None
        card = build_card(insight, body.subject.strip(), source=provider_name)
        if card is None:
            raise HTTPException(502, "The AI provider returned no usable notes. Please try again.")
        store = request.app.state.store
        local_id = store.save_transcript(user["id"], meeting_key, meeting_key, body.text)
        if local_id is None:
            raise HTTPException(409, "Your account was disconnected. Please connect again.")
        store.save_meeting(
            user["id"],
            body.subject.strip(),
            {
                "meeting_id": meeting_key,
                "source": "upload",
                "transcript": {
                    "id": meeting_key,
                    "local_id": local_id,
                    "createdDateTime": datetime.now(timezone.utc).isoformat(),
                },
                "insight": {
                    **insight.model_dump(mode="json"),
                    "provider": provider_id,
                },
                "card": card,
            },
        )
        store.retain_latest_upload(user["id"])
        return {"status": "saved", "meeting_id": meeting_key}

    @app.post("/api/meetings/{meeting_id}/regenerate")
    async def regenerate_insight(
        meeting_id: int,
        body: RegenerateInsight,
        request: Request,
        user: dict = Depends(current_user),
    ):
        store = request.app.state.store
        meeting = store.meeting(user["id"], meeting_id)
        if not meeting:
            raise HTTPException(404, "Meeting not found.")
        selected = scoped_meeting(meeting, body.occurrence_id)
        config = settings()
        provider = body.provider or config.summary_provider
        if provider not in config.external_summary_providers:
            raise HTTPException(409, "Configure the selected AI provider to regenerate insights.")
        transcripts = selected["content"].get("transcripts") or []
        if not transcripts:
            raise HTTPException(409, "No transcript available to summarize yet.")
        latest = transcripts[-1]["transcript"]
        # Join only the selected call's segments, including transcription restarts.
        text = meeting_transcript_text(store, user["id"], selected["content"])
        if not text:
            raise HTTPException(404, "Transcript content not found.")
        event = TranscriptEvent(
            user_id=user["id"],
            meeting_id=meeting["content"]["meeting_id"],
            transcript_id=latest["id"],
        )
        try:
            ok = await summarize_with_ai(
                store,
                user["id"],
                event,
                meeting["subject"],
                text,
                provider=provider,
                raise_on_failure=True,
                occurrence_id=body.occurrence_id,
                source_version=transcript_version(selected["content"]),
            )
        except OpenAIProviderError as error:
            raise HTTPException(
                502,
                f"ChatGPT Enterprise could not generate a summary. Error code: {error.code}.",
            ) from None
        if not ok:
            raise HTTPException(502, "The AI provider could not generate a summary. Try again.")
        return present_meeting(store.meeting(user["id"], meeting_id))

    @app.post("/api/sync")
    async def sync(request: Request, user: dict = Depends(current_user)):
        from app.sync import sync_now

        store = request.app.state.store
        # One Refresh per person at a time, across tabs and replicas. A second
        # click -- typically from a second tab -- would repeat every Graph call
        # the first is already making; it is told to wait for that one instead.
        run = {"run": secrets.token_hex(8), "started": time.time()}
        if not store.claim("refresh_lock", user["id"], run, ttl=REFRESH_LOCK_SECONDS):
            log.info("Refresh already running user=%s", user["id"])
            return {"status": "already_running"}
        try:
            # Refresh asks Graph what is new; it does not force a subscription
            # repair. Forcing one PATCHes every subscription ahead of the fetch
            # the click is waiting for. Renewal still runs on its own schedule,
            # and /api/reconnect remains the explicit repair path.
            result = await sync_now(
                store, request.app.state.graph, user["id"], slots=request.app.state.refresh_slots
            )
            result["finished"] = time.time()
            # Kept for a tab that was told already_running, or reloaded while
            # this ran, to read the outcome from /api/sync/status.
            store.put("refresh_result", user["id"], result, ttl=600)
        finally:
            store.release("refresh_lock", user["id"], run)
        return {"status": "completed", **result}

    @app.get("/api/sync/status")
    async def sync_status(request: Request, user: dict = Depends(current_user)):
        store = request.app.state.store
        return {
            "running": store.get("refresh_lock", user["id"]) is not None,
            "result": store.get("refresh_result", user["id"]),
        }

    @app.post("/api/recover-meeting")
    @graph_action
    async def recover_meeting(
        body: MeetingRecovery, request: Request, user: dict = Depends(current_user)
    ):
        from app.sync import recover_from_link

        try:
            result = await recover_from_link(
                request.app.state.store, request.app.state.graph, user["id"], body.meeting_url
            )
        except httpx.HTTPStatusError as error:
            busy = busy_from(error)
            if busy is None:
                raise
            raise busy from None
        except ValueError as error:
            log.warning(
                "Meeting recovery input rejected user=%s url_chars=%s error=%s",
                user["id"],
                len(body.meeting_url),
                error,
                exc_info=True,
            )
            raise HTTPException(400, str(error)) from None
        if not result["found"]:
            raise HTTPException(404, "Meeting not found for your organizer account.")
        if not result.get("eligible", 1):
            status = (
                403
                if result.get("skipped_not_organizer") or result.get("skipped_access_denied")
                else 409
            )
            raise HTTPException(status, result["message"])
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
        request_repair(request)
        return {"status": "CONNECTING"}

    @app.post("/api/logout")
    async def logout(request: Request, user: dict = Depends(current_user)):
        request.app.state.store.logout(bearer(request))
        return {"status": "signed_out"}

    @app.post("/api/disconnect")
    async def disconnect(request: Request, user: dict = Depends(current_user)):
        request.app.state.store.disconnect(user["id"])
        request_repair(request)
        return {"status": "disconnected"}

    async def accept(request: Request, lifecycle: bool = False):
        if "validationToken" in request.query_params:
            return PlainTextResponse(request.query_params["validationToken"])
        try:
            payload = await request.json()
            messages = validate_notifications(
                payload,
                request.app.state.config,
                lifecycle,
                request.app.state.store.subscription_user,
            )
        except PermissionError as error:
            log.warning(
                "Graph webhook authorization rejected lifecycle=%s content_length=%s error=%s",
                lifecycle,
                request.headers.get("content-length", "-"),
                error,
                exc_info=True,
            )
            raise HTTPException(403, "Invalid notification") from None
        except (ValueError, TypeError) as error:
            log.warning(
                "Graph webhook payload rejected lifecycle=%s content_length=%s "
                "error_type=%s error=%s resource_shape=%s",
                lifecycle,
                request.headers.get("content-length", "-"),
                type(error).__name__,
                error,
                getattr(error, "resource_shape", "-"),
                exc_info=True,
            )
            raise HTTPException(400, "Invalid notification") from None
        if lifecycle:
            if messages:
                request_repair(request)
            missed = [resource for event, resource in messages if event == "missed"]
            if missed:
                store = request.app.state.store
                enrolled = set(store.users())
                affected = {resource_owner(resource) for resource in missed}
                affected.discard(None)
                affected &= enrolled
                # A missed event without a resolvable resource/user still needs a
                # reaction; fall back to treating every enrolled user as affected
                # rather than silently doing nothing.
                for user_id in affected or enrolled:
                    store.status(user_id, "MISSED_EVENTS")
                    store.enqueue([UserSync(user_id=user_id).model_dump_json()])
                log.warning(
                    "Missed Graph events; queued discovery for user_count=%s",
                    len(affected or enrolled),
                )
        else:
            users = set(request.app.state.store.users())
            accepted = [
                message for message in messages if str(parse_event(message).user_id) in users
            ]
            request.app.state.store.enqueue(accepted)
            kinds = sorted(
                {
                    "insight" if isinstance(parse_event(message), InsightEvent) else "transcript"
                    for message in accepted
                }
            )
            log.info(
                "Graph webhook received=%s enrolled=%s kinds=%s",
                len(messages),
                len(accepted),
                ",".join(kinds) or "none",
            )
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
