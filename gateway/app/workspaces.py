"""Workspaces: server-side project folders for the web console's editor (SPEC 2.10).

A workspace is a directory under FORGE_WORKSPACE_ROOT, either empty (`git init -b main`) or a
shallow clone of a repository. The editor lists, reads and writes files, downloads a ZIP, and
commits and pushes with git. Admin session only (require_admin also checks the CSRF header).

Every client path goes through `_resolve`, which keeps it inside the workspace, away from `.git`
and never through a symlink. A push token is handed to git only through environment variables,
so it never appears in a process list, a log line or an error message.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
import shutil
import stat
import tempfile
import zipfile
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.auth import require_admin
from app.config import Settings
from app.errors import ForgeError, invalid_request
from app.rag.indexer import RepoUrlError, UrlPolicy, _git_env, validate_branch, validate_repo_url
from app.rag.router import get_url_policy

router = APIRouter(prefix="/api/workspaces", tags=["workspaces"], dependencies=[Depends(require_admin)])

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
GIT_TIMEOUT_S = 120
MAX_TREE_ENTRIES = 5000
MAX_DIFF_BYTES = 200 * 1024
MAX_ERROR_CHARS = 1000
ZIP_SPOOL_BYTES = 16 * 1024 * 1024
# Same hardening as the RAG clone: https only (also for redirects and submodules), symlinks
# checked out as plain files, and no hooks, so a cloned repo can't run code in the gateway.
_GIT_SAFE_CONFIG = [
    "-c",
    "protocol.allow=never",
    "-c",
    "protocol.https.allow=always",
    "-c",
    "http.followRedirects=false",
    "-c",
    "core.symlinks=false",
    "-c",
    "core.hooksPath=" + os.devnull,
]

# One lock per workspace for git operations and deletes, so two requests don't race on .git.
_locks: dict[str, asyncio.Lock] = {}


def _lock(name: str) -> asyncio.Lock:
    return _locks.setdefault(name, asyncio.Lock())


# --------------------------------------------------------------------------- helpers


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _root(request: Request) -> Path:
    root = Path(_settings(request).workspace_root)
    root.mkdir(parents=True, exist_ok=True)
    return root.resolve()


def _not_found(what: str) -> ForgeError:
    return ForgeError(404, "not_found", f"{what} not found")


def _conflict(message: str, code: str = "conflict") -> ForgeError:
    return ForgeError(409, "conflict", message, code=code)


def _invalid_path(message: str = "Invalid path") -> ForgeError:
    return invalid_request(message, code="invalid_path")


def _check_name(name: str) -> str:
    if not _NAME_RE.match(name):
        raise invalid_request("Workspace name must be 1-64 characters: a-z, 0-9, '.', '_' or '-'.")
    return name


def _workspace_dir(request: Request, name: str) -> Path:
    path = _root(request) / _check_name(name)
    if not path.is_dir():
        raise _not_found(f"Workspace '{name}'")
    return path


def _resolve(workspace: Path, rel: str) -> Path:
    """The absolute path of `rel` inside `workspace`, or 400 invalid_path."""
    if not rel or "\\" in rel or "\x00" in rel or rel.startswith("/"):
        raise _invalid_path()
    parts = PurePosixPath(rel).parts
    if not parts or any(p in ("", ".", "..") or p.lower() == ".git" for p in parts):
        raise _invalid_path()
    current = workspace
    for part in parts:
        current = current / part
        if current.is_symlink():
            raise _invalid_path("Symlinks can't be opened in a workspace")
    resolved = current.resolve()
    if resolved == workspace or workspace not in resolved.parents:
        raise _invalid_path()
    return current


def _remove_tree(path: Path) -> None:
    # git marks pack files read-only; on Windows rmtree needs them made writable first.
    def make_writable_and_retry(func: Callable[[str], Any], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)


def _walk(workspace: Path) -> Iterator[tuple[Path, os.stat_result]]:
    """Every file and directory below `workspace`, skipping .git and symlinks."""
    for dirpath, dirnames, filenames in os.walk(workspace):
        dirnames[:] = sorted(d for d in dirnames if d != ".git" and not os.path.islink(os.path.join(dirpath, d)))
        for entry in [*dirnames, *sorted(filenames)]:
            full = Path(dirpath) / entry
            if full.is_symlink():
                continue
            yield full, full.stat()


def _dir_size(path: Path) -> int:
    return sum(st.st_size for _, st in _walk(path) if stat.S_ISREG(st.st_mode))


# --------------------------------------------------------------------------- git


class GitError(RuntimeError):
    pass


async def _git(
    workspace: Path, args: list[str], *, extra_env: dict[str, str] | None = None, secrets: tuple[str, ...] = ()
) -> str:
    env = _git_env(workspace.parent)
    env.update(extra_env or {})
    proc = await asyncio.create_subprocess_exec(
        "git",
        *_GIT_SAFE_CONFIG,
        *args,
        cwd=workspace,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=GIT_TIMEOUT_S)
    except TimeoutError:
        raise GitError(f"git timed out after {GIT_TIMEOUT_S} s") from None
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip() or stdout.decode("utf-8", "replace").strip()
        raise GitError(_scrub(message or f"exit code {proc.returncode}", secrets)[-MAX_ERROR_CHARS:])
    return stdout.decode("utf-8", "replace")


def _scrub(text: str, secrets: tuple[str, ...]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "***")
    return text


async def _git_info(workspace: Path) -> dict[str, str | None]:
    if not (workspace / ".git").exists():
        return {"remote": None, "branch": None}
    try:
        branch = (await _git(workspace, ["symbolic-ref", "--short", "-q", "HEAD"])).strip() or None
    except GitError:
        branch = None
    try:
        remote = (await _git(workspace, ["remote", "get-url", "origin"])).strip() or None
    except GitError:
        remote = None
    return {"remote": remote, "branch": branch}


async def _workspace_out(path: Path) -> dict[str, Any]:
    created = datetime.fromtimestamp(os.stat(path).st_mtime, UTC)
    return {"name": path.name, "created_at": created.isoformat(), "git": await _git_info(path)}


def _check_url(url: str, policy: UrlPolicy) -> str:
    try:
        return validate_repo_url(url, policy)
    except RepoUrlError as exc:
        raise invalid_request(str(exc), code="invalid_url") from exc


# --------------------------------------------------------------------------- workspaces


class WorkspaceCreate(BaseModel):
    name: str = Field(max_length=64)
    git_url: str | None = Field(default=None, max_length=2000)
    branch: str | None = Field(default=None, max_length=200)


@router.get("")
async def list_workspaces(request: Request) -> list[dict[str, Any]]:
    root = _root(request)
    dirs = sorted(p for p in root.iterdir() if p.is_dir() and _NAME_RE.match(p.name))
    return [await _workspace_out(p) for p in dirs]


@router.post("", status_code=201)
async def create_workspace(
    request: Request, body: WorkspaceCreate, policy: UrlPolicy = Depends(get_url_policy)
) -> dict[str, Any]:
    name = _check_name(body.name)
    path = _root(request) / name
    url = _check_url(body.git_url, policy) if body.git_url and body.git_url.strip() else None
    try:
        branch = validate_branch(body.branch)
    except RepoUrlError as exc:
        raise invalid_request(str(exc)) from exc
    async with _lock(name):
        if path.exists():
            raise _conflict(f"Workspace '{name}' already exists")
        if url is None:
            path.mkdir()
            await _git(path, ["init", "-q", "-b", branch or "main"])
            return await _workspace_out(path)
        args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--template="]
        if branch:
            args += ["--branch", branch]
        try:
            if policy.allow_file:
                args = ["-c", "protocol.file.allow=always", *args]
            await _git(path.parent, [*args, "--", url, name])
        except GitError as exc:
            if path.exists():
                await asyncio.to_thread(_remove_tree, path)
            raise ForgeError(400, "invalid_request_error", f"git clone failed: {exc}", code="clone_failed") from exc
        limit = _settings(request).workspace_max_bytes
        if await asyncio.to_thread(_dir_size, path) > limit:
            await asyncio.to_thread(_remove_tree, path)
            raise ForgeError(413, "invalid_request_error", "Repository is too large", code="workspace_too_large")
        return await _workspace_out(path)


@router.delete("/{name}", status_code=204)
async def delete_workspace(request: Request, name: str) -> Response:
    path = _workspace_dir(request, name)
    async with _lock(name):
        await asyncio.to_thread(_remove_tree, path)
    return Response(status_code=204)


# --------------------------------------------------------------------------- files


class FileWrite(BaseModel):
    path: str = Field(max_length=1000)
    content: str


class PathBody(BaseModel):
    path: str = Field(max_length=1000)


class MoveBody(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source: str = Field(alias="from", max_length=1000)
    to: str = Field(max_length=1000)


def _tree(workspace: Path) -> dict[str, Any]:
    entries: list[dict[str, Any]] = []
    truncated = False
    for full, st in _walk(workspace):
        if len(entries) >= MAX_TREE_ENTRIES:
            truncated = True
            break
        is_dir = stat.S_ISDIR(st.st_mode)
        entries.append(
            {
                "path": full.relative_to(workspace).as_posix(),
                "type": "dir" if is_dir else "file",
                "size": 0 if is_dir else st.st_size,
            }
        )
    entries.sort(key=lambda e: e["path"])
    return {"entries": entries, "truncated": truncated}


@router.get("/{name}/tree")
async def get_tree(request: Request, name: str) -> dict[str, Any]:
    return await asyncio.to_thread(_tree, _workspace_dir(request, name))


@router.get("/{name}/file")
async def read_file(request: Request, name: str, path: Annotated[str, Query(max_length=1000)]) -> dict[str, Any]:
    target = _resolve(_workspace_dir(request, name), path)
    if not target.is_file():
        raise _not_found(f"File '{path}'")
    size = target.stat().st_size
    if size > _settings(request).workspace_max_file_bytes:
        raise ForgeError(413, "invalid_request_error", "File is too large to open in the editor", code="file_too_large")
    data = target.read_bytes()
    try:
        if b"\x00" in data:
            raise UnicodeDecodeError("utf-8", data, 0, 1, "NUL byte")
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        raise ForgeError(415, "invalid_request_error", "Binary files can't be edited", code="binary_file") from None
    return {"path": path, "content": content, "size": size}


@router.put("/{name}/file")
async def write_file(request: Request, name: str, body: FileWrite) -> dict[str, Any]:
    workspace = _workspace_dir(request, name)
    target = _resolve(workspace, body.path)
    data = body.content.encode("utf-8")
    if len(data) > _settings(request).workspace_max_file_bytes:
        raise ForgeError(413, "invalid_request_error", "File is too large", code="file_too_large")
    if target.is_dir():
        raise _conflict(f"'{body.path}' is a folder")
    target.parent.mkdir(parents=True, exist_ok=True)
    # Write a temp file next to the target and rename it over, so readers never see half a file.
    fd, tmp = tempfile.mkstemp(dir=target.parent, prefix=".forge-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise
    return {"path": body.path, "size": len(data)}


@router.post("/{name}/folder", status_code=201)
async def create_folder(request: Request, name: str, body: PathBody) -> dict[str, str]:
    target = _resolve(_workspace_dir(request, name), body.path)
    if target.exists():
        raise _conflict(f"'{body.path}' already exists")
    target.mkdir(parents=True)
    return {"path": body.path}


@router.post("/{name}/move")
async def move_entry(request: Request, name: str, body: MoveBody) -> dict[str, str]:
    workspace = _workspace_dir(request, name)
    source = _resolve(workspace, body.source)
    target = _resolve(workspace, body.to)
    if not source.exists():
        raise _not_found(f"'{body.source}'")
    if target.exists():
        raise _conflict(f"'{body.to}' already exists")
    if source.is_dir() and (target == source or source in target.parents):
        raise _invalid_path("A folder can't be moved into itself")
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return {"path": body.to}


@router.delete("/{name}/entry", status_code=204)
async def delete_entry(request: Request, name: str, path: Annotated[str, Query(max_length=1000)]) -> Response:
    target = _resolve(_workspace_dir(request, name), path)
    if not target.exists():
        raise _not_found(f"'{path}'")
    if target.is_dir():
        await asyncio.to_thread(_remove_tree, target)
    else:
        target.unlink()
    return Response(status_code=204)


def _build_zip(workspace: Path) -> tempfile.SpooledTemporaryFile:
    spool = tempfile.SpooledTemporaryFile(max_size=ZIP_SPOOL_BYTES)  # noqa: SIM115 - closed by the response
    with zipfile.ZipFile(spool, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for full, st in _walk(workspace):
            if stat.S_ISREG(st.st_mode):
                archive.write(full, f"{workspace.name}/{full.relative_to(workspace).as_posix()}")
    spool.seek(0)
    return spool


@router.get("/{name}/archive.zip")
async def download_zip(request: Request, name: str) -> StreamingResponse:
    workspace = _workspace_dir(request, name)
    spool = await asyncio.to_thread(_build_zip, workspace)

    def chunks() -> Iterator[bytes]:
        try:
            while block := spool.read(64 * 1024):
                yield block
        finally:
            spool.close()

    return StreamingResponse(
        chunks(),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'},
    )


# --------------------------------------------------------------------------- git routes


class RemoteBody(BaseModel):
    url: str = Field(min_length=1, max_length=2000)


class CommitBody(BaseModel):
    message: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)]
    author_name: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=100)]
    author_email: Annotated[str, StringConstraints(strip_whitespace=True, min_length=3, max_length=200)]


class PushBody(BaseModel):
    token: str | None = Field(default=None, max_length=1000)


def _git_failed(code: str, exc: GitError) -> ForgeError:
    return ForgeError(400, "invalid_request_error", f"git failed: {exc}", code=code)


def _require_repo(workspace: Path) -> None:
    if not (workspace / ".git").exists():
        raise _conflict("This workspace is not a git repository", code="not_a_repo")


async def _has_head(workspace: Path) -> bool:
    try:
        await _git(workspace, ["rev-parse", "--verify", "-q", "HEAD"])
    except GitError:
        return False
    return True


@router.get("/{name}/git")
async def git_status(request: Request, name: str) -> dict[str, Any]:
    workspace = _workspace_dir(request, name)
    _require_repo(workspace)
    info = await _git_info(workspace)
    head = None
    if await _has_head(workspace):
        head = (await _git(workspace, ["rev-parse", "--short", "HEAD"])).strip()
    raw = await _git(workspace, ["status", "--porcelain=v1", "-z", "--untracked-files=all"])
    changes: list[dict[str, str]] = []
    items = raw.split("\x00")
    i = 0
    while i < len(items):
        item = items[i]
        i += 1
        if len(item) < 4:
            continue
        code, path = item[:2], item[3:]
        if code[0] in "RC":
            i += 1  # a rename is followed by its source path
        changes.append({"path": path, "status": code.strip()})
    return {**info, "head": head, "changes": changes}


@router.get("/{name}/git/diff")
async def git_diff(request: Request, name: str) -> dict[str, Any]:
    workspace = _workspace_dir(request, name)
    _require_repo(workspace)
    # Stage everything into a throw-away index, so untracked files show up as additions
    # without touching the real index.
    fd, index = tempfile.mkstemp(prefix="forge-index-")
    os.close(fd)
    os.unlink(index)
    try:
        real_index = workspace / ".git" / "index"
        if real_index.exists():
            shutil.copyfile(real_index, index)
        env = {"GIT_INDEX_FILE": index}
        await _git(workspace, ["add", "-A"], extra_env=env)
        base = (
            "HEAD"
            if await _has_head(workspace)
            else (await _git(workspace, ["hash-object", "-t", "tree", "--stdin"])).strip()
        )
        diff = await _git(workspace, ["diff", "--cached", "--no-color", base], extra_env=env)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(index)
    data = diff.encode("utf-8")
    truncated = len(data) > MAX_DIFF_BYTES
    if truncated:
        diff = data[:MAX_DIFF_BYTES].decode("utf-8", "ignore")
    return {"diff": diff, "truncated": truncated}


@router.post("/{name}/git/remote")
async def set_remote(
    request: Request, name: str, body: RemoteBody, policy: UrlPolicy = Depends(get_url_policy)
) -> dict[str, str]:
    workspace = _workspace_dir(request, name)
    _require_repo(workspace)
    url = _check_url(body.url, policy)
    async with _lock(name):
        with contextlib.suppress(GitError):
            await _git(workspace, ["remote", "remove", "origin"])
        await _git(workspace, ["remote", "add", "origin", url])
    return {"remote": url}


@router.post("/{name}/git/commit")
async def git_commit(request: Request, name: str, body: CommitBody) -> dict[str, str]:
    workspace = _workspace_dir(request, name)
    _require_repo(workspace)
    async with _lock(name):
        await _git(workspace, ["add", "-A"])
        if not (await _git(workspace, ["status", "--porcelain"])).strip():
            raise _conflict("Nothing to commit", code="nothing_to_commit")
        try:
            await _git(
                workspace,
                [
                    *("-c", f"user.name={body.author_name}", "-c", f"user.email={body.author_email}"),
                    *("commit", "-q", "--no-verify", "-m", body.message),
                ],
            )
        except GitError as exc:
            raise _git_failed("commit_failed", exc) from exc
        sha = (await _git(workspace, ["rev-parse", "--short", "HEAD"])).strip()
    return {"sha": sha, "summary": body.message.splitlines()[0]}


@router.post("/{name}/git/push")
async def git_push(
    request: Request, name: str, body: PushBody, policy: UrlPolicy = Depends(get_url_policy)
) -> dict[str, Any]:
    workspace = _workspace_dir(request, name)
    _require_repo(workspace)
    info = await _git_info(workspace)
    if not info["remote"]:
        raise _conflict("Set a remote before pushing", code="no_remote")
    if not info["branch"]:
        raise _conflict("HEAD is detached; check out a branch first", code="detached_head")
    _check_url(info["remote"], policy)  # the remote must still be allowed at push time
    env: dict[str, str] = {}
    secrets: tuple[str, ...] = ()
    if body.token:
        basic = base64.b64encode(f"x-access-token:{body.token}".encode()).decode()
        env = {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": "http.extraHeader",
            "GIT_CONFIG_VALUE_0": f"Authorization: Basic {basic}",
        }
        secrets = (body.token, basic)
    extra = ["-c", "protocol.file.allow=always"] if policy.allow_file else []
    async with _lock(name):
        try:
            output = await _git(
                workspace,
                [*extra, "push", "--porcelain", "-u", "origin", f"HEAD:refs/heads/{info['branch']}"],
                extra_env=env,
                secrets=secrets,
            )
        except GitError as exc:
            raise _git_failed("push_failed", exc) from exc
    return {"pushed": True, "branch": info["branch"], "output": _scrub(output.strip(), secrets)[-MAX_ERROR_CHARS:]}
