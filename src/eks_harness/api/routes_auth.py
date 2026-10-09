from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request, Response

from eks_harness.api.deps import current_principal, get_ctx, optional_principal, require_admin
from eks_harness.api.errors import ApiError, bad_request, forbidden, not_found, unauthorized
from eks_harness.api.schemas import (
    ApiKeyCreate,
    ApiKeyCreated,
    ApiKeyList,
    ApiKeyOut,
    GrantCreate,
    GrantList,
    GrantOut,
    LoginRequest,
    LoginResponse,
    MeResponse,
    OkResponse,
    UserCreate,
    UserList,
    UserOut,
    UserUpdate,
    ts_to_datetime,
)
from eks_harness.auth import core, proxies
from eks_harness.auth import grants as auth_grants
from eks_harness.auth import keys as auth_keys
from eks_harness.auth import users as auth_users
from eks_harness.auth import sessions as auth_sessions
from eks_harness.auth.core import Principal
from eks_harness.auth.ratelimit import LoginRateLimiter, login_keys
from eks_harness.daemon.context import AppContext
from eks_harness.db.repos import api_keys, grants, sessions, users

router = APIRouter(tags=["auth"])

LIMITER_SERVICE = "auth.login_limiter"


def login_limiter(ctx: AppContext) -> LoginRateLimiter:
    with ctx.lock:
        if not ctx.has_service(LIMITER_SERVICE):
            ctx.register_service(LIMITER_SERVICE, LoginRateLimiter())
        return ctx.service(LIMITER_SERVICE)


def api_error(problem: auth_users.AccountError) -> ApiError:
    return ApiError(problem.status, problem.error, problem.message)


def user_out(user: users.User) -> UserOut:
    return UserOut(id=user.id, username=user.username, role=user.role, disabled=user.disabled,
                   builtin=user.builtin, has_password=user.has_password, created_at=ts_to_datetime(user.created_at),
                   updated_at=ts_to_datetime(user.updated_at))


def key_out(record: api_keys.ApiKey) -> ApiKeyOut:
    return ApiKeyOut(id=record.id, user_id=record.user_id, username=record.username, name=record.name,
                     prefix=record.prefix, created_at=ts_to_datetime(record.created_at),
                     last_used_at=ts_to_datetime(record.last_used_at), revoked_at=ts_to_datetime(record.revoked_at),
                     active=record.active)


def grant_out(grant: grants.Grant) -> GrantOut:
    return GrantOut(id=grant.id, user_id=grant.user_id, username=grant.username, project_id=grant.project_id,
                    session_id=grant.session_id, session_slug=grant.session_slug, session_name=grant.session_name,
                    level=grant.level, created_at=ts_to_datetime(grant.created_at))


@router.post("/api/auth/login", response_model=LoginResponse)
def login(body: LoginRequest, request: Request, response: Response) -> LoginResponse:
    ctx = get_ctx(request)
    if not ctx.config["auth.enabled"]:
        raise bad_request("Authentication is disabled on this daemon; there is nothing to log in to.",
                          error="auth_disabled")
    use_key = bool(body.api_key and body.api_key.strip())
    if not use_key and not (body.username and body.password):
        raise bad_request("Send a username and password, or an API key.", error="credentials_required")
    ip = proxies.client_ip(request, ctx.config)
    keys = login_keys(ip, None if use_key else body.username)
    limiter = login_limiter(ctx)
    wait = limiter.retry_after(keys)
    if wait is not None:
        raise ApiError(429, "too_many_attempts", f"Too many failed logins; try again in {wait} seconds.",
                       headers={"Retry-After": str(wait)}, retry_after=wait)
    if use_key:
        user = auth_sessions.check_key_login(ctx.db, body.api_key)
    else:
        user = auth_sessions.check_password_login(ctx.db, body.username, body.password)
    if user is None:
        limiter.failure(keys)
        if use_key:
            raise unauthorized("The API key is unknown, revoked or malformed.", error="invalid_credentials")
        raise unauthorized("Wrong username or password.", error="invalid_credentials")
    limiter.success(keys)
    hours = int(ctx.config["auth.sessionHours"])
    token, record = core.create_web_session(ctx.db, user.id, hours, ip, request.headers.get("user-agent"))
    auth_sessions.set_session_cookie(response, token, ctx.config, request, hours * 3600)
    response.headers["Cache-Control"] = "no-store"
    return LoginResponse(user=user_out(user), csrf_token=record.csrf, expires_at=ts_to_datetime(record.expires_at))


