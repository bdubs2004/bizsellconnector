"""Server-rendered web app (scope 6.3): FastAPI + Jinja + HTMX, no build step.

Every page works as plain HTML forms; HTMX swaps in partials when it's loaded
(requests carry an `HX-Request` header, and those get just the fragment back).
All data access goes through services/ops.py with surface="web", so plan
limits and metering match the API and MCP server.
"""
from __future__ import annotations

import json
import uuid
from datetime import date
from pathlib import Path
from urllib.parse import urlencode

import psycopg
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError


from .. import db, security
from ..config import get_settings
from ..industries import INDUSTRIES
from ..services import accounts
from ..services.accounts import PlanError, User
from ..services.business import NotFound
from ..buy import industry_options
from ..services.buybox import BuyBoxInput
from ..services.deal_math import DealInputs
from ..services.ops import Ops
from ..services.search import COUNT_CAP, SearchParams, search_rows, target_summary
from ..states_meta import STATE_NAMES, covered_states_text
from ..services.targets import STATUSES
from . import auth

HERE = Path(__file__).parent
templates = Jinja2Templates(directory=HERE / "templates")
class CSRFError(Exception):
    pass


async def csrf_protect(request: Request) -> None:
    """Every state-changing web request must carry this session's CSRF token, as the X-CSRF-Token header
    (HTMX, set on <body>) or a csrf_token form field. Blocks other sites from acting as a signed-in user."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return
    submitted = request.headers.get("X-CSRF-Token")
    if not submitted and "form" in request.headers.get("content-type", ""):
        submitted = (await request.form()).get("csrf_token")
    if not security.csrf_ok(request, submitted):
        raise CSRFError()


router = APIRouter(include_in_schema=False, dependencies=[Depends(csrf_protect)])
PAGE_SIZE = 25


# --- template helpers ---------------------------------------------------------

def _money(v) -> str:
    if v is None:
        return "–"
    v = float(v)
    if abs(v) >= 1e6:
        return f"${v / 1e6:,.2f}M"
    return f"${v:,.0f}"


def _score_band(score) -> str:
    if score is None:
        return "none"
    return "high" if score >= 75 else "mid" if score >= 50 else "low"


templates.env.filters["money"] = _money
templates.env.filters["score_band"] = _score_band
templates.env.globals["STATUSES"] = STATUSES
templates.env.globals["INDUSTRIES"] = INDUSTRIES
templates.env.globals["STATE_NAMES"] = STATE_NAMES
templates.env.globals["COVERED_STATES"] = covered_states_text()
templates.env.globals["COUNT_CAP"] = COUNT_CAP


def render(request: Request, name: str, user: User | None, status_code: int = 200, **ctx) -> HTMLResponse:
    ctx |= {"user": user, "dev_auth": auth.dev_login_allowed(), "path": request.url.path,
            "csrf_token": security.csrf_token(request)}
    return templates.TemplateResponse(request, name, ctx, status_code=status_code)


def is_htmx(request: Request) -> bool:
    return request.headers.get("HX-Request") == "true"


def plan_error(request: Request, user: User | None, e: Exception) -> HTMLResponse:
    if is_htmx(request):
        return render(request, "partials/alert.html", user, 200, kind="warn", message=str(e))
    return render(request, "message.html", user, 402, title="Plan limit", message=str(e))


class LoginRequired(Exception):
    def __init__(self, next_url: str):
        self.next_url = next_url


def get_user(request: Request, conn: psycopg.Connection = Depends(db.request_conn)) -> User | None:
    return auth.current_user(request, conn)


def require_user(request: Request, conn: psycopg.Connection = Depends(db.request_conn)) -> User:
    user = auth.current_user(request, conn)
    if not user:
        raise LoginRequired(str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""))
    security.limiter.hit("web", str(user.id))
    return user


def login_redirect(next_url: str) -> RedirectResponse:
    return RedirectResponse(f"/login?{urlencode({'next': next_url})}", status_code=303)


# --- public ---------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
def index(request: Request, conn=Depends(db.request_conn), user=Depends(get_user)):
    """Landing page with a free sample: top 3 per v1 industry (the marketing hook)."""
    today = date.today()
    samples = []
    for ind in (i for i in INDUSTRIES if i.v1):
        # min_score keeps just-acquired or too-young businesses out of the showcase
        rows, total, _ = search_rows(conn, SearchParams(industry=ind.slug, min_years_in_business=10, min_score=50),
                                     limit=3)
        samples.append({"industry": ind, "total": total, "targets": [target_summary(r, today) for r in rows]})
    return render(request, "index.html", user, samples=samples)


@router.get("/privacy", response_class=HTMLResponse)
def privacy(request: Request, user=Depends(get_user)):
    return render(request, "privacy.html", user, contact=get_settings().contact_email)


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/search", user=Depends(get_user)):
    if user:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", None, next=_safe_next(next))


def _safe_next(n: str | None) -> str:
    # Only allow local paths so the login form can't be used as an open redirect.
    return n if n and n.startswith("/") and not n.startswith("//") else "/search"


@router.post("/login")
def login(request: Request, email: str = Form(..., max_length=254), next: str = Form("/search", max_length=500),
          conn=Depends(db.request_conn)):
    security.limiter.hit("login", security.client_ip(request))
    try:
        auth.dev_sign_in(request, conn, email)
    except PermissionError as e:
        return render(request, "message.html", None, 403, title="Sign-in unavailable", message=str(e))
    except ValueError as e:
        return render(request, "login.html", None, 400, next=_safe_next(next), error=str(e), email=email)
    return RedirectResponse(_safe_next(next), status_code=303)


@router.post("/logout")
def logout(request: Request):
    auth.sign_out(request)
    return RedirectResponse("/", status_code=303)


# --- search ---------------------------------------------------------------------

def _int(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def params_from_query(qp) -> tuple[SearchParams, int, dict]:
    page = max(1, _int(qp.get("page")) or 1)
    form = {k: qp.get(k, "") for k in ("industry", "state", "counties", "city", "name", "min_years", "min_employees",
                                        "max_employees", "min_score", "sized_only", "sort", "in_box")}
    form["sort"] = form["sort"] if form["sort"] in ("priority", "sell", "buy") else "priority"
    counties = [c.strip() for c in form["counties"].split(",") if c.strip()] or None
    p = SearchParams(
        industry=form["industry"] or None, state=form["state"] or None, counties=counties, city=form["city"] or None,
        name_contains=form["name"] or None, min_years_in_business=_int(form["min_years"]),
        min_employees=_int(form["min_employees"]), max_employees=_int(form["max_employees"]),
        min_score=_int(form["min_score"]), include_unknown_size=not form["sized_only"],
        sort=form["sort"], in_buy_box=bool(form["in_box"]),
        limit=PAGE_SIZE, offset=(page - 1) * PAGE_SIZE,
    )
    return p, page, form


@router.get("/search", response_class=HTMLResponse)
def search_page(request: Request, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    try:
        p, page, form = params_from_query(request.query_params)
    except ValidationError:
        p, page, form = params_from_query({})
    ops = Ops(conn, user, "web")
    try:
        result = ops.search_targets(p)
    except PlanError as e:
        return plan_error(request, user, e)
    box = ops.buy_box()
    qs = {k: v for k, v in form.items() if v}
    ctx = dict(result=result, form=form, page=page, page_size=PAGE_SIZE, box=box,
               prev_qs=urlencode(qs | {"page": page - 1}) if page > 1 else None,
               next_qs=urlencode(qs | {"page": page + 1}) if result["next_offset"] is not None else None,
               export_qs=urlencode(qs),
               # "<" escaped so a business name can't close the <script> tag the JSON sits in
               points=json.dumps([{"id": t["business_id"], "name": t["name"], "score": t["score"], **t["location"]}
                                  for t in result["results"] if t["location"]]).replace("<", "\\u003c"))
    if is_htmx(request) and request.headers.get("HX-Target") == "results":
        return render(request, "partials/results.html", user, **ctx)
    return render(request, "search.html", user, **ctx)


@router.get("/search/export.csv")
def search_export(request: Request, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    p, _, _ = params_from_query(request.query_params)
    try:
        out = Ops(conn, user, "web").export_targets(search_params=p)
    except PlanError as e:
        return plan_error(request, user, e)
    return Response(out["csv"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=dealfeed-search.csv"})


# --- business profile -------------------------------------------------------------

@router.get("/business/{business_id}", response_class=HTMLResponse)
def business_page(business_id: str, request: Request, conn=Depends(db.request_conn),
                  user: User = Depends(require_user)):
    try:
        b = Ops(conn, user, "web").get_business(business_id)
    except NotFound:
        return render(request, "message.html", user, 404, title="Not found", message="No business with that id.")
    except PlanError as e:
        return plan_error(request, user, e)
    return render(request, "business.html", user, b=b, saved=b["your_pipeline"])


@router.get("/business/{business_id}/live", response_class=HTMLResponse)
def business_live(business_id: str, request: Request, conn=Depends(db.request_conn),
                  user: User = Depends(require_user)):
    try:
        res = Ops(conn, user, "web").live_status(business_id)
    except NotFound:
        return render(request, "partials/alert.html", user, 404, kind="error", message="No business with that id.")
    if is_htmx(request):
        return render(request, "partials/live_status.html", user, live=res, business_id=business_id)
    return RedirectResponse(f"/business/{business_id}", status_code=303)


@router.post("/business/{business_id}/save", response_class=HTMLResponse)
def save_target(business_id: str, request: Request, status: str = Form("new"), notes: str = Form(""),
                conn=Depends(db.request_conn), user: User = Depends(require_user)):
    ops = Ops(conn, user, "web")
    try:
        saved = ops.save_target(business_id, status, notes or None)
    except (ValueError, NotFound) as e:
        return render(request, "partials/alert.html", user, 400, kind="error", message=str(e))
    if is_htmx(request):
        return render(request, "partials/save_form.html", user, business_id=business_id, saved=saved, flash="Saved")
    return RedirectResponse(f"/business/{business_id}", status_code=303)


@router.post("/business/{business_id}/remove", response_class=HTMLResponse)
def remove_target(business_id: str, request: Request, conn=Depends(db.request_conn),
                  user: User = Depends(require_user)):
    Ops(conn, user, "web").remove_target(business_id)
    if is_htmx(request):
        if request.headers.get("HX-Target", "").startswith("row-"):
            return HTMLResponse("")
        return render(request, "partials/save_form.html", user, business_id=business_id, saved=None, flash="Removed")
    return RedirectResponse("/pipeline", status_code=303)


# --- pipeline ---------------------------------------------------------------------

@router.get("/pipeline", response_class=HTMLResponse)
def pipeline_page(request: Request, status: str | None = None, conn=Depends(db.request_conn),
                  user: User = Depends(require_user)):
    status = status if status in STATUSES else None
    data = Ops(conn, user, "web").list_saved_targets(status)
    return render(request, "pipeline.html", user, data=data, status=status)


@router.post("/pipeline/{business_id}", response_class=HTMLResponse)
def pipeline_update(business_id: str, request: Request, status: str = Form(...), notes: str = Form(""),
                    conn=Depends(db.request_conn), user: User = Depends(require_user)):
    ops = Ops(conn, user, "web")
    try:
        ops.save_target(business_id, status, notes)
    except (ValueError, NotFound) as e:
        return render(request, "partials/alert.html", user, 400, kind="error", message=str(e))
    if is_htmx(request):
        t = next(t for t in ops.list_saved_targets()["targets"] if t["business_id"] == business_id)
        return render(request, "partials/pipeline_row.html", user, t=t, flash=True)
    return RedirectResponse("/pipeline", status_code=303)


@router.get("/pipeline/export.csv")
def pipeline_export(request: Request, status: str | None = None, conn=Depends(db.request_conn),
                    user: User = Depends(require_user)):
    try:
        out = Ops(conn, user, "web").export_targets(saved=True, saved_status=status if status in STATUSES else None)
    except PlanError as e:
        return plan_error(request, user, e)
    return Response(out["csv"], media_type="text/csv",
                    headers={"Content-Disposition": "attachment; filename=dealfeed-pipeline.csv"})


# --- deal math --------------------------------------------------------------------

DEAL_DEFAULTS = {name: f.default for name, f in DealInputs.model_fields.items()
                 if not f.is_required() and f.default is not None and not isinstance(f.default, (bool, str))}

@router.get("/deal-math", response_class=HTMLResponse)
def deal_math_page(request: Request, business_id: str | None = None, conn=Depends(db.request_conn),
                   user: User = Depends(require_user)):
    name = None
    if business_id:
        row = conn.execute("SELECT legal_name FROM businesses WHERE id::text = %s", (business_id,)).fetchone()
        name = row["legal_name"] if row else None
    # Prefill from query (e.g. the business page's buy-score estimate): ?purchase_price=..&cash_flow=..
    prefill = {k: v for k, v in request.query_params.items() if k in DealInputs.model_fields and v}
    form = (DEAL_DEFAULTS | prefill) if prefill else {}
    return render(request, "deal_math.html", user, business_name=name, defaults=DEAL_DEFAULTS, form=form)


@router.post("/deal-math", response_class=HTMLResponse)
async def deal_math_run(request: Request, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    form = dict(await request.form())
    data = {k: v for k, v in form.items() if v != ""}
    data["seller_note_full_standby"] = "seller_note_full_standby" in form
    try:
        out = Ops(conn, user, "web").sba_deal_math(DealInputs(**data))
        ctx = {"r": out, "errors": None}
    except ValidationError as e:
        ctx = {"r": None, "errors": [f"{'.'.join(map(str, err['loc'])) or 'input'}: {err['msg']}" for err in e.errors()]}
    if is_htmx(request):
        return render(request, "partials/deal_result.html", user, **ctx)
    return render(request, "deal_math.html", user, business_name=None, defaults=DEAL_DEFAULTS,
                  form=form, **ctx)


# --- buy box ---------------------------------------------------------------------------

@router.get("/buy-box", response_class=HTMLResponse)
def buy_box_page(request: Request, saved: str | None = None, conn=Depends(db.request_conn),
                 user: User = Depends(require_user)):
    ops = Ops(conn, user, "web")
    return render(request, "buy_box.html", user, box=ops.buy_box(), options=industry_options(), errors=None,
                  flash="Saved. Buy scores now use your buy box." if saved else None)


@router.post("/buy-box", response_class=HTMLResponse)
async def buy_box_save(request: Request, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    form = await request.form()
    data = {k: v for k, v in form.items() if k not in ("industries", "states") and v != ""}
    data["industries"] = form.getlist("industries")
    data["states"] = form.getlist("states")
    ops = Ops(conn, user, "web")
    try:
        ops.set_buy_box(BuyBoxInput(**data))
    except ValidationError as e:
        errors = [f"{'.'.join(map(str, err['loc'])) or 'input'}: {err['msg']}" for err in e.errors()]
        return render(request, "buy_box.html", user, 400, box=ops.buy_box(), options=industry_options(),
                      errors=errors, flash=None)
    return RedirectResponse("/buy-box?saved=1", status_code=303)


# --- connect your AI / account -------------------------------------------------------

@router.get("/connect", response_class=HTMLResponse)
def connect_page(request: Request, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    return _connect(request, conn, user)


def _connect(request, conn, user, new_key=None, status_code=200):
    s = get_settings()
    return render(request, "connect.html", user, status_code,
                  usage=accounts.usage_summary(conn, user), keys=accounts.list_api_keys(conn, user),
                  new_key=new_key, mcp_url=s.public_url.rstrip("/") + "/mcp", plans=accounts.PLANS,
                  db_url_hint="postgresql://USER:PASSWORD@HOST:5432/dealfeed")


@router.post("/connect/keys", response_class=HTMLResponse)
def create_key(request: Request, label: str = Form("", max_length=200), conn=Depends(db.request_conn),
               user: User = Depends(require_user)):
    try:
        key = accounts.create_api_key(conn, user, security.clean_user_text(label, 60))
    except PlanError as e:
        return plan_error(request, user, e)
    return _connect(request, conn, user, new_key=key)


@router.post("/connect/keys/{key_id}/revoke")
def revoke_key(key_id: str, conn=Depends(db.request_conn), user: User = Depends(require_user)):
    try:
        uuid.UUID(key_id)
    except ValueError:
        return RedirectResponse("/connect", status_code=303)
    accounts.revoke_user_key(conn, user, key_id)     # scoped to this user: can't touch anyone else's keys
    return RedirectResponse("/connect", status_code=303)


@router.post("/connect/plan")
def dev_set_plan(request: Request, plan: str = Form(...), conn=Depends(db.request_conn),
                 user: User = Depends(require_user)):
    """Dev-only plan switcher, so every plan can be tried before Stripe exists."""
    if not auth.dev_login_allowed():
        return render(request, "message.html", user, 403, title="Not available",
                      message="Plan changes go through billing in production.")
    accounts.set_plan(conn, user, plan)
    return RedirectResponse("/connect", status_code=303)
