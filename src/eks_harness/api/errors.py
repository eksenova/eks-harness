from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger("eks_harness.api")

STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    410: "gone",
    413: "payload_too_large",
    415: "unsupported_media_type",
    416: "range_not_satisfiable",
    422: "validation_failed",
    429: "too_many_requests",
    500: "internal_error",
    502: "bad_gateway",
    503: "unavailable",
}


class ApiError(Exception):
    def __init__(self, status: int, error: str, message: str, headers: dict[str, str] | None = None,
                 **extra: Any) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.headers = headers or {}
        self.extra = extra

    def payload(self) -> dict[str, Any]:
        return {"error": self.error, "message": self.message, **self.extra}


def bad_request(message: str, error: str = "bad_request", **extra: Any) -> ApiError:
    return ApiError(400, error, message, **extra)


def unauthorized(message: str = "Log in or send an API key (Authorization: Bearer ehk_...).",
                 error: str = "unauthorized", **extra: Any) -> ApiError:
    return ApiError(401, error, message, headers={"WWW-Authenticate": "Bearer"}, **extra)


def forbidden(message: str = "You do not have access to this.", error: str = "forbidden", **extra: Any) -> ApiError:
    return ApiError(403, error, message, **extra)


def not_found(message: str, error: str = "not_found", **extra: Any) -> ApiError:
    return ApiError(404, error, message, **extra)


def conflict(message: str, error: str = "conflict", **extra: Any) -> ApiError:
    return ApiError(409, error, message, **extra)


def gone(message: str, error: str = "gone", **extra: Any) -> ApiError:
    return ApiError(410, error, message, **extra)


def too_large(message: str, **extra: Any) -> ApiError:
    return ApiError(413, "payload_too_large", message, **extra)


def unavailable(message: str, error: str = "unavailable", **extra: Any) -> ApiError:
    return ApiError(503, error, message, **extra)


def lease_released(project: str | None, session: str | None, kind: str, sid: str,
                   state: str = "released") -> ApiError:
    command = f"eks-harness lease acquire --project {project or '<owner/name>'} --session {session or '<session>'} --kind {kind}"
    return ApiError(
        410, "lease_released",
        f"Session id {sid} is no longer valid: its {kind} lease was {state}. Reacquire with: {command} "
        f"(or eks-harness lease resume {sid}).",
        project=project, session=session, reacquire=command, sid=sid, state=state,
    )


def error_response(status: int, error: str, message: str, headers: dict[str, str] | None = None,
                   **extra: Any) -> JSONResponse:
    return JSONResponse({"error": error, "message": message, **extra}, status_code=status, headers=headers)


def install_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(_: Request, exc: ApiError) -> JSONResponse:
        return JSONResponse(exc.payload(), status_code=exc.status, headers=exc.headers or None)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            payload = {"message": str(detail.get("message", "")), **detail}
        else:
            payload = {"error": STATUS_CODES.get(exc.status_code, "error"), "message": str(detail)}
        return JSONResponse(payload, status_code=exc.status_code, headers=getattr(exc, "headers", None))

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        problems = []
        for item in exc.errors():
            location = ".".join(str(part) for part in item.get("loc", ()) if part not in ("body", "query", "path"))
            problems.append({"field": location, "message": item.get("msg", ""), "type": item.get("type", "")})
        summary = "; ".join(f"{p['field'] or 'request'}: {p['message']}" for p in problems[:5])
        return JSONResponse({"error": "validation_failed", "message": summary or "Invalid request.",
                             "problems": problems}, status_code=422)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error on %s %s", request.method, request.url.path)
        return JSONResponse({"error": "internal_error", "message": f"{type(exc).__name__}: {exc}"}, status_code=500)