@router.post("/api/auth/logout", response_model=OkResponse)
def logout(request: Request, response: Response,
           principal: Principal | None = Depends(optional_principal)) -> OkResponse:
    ctx = get_ctx(request)
    token = request.cookies.get(core.COOKIE_NAME)
    if token:
        core.end_web_session(ctx.db, token)
    auth_sessions.clear_session_cookie(response, ctx.config, request)
    return OkResponse()


@router.get("/api/auth/me", response_model=MeResponse)
def me(request: Request, response: Response, principal: Principal = Depends(current_principal)) -> MeResponse:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    user = users.get(conn, principal.user_id)
    if user is None:
        raise unauthorized()
    own = [] if principal.is_admin else [grant_out(g) for g in grants.list_grants(conn, user_id=user.id)]
    response.headers["Cache-Control"] = "no-store"
    return MeResponse(user=user_out(user), via=principal.via, auth_enabled=bool(ctx.config["auth.enabled"]),
                      csrf_token=principal.csrf, key_prefix=principal.key_prefix, grants=own)


def _target_user(ctx: AppContext, principal: Principal, user_id: int | None, username: str | None) -> users.User:
    conn = ctx.db.conn()
    if user_id is None and not username:
        user = users.get(conn, principal.user_id)
        if user is None:
            raise unauthorized()
        return user
    try:
        user = auth_users.resolve_user(conn, user_id=user_id, username=username)
    except auth_users.AccountError as problem:
        if not principal.is_admin:
            raise forbidden("Only an admin can manage another user's keys.", error="admin_required") from problem
        raise api_error(problem) from problem
    if user.id != principal.user_id and not principal.is_admin:
        raise forbidden("Only an admin can manage another user's keys.", error="admin_required")
    return user


@router.get("/api/keys", response_model=ApiKeyList)
def list_keys(request: Request, all: bool = Query(False), user_id: int | None = Query(None, alias="userId"),
              user: str | None = Query(None), include_revoked: bool = Query(True, alias="includeRevoked"),
              principal: Principal = Depends(current_principal)) -> ApiKeyList:
    ctx = get_ctx(request)
    conn = ctx.db.conn()
    if all:
        if not principal.is_admin:
            raise forbidden("Only an admin can list every key.", error="admin_required")
        records = api_keys.list_keys(conn, None, include_revoked=include_revoked)
    else:
        target = _target_user(ctx, principal, user_id, user)
        records = api_keys.list_keys(conn, target.id, include_revoked=include_revoked)
    return ApiKeyList(items=[key_out(r) for r in records])


@router.post("/api/keys", response_model=ApiKeyCreated, status_code=201)
def create_key(body: ApiKeyCreate, request: Request, response: Response,
               principal: Principal = Depends(current_principal)) -> ApiKeyCreated:
    ctx = get_ctx(request)
    target = _target_user(ctx, principal, body.user_id, body.username)
    try:
        raw, record = auth_keys.create_key(ctx.db, target, body.name)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    response.headers["Cache-Control"] = "no-store"
    return ApiKeyCreated(**key_out(record).model_dump(), key=raw)


@router.delete("/api/keys/{ref}", response_model=ApiKeyOut)
def revoke_key(ref: str, request: Request, principal: Principal = Depends(current_principal)) -> ApiKeyOut:
    ctx = get_ctx(request)
    try:
        with ctx.db.transaction() as conn:
            record = auth_keys.find_key(conn, ref)
            if record.user_id != principal.user_id and not principal.is_admin:
                raise not_found(f"No API key {ref}.", error="key_not_found")
            api_keys.revoke(conn, record.id)
            record = api_keys.get(conn, record.id)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    return key_out(record)


