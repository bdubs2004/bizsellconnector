"""REST API (scope 6.2). Same operations as the MCP tools; OpenAPI docs at /docs.

Auth: `Authorization: Bearer dfk_...` or `X-API-Key: dfk_...`.
Errors never carry internals: unexpected failures return a short reference id (details are in the server log).
"""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .. import db, security
from ..security import RateLimited, SecurityHeadersMiddleware
from ..config import get_settings
from ..states_meta import covered_states_text
from ..services import accounts
from ..services.accounts import AuthError, PlanError
from ..services.business import NotFound
from ..services.buybox import BuyBoxInput
from ..services.deal_math import DealInputs
from ..services.ops import Ops
from ..services.search import SearchParams
from ..web import auth as web_auth
from ..web.routes import HERE as WEB_DIR
from ..web.routes import CSRFError, LoginRequired, login_redirect
from ..web.routes import router as web_router

@asynccontextmanager
async def lifespan(_app):
    security.enforce_production()     # refuse to boot with insecure production settings
    yield


app = FastAPI(
    title="Deal Feed API",
    version="0.1.0",
    description=("Off-market small-business targets in " + covered_states_text()
                 + ", scored by likelihood to sell."),
    lifespan=lifespan,
)

# Web app (server-rendered, same process). Session cookie is signed, HttpOnly, SameSite=Lax.
app.add_middleware(SessionMiddleware, secret_key=web_auth.session_secret(), same_site="lax",
                   https_only=get_settings().env == "production", max_age=60 * 60 * 24 * 30)
app.add_middleware(SecurityHeadersMiddleware)          # outermost: headers on every response, incl. errors
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.include_router(web_router)


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith(("/v1/", "/healthz"))


@app.exception_handler(RateLimited)
async def _rate(request: Request, e: RateLimited):
    headers = {"Retry-After": str(e.retry_after)}
    if _wants_json(request):
        return JSONResponse({"detail": str(e)}, status_code=429, headers=headers)
    return HTMLResponse(f"<p>{str(e)}</p>", status_code=429, headers=headers)


@app.exception_handler(Exception)
async def _internal(request: Request, e: Exception):
    ref = security.log_internal_error(request.url.path, e)
    msg = f"Something went wrong on our side. Reference: {ref}"
    if _wants_json(request):
        return JSONResponse({"detail": msg, "reference": ref}, status_code=500)
    return HTMLResponse(f"<!doctype html><title>Error</title><p>{msg}</p><p><a href='/'>Home</a></p>",
                        status_code=500)


@app.exception_handler(CSRFError)
async def _csrf(request: Request, _: CSRFError):
    msg = "Your session expired or this form came from somewhere else. Reload the page and try again."
    if _wants_json(request) or request.headers.get("HX-Request") == "true":
        return JSONResponse({"detail": msg}, status_code=403) if _wants_json(request) else HTMLResponse(
            f'<div class="alert alert-error" role="alert">{msg}</div>', status_code=403)
    return HTMLResponse(f"<!doctype html><title>Expired</title><p>{msg}</p><p><a href='/'>Home</a></p>", status_code=403)


@app.exception_handler(LoginRequired)
async def _login(_: Request, e: LoginRequired):
    return login_redirect(e.next_url)


@app.exception_handler(AuthError)
async def _auth(_: Request, e: AuthError):
    return JSONResponse({"detail": str(e)}, status_code=401)


@app.exception_handler(PlanError)
async def _plan(_: Request, e: PlanError):
    return JSONResponse({"detail": str(e)}, status_code=402)


@app.exception_handler(NotFound)
async def _nf(_: Request, e: NotFound):
    return JSONResponse({"detail": str(e)}, status_code=404)


@app.exception_handler(ValueError)
async def _bad(_: Request, e: ValueError):
    return JSONResponse({"detail": str(e)}, status_code=400)


get_conn = db.request_conn


def get_ops(request: Request, conn=Depends(get_conn), authorization: str | None = Header(None),
            x_api_key: str | None = Header(None)) -> Ops:
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key or len(key) > 200:
        raise HTTPException(401, "Missing API key (Authorization: Bearer <key> or X-API-Key header)")
    try:
        user = accounts.user_for_api_key(conn, key)
    except AuthError:
        security.limiter.hit("auth_fail", security.client_ip(request))   # slows key guessing per IP
        raise
    security.limiter.hit("api", str(user.id))
    return Ops(conn, user, surface="api")


@app.get("/healthz", tags=["meta"])
def healthz(conn=Depends(get_conn)):
    conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/v1/me", tags=["account"])
def me(ops: Ops = Depends(get_ops)):
    return ops.me()


@app.get("/v1/buy-box", tags=["account"])
def get_buy_box(ops: Ops = Depends(get_ops)):
    return ops.get_buy_box()


@app.put("/v1/buy-box", tags=["account"])
def put_buy_box(body: BuyBoxInput, ops: Ops = Depends(get_ops)):
    return ops.set_buy_box(body)


@app.post("/v1/targets/search", tags=["targets"])
def search_targets(params: SearchParams, ops: Ops = Depends(get_ops)):
    return ops.search_targets(params)


@app.get("/v1/businesses/{business_id}", tags=["targets"])
def get_business(business_id: str, ops: Ops = Depends(get_ops)):
    return ops.get_business(business_id)


@app.get("/v1/businesses/{business_id}/score", tags=["targets"])
def explain_score(business_id: str, ops: Ops = Depends(get_ops)):
    return ops.explain_score(business_id)


@app.get("/v1/businesses/{business_id}/live-status", tags=["targets"])
def live_status(business_id: str, ops: Ops = Depends(get_ops)):
    """Ask the state registry about this business right now (CO, NY, CT, OR, TX)."""
    return ops.live_status(business_id)


@app.post("/v1/deal-math", tags=["analysis"])
def deal_math(inputs: DealInputs, ops: Ops = Depends(get_ops)):
    return ops.sba_deal_math(inputs)


@app.get("/v1/markets/summary", tags=["analysis"])
def market_summary(state: str | None = None, industry: str | None = None, naics_code: str | None = None,
                   ops: Ops = Depends(get_ops)):
    return ops.market_summary(state, industry, naics_code)


class SaveTarget(BaseModel):
    business_id: str = Field(max_length=64)
    status: str = Field("new", description="new, researching, contacted, passed")
    notes: str | None = Field(None, max_length=2000)


@app.post("/v1/saved-targets", tags=["pipeline"])
def save_target(body: SaveTarget, ops: Ops = Depends(get_ops)):
    return ops.save_target(body.business_id, body.status, body.notes)


@app.get("/v1/saved-targets", tags=["pipeline"])
def list_saved(status: str | None = None, ops: Ops = Depends(get_ops)):
    return ops.list_saved_targets(status)


@app.delete("/v1/saved-targets/{business_id}", tags=["pipeline"])
def remove_saved(business_id: str, ops: Ops = Depends(get_ops)):
    return ops.remove_target(business_id)


class ExportRequest(BaseModel):
    saved: bool = Field(False, description="Export your saved pipeline instead of a search")
    saved_status: str | None = None
    search: SearchParams | None = None


@app.post("/v1/exports", tags=["pipeline"], response_class=PlainTextResponse)
def export(body: ExportRequest, ops: Ops = Depends(get_ops),
           fmt: str = Query("csv", pattern="^csv$")):
    out = ops.export_targets(body.search, body.saved, body.saved_status)
    return PlainTextResponse(out["csv"], media_type="text/csv",
                             headers={"Content-Disposition": "attachment; filename=dealfeed-targets.csv",
                                      "X-Rows": str(out["rows"])})
