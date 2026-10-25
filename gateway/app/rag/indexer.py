"""Clone a public repository, split its source files into chunks and store their embeddings.

Indexing runs as a background asyncio task. Within one gateway an in-process queue runs one job
at a time; across gateway replicas (and the dev/prod gateways, which share Redis) the Redis lock
`forge:rag:lock` makes sure only one clone-and-embed job hits the GPU at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
import stat
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import delete, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import Principal
from app.llm import LLM
from app.rag.models import EMBEDDING_DIMENSIONS, Repo, RepoChunk
from app.scheduler import Priority

log = logging.getLogger(__name__)

CLONE_TIMEOUT_S = 120
CHUNK_LINES = 60
CHUNK_OVERLAP = 10
EMBED_BATCH = 32
MAX_FILE_BYTES = 200 * 1024
MAX_FILES = 3000
MAX_TOTAL_BYTES = 30 * 1024 * 1024
# The embedding model's context is limited; a chunk of 60 very long lines (generated code,
# data tables) is cut so the embed request doesn't fail. The line range is still recorded.
MAX_CHUNK_CHARS = 6000
MAX_ERROR_CHARS = 1000

LOCK_NAME = "forge:rag:lock"
LOCK_TTL_S = 60

# fmt: off
SKIP_DIRS = frozenset(
    {
        ".git", "node_modules", "vendor", "dist", "build", ".venv", "venv",
        "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", ".next",
    }
)

LOCK_FILES = frozenset(
    {
        "package-lock.json", "npm-shrinkwrap.json", "yarn.lock", "pnpm-lock.yaml", "bun.lockb",
        "poetry.lock", "pipfile.lock", "uv.lock", "pdm.lock", "cargo.lock", "gemfile.lock",
        "composer.lock", "go.sum", "flake.lock", "mix.lock", "pubspec.lock", "podfile.lock",
        "packages.lock.json", "gradle.lockfile",
    }
)

TEXT_EXTENSIONS = frozenset(
    {
        # code
        ".py", ".pyi", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".go", ".rs",
        ".java", ".kt", ".kts", ".scala", ".groovy", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp",
        ".hh", ".cs", ".fs", ".fsx", ".rb", ".php", ".swift", ".m", ".mm", ".lua", ".pl", ".pm",
        ".r", ".jl", ".dart", ".ex", ".exs", ".erl", ".hrl", ".clj", ".cljs", ".hs", ".ml",
        ".mli", ".zig", ".nim", ".v", ".sv", ".vhd", ".sol", ".elm", ".vue", ".svelte", ".astro",
        # shell and build
        ".sh", ".bash", ".zsh", ".fish", ".ps1", ".psm1", ".bat", ".cmd", ".mk", ".cmake",
        ".gradle", ".tf", ".hcl", ".nix", ".bzl", ".dockerfile",
        # web and data formats
        ".html", ".htm", ".css", ".scss", ".sass", ".less", ".sql", ".graphql", ".gql", ".proto",
        ".json", ".jsonc", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".xml", ".csv",
        # docs
        ".md", ".mdx", ".rst", ".txt", ".adoc", ".tex",
    }
)

# Files that are text but conventionally have no extension.
TEXT_FILENAMES = frozenset(
    {
        "dockerfile", "makefile", "gnumakefile", "justfile", "jenkinsfile", "vagrantfile",
        "gemfile", "rakefile", "procfile", "readme", "license", "copying", "changelog",
        ".gitignore", ".dockerignore", ".editorconfig", ".gitattributes",
    }
)

# fmt: on

_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,199}$")
_URL_PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]+$")


class RepoUrlError(ValueError):
    """The repository URL or branch is not allowed."""


class IndexingError(RuntimeError):
    """Indexing failed for a reason worth showing to the user as-is."""


# --------------------------------------------------------------------------- URL policy


@dataclass(frozen=True)
class UrlPolicy:
    """Which clone URLs are acceptable.

    Production only ever allows https on the configured hosts. `allow_file` exists so tests can
    index a local repository; it is set only through a test dependency override.
    """

    allowed_hosts: frozenset[str]
    allow_file: bool = False

    @classmethod
    def from_hosts(cls, hosts: str | Iterable[str]) -> UrlPolicy:
        items = hosts.split(",") if isinstance(hosts, str) else hosts
        return cls(allowed_hosts=frozenset(h.strip().lower() for h in items if h.strip()))


def validate_repo_url(url: str, policy: UrlPolicy) -> str:
    """Return the normalised clone URL, or raise RepoUrlError.

    Normalising (lower-case host, no trailing slash or `.git`) keeps the unique `repos.url`
    meaningful and gives the web UI a URL it can turn into blob links.
    """
    url = url.strip()
    if not url or any(ch.isspace() or ord(ch) < 32 for ch in url):
        raise RepoUrlError("Repository URL must be a single https URL without spaces.")

    if policy.allow_file and url.startswith("file://"):
        return url.rstrip("/")

    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        raise RepoUrlError(f"Invalid repository URL: {exc}") from exc

    if parts.scheme.lower() != "https":
        raise RepoUrlError("Only https:// repository URLs are allowed.")
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise RepoUrlError("Credentials in the repository URL are not allowed.")
    if port is not None:
        raise RepoUrlError("Custom ports are not allowed in the repository URL.")
    host = (parts.hostname or "").lower()
    if host not in policy.allowed_hosts:
        allowed = ", ".join(sorted(policy.allowed_hosts)) or "none"
        raise RepoUrlError(f"Host '{host}' is not allowed. Allowed hosts: {allowed}.")
    if parts.query or parts.fragment:
        raise RepoUrlError("Repository URL must not contain a query string or fragment.")

    path = parts.path.rstrip("/")
    path = path.removesuffix(".git")
    segments = [s for s in path.split("/") if s]
    if (
        len(segments) < 2
        or not _URL_PATH_RE.match(path)
        or any(s in (".", "..") or s.startswith("-") for s in segments)
    ):
        raise RepoUrlError("Repository URL must look like https://host/owner/name.")
    return f"https://{host}/{'/'.join(segments)}"


def validate_branch(branch: str | None) -> str | None:
    """Branch names are passed to git, so only plain ref names are accepted."""
    if branch is None or not branch.strip():
        return None
    branch = branch.strip()
    if not _BRANCH_RE.match(branch) or ".." in branch or branch.endswith((".lock", "/", ".")):
        raise RepoUrlError("Invalid branch name.")
    return branch


# --------------------------------------------------------------------------- git clone


def _git_env(home: Path) -> dict[str, str]:
    """A minimal environment: no user/system git config, no credential helpers, no prompts,
    and none of the gateway's own secrets passed down to the child process."""
    env = {k: v for k in ("PATH", "SYSTEMROOT", "TMP", "TEMP", "TMPDIR") if (v := os.environ.get(k))}
    env.update(
        HOME=str(home),
        LANG="C",
        GIT_TERMINAL_PROMPT="0",
        GIT_ASKPASS="",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_LFS_SKIP_SMUDGE="1",
    )
    return env


