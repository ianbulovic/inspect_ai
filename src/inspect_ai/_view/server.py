import os
import urllib.parse
from logging import getLogger
from pathlib import Path
from typing import Any

from aiohttp import web
from pydantic_core import to_jsonable_python

from inspect_ai._util.constants import DEFAULT_SERVER_HOST, DEFAULT_VIEW_PORT
from inspect_ai.log._file import (
    EvalLogInfo,
    eval_log_json,
    list_eval_logs,
    read_eval_log,
    read_eval_log_headers,
)

from .notify import view_last_eval_time

WWW_DIR = os.path.abspath((Path(__file__).parent / "www" / "dist").as_posix())


logger = getLogger(__name__)

# TODO: no samples!
# TODO: writing zip file using stream
# TODO: cache busting
# TODO: byte range


def view_server(
    log_dir: str,
    recursive: bool = True,
    host: str = DEFAULT_SERVER_HOST,
    port: int = DEFAULT_VIEW_PORT,
    fs_options: dict[str, Any] = {},
) -> None:
    routes = web.RouteTableDef()

    @routes.get("/api/logs/{log}")
    async def api_log(request: web.Request) -> web.Response:
        file = request.match_info["log"]
        file = urllib.parse.unquote(file)
        header_only = request.query.get("header-only", None) is not None
        return log_file_response(file, header_only)

    @routes.get("/api/logs")
    async def api_logs(_: web.Request) -> web.Response:
        logs = list_eval_logs(
            log_dir=log_dir, recursive=recursive, fs_options=fs_options
        )
        return log_listing_response(logs, log_dir)

    @routes.get("/api/log-headers")
    async def api_log_headers(request: web.Request) -> web.Response:
        files = request.query.getall("file", [])
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

    # setup app and routes
    app = web.Application()
    app.router.add_routes(routes)
    app.router.register_resource(WWWResource())
    # app.router.add_static(prefix="/", path=WWW_DIR, append_version=True)

    # run app
    web.run_app(app=app, host=host, port=port, print=print)


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


def log_file_response(file: str, header_only: bool) -> web.Response:
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


def log_headers_response(files: list[str]) -> web.Response:
    headers = read_eval_log_headers(files)
    return web.json_response(to_jsonable_python(headers, exclude_none=True))


class WWWResource(web.StaticResource):
    def __init__(self) -> None:
        super().__init__("", WWW_DIR, append_version=True)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        filename = request.match_info["filename"]
        if not filename:
            request.match_info["filename"] = "index.html"
        return await super()._handle(request)


def aliased_path(path: str) -> str:
    home_dir = os.path.expanduser("~")
    if path.startswith(home_dir):
        return path.replace(home_dir, "~", 1)
    else:
        return path
