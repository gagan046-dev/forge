"""Workspaces API: files, ZIP, and git clone/status/diff/commit/push against local bare repos."""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

from app.auth import Principal, require_admin
from app.config import Settings
from app.errors import install_error_handlers
from app.rag.indexer import UrlPolicy
from app.rag.router import get_url_policy
from app.workspaces import router

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")

ADMIN = Principal(key_id=1, name="web-console", is_admin=True)
TOKEN = "fake-push-token-1234567890"


def run_git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", "-c", "protocol.file.allow=always", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


@pytest.fixture
def bare_repo(tmp_path: Path) -> Path:
    """A bare repository with one commit on main, used as clone source and push target."""
    seed = tmp_path / "seed"
    seed.mkdir()
    run_git("init", "-q", "-b", "main", cwd=seed)
    (seed / "README.md").write_text("# hello\n")
    run_git("add", "-A", cwd=seed)
    run_git("-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "init", cwd=seed)
    bare = tmp_path / "remote.git"
    run_git("clone", "-q", "--bare", str(seed), str(bare))
    return bare


@pytest_asyncio.fixture
async def client(tmp_path: Path):
    app = FastAPI()
    install_error_handlers(app)
    app.include_router(router)
    app.state.settings = Settings(
        admin_password="test-admin-password",
        session_secret="test-session-secret-0123456789",
        workspace_root=str(tmp_path / "workspaces"),
        workspace_max_file_bytes=1000,
    )
    app.dependency_overrides[require_admin] = lambda: ADMIN
    app.dependency_overrides[get_url_policy] = lambda: UrlPolicy(frozenset({"github.com"}), allow_file=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://forge.test") as http:
        yield http


async def make(client: httpx.AsyncClient, name: str = "demo", **body) -> dict:
    response = await client.post("/api/workspaces", json={"name": name, **body})
    assert response.status_code == 201, response.text
    return response.json()


async def test_create_list_delete(client):
    created = await make(client)
    assert created["name"] == "demo"
    assert created["git"] == {"remote": None, "branch": "main"}
    assert (await client.post("/api/workspaces", json={"name": "demo"})).status_code == 409
    assert (await client.post("/api/workspaces", json={"name": "Bad Name"})).status_code == 400
    assert [w["name"] for w in (await client.get("/api/workspaces")).json()] == ["demo"]
    assert (await client.delete("/api/workspaces/demo")).status_code == 204
    assert (await client.get("/api/workspaces")).json() == []
    assert (await client.get("/api/workspaces/demo/tree")).status_code == 404


@pytest.mark.parametrize("path", ["../x", "/etc/passwd", "a/../../x", ".git/config", "a\\b", "sub/.GIT/x", ""])
async def test_paths_outside_the_workspace_or_in_git_are_rejected(client, path):
    await make(client)
    response = await client.put("/api/workspaces/demo/file", json={"path": path, "content": "x"})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_path"


async def test_file_and_folder_operations(client):
    await make(client)
    base = "/api/workspaces/demo"
    put = await client.put(f"{base}/file", json={"path": "src/app.py", "content": "print('hi')\n"})
    assert put.json() == {"path": "src/app.py", "size": 12}
    assert (await client.get(f"{base}/file", params={"path": "src/app.py"})).json()["content"] == "print('hi')\n"
    assert (await client.post(f"{base}/folder", json={"path": "docs"})).status_code == 201
    assert (await client.post(f"{base}/folder", json={"path": "docs"})).status_code == 409
    moved = await client.post(f"{base}/move", json={"from": "src/app.py", "to": "src/main.py"})
    assert moved.json() == {"path": "src/main.py"}
    assert (await client.post(f"{base}/move", json={"from": "nope", "to": "x"})).status_code == 404
    tree = (await client.get(f"{base}/tree")).json()
    assert [(e["path"], e["type"]) for e in tree["entries"]] == [
        ("docs", "dir"),
        ("src", "dir"),
        ("src/main.py", "file"),
    ]
    assert tree["truncated"] is False
    assert (await client.delete(f"{base}/entry", params={"path": "src"})).status_code == 204
    assert [e["path"] for e in (await client.get(f"{base}/tree")).json()["entries"]] == ["docs"]


async def test_binary_and_too_large_files(client, tmp_path):
    await make(client)
    (tmp_path / "workspaces" / "demo" / "logo.png").write_bytes(b"\x89PNG\x00\x01")
    (tmp_path / "workspaces" / "demo" / "big.txt").write_text("x" * 2000)
    base = "/api/workspaces/demo"
    assert (await client.get(f"{base}/file", params={"path": "logo.png"})).status_code == 415
    assert (await client.get(f"{base}/file", params={"path": "big.txt"})).status_code == 413
    too_big = await client.put(f"{base}/file", json={"path": "new.txt", "content": "y" * 2000})
    assert too_big.status_code == 413


async def test_zip_download(client):
    await make(client)
    await client.put("/api/workspaces/demo/file", json={"path": "a/b.txt", "content": "bee"})
    response = await client.get("/api/workspaces/demo/archive.zip")
    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert 'filename="demo.zip"' in response.headers["content-disposition"]
    archive = zipfile.ZipFile(io.BytesIO(response.content))
    assert archive.namelist() == ["demo/a/b.txt"]  # .git is left out
    assert archive.read("demo/a/b.txt") == b"bee"


async def test_clone_edit_commit_push(client, bare_repo):
    created = await make(client, "cloned", git_url=bare_repo.as_uri())
    assert created["git"]["branch"] == "main"
    base = "/api/workspaces/cloned"
    assert (await client.get(f"{base}/file", params={"path": "README.md"})).json()["content"] == "# hello\n"

    await client.put(f"{base}/file", json={"path": "README.md", "content": "# hello world\n"})
    await client.put(f"{base}/file", json={"path": "new.txt", "content": "fresh\n"})
    status = (await client.get(f"{base}/git")).json()
    assert {(c["path"], c["status"]) for c in status["changes"]} == {("README.md", "M"), ("new.txt", "??")}
    diff = (await client.get(f"{base}/git/diff")).json()
    assert "+# hello world" in diff["diff"] and "+fresh" in diff["diff"]
    # The diff used a throw-away index: new.txt is still untracked.
    assert any(c["status"] == "??" for c in (await client.get(f"{base}/git")).json()["changes"])

    author = {"author_name": "Dev", "author_email": "dev@example.com"}
    commit = await client.post(f"{base}/git/commit", json={"message": "Update readme", **author})
    assert commit.status_code == 200, commit.text
    assert commit.json()["summary"] == "Update readme"
    again = await client.post(f"{base}/git/commit", json={"message": "again", **author})
    assert again.status_code == 409 and again.json()["error"]["code"] == "nothing_to_commit"

    push = await client.post(f"{base}/git/push", json={"token": None})
    assert push.status_code == 200, push.text
    assert run_git("log", "-1", "--format=%s", "main", cwd=bare_repo).strip() == "Update readme"


async def test_failed_clone_leaves_nothing_behind(client, tmp_path):
    response = await client.post(
        "/api/workspaces", json={"name": "broken", "git_url": (tmp_path / "missing.git").as_uri()}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "clone_failed"
    assert not (tmp_path / "workspaces" / "broken").exists()


async def test_push_needs_a_remote(client):
    await make(client)
    response = await client.post("/api/workspaces/demo/git/push", json={"token": None})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "no_remote"


async def test_remote_must_pass_the_url_policy(client):
    await make(client)
    response = await client.post("/api/workspaces/demo/git/remote", json={"url": "http://evil.test/a/b"})
    assert response.status_code == 400


async def test_token_never_appears_in_errors(client, tmp_path):
    await make(client)
    await client.put("/api/workspaces/demo/file", json={"path": "a.txt", "content": "a"})
    author = {"author_name": "Dev", "author_email": "dev@example.com"}
    await client.post("/api/workspaces/demo/git/commit", json={"message": "first", **author})
    bad = (tmp_path / "does-not-exist.git").as_uri()
    assert (await client.post("/api/workspaces/demo/git/remote", json={"url": bad})).status_code == 200
    response = await client.post("/api/workspaces/demo/git/push", json={"token": TOKEN})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "push_failed"
    assert TOKEN not in response.text
