"""Web version: sign in, run the sweep, review each visit, open the state form.

Prepared visits (which include client names) are kept in memory only, for the
current run. The ledger on disk holds visit IDs and statuses, nothing more.

Start locally:  uvicorn --factory alora_evv.web.app:create_app --reload
"""
from __future__ import annotations

import os
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
from ..paths import sub
from ..sweep import SweepResult, collect

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def clock(dt: datetime | None, day: bool = False) -> str:
    """'8:05 AM' or 'Friday 8:05 AM' on every platform (strftime's %-I is glibc-only
    and raises on Windows)."""
    if not dt:
        return ""
    t = dt.strftime("%I:%M %p").lstrip("0")
    return f"{dt:%A} {t}" if day else t


TEMPLATES.env.filters["clock"] = clock


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


def cross_site(request: Request) -> bool:
    """True when another site sent this POST. Every current browser sets Sec-Fetch-Site;
    the Origin header is the fallback. Same-site subdomains count as foreign."""
    site = request.headers.get("sec-fetch-site")
    if site and site not in ("same-origin", "none"):
        return True
    origin = request.headers.get("origin")
    if origin and origin.split("//", 1)[-1].lower() != request.headers.get("host", "").lower():
        return True
    return False


def create_app(cfg: Config | None = None, state: RunState | None = None) -> FastAPI:
    cfg = cfg or load_config()
    web = cfg.settings.get("web", {})
    # ALORA_EVV_WEB_AUTH wins over settings.yaml: the Docker image sets it to google, so a
    # settings file left on 'none' for local testing can't switch sign-in off on the server.
    auth_mode = (os.environ.get("ALORA_EVV_WEB_AUTH") or web.get("auth") or "google").strip().lower()
    if auth_mode not in ("google", "none"):
        raise RuntimeError(f"web.auth must be 'google' or 'none', not '{auth_mode}'")
    domain = (web.get("allowed_domain") or "").strip().lower()
    if auth_mode == "google" and not domain:
        raise RuntimeError("web.allowed_domain is required with web.auth: google")
    if auth_mode == "none":
        print("WARNING: web.auth is 'none' — no sign-in. Local testing only; never on a server.")
    include_phi = bool(web.get("prefill_phi", False))
    state = state or RunState()
    app = FastAPI(title=web.get("app_name", "Alora Sweep"), docs_url=None, redoc_url=None)
    for folder in ("axiscare", "reports"):
        sub(folder)  # so the AxisCare reports can be copied in before the first sweep

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

    def page(request: Request, name: str, status_code: int = 200, **ctx) -> HTMLResponse:
        ctx.update(request=request, app_name=app.title, user=current_user(request), state=state)
        resp = TEMPLATES.TemplateResponse(request, name, ctx, status_code=status_code)
        resp.headers["Cache-Control"] = "no-store"  # pages may show PHI
        return resp

    def decision_choices() -> list[tuple[str, str]]:
        """The 'Handle as' options, from rules.yaml so a new error type needs no code change."""
        out = []
        for key, rule in cfg.rules.get("error_types", {}).items():
            text = rule.get("description") or key
            if rule.get("reason_code"):
                text += f" ({rule['reason_code']})"
            out.append((key, f"{key}: {text}"))
        out += [(x, x.title()) for x in cfg.rules.get("decision_extras", [])]
        return out

    def dashboard(request: Request, status_code: int = 200, **ctx) -> HTMLResponse:
        ledger = Ledger()
        r = state.result
        submitted_now: dict[str, str] = {}
        if r:  # only marks made since this run started grey a row out
            since = r.started.isoformat(timespec="seconds")
            submitted_now = {v: t for v, t in ledger.recently_submitted(365).items() if t >= since}
        flash = ctx.pop("flash", None) or request.session.pop("flash", None)
        return page(request, "dashboard.html", status_code, flash=flash, submitted_now=submitted_now,
                    decisions=ledger.decisions(), choices=decision_choices(), **ctx)

    def run_sweep(limit: int | None, include_submitted: bool) -> None:
        try:
            state.result = collect(cfg, limit=limit, include_submitted=include_submitted)
            state.error = None
        except BaseException as e:  # a thread must not swallow SystemExit-style errors either
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
        from authlib.integrations.base_client.errors import OAuthError
        try:
            token = await oauth.google.authorize_access_token(request)
        except OAuthError:  # cancelled, expired, or a reloaded callback
            return page(request, "login.html", domain=domain,
                        error="Sign-in didn't complete. Please try again.")
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
    async def home(request: Request):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        return dashboard(request)

    @app.post("/sweep")
    async def start_sweep(request: Request, limit: str = Form(""), include_submitted: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        if cross_site(request):
            return dashboard(request, 403, flash="That request didn't come from this site.")
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
            return dashboard(request, flash=f"Visit {visit_id} isn't in the current run.")
        return page(request, "visit.html", p=p, form_url=prefill_url(p.payload, cfg.form, include_phi),
                    prefill_phi=include_phi, phi_keys=cfg.form.get("prefill_phi_keys") or [],
                    signer=cfg.settings.get("signer_name", ""))

    @app.post("/visit/{visit_id}/submitted")
    async def mark_submitted(request: Request, visit_id: str, undo: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        if cross_site(request):
            return dashboard(request, 403, flash="That request didn't come from this site.")
        if not state.prepared(visit_id):
            request.session["flash"] = f"Visit {visit_id} isn't in the current run; nothing changed."
            return RedirectResponse("/", 303)
        (Ledger().unmark_submitted if undo else Ledger().mark_submitted)(visit_id)
        return RedirectResponse("/", 303)

    @app.post("/visit/{visit_id}/decide")
    async def decide(request: Request, visit_id: str, choice: str = Form("")):
        if not current_user(request):
            return RedirectResponse("/login", 303)
        if cross_site(request):
            return dashboard(request, 403, flash="That request didn't come from this site.")
        choice = choice.upper().strip()
        if choice == "CLEAR":
            Ledger().clear_decision(visit_id)
            request.session["flash"] = f"Cleared the decision for visit {visit_id}."
        elif choice in cfg.allowed_decisions():
            Ledger().decide(visit_id, choice, f"web: {current_user(request)}")
            request.session["flash"] = f"Visit {visit_id} will be handled as {choice} on the next sweep."
        else:
            request.session["flash"] = f"'{choice or 'blank'}' isn't a choice this app knows; nothing was saved."
        return RedirectResponse("/", 303)

    return app
