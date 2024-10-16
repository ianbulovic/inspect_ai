import logging
import os
import urllib.parse
from logging import LogRecord, getLogger
from pathlib import Path
from typing import Any, Awaitable, Callable

from aiohttp import web
from pydantic_core import to_jsonable_python

from inspect_ai._display import display
from inspect_ai._util.constants import DEFAULT_SERVER_HOST, DEFAULT_VIEW_PORT
from inspect_ai._util.file import file, filesystem, size_in_mb
from inspect_ai.log._file import (
    EvalLogInfo,
    eval_log_json,
    list_eval_logs,
    read_eval_log,
    read_eval_log_headers,
)

from .notify import view_last_eval_time

logger = getLogger(__name__)


def view_server(
    log_dir: str,
    recursive: bool = True,
    host: str = DEFAULT_SERVER_HOST,
    port: int = DEFAULT_VIEW_PORT,
    authorization: str | None = None,
    fs_options: dict[str, Any] = {},
) -> None:
    # route table
    routes = web.RouteTableDef()

    # resolve log_dir to full path
    log_dir = filesystem(log_dir).info(log_dir).name

    # validate log file requests (must be in the log_dir
    # unless authorization has been provided)
    def validate_log_file_request(log_file: str) -> None:
        if not authorization and (not log_file.startswith(log_dir) or ".." in log_file):
            raise web.HTTPUnauthorized()

    @routes.get("/api/logs/{log}")
    async def api_log(request: web.Request) -> web.Response:
        # log file requested
        file = request.match_info["log"]
        file = urllib.parse.unquote(file)
        validate_log_file_request(file)

        # header_only is based on a size threshold
        header_only = request.query.get("header-only", None)
        return log_file_response(file, header_only)

    @routes.get("/api/log-size/{log}")
    async def api_log_size(request: web.Request) -> web.Response:
        # log file requested
        file = request.match_info["log"]
        file = urllib.parse.unquote(file)
        validate_log_file_request(file)

        return log_size_response(file)

    @routes.get("/api/log-bytes/{log}")
    async def api_log_bytes(request: web.Request) -> web.Response:
        # log file requested
        file = request.match_info["log"]
        file = urllib.parse.unquote(file)
        validate_log_file_request(file)

        # header_only is based on a size threshold
        start = request.query.get("start", None)
        if start is None:
            return web.HTTPBadRequest(reason="No 'start' query param.")
        end = request.query.get("end", None)
        if end is None:
            return web.HTTPBadRequest(reason="No 'end' query param")

        return log_bytes_response(file, int(start), int(end))

    @routes.get("/api/logs")
    async def api_logs(request: web.Request) -> web.Response:
        # log dir can optionally be overridden by the request
        if authorization:
            request_log_dir = request.query.getone("log_dir", None)
            if request_log_dir:
                request_log_dir = urllib.parse.unquote(request_log_dir)
            else:
                request_log_dir = log_dir
        else:
            request_log_dir = log_dir

        # list logs
        logs = list_eval_logs(
            log_dir=request_log_dir, recursive=recursive, fs_options=fs_options
        )
        return log_listing_response(logs, request_log_dir)

    @routes.get("/api/log-headers")
    async def api_log_headers(request: web.Request) -> web.Response:
        files = request.query.getall("file", [])
        files = [urllib.parse.unquote(file) for file in files]
        map(validate_log_file_request, files)
        return log_headers_response(files)

    @routes.get("/api/events")
    async def api_events(request: web.Request) -> web.Response:
        last_eval_time = request.query.get("last_eval_time", None)
        actions = (
            ["refresh-evals"]
            if last_eval_time and view_last_eval_time() > int(last_eval_time)
            else []
        )
        return web.json_response(actions)

    # optional auth middleware
    @web.middleware
    async def authorize(
        request: web.Request,
        handler: Callable[[web.Request], Awaitable[web.StreamResponse]],
    ) -> web.StreamResponse:
        if request.headers.get("Authorization", None) != authorization:
            return web.HTTPUnauthorized()
        else:
            return await handler(request)

    # setup server
    app = web.Application(middlewares=[authorize] if authorization else [])
    app.router.add_routes(routes)
    app.router.register_resource(WWWResource())

    # filter request log (remove /api/events)
    filter_aiohttp_log()

    # run app
    display().print(f"Inspect View: {log_dir}")
    web.run_app(
        app=app, host=host, port=port, print=display().print, shutdown_timeout=1
    )


