"""A deliberately legacy credit-union teller console.

Ugly is correct. The hostile properties are the point: the work area lives two
iframes deep, layout is table soup, element ids regenerate on every request,
there is not a single test id, and the sub-account form labels its controls with
plain <td> text rather than <label for>. Everything is server-rendered with full
page reloads. Accessible names exist where a real app would have them and are
absent where a real legacy app would have forgotten them.
"""

from __future__ import annotations

import os
import random
import string
import time
from typing import Any
from urllib.parse import parse_qs, urlencode

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from mock_app.data import ACCOUNT_TYPES, get_member, load_tenant

app = FastAPI(title="Legacy Teller Console (mock)")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))

DEFAULT_TENANT = os.environ.get("MOCK_TENANT", "heritage")
SESSION_TTL = int(os.environ.get("MOCK_SESSION_TTL", "3600"))

FAULT_MODES = {"timeout", "interstitial", "slow", "server_error",
               "unknown_dialog", "clear"}

# Single process, flat state. No database -- see PLAN.md non-negotiable 6.
_armed: str | None = None
_pending: dict[str, dict[str, Any]] = {}
_counter = {"confirmation": 100041, "account": 9000}


def rid() -> str:
    """Legacy control ids regenerate on every render. Never targetable."""
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=6))


def tenant_of(request: Request) -> dict[str, Any]:
    name = request.query_params.get("tenant") or request.cookies.get("tenant") or DEFAULT_TENANT
    return load_tenant(name)


def _set_tenant_cookie(response, request: Request) -> None:
    """A tenant chosen by query param sticks for the rest of the session."""
    q = request.query_params.get("tenant")
    if q:
        response.set_cookie("tenant", q, max_age=SESSION_TTL)


async def _form(request: Request) -> dict[str, str]:
    """Parse an urlencoded body.

    Starlette's request.form() asserts python-multipart is installed even for
    urlencoded bodies, and the locked dependency list excludes it. Every form in
    this app is urlencoded, so parsing the body directly is both sufficient and
    one fewer dependency to justify.
    """
    body = (await request.body()).decode("utf-8")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


def _authed(request: Request) -> bool:
    return request.cookies.get("sess") is not None


def _frame_url(path: str, **params: Any) -> str:
    clean = {k: v for k, v in params.items() if v is not None}
    return f"{path}?{urlencode(clean)}" if clean else path


def _chrome(request: Request, view: str, **params: Any) -> HTMLResponse:
    """Render the top-level page. The real content is two frames down."""
    tenant = tenant_of(request)
    shell_src = _frame_url("/frame/shell", view=view, tenant=tenant["tenant_id"], **params)
    response = templates.TemplateResponse(
        request, "chrome.html", {"tenant": tenant, "rid": rid, "shell_src": shell_src}
    )
    _set_tenant_cookie(response, request)
    return response


# --------------------------------------------------------------------------
# Top-level routes. Full page loads, no XHR.
# --------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def root(request: Request):
    return RedirectResponse("/login", status_code=303)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str | None = None):
    tenant = tenant_of(request)
    response = templates.TemplateResponse(
        request, "login.html", {"tenant": tenant, "rid": rid, "error": error}
    )
    _set_tenant_cookie(response, request)
    return response


@app.post("/login")
async def login_submit(request: Request):
    form = await _form(request)
    operator = form.get("operator", "")
    if not operator.strip():
        return RedirectResponse("/login?error=Operator+ID+is+required", status_code=303)
    response = RedirectResponse("/search", status_code=303)
    response.set_cookie("sess", f"S-{rid()}", max_age=SESSION_TTL)
    return response


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, member_id: str | None = None):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    if member_id:
        member_id = member_id.strip()
        if get_member(member_id):
            return RedirectResponse(f"/members/{member_id}", status_code=303)
        # Not a crash. A legitimate answer the caller needs.
        return _chrome(request, "search", member_id=member_id, error="No record found")
    return _chrome(request, "search")