async def _run_git(args: list[str], *, env: dict[str, str], timeout_s: float) -> str:
    proc = await asyncio.create_subprocess_exec(
        "git",
        *args,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        raise IndexingError(f"git timed out after {timeout_s:.0f} s") from None
    finally:
        # On timeout or task cancellation, don't leave a git process running.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
    if proc.returncode != 0:
        message = stderr.decode("utf-8", "replace").strip() or f"exit code {proc.returncode}"
        raise IndexingError(f"git failed: {message[-MAX_ERROR_CHARS:]}")
    return stdout.decode("utf-8", "replace").strip()


async def clone_repo(
    url: str, branch: str | None, dest: Path, policy: UrlPolicy, *, timeout_s: float = CLONE_TIMEOUT_S
) -> str:
    """Shallow-clone `url` into `dest` and return the checked-out commit sha."""
    url = validate_repo_url(url, policy)
    branch = validate_branch(branch)

    config = [
        # Only https may be used, including for redirects and submodules.
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        # An allowed host must not be able to bounce us to an arbitrary one.
        "-c",
        "http.followRedirects=false",
        # Symlinks become plain files, so the walker can never escape the checkout.
        "-c",
        "core.symlinks=false",
        "-c",
        "core.hooksPath=" + os.devnull,
    ]
    if policy.allow_file:
        config += ["-c", "protocol.file.allow=always"]

    clone_args = ["clone", "--depth", "1", "--single-branch", "--no-tags", "--template="]
    if branch:
        clone_args += ["--branch", branch]
    clone_args += ["--", url, str(dest)]

    env = _git_env(dest.parent)
    await _run_git(config + clone_args, env=env, timeout_s=timeout_s)
    return await _run_git(["-C", str(dest), "rev-parse", "HEAD"], env=env, timeout_s=30)


# --------------------------------------------------------------------------- files and chunks


@dataclass(frozen=True)
class SourceFile:
    path: str  # relative, forward slashes
    text: str


@dataclass(frozen=True)
class Chunk:
    path: str
    start_line: int  # 1-based, inclusive
    end_line: int
    content: str  # "# <path>" header line followed by the source lines; this is what is embedded


def is_indexable_name(name: str) -> bool:
    lower = name.lower()
    if lower in LOCK_FILES or lower.endswith(".lock"):
        return False
    if lower.endswith((".min.js", ".min.css")):
        return False
    if lower in TEXT_FILENAMES:
        return True
    return Path(lower).suffix in TEXT_EXTENSIONS


def collect_files(
    root: Path,
    *,
    max_files: int = MAX_FILES,
    max_total_bytes: int = MAX_TOTAL_BYTES,
    max_file_bytes: int = MAX_FILE_BYTES,
) -> list[SourceFile]:
    """Walk the checkout and return indexable text files, in a stable (sorted) order."""
    files: list[SourceFile] = []
    total = 0
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS and d.lower() not in SKIP_DIRS)
        for name in sorted(filenames):
            if not is_indexable_name(name):
                continue
            full = Path(dirpath, name)
            try:
                info = full.lstat()
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_size == 0 or info.st_size > max_file_bytes:
                continue
            if total + info.st_size > max_total_bytes:
                continue
            data = full.read_bytes()
            if b"\x00" in data:  # binary file with a text-looking name
                continue
            text = data.decode("utf-8", "replace")
            if not text.strip():
                continue
            files.append(SourceFile(path=full.relative_to(root).as_posix(), text=text))
            total += info.st_size
            if len(files) >= max_files:
                return files
    return files


