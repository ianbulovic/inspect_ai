import functools
import os
from json import dumps
from typing import Any, Callable, Literal, cast
from urllib.parse import urlparse

import click
from fsspec.core import split_protocol  # type: ignore
from pydantic_core import to_jsonable_python
from typing_extensions import Unpack

from inspect_ai._cli.common import CommonOptions, common_options, process_common_options
from inspect_ai._display import display
from inspect_ai._util.constants import PKG_PATH
from inspect_ai._util.error import PrerequisiteError
from inspect_ai._util.file import copy_file, exists, filesystem
from inspect_ai._view.server import resolve_header_only
from inspect_ai.log import list_eval_logs
from inspect_ai.log._file import (
    eval_log_json,
    log_files_from_ls,
    read_eval_log,
    read_eval_log_headers,
    write_eval_log,
)


@click.group("log")
def log_command() -> None:
    """Query, read, and convert logs.

    Inspect supports two log formats: 'eval' which is a compact, high performance binary format and 'json' which represents logs as JSON.

    The default format is 'eval'. You can change this by setting the INSPECT_LOG_FORMAT environment variable or using the --log-format command line option.

    The 'log' commands enable you to read Inspect logs uniformly as JSON no matter their physical storage format, and also enable you to read only the headers (everything but the samples) from log files, which is useful for very large logs.
    """
    return None


def list_logs_options(func: Callable[..., Any]) -> Callable[..., click.Context]:
    @click.option(
        "--status",
        type=click.Choice(
            ["started", "success", "cancelled", "error"], case_sensitive=False
        ),
        help="List only log files with the indicated status.",
    )
    @click.option(
        "--absolute",
        type=bool,
        is_flag=True,
        default=False,
        help="List absolute paths to log files (defaults to relative to the cwd).",
    )
    @click.option(
        "--json",
        type=bool,
        is_flag=True,
        default=False,
        help="Output listing as JSON",
    )
    @click.option(
        "--no-recursive",
        type=bool,
        is_flag=True,
        help="List log files recursively (defaults to True).",
    )
    @common_options
    @functools.wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> click.Context:
        return cast(click.Context, func(*args, **kwargs))

    return wrapper


def log_list(
    status: Literal["started", "success", "cancelled", "error"] | None,
    absolute: bool,
    json: bool,
    no_recursive: bool | None,
    **common: Unpack[CommonOptions],
) -> None:
    process_common_options(common)

    # list the logs
    logs = list_eval_logs(
        log_dir=common["log_dir"],
        filter=(lambda log: log.status == status) if status else None,
        recursive=no_recursive is not True,
    )

    # convert file names
    for log in logs:
        if urlparse(log.name).scheme == "file":
            _, path = split_protocol(log.name)
            log.name = path
            if not absolute:
                log.name = os.path.relpath(log.name, os.path.curdir)

    if json:
        logs_dicts = [log.model_dump() for log in logs]
        print(dumps(logs_dicts, indent=2))

    else:
        for log in logs:
            print(log.name)


@log_command.command("list")
@list_logs_options
def list_command(
    status: Literal["started", "success", "cancelled", "error"] | None,
    absolute: bool,
    json: bool,
    no_recursive: bool | None,
    **common: Unpack[CommonOptions],
) -> None:
    """List all logs in the log directory."""
    log_list(status, absolute, json, no_recursive, **common)


@log_command.command("dump")
@click.argument("path")
@click.option(
    "--header-only",
    type=int,
    is_flag=False,
    flag_value=0,
    help="Read and print only the header of the log file (i.e. no samples).",
)
def dump_command(path: str, header_only: int) -> None:
    """Print log file contents as JSON."""
    dump(path, header_only)


def dump(path: str, header_only: int) -> None:
    """Print log file contents as JSON."""
    # Resolve the header only to a boolean
    header_only = resolve_header_only(path, header_only)

    log = read_eval_log(path, header_only=header_only)
    print(eval_log_json(log))


@log_command.command("convert")
@click.argument("path")
@click.option(
    "--to",
    type=click.Choice(["eval", "json"], case_sensitive=False),
    required=True,
    help="Target format to convert to.",
)
@click.option(
    "--output-dir",
    required=True,
    help="Directory to write converted log files to.",
)
@click.option(
    "--overwrite",
    type=bool,
    is_flag=True,
    default=False,
    help="Overwrite files in the output directory.",
)
def convert_command(
    path: str, to: Literal["eval", "json"], output_dir: str, overwrite: bool
) -> None:
    """Convert between log file formats."""
    # confirm that path exists
    fs = filesystem(path)
    if not fs.exists(path):
        raise PrerequisiteError(f"Error: path '{path}' does not exist.")

    # normalise output dir and ensure it exists
    if output_dir.endswith(fs.sep):
        output_dir = output_dir[:-1]
    fs.mkdir(output_dir, exist_ok=True)

    # convert a single file (input file is relative to the 'path')
    def convert_file(input_file: str) -> None:
        # compute input and ensure output dir exists
        input_name, _ = os.path.splitext(input_file)
        input_dir = os.path.dirname(input_name.replace("\\", "/"))
        target_dir = f"{output_dir}{fs.sep}{input_dir}"
        output_fs = filesystem(target_dir)
        output_fs.mkdir(target_dir, exist_ok=True)

        # compute file input file based on path
        input_file = f"{path}{fs.sep}{input_file}"

        # compute full output file and enforce overwrite
        output_file = f"{output_dir}{fs.sep}{input_name}.{to}"
        if exists(output_file) and not overwrite:
            raise FileExistsError(
                "Output file {output_file} already exists (use --overwrite to overwrite existing files)"
            )

        # if the input and output files have the same format just copy
        if input_file.endswith(f".{to}"):
            copy_file(input_file, output_file)

        # otherwise do a full read/write
        else:
            log = read_eval_log(input_file)
            write_eval_log(log, output_file)

    if fs.info(path).type == "file":
        convert_file(path)
    else:
        root_dir = fs.info(path).name
        eval_logs = log_files_from_ls(fs.ls(path, recursive=True), None, True)
        input_files = [
            eval_log.name.replace(f"{root_dir}/", "", 1) for eval_log in eval_logs
        ]
        display().print("Converting log files...")
        with display().progress(total=len(input_files)) as p:
            for input_file in input_files:
                convert_file(input_file)
                p.update()


@log_command.command("headers")
@click.argument("files", nargs=-1)
def headers_command(files: tuple[str]) -> None:
    """Print log file headers as JSON."""
    headers(files)


def headers(files: tuple[str]) -> None:
    """Print log file headers as JSON."""
    headers = read_eval_log_headers(list(files))
    print(dumps(to_jsonable_python(headers, exclude_none=True), indent=2))


@log_command.command("schema")
def schema_command() -> None:
    """Print JSON schema for log files."""
    schema()


def schema() -> None:
    print(view_resource("log-schema.json"))


@log_command.command("types")
def types_command() -> None:
    """Print TS declarations for log files."""
    types()


def types() -> None:
    print(view_type_resource("log.d.ts"))


def view_resource(file: str) -> str:
    resource = PKG_PATH / "_view" / "www" / file
    with open(resource, "r", encoding="utf-8") as f:
        return f.read()


def view_type_resource(file: str) -> str:
    resource = PKG_PATH / "_view" / "www" / "src" / "types" / file
    with open(resource, "r", encoding="utf-8") as f:
        return f.read()
