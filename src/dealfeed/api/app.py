"""REST API (scope 6.2). Same operations as the MCP tools; OpenAPI docs at /docs.

Auth: `Authorization: Bearer dfk_...` or `X-API-Key: dfk_...`.
"""
from __future__ import annotations

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.middleware.sessions import SessionMiddleware

from .. import db
from ..config import get_settings
from ..services import accounts
from ..services.accounts import AuthError, PlanError
from ..services.business import NotFound
from ..services.deal_math import DealInputs
from ..services.ops import Ops
from ..services.search import SearchParams
from ..web import auth as web_auth
from ..web.routes import HERE as WEB_DIR
from ..web.routes import LoginRequired, login_redirect
from ..web.routes import router as web_router

app = FastAPI(
    title="Deal Feed API",
    version="0.1.0",
    description="Off-market small-business targets in the Upper Midwest, scored by likelihood to sell.",
)

# Web app (server-rendered, same process). Session cookie is signed, HttpOnly, SameSite=Lax.
app.add_middleware(SessionMiddleware, secret_key=web_auth.session_secret(), same_site="lax",
                   https_only=get_settings().env == "production", max_age=60 * 60 * 24 * 30)
app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")
app.include_router(web_router)


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


def get_ops(conn=Depends(get_conn), authorization: str | None = Header(None),
            x_api_key: str | None = Header(None)) -> Ops:
    key = x_api_key
    if not key and authorization and authorization.lower().startswith("bearer "):
        key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "Missing API key (Authorization: Bearer <key> or X-API-Key header)")
    user = accounts.user_for_api_key(conn, key)
    return Ops(conn, user, surface="api")


@app.get("/healthz", tags=["meta"])
def healthz(conn=Depends(get_conn)):
    conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/v1/me", tags=["account"])
def me(ops: Ops = Depends(get_ops)):
    return ops.me()


@app.post("/v1/targets/search", tags=["targets"])
def search_targets(params: SearchParams, ops: Ops = Depends(get_ops)):
    return ops.search_targets(params)


@app.get("/v1/businesses/{business_id}", tags=["targets"])
def get_business(business_id: str, ops: Ops = Depends(get_ops)):
    return ops.get_business(business_id)


@app.get("/v1/businesses/{business_id}/score", tags=["targets"])
def explain_score(business_id: str, ops: Ops = Depends(get_ops)):
    return ops.explain_score(business_id)


@app.post("/v1/deal-math", tags=["analysis"])
def deal_math(inputs: DealInputs, ops: Ops = Depends(get_ops)):
    return ops.sba_deal_math(inputs)


@app.get("/v1/markets/summary", tags=["analysis"])
def market_summary(state: str = "IA", industry: str | None = None, naics_code: str | None = None,
                   ops: Ops = Depends(get_ops)):
    return ops.market_summary(state, industry, naics_code)


class SaveTarget(BaseModel):
    business_id: str
    status: str = Field("new", description="new, researching, contacted, passed")
    notes: str | None = None


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
