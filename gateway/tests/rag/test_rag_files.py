import os
from pathlib import Path

import pytest

from app.rag.indexer import MAX_FILE_BYTES, collect_files, is_indexable_name


def write(root: Path, rel: str, data: str | bytes) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(data, bytes):
        path.write_bytes(data)
    else:
        path.write_text(data, encoding="utf-8")


def paths(root: Path, **kwargs) -> list[str]:
    return [f.path for f in collect_files(root, **kwargs)]


def test_walk_skips_vendored_build_and_vcs_directories(tmp_path):
    write(tmp_path, "src/main.py", "print('hi')\n")
    for skipped in [".git", "node_modules", "vendor", "dist", "build", ".venv", "__pycache__"]:
        write(tmp_path, f"{skipped}/inner.py", "x = 1\n")
        write(tmp_path, f"src/{skipped}/nested.js", "let x = 1\n")
    assert paths(tmp_path) == ["src/main.py"]


def test_walk_skips_lock_files_binaries_large_and_empty_files(tmp_path):
    write(tmp_path, "app.ts", "export const a = 1\n")
    for lock in [
        "package-lock.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "poetry.lock",
        "uv.lock",
        "Cargo.lock",
        "go.sum",
        "Gemfile.lock",
        "something.lock",
    ]:
        write(tmp_path, lock, "{}\n")
    write(tmp_path, "fake.py", b"print('x')\x00\x01\x02")  # NUL byte => binary
    write(tmp_path, "logo.png", b"\x89PNG\r\n")
    write(tmp_path, "bundle.min.js", "var a=1;")
    write(tmp_path, "huge.py", "x" * (MAX_FILE_BYTES + 1))
    write(tmp_path, "limit.py", "y" * MAX_FILE_BYTES)  # exactly at the limit is fine
    write(tmp_path, "empty.py", "")
    write(tmp_path, "blank.md", "\n\n  \n")
    assert paths(tmp_path) == ["app.ts", "limit.py"]


def test_text_extensions_and_extensionless_names():
    for name in [
        "main.go",
        "lib.rs",
        "App.tsx",
        "README.md",
        "Dockerfile",
        "Makefile",
        "values.yaml",
        "pyproject.toml",
        "script.ps1",
        "README",
        "CMakeLists.txt",
    ]:
        assert is_indexable_name(name), name
    for name in [
        "photo.jpg",
        "font.woff2",
        "archive.zip",
        "model.bin",
        "app.exe",
        "data.sqlite",
        "notebook.ipynb",
        "yarn.lock",
        "app.min.css",
        "random",
    ]:
        assert not is_indexable_name(name), name


def test_output_is_stable_with_forward_slash_paths(tmp_path):
    # Walk order: a directory's own files first, then its subdirectories, each sorted.
    write(tmp_path, "b/z.py", "1\n")
    write(tmp_path, "a/y.py", "1\n")
    write(tmp_path, "a/c/x.py", "1\n")
    write(tmp_path, "root.py", "1\n")
    assert paths(tmp_path) == ["root.py", "a/y.py", "a/c/x.py", "b/z.py"]


def test_file_count_and_total_size_limits(tmp_path):
    for i in range(10):
        write(tmp_path, f"f{i}.py", b"a" * 100 + b"\n")  # bytes: exactly 101 on every OS
    assert len(paths(tmp_path, max_files=3)) == 3
    assert len(paths(tmp_path, max_total_bytes=101 * 4)) == 4


def test_invalid_utf8_is_replaced_not_fatal(tmp_path):
    write(tmp_path, "latin1.txt", "caf\xe9\n".encode("latin-1"))
    (f,) = collect_files(tmp_path)
    assert f.text == "caf�\n"


def test_symlinks_are_never_followed(tmp_path):
    outside = tmp_path / "outside"
    write(outside, "secret.txt", "do not index\n")
    repo = tmp_path / "repo"
    write(repo, "ok.py", "x = 1\n")
    try:
        os.symlink(outside / "secret.txt", repo / "link.txt")
        os.symlink(outside, repo / "linkdir", target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("creating symlinks needs extra privileges on this OS")
    assert paths(repo) == ["ok.py"]