@router.get("/api/users", response_model=UserList)
def list_users(request: Request, include_builtin: bool = Query(False, alias="includeBuiltin"),
               _: Principal = Depends(require_admin)) -> UserList:
    conn = get_ctx(request).db.conn()
    return UserList(items=[user_out(u) for u in users.list_all(conn, include_builtin=include_builtin)])


@router.post("/api/users", response_model=UserOut, status_code=201)
def create_user(body: UserCreate, request: Request, _: Principal = Depends(require_admin)) -> UserOut:
    try:
        user = auth_users.create_user(get_ctx(request).db, body.username, body.role, body.password)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    return user_out(user)


def _load_user(request: Request, ref: str) -> users.User:
    try:
        return auth_users.resolve_user(get_ctx(request).db.conn(), ref)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem


@router.get("/api/users/{ref}", response_model=UserOut)
def get_user(ref: str, request: Request, _: Principal = Depends(require_admin)) -> UserOut:
    return user_out(_load_user(request, ref))


@router.patch("/api/users/{ref}", response_model=UserOut)
def update_user(ref: str, body: UserUpdate, request: Request,
                principal: Principal = Depends(require_admin)) -> UserOut:
    user = _load_user(request, ref)
    try:
        updated = auth_users.update_user(get_ctx(request).db, user, username=body.username, password=body.password,
                                       role=body.role, disabled=body.disabled,
                                       keep_web_session=principal.web_session_id)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    return user_out(updated)


@router.delete("/api/users/{ref}", response_model=OkResponse)
def delete_user(ref: str, request: Request, _: Principal = Depends(require_admin)) -> OkResponse:
    user = _load_user(request, ref)
    try:
        auth_users.delete_user(get_ctx(request).db, user)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    return OkResponse()


@router.get("/api/grants", response_model=GrantList)
def list_grants(request: Request, user_id: int | None = Query(None, alias="userId"), user: str | None = Query(None),
                project: str | None = Query(None), session: str | None = Query(None),
                _: Principal = Depends(require_admin)) -> GrantList:
    conn = get_ctx(request).db.conn()
    target_id = None
    if user_id is not None or user:
        try:
            target_id = auth_users.resolve_user(conn, user_id=user_id, username=user).id
        except auth_users.AccountError as problem:
            raise api_error(problem) from problem
    session_id = None
    if session:
        if not project:
            raise bad_request("Filtering by session needs the project too.", error="project_required")
        found = sessions.find_in_project(conn, project, session)
        if found is None:
            raise not_found(f"No session {session} in {project}.", error="session_not_found")
        session_id = found.id
    items = grants.list_grants(conn, user_id=target_id, project_id=project or None, session_id=session_id)
    return GrantList(items=[grant_out(g) for g in items])


@router.post("/api/grants", response_model=GrantOut, status_code=201)
def create_grant(body: GrantCreate, request: Request, _: Principal = Depends(require_admin)) -> GrantOut:
    db = get_ctx(request).db
    if body.user_id is None and not body.username:
        raise bad_request("Name the user (userId or username).", error="user_required")
    try:
        user = auth_users.resolve_user(db.conn(), user_id=body.user_id, username=body.username)
        grant = auth_grants.add_grant(db, user, body.project, body.level, body.session)
    except auth_users.AccountError as problem:
        raise api_error(problem) from problem
    return grant_out(grant)


@router.delete("/api/grants/{grant_id}", response_model=OkResponse)
def delete_grant(grant_id: int, request: Request, _: Principal = Depends(require_admin)) -> OkResponse:
    with get_ctx(request).db.transaction() as conn:
        if not grants.delete(conn, grant_id):
            raise not_found(f"No grant {grant_id}.", error="grant_not_found")
    return OkResponse()
