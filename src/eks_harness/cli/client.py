from __future__ import annotations

import json
import mimetypes
import os
import re
from collections.abc import Callable, Iterator, Sequence
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx

from eks_harness import __version__
from eks_harness.cli import credentials as creds
from eks_harness.cli.ui import (
    EXIT_CONFLICT,
    EXIT_DAEMON_DOWN,
    EXIT_ERROR,
    EXIT_FORBIDDEN,
    EXIT_GONE,
    EXIT_NOT_FOUND,
    EXIT_UNAUTHORIZED,
    EXIT_USAGE,
)
from eks_harness.config import Config
from eks_harness.config import load as load_config
from eks_harness.paths import Paths, resolve_paths

URL_ENV = "EKS_HARNESS_URL"
USER_AGENT = f"eks-harness-cli/{__version__}"
CHUNK = 1024 * 256

ProgressCallback = Callable[[int, int | None], None]


class ApiClientError(Exception):
    exit_code = EXIT_ERROR

    def __init__(self, status: int, error: str, message: str, payload: dict | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.error = error
        self.message = message
        self.payload = payload or {}


class BadRequest(ApiClientError):
    exit_code = EXIT_USAGE


class NotAuthenticated(ApiClientError):
    exit_code = EXIT_UNAUTHORIZED


class Forbidden(ApiClientError):
    exit_code = EXIT_FORBIDDEN


class NotFound(ApiClientError):
    exit_code = EXIT_NOT_FOUND


class Conflict(ApiClientError):
    exit_code = EXIT_CONFLICT


class Gone(ApiClientError):
    exit_code = EXIT_GONE


class DaemonUnavailable(ApiClientError):
    exit_code = EXIT_DAEMON_DOWN


_BY_STATUS: dict[int, type[ApiClientError]] = {
    400: BadRequest,
    401: NotAuthenticated,
    403: Forbidden,
    404: NotFound,
    409: Conflict,
    410: Gone,
    413: BadRequest,
    422: BadRequest,
}


def error_from_response(response: httpx.Response) -> ApiClientError:
    payload: dict = {}
    try:
        data = response.json()
        if isinstance(data, dict):
            payload = data
    except ValueError:
        pass
    error = str(payload.get("error") or f"http_{response.status_code}")
    message = str(payload.get("message") or response.reason_phrase or f"HTTP {response.status_code}")
    if response.status_code == 401 and not payload.get("message"):
        message = "Not logged in: run 'eks-harness login' or set EKS_HARNESS_API_KEY."
    cls = _BY_STATUS.get(response.status_code, ApiClientError)
    if response.status_code >= 500:
        cls = ApiClientError
    return cls(response.status_code, error, message, payload)


def base_url_for(config: Config) -> str:
    override = os.environ.get(URL_ENV, "").strip()
    return (override or config.local_url()).rstrip("/")


class HarnessClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, *, paths: Paths | None = None,
                 config: Config | None = None, timeout: float = 30.0, transport: httpx.BaseTransport | None = None) -> None:
        self.paths = paths or (config.paths if config else resolve_paths())
        self.config = config or load_config(self.paths)
        self.base_url = (base_url or base_url_for(self.config)).rstrip("/")
        if api_key is None:
            api_key, self.key_source = creds.resolve_api_key(self.paths)
        else:
            self.key_source = "explicit"
        self.api_key = api_key
        self.timeout = timeout
        self._netloc = urlsplit(self.base_url).netloc
        self._client = httpx.Client(base_url=self.base_url, timeout=httpx.Timeout(timeout, connect=5.0),
                                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                                    follow_redirects=False, transport=transport,
                                    event_hooks={"request": [self._authorize]})

    def _authorize(self, request: httpx.Request) -> None:
        if self.api_key and request.url.netloc.decode() == self._netloc:
            request.headers["Authorization"] = f"Bearer {self.api_key}"
        else:
            request.headers.pop("Authorization", None)

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> "HarnessClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def url(self, path: str) -> str:
        return self.base_url + (path if path.startswith("/") else "/" + path)

    def _send(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.ConnectError as error:
            raise DaemonUnavailable(0, "daemon_unreachable",
                                    f"The eks-harness daemon is not reachable at {self.base_url}: start it with "
                                    f"'eks-harness daemon start'.") from error
        except httpx.TimeoutException as error:
            raise DaemonUnavailable(0, "timeout", f"The daemon at {self.base_url} did not answer in time.") from error
        except httpx.HTTPError as error:
            raise DaemonUnavailable(0, "transport_error", f"Request to {self.base_url} failed: {error}") from error
        if response.status_code >= 400:
            try:
                response.read()
            except httpx.HTTPError:
                pass
            raise error_from_response(response)
        return response

    def request(self, method: str, path: str, *, json: Any = None, params: dict | None = None,
                data: dict | None = None, files: Any = None, timeout: float | None = None) -> Any:
        kwargs: dict[str, Any] = {}
        if json is not None:
            kwargs["json"] = json
        if params:
            kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        if data is not None:
            kwargs["data"] = data
        if files is not None:
            kwargs["files"] = files
        if timeout is not None:
            kwargs["timeout"] = timeout
        response = self._send(method, path, **kwargs)
        if response.status_code == 204 or not response.content:
            return None
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            return response.json()
        return response.text

    def get(self, path: str, **kwargs: Any) -> Any:
        return self.request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> Any:
        return self.request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> Any:
        return self.request("PUT", path, **kwargs)

    def patch(self, path: str, **kwargs: Any) -> Any:
        return self.request("PATCH", path, **kwargs)

    def delete(self, path: str, **kwargs: Any) -> Any:
        return self.request("DELETE", path, **kwargs)

    def is_up(self, timeout: float = 2.0) -> bool:
        try:
            response = self._client.get("/api/health", timeout=timeout)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    @contextmanager
    def stream(self, method: str, path: str, *, params: dict | None = None, timeout: float | None = None,
               **kwargs: Any) -> Iterator[httpx.Response]:
        request_kwargs: dict[str, Any] = dict(kwargs)
        if params:
            request_kwargs["params"] = {k: v for k, v in params.items() if v is not None}
        request_kwargs["timeout"] = httpx.Timeout(timeout, connect=5.0) if timeout is not None else httpx.Timeout(None, connect=5.0)
        try:
            with self._client.stream(method, path, **request_kwargs) as response:
                if response.status_code >= 400:
                    response.read()
                    raise error_from_response(response)
                yield response
        except httpx.ConnectError as error:
            raise DaemonUnavailable(0, "daemon_unreachable",
                                    f"The eks-harness daemon is not reachable at {self.base_url}.") from error

    def download(self, path: str, dest: Path, *, params: dict | None = None,
                 progress: ProgressCallback | None = None, overwrite: bool = True) -> Path:
        dest = Path(dest)
        with self.stream("GET", path, params=params) as response:
            if dest.is_dir():
                dest = dest / filename_from_response(response, Path(urlsplit(str(response.url)).path).name or "download")
            if dest.exists() and not overwrite:
                raise FileExistsError(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            total = int(response.headers["content-length"]) if response.headers.get("content-length") else None
            tmp = dest.with_name(dest.name + ".part")
            done = 0
            try:
                with open(tmp, "wb") as handle:
                    for chunk in response.iter_bytes(CHUNK):
                        handle.write(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, total)
                os.replace(tmp, dest)
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
        return dest

    def upload(self, path: str, files: Sequence[Path | tuple[str, Path]], fields: dict[str, Any] | None = None, *,
               field_name: str = "file", progress: ProgressCallback | None = None,
               timeout: float | None = None) -> Any:
        data = {k: _form_value(v) for k, v in (fields or {}).items() if v is not None}
        entries: list[tuple[str, Path, str]] = []
        for item in files:
            if isinstance(item, tuple):
                name, file_path = item
            else:
                name, file_path = Path(item).name, Path(item)
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            entries.append((name, Path(file_path), mime))
        total = sum(p.stat().st_size for _, p, _ in entries)
        with ExitStack() as stack:
            multipart = []
            for name, file_path, mime in entries:
                handle = stack.enter_context(open(file_path, "rb"))
                if progress:
                    handle = _ProgressReader(handle, total, progress)
                multipart.append((field_name, (name, handle, mime)))
            return self.request("POST", path, data=data, files=multipart,
                                timeout=timeout if timeout is not None else max(self.timeout, 600.0))


class _ProgressReader:
    def __init__(self, handle, total: int, callback: ProgressCallback) -> None:
        self._handle = handle
        self._total = total
        self._callback = callback
        self._done = 0

    def read(self, size: int = -1) -> bytes:
        chunk = self._handle.read(size)
        self._done += len(chunk)
        self._callback(self._done, self._total)
        return chunk

    def __getattr__(self, name: str) -> Any:
        return getattr(self._handle, name)

    def __iter__(self):
        while True:
            chunk = self.read(CHUNK)
            if not chunk:
                return
            yield chunk


def _form_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (list, tuple)):
        return ",".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def filename_from_response(response: httpx.Response, fallback: str) -> str:
    disposition = response.headers.get("content-disposition", "")
    match = re.search(r"filename\*=UTF-8''([^;]+)", disposition, re.IGNORECASE)
    if match:
        return Path(unquote(match.group(1))).name or fallback
    match = re.search(r'filename="?([^";]+)"?', disposition, re.IGNORECASE)
    if match:
        return Path(match.group(1)).name or fallback
    return fallback


def client_from_args(args: Any) -> HarnessClient:
    paths = resolve_paths()
    config = load_config(paths)
    return HarnessClient(base_url=getattr(args, "url", None), api_key=getattr(args, "api_key", None),
                         paths=paths, config=config)