@app.get("/members/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: str):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return _chrome(request, "member", member_id=member_id)


@app.get("/members/{member_id}/subaccount/new", response_class=HTMLResponse)
def subaccount_new(request: Request, member_id: str, error: str | None = None):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return _chrome(request, "subaccount_new", member_id=member_id, error=error)


@app.post("/members/{member_id}/subaccount/new")
async def subaccount_create(request: Request, member_id: str):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    form = await _form(request)
    account_type = form.get("account_type", "")
    initial_deposit = form.get("initial_deposit", "")
    nickname = form.get("nickname", "")
    raw = initial_deposit.replace(",", "").replace("$", "").strip()
    try:
        amount = float(raw or "0")
    except ValueError:
        return RedirectResponse(
            f"/members/{member_id}/subaccount/new?error=Initial+deposit+must+be+a+number",
            status_code=303,
        )
    _counter["confirmation"] += 1
    _counter["account"] += 1
    prefix = "SV" if account_type.startswith("Sav") else "CK"
    _pending[member_id] = {
        "account_type": account_type or "Savings",
        "nickname": nickname,
        "opening_balance": f"{amount:,.2f}",
        "confirmation_number": f"CNF-{_counter['confirmation']}",
        "new_account_number": f"{prefix}-{_counter['account']}",
        "acked": False,
    }
    return RedirectResponse(f"/members/{member_id}/subaccount/confirm", status_code=303)


@app.get("/members/{member_id}/subaccount/confirm", response_class=HTMLResponse)
def subaccount_confirm(request: Request, member_id: str):
    if not _authed(request):
        return RedirectResponse("/login", status_code=303)
    return _chrome(request, "subaccount_confirm", member_id=member_id)


@app.post("/members/{member_id}/subaccount/confirm")
async def subaccount_ack(request: Request, member_id: str):
    form = await _form(request)
    ack = form.get("ack", "")
    if member_id in _pending and ack == "1":
        _pending[member_id]["acked"] = True
    return RedirectResponse(f"/members/{member_id}/subaccount/confirm", status_code=303)


# --------------------------------------------------------------------------
# Frames. The work area is where the faults land.
# --------------------------------------------------------------------------

@app.get("/frame/shell", response_class=HTMLResponse)
def frame_shell(request: Request):
    tenant = tenant_of(request)
    params = dict(request.query_params)
    workarea_src = _frame_url("/frame/workarea", **params)
    return templates.TemplateResponse(
        request, "shell.html", {"tenant": tenant, "rid": rid, "workarea_src": workarea_src}
    )


@app.get("/frame/workarea", response_class=HTMLResponse)
def frame_workarea(request: Request):
    """The real work area, and the only place fault injection is observable."""
    global _armed
    tenant = tenant_of(request)
    params = dict(request.query_params)
    view = params.get("view", "search")
    member_id = params.get("member_id")

    fault, _armed = _armed, None  # armed faults fire once

    if fault == "slow":
        time.sleep(8)
    elif fault == "interstitial":
        retry = _frame_url("/frame/workarea", **params)
        return _workarea(request, tenant, "v_maintenance.html", "Scheduled Maintenance",
                         {"retry_url": retry})
    elif fault == "unknown_dialog":
        # Undeclared on purpose: the artifact has no answer for this screen, so
        # replay must hand the live session to a human rather than guess.
        return _workarea(request, tenant, "v_override.html", "Supervisor Override Required",
                         {"passthrough": params})
    elif fault == "server_error":
        return _workarea(request, tenant, "v_error.html", "Error", {}, status_code=500)
    elif fault == "timeout":
        response = _workarea(request, tenant, "v_expired.html", "Session Expired", {})
        response.delete_cookie("sess")
        return response

    ctx: dict[str, Any] = {"member_id": member_id, "account_types": ACCOUNT_TYPES,
                           "error": params.get("error")}
    member = get_member(member_id) if member_id else None

    if view == "search":
        return _workarea(request, tenant, "v_search.html", tenant["headings"]["search"], ctx)

    if member is None:
        ctx["error"] = "No record found"
        return _workarea(request, tenant, "v_search.html", tenant["headings"]["search"], ctx)
    ctx["member"] = member

    if member.get("restricted"):
        return _workarea(request, tenant, "v_denied.html", tenant["headings"]["member"], ctx)

    if view == "member":
        return _workarea(request, tenant, "v_member.html", tenant["headings"]["member"], ctx)
    if view == "subaccount_new":
        return _workarea(request, tenant, "v_subaccount_new.html",
                         tenant["headings"]["subaccount_new"], ctx)
    if view == "subaccount_confirm":
        pending = _pending.get(member_id, {})
        if tenant["extra_confirmation"] and not pending.get("acked"):
            return _workarea(request, tenant, "v_subaccount_ack.html",
                             "Acknowledgement Required", ctx)
        ctx.update(pending)
        return _workarea(request, tenant, "v_subaccount_confirm.html",
                         tenant["headings"]["subaccount_confirm"], ctx)

    return _workarea(request, tenant, "v_search.html", tenant["headings"]["search"], ctx)


def _workarea(request: Request, tenant: dict, view_template: str, page_title: str,
              ctx: dict[str, Any], status_code: int = 200) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "workarea.html",
        {"tenant": tenant, "rid": rid, "view_template": view_template,
         "page_title": page_title, **ctx},
        status_code=status_code,
    )


# --------------------------------------------------------------------------
# Fault injection. An endpoint, not a code edit -- tests and evidence runs
# call it. Denied to the agent by allowlist.yaml.
# --------------------------------------------------------------------------

@app.post("/admin/inject")
async def inject(request: Request):
    global _armed
    body = await request.json()
    mode = body.get("mode", "clear")
    if mode not in FAULT_MODES:
        return JSONResponse({"error": f"unknown mode {mode!r}",
                             "modes": sorted(FAULT_MODES)}, status_code=400)
    _armed = None if mode == "clear" else mode
    return {"armed": _armed}


@app.post("/admin/reset")
def reset():
    global _armed
    _armed = None
    _pending.clear()
    return {"ok": True}
