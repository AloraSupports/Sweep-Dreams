"""Web version: sign in, run the sweep, review each visit, open the state form.

Prepared visits (which include client names) are kept in memory only, for the
current run. The ledger on disk holds visit IDs and statuses, nothing more.

Start locally:  uvicorn --factory alora_evv.web.app:create_app --reload
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from .. import credentials
from ..config import Config, load_config
from ..form import prefill_url
from ..ledger import Ledger
from ..sweep import SweepResult, collect

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


@dataclass
class RunState:
    running: bool = False
    started: datetime | None = None
    error: str | None = None
    result: SweepResult | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def prepared(self, visit_id: str):
        if self.result:
            return next((p for p in self.result.prepared if p.visit_id == visit_id), None)
        return None


def create_app(cfg: Config | None = None, state: RunState | None = None) -> FastAPI:
    cfg = cfg or load_config()
    web = cfg.settings.get("web", {})
    auth_mode = web.get("auth", "google")
    domain = (web.get("allowed_domain") or "").lower()
    state = state or RunState()
    app = FastAPI(title=web.get("app_name", "Alora Sweep"), docs_url=None, redoc_url=None)

    secret = credentials.get("web_session_secret") if auth_mode == "google" else "local-only"
    app.add_middleware(SessionMiddleware, secret_key=secret, https_only=auth_mode == "google",
                       same_site="lax", max_age=8 * 3600)

    oauth = None
    if auth_mode == "google":
        from authlib.integrations.starlette_client import OAuth
        oauth = OAuth()
        oauth.register(
            "google",
            client_id=credentials.get("google_client_id"),
            client_secret=credentials.get("google_client_secret"),
            server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
            client_kwargs={"scope": "openid email profile"})

    # ---------------------------------------------------------------- helpers
    def current_user(request: Request) -> str | None:
        if auth_mode == "none":
            return "local"
        return request.session.get("user")

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        ctx.update(request=request, app_name=app.title, user=current_user(request),
                   state=state, ledger=Ledger())
        resp = TEMPLATES.TemplateResponse(request, name, ctx)
        resp.headers["Cache-Control"] = "no-store"  # pages may show PHI
        return resp

    def run_sweep(limit: int | None, include_submitted: bool) -> None:
        try:
            state.result = collect(cfg, limit=limit, include_submitted=include_submitted)
            state.error = None
        except Exception as e:
            state.error = str(e).splitlines()[0] if str(e) else type(e).__name__
        finally:
            state.running = False

    # ------------------------------------------------------------------ auth
    @app.get("/login", response_class=HTMLResponse)
    async def login(request: Request):
        return page(request, "login.html", domain=domain)

    @app.get("/login/google")
    async def login_google(request: Request):
        if not oauth:
            return RedirectResponse("/", 303)
        return await oauth.google.authorize_redirect(
            request, str(request.url_for("auth_callback")), hd=domain or None)

    @app.get("/auth/callback")
    async def auth_callback(request: Request):
        token = await oauth.google.authorize_access_token(request)
        info = token.get("userinfo") or {}
        email = (info.get("email") or "").lower()
        if not info.get("email_verified") or not email.endswith("@" + domain):
            return page(request, "login.html", domain=domain,
                        error=f"Sign in with an @{domain} account.")
        request.session["user"] = email
        return RedirectResponse("/", 303)

    @app.get("/logout")
    async def logout(request: Request):
        request.session.clear()
        return RedirectResponse("/login", 303)

    @app.get("/health", response_class=PlainTextResponse)
    async def health():
        return "ok"

    # ------------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        return page(request, "dashboard.html")

    @app.post("/sweep")
    async def start_sweep(request: Request, limit: str = Form(""), include_submitted: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        with state.lock:
            if not state.running:
                state.running, state.started, state.error = True, datetime.now(), None
                threading.Thread(target=run_sweep, daemon=True,
                                 args=(int(limit) if limit.strip().isdigit() else None,
                                       bool(include_submitted))).start()
        return RedirectResponse("/", 303)

    @app.get("/visit/{visit_id}", response_class=HTMLResponse)
    async def visit(request: Request, visit_id: str):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        p = state.prepared(visit_id)
        if not p:
            return page(request, "dashboard.html", flash=f"Visit {visit_id} isn't in the current run.")
        return page(request, "visit.html", p=p, form_url=prefill_url(p.payload, cfg.form),
                    signer=cfg.settings.get("signer_name", ""))

    @app.post("/visit/{visit_id}/submitted")
    async def mark_submitted(request: Request, visit_id: str, undo: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        (Ledger().unmark_submitted if undo else Ledger().mark_submitted)(visit_id)
        return RedirectResponse("/", 303)

    @app.post("/visit/{visit_id}/decide")
    async def decide(request: Request, visit_id: str, choice: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        choice = choice.upper().strip()
        if choice == "CLEAR":
            Ledger().clear_decision(visit_id)
        elif choice in cfg.allowed_decisions():
            Ledger().decide(visit_id, choice, f"web: {current_user(request)}")
        return RedirectResponse("/", 303)

    return app
