"""OpenAI-shaped errors (SPEC 2.3): {"error": {"message", "type", "code"}}."""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

log = logging.getLogger(__name__)


class ForgeError(Exception):
    """An error with a known HTTP status and OpenAI-style type.

    `type` and `code` carry the same machine-readable name unless a more specific code is given,
    so clients that check either field see e.g. "queue_full".
    """

    def __init__(
        self,
        status: int,
        type: str,
        message: str,
        *,
        code: str | None = None,
        retry_after: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.type = type
        self.message = message
        self.code = code or type
        self.retry_after = retry_after

    def to_body(self) -> dict:
        return {"error": {"message": self.message, "type": self.type, "code": self.code}}

    def headers(self) -> dict[str, str]:
        return {"Retry-After": str(max(1, self.retry_after))} if self.retry_after is not None else {}


def invalid_request(message: str, code: str | None = None) -> ForgeError:
    return ForgeError(400, "invalid_request_error", message, code=code)


def error_response(err: ForgeError) -> JSONResponse:
    return JSONResponse(err.to_body(), status_code=err.status, headers=err.headers())


_STATUS_TYPES = {
    400: "invalid_request_error",
    401: "invalid_api_key",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
}


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ForgeError)
    async def _forge_error(_: Request, exc: ForgeError) -> JSONResponse:
        return error_response(exc)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        first = exc.errors()[0] if exc.errors() else {}
        where = ".".join(str(p) for p in first.get("loc", ()) if p != "body")
        message = f"{where}: {first.get('msg', 'invalid request')}" if where else "Invalid request body"
        return error_response(invalid_request(message))

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        err = ForgeError(exc.status_code, _STATUS_TYPES.get(exc.status_code, "error"), str(exc.detail))
        response = error_response(err)
        for name, value in (exc.headers or {}).items():
            response.headers[name] = value
        return response

    @app.exception_handler(Exception)
    async def _unexpected(_: Request, exc: Exception) -> JSONResponse:
        log.exception("unhandled error", exc_info=exc)
        return error_response(ForgeError(500, "server_error", "Internal server error"))