def log_listing_response(logs: list[EvalLogInfo], log_dir: str) -> web.Response:
    response = dict(
        log_dir=aliased_path(log_dir),
        files=[
            dict(
                name=log.name,
                size=log.size,
                mtime=log.mtime,
                task=log.task,
                task_id=log.task_id,
            )
            for log in logs
        ],
    )
    return web.json_response(response)


def log_file_response(file: str, header_only_param: str | None) -> web.Response:
    # resolve header_only
    header_only_mb = int(header_only_param) if header_only_param is not None else None
    header_only = resolve_header_only(file, header_only_mb)

    try:
        contents: bytes | None = None
        if header_only:
            try:
                log = read_eval_log(file, header_only=True)
                contents = eval_log_json(log).encode()
            except ValueError as ex:
                logger.info(
                    f"Unable to read headers from log file {file}: {ex}. "
                    + "The file may include a NaN or Inf value. Falling back to reading entire file."
                )

        if contents is None:  # normal read
            log = read_eval_log(file, header_only=False)
            contents = eval_log_json(log).encode()

        return web.Response(body=contents, content_type="application/json")

    except Exception as error:
        logger.exception(error)
        return web.Response(status=500, reason="File not found")


def log_size_response(log_file: str) -> web.Response:
    info = filesystem(log_file).info(log_file)
    return web.json_response(info.size)


def log_bytes_response(log_file: str, start: int, end: int) -> web.Response:
    with file(log_file, "rb") as f:
        # read the bytes
        f.seek(start)
        bytes = f.read(end - start + 1)

    # build headers
    content_length = end - start + 1
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(content_length),
    }

    # return response
    return web.Response(status=200, body=bytes, headers=headers)


def log_headers_response(files: list[str]) -> web.Response:
    headers = read_eval_log_headers(files)
    return web.json_response(to_jsonable_python(headers, exclude_none=True))


class WWWResource(web.StaticResource):
    def __init__(self) -> None:
        super().__init__(
            "", os.path.abspath((Path(__file__).parent / "www" / "dist").as_posix())
        )

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        # serve /index.html for /
        filename = request.match_info["filename"]
        if not filename:
            request.match_info["filename"] = "index.html"

        # call super
        response = await super()._handle(request)

        # disable caching as this is only ever served locally
        # and w/ caching sometimes we get stale assets
        response.headers.update(
            {
                "Expires": "Fri, 01 Jan 1990 00:00:00 GMT",
                "Pragma": "no-cache",
                "Cache-Control": "no-cache, no-store, max-age=0, must-revalidate",
            }
        )

        # return response
        return response


def aliased_path(path: str) -> str:
    home_dir = os.path.expanduser("~")
    if path.startswith(home_dir):
        return path.replace(home_dir, "~", 1)
    else:
        return path


def resolve_header_only(path: str, header_only: int | None) -> bool:
    # if there is a max_size passed, respect that and switch to
    # header_only mode if the file is too large
    if header_only == 0:
        return True
    if header_only is not None and size_in_mb(path) > int(header_only):
        return True
    else:
        return False


def filter_aiohttp_log() -> None:
    #  filter overly chatty /api/events messages
    class RequestFilter(logging.Filter):
        def filter(self, record: LogRecord) -> bool:
            return "/api/events" not in record.getMessage()

    # don't add if we already have
    access_logger = getLogger("aiohttp.access")
    for existing_filter in access_logger.filters:
        if isinstance(existing_filter, RequestFilter):
            return

    # add the filter
    access_logger.addFilter(RequestFilter())
