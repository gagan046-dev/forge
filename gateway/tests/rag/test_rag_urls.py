from pathlib import Path

import pytest

from app.rag.indexer import RepoUrlError, UrlPolicy, clone_repo, validate_branch, validate_repo_url

PROD = UrlPolicy.from_hosts("github.com,gitlab.com,codeberg.org")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://github.com/owner/name", "https://github.com/owner/name"),
        ("https://github.com/owner/name.git", "https://github.com/owner/name"),
        ("https://GitHub.com/owner/name/", "https://github.com/owner/name"),
        ("  https://codeberg.org/owner/name  ", "https://codeberg.org/owner/name"),
        ("https://gitlab.com/group/subgroup/project", "https://gitlab.com/group/subgroup/project"),
        ("HTTPS://github.com/owner/repo.name_1", "https://github.com/owner/repo.name_1"),
    ],
)
def test_accepts_https_urls_on_allowed_hosts(url, expected):
    assert validate_repo_url(url, PROD) == expected


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://github.com/owner/name", "https"),
        ("git://github.com/owner/name", "https"),
        ("ssh://git@github.com/owner/name", "https"),
        ("git@github.com:owner/name.git", "https"),
        ("ext::sh -c touch% /tmp/pwned", "spaces"),
        ("file:///etc/passwd", "https"),
        ("https://example.com/owner/name", "not allowed"),
        ("https://github.com.evil.example/owner/name", "not allowed"),
        ("https://evilgithub.com/owner/name", "not allowed"),
        ("https://localhost/owner/name", "not allowed"),
        ("https://169.254.169.254/latest/meta-data", "not allowed"),
        ("https://user@github.com/owner/name", "Credentials"),
        ("https://user:token@github.com/owner/name", "Credentials"),
        ("https://:token@github.com/owner/name", "Credentials"),
        ("https://github.com@evil.example/owner/name", "Credentials"),
        ("https://github.com:8443/owner/name", "ports"),
        ("https://github.com:notaport/owner/name", "Invalid"),
        ("https://github.com/owner/name?ref=main", "query"),
        ("https://github.com/owner/name#readme", "fragment"),
        ("https://github.com/owner", "owner/name"),
        ("https://github.com/", "owner/name"),
        ("https://github.com/owner/../../etc", "owner/name"),
        ("https://github.com/owner/-name", "owner/name"),
        ("https://github.com/owner/na%20me", "owner/name"),
        ("https://github.com/owner/name\n--upload-pack=x", "spaces"),
        ("", "spaces"),
    ],
)
def test_rejects_unsafe_urls(url, reason):
    with pytest.raises(RepoUrlError, match=reason):
        validate_repo_url(url, PROD)


def test_file_urls_only_with_explicit_test_policy(tmp_path):
    url = tmp_path.as_uri()
    with pytest.raises(RepoUrlError):
        validate_repo_url(url, PROD)
    assert validate_repo_url(url, UrlPolicy(frozenset(), allow_file=True)) == url


def test_policy_from_hosts_accepts_string_or_list():
    assert UrlPolicy.from_hosts(" GitHub.com , ,gitlab.com").allowed_hosts == {"github.com", "gitlab.com"}
    assert UrlPolicy.from_hosts(["codeberg.org"]).allowed_hosts == {"codeberg.org"}
    assert not UrlPolicy.from_hosts("").allowed_hosts


@pytest.mark.parametrize("branch", ["main", "release/1.2", "feature_x-1", "v2.0.1"])
def test_accepts_plain_branch_names(branch):
    assert validate_branch(branch) == branch


@pytest.mark.parametrize("branch", [None, "", "   "])
def test_empty_branch_means_default(branch):
    assert validate_branch(branch) is None


@pytest.mark.parametrize("branch", ["--upload-pack=touch x", "-b", "a..b", "a b", "main;rm", "x.lock", "dir/", "$(id)"])
def test_rejects_unsafe_branch_names(branch):
    with pytest.raises(RepoUrlError):
        validate_branch(branch)


@pytest.mark.asyncio
async def test_clone_refuses_disallowed_url_before_running_git(tmp_path: Path):
    with pytest.raises(RepoUrlError):
        await clone_repo("http://github.com/owner/name", None, tmp_path / "repo", PROD)
    assert not (tmp_path / "repo").exists()