def chunk_file(
    path: str, text: str, *, lines_per_chunk: int = CHUNK_LINES, overlap: int = CHUNK_OVERLAP
) -> list[Chunk]:
    """Split a file into windows of `lines_per_chunk` lines that overlap by `overlap` lines.

    The overlap keeps a function that straddles a boundary visible in at least one chunk; the
    path header lets the embedding (and the model) know which file the lines belong to.
    """
    if overlap >= lines_per_chunk:
        raise ValueError("overlap must be smaller than lines_per_chunk")
    lines = text.splitlines()
    chunks: list[Chunk] = []
    start = 0
    while start < len(lines):
        end = min(start + lines_per_chunk, len(lines))
        body = "\n".join(lines[start:end])
        if body.strip():
            content = f"# {path}\n{body}"[:MAX_CHUNK_CHARS]
            chunks.append(Chunk(path=path, start_line=start + 1, end_line=end, content=content))
        if end == len(lines):
            break
        start = end - overlap
    return chunks


def _batched(items: list[Chunk], size: int) -> Iterable[list[Chunk]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def _remove_tree(path: Path) -> None:
    # git marks pack files read-only; on Windows rmtree needs them made writable first.
    def make_writable_and_retry(func: Callable[[str], Any], target: str, _exc: BaseException) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    shutil.rmtree(path, onexc=make_writable_and_retry)


# --------------------------------------------------------------------------- the indexing job

SessionFactory = Callable[[], AsyncSession]


def describe_error(exc: BaseException) -> str:
    """A short, user-facing message. ForgeError carries it in `.message`, HTTPException in
    `.detail` (possibly an OpenAI-style dict)."""
    detail = getattr(exc, "message", None) or getattr(exc, "detail", None)
    if isinstance(detail, dict):
        detail = detail.get("message") or detail.get("error") or detail
    message = str(detail) if detail else str(exc)
    return (message or type(exc).__name__)[:MAX_ERROR_CHARS]


async def _update_repo(session_factory: SessionFactory, repo_id: int, **values: Any) -> None:
    async with session_factory() as session:
        await session.execute(update(Repo).where(Repo.id == repo_id).values(**values))
        await session.commit()


async def index_repo(
    repo_id: int,
    *,
    session_factory: SessionFactory,
    llm: LLM,
    principal: Principal,
    policy: UrlPolicy,
    clone_timeout: float = CLONE_TIMEOUT_S,
) -> None:
    """Run one full (re)index of a repository. Never raises for ordinary failures: the error is
    stored on the row as status `failed`, which is what the UI polls."""
    async with session_factory() as session:
        repo = await session.get(Repo, repo_id)
        if repo is None:  # deleted while queued
            return
        url, branch = repo.url, repo.branch

    workdir = Path(tempfile.mkdtemp(prefix="forge-rag-"))
    try:
        await _update_repo(session_factory, repo_id, status="cloning", error=None)
        commit_sha = await clone_repo(url, branch, workdir / "repo", policy, timeout_s=clone_timeout)

        files = collect_files(workdir / "repo")
        if not files:
            raise IndexingError("No indexable text files found in the repository.")
        chunks = [chunk for f in files for chunk in chunk_file(f.path, f.text)]
        del files  # the chunks hold everything we still need

        # Re-indexing replaces the old chunks. Asking is only allowed while status is `ready`,
        # so nobody reads the half-built set in between.
        async with session_factory() as session:
            await session.execute(delete(RepoChunk).where(RepoChunk.repo_id == repo_id))
            await session.execute(
                update(Repo)
                .where(Repo.id == repo_id)
                .values(
                    status="embedding",
                    files_indexed=len({c.path for c in chunks}),
                    chunks=0,
                    commit_sha=commit_sha,
                )
            )
            await session.commit()

        stored = 0
        for batch in _batched(chunks, EMBED_BATCH):
            vectors = await llm.embed([c.content for c in batch], principal=principal, priority=Priority.BATCH)
            if len(vectors) != len(batch) or any(len(v) != EMBEDDING_DIMENSIONS for v in vectors):
                raise IndexingError(
                    f"Embedding model returned an unexpected shape; expected {len(batch)} vectors "
                    f"of {EMBEDDING_DIMENSIONS} dimensions."
                )
            stored += len(batch)
            async with session_factory() as session:
                session.add_all(
                    RepoChunk(
                        repo_id=repo_id,
                        path=c.path,
                        start_line=c.start_line,
                        end_line=c.end_line,
                        content=c.content,
                        embedding=vector,
                    )
                    for c, vector in zip(batch, vectors, strict=True)
                )
                await session.execute(update(Repo).where(Repo.id == repo_id).values(chunks=stored))
                await session.commit()

        await _update_repo(session_factory, repo_id, status="ready", error=None)
        log.info("indexed repo %s: %d chunks at %s", repo_id, stored, commit_sha[:12])
    except asyncio.CancelledError:
        # Cancelled because the repo was deleted or the gateway is shutting down.
        with contextlib.suppress(Exception):
            await _update_repo(session_factory, repo_id, status="failed", error="Indexing was interrupted.")
        raise
    except Exception as exc:
        log.warning("indexing repo %s failed: %s", repo_id, describe_error(exc))
        with contextlib.suppress(Exception):
            await _update_repo(session_factory, repo_id, status="failed", error=describe_error(exc))
    finally:
        await asyncio.to_thread(_remove_tree, workdir)


# --------------------------------------------------------------------------- job queue + lock


@contextlib.asynccontextmanager
async def cluster_lock(redis: Any, name: str = LOCK_NAME, ttl: float = LOCK_TTL_S):
    """Hold a Redis lock for the duration of the block, renewing it while we work.

    The TTL means a crashed gateway releases the lock by itself within `ttl` seconds.
    """
    lock = redis.lock(name, timeout=ttl, sleep=1.0, thread_local=False, raise_on_release_error=False)
    await lock.acquire(blocking=True)

    async def renew() -> None:
        try:
            while True:
                await asyncio.sleep(ttl / 3)
                await lock.extend(ttl, replace_ttl=True)
        except Exception as exc:  # the job keeps going; at worst two jobs overlap once
            log.warning("could not renew %s: %s", name, exc)

    renewer = asyncio.create_task(renew())
    try:
        yield
    finally:
        renewer.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await renewer
        with contextlib.suppress(Exception):
            await lock.release()


@dataclass
class _Job:
    repo_id: int
    principal: Principal


@dataclass
class Indexer:
    """Runs index jobs one at a time in this process, each under the cluster-wide Redis lock."""

    session_factory: SessionFactory
    llm: LLM
    redis: Any
    policy: UrlPolicy
    clone_timeout: float = CLONE_TIMEOUT_S
    _queue: asyncio.Queue[_Job] = field(default_factory=asyncio.Queue, init=False)
    _pending: set[int] = field(default_factory=set, init=False)
    _current: tuple[int, asyncio.Task[None]] | None = field(default=None, init=False)
    _worker: asyncio.Task[None] | None = field(default=None, init=False)

    def is_pending(self, repo_id: int) -> bool:
        return repo_id in self._pending

    def submit(self, repo_id: int, principal: Principal) -> bool:
        """Queue a job; returns False if this repo is already queued or running here."""
        if repo_id in self._pending:
            return False
        self._pending.add(repo_id)
        self._queue.put_nowait(_Job(repo_id, principal))
        if self._worker is None or self._worker.done():
            self._worker = asyncio.create_task(self._run(), name="forge-rag-indexer")
        return True

    def cancel(self, repo_id: int) -> None:
        """Forget a queued job, or stop the running one (used when a repo is deleted)."""
        self._pending.discard(repo_id)
        if self._current and self._current[0] == repo_id:
            self._current[1].cancel()

    async def join(self) -> None:
        """Wait until every queued job has finished (used by tests and graceful shutdown)."""
        await self._queue.join()

    async def aclose(self) -> None:
        if self._worker:
            self._worker.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._worker
        if self._current:
            self._current[1].cancel()

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            try:
                if job.repo_id not in self._pending:  # cancelled while queued
                    continue
                await self._run_job(job)
            except Exception as exc:
                # index_repo records its own failures; this covers e.g. Redis being unreachable,
                # which would otherwise leave the repo `queued` forever.
                log.exception("index job for repo %s crashed", job.repo_id)
                with contextlib.suppress(Exception):
                    await _update_repo(self.session_factory, job.repo_id, status="failed", error=describe_error(exc))
            finally:
                self._pending.discard(job.repo_id)
                self._current = None
                self._queue.task_done()

    async def _run_job(self, job: _Job) -> None:
        async with cluster_lock(self.redis):
            if job.repo_id not in self._pending:  # deleted while we waited for the lock
                return
            task = asyncio.create_task(
                index_repo(
                    job.repo_id,
                    session_factory=self.session_factory,
                    llm=self.llm,
                    principal=job.principal,
                    policy=self.policy,
                    clone_timeout=self.clone_timeout,
                )
            )
            self._current = (job.repo_id, task)
            # asyncio.wait doesn't propagate the child's cancellation into this worker.
            await asyncio.wait([task])
