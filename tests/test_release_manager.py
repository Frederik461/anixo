"""Deterministic tests for the in-CLI ANIXO release manager.

The tests never contact GitHub, run pytest recursively, or modify a real
repository.  Git, gh, HTTP, confirmation, and sleeping are replaced with
in-memory fakes.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

import pytest

from config import Config
from services import release_manager as rm

CURRENT = "0.1.0-beta.1"
NEXT_BETA = "0.1.0-beta.2"
STABLE = "0.1.0"
TOKEN = "Frederik461/anixo"


def _files(root: Path) -> dict[str, str]:
    return {
        "version.py": f'__version__ = "{CURRENT}"\nVERSION = __version__\n',
        "pyproject.toml": f'[project]\nname = "anixo"\nversion = "{CURRENT}"\n',
        "installer/AnixoSetup.iss": (
            f'; installer for {CURRENT}\n'
            f'#define MyAppVersion "{CURRENT}"\n'
            'VersionInfoProductVersion=0.1.0.0\n'
            'VersionInfoVersion=0.1.0.0\n'
        ),
        "uv.lock": '[[package]]\nname = "anixo"\nversion = "0.1.0b1"\n',
        ".github/workflows/release.yml": (
            "name: Release ANIXO\n"
            "on:\n  push:\n    tags:\n      - 'v*'\n"
            "      uses: softprops/action-gh-release@v2\n"
            "      SHA256SUMS.txt\n"
            "      release-manifest.json\n"
            f"      APP_VERSION: {CURRENT}\n"
        ),
        ".github/workflows/macos.yml": f"APP_VERSION: {CURRENT}\n",
        "packaging/build_windows.ps1": f"[string] $Version = '{CURRENT}'\n",
        "packaging/make_portable.ps1": f"[string] $Version = '{CURRENT}'\n",
        "packaging/validate_build.ps1": f"[string] $Version = '{CURRENT}'\n",
        "packaging/build_macos.sh": f'VERSION="${{1:-{CURRENT}}}"\n',
        "packaging/make_release_manifest.py": "print('generator')\n",
        "packaging/validate_macos_runtime.py": "VERSION = '0.1.0-beta.1'\n",
        "README.md": f"Current release {CURRENT} is available.\n",
        "anixo.spec": "spec\n",
        "anixo_macos.spec": "spec\n",
        "uninstaller.spec": "spec\n",
        "uninstaller_macos.spec": "spec\n",
        "tests/test_cli.py": f'assert "ANIXO {CURRENT}" in output\n',
    }


def _repo(tmp_path: Path, *, line_endings: str = "\n") -> Path:
    root = tmp_path / "repo"
    for relative, content in _files(root).items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content.replace("\n", line_endings).encode("utf-8"))
    return root


def _digest() -> str:
    return "ab" * 32


def _release_json(version: str) -> dict[str, Any]:
    digest = _digest()
    base = f"https://github.com/{TOKEN}/releases/download/v{version}"
    return {
        "tagName": f"v{version}",
        "name": f"ANIXO v{version}",
        "isDraft": False,
        "isPrerelease": "-" in version,
        "url": f"https://github.com/{TOKEN}/releases/tag/v{version}",
        "assets": [
            {
                "name": name,
                "url": f"{base}/{name}",
                "digest": f"sha256:{digest}",
                "size": 10,
            }
            for name in rm.expected_asset_names(version)
        ],
    }


def _manifest_json(version: str) -> dict[str, Any]:
    digest = _digest()
    base = f"https://github.com/{TOKEN}/releases/download/v{version}"
    return {
        "schema_version": 1,
        "version": version,
        "channel": rm.channel_for_version(version),
        "published_at": "2026-11-25T00:00:00Z",
        "notes": "Test manifest",
        "assets": [
            {
                "name": name,
                "url": f"{base}/{name}",
                "size": 10,
                "sha256": digest,
                "platform": "windows",
                "arch": "x86_64",
                "kind": "installer",
            }
            for name in rm.expected_manifest_asset_names(version)
        ],
    }


def _fetch_map(version: str, *, manifest: dict[str, Any] | None = None) -> dict[str, bytes]:
    release = _release_json(version)
    manifest = manifest or _manifest_json(version)
    checksums = "\n".join(
        f"{_digest()}  {name}" for name in rm.expected_checksum_names(version)
    ).encode("utf-8")
    result: dict[str, bytes] = {}
    for asset in release["assets"]:
        if asset["name"] == "release-manifest.json":
            result[asset["url"]] = json.dumps(manifest).encode("utf-8")
        elif asset["name"] == "SHA256SUMS.txt":
            result[asset["url"]] = checksums
        else:
            result[asset["url"]] = b"asset"
    return result


class FakeRunner:
    """Command runner with deterministic git/gh responses and call recording."""

    def __init__(
        self,
        root: Path,
        *,
        version: str = CURRENT,
        workflow_tag: str = f"v{NEXT_BETA}",
        dirty: str = "",
        head_sha: str = "abc123def456",
        remote_head: str | None = None,
        local_tag: bool = False,
        remote_tag: bool = False,
        run_states: list[dict[str, Any]] | None = None,
        release_json: dict[str, Any] | None = None,
        gh_auth_ok: bool = True,
    ) -> None:
        self.root = root
        self.version = version
        self.workflow_tag = workflow_tag
        self.dirty = dirty
        self.head_sha = head_sha
        self.remote_head = head_sha if remote_head is None else remote_head
        self.local_tag = local_tag
        self.remote_tag = remote_tag
        self.run_states = list(run_states or [])
        self.release_json = release_json
        self.gh_auth_ok = gh_auth_ok
        self.calls: list[tuple[str, ...]] = []

    def __call__(
        self,
        command: list[str],
        *,
        cwd: Path | None = None,
        timeout: float | None = None,
    ) -> rm.CommandResult:
        key = tuple(command)
        self.calls.append(key)
        ok = rm.CommandResult(key, 0, "", "")
        fail = lambda detail: rm.CommandResult(key, 1, "", detail)

        if key[:3] == ("git", "rev-parse", "--is-inside-work-tree"):
            return rm.CommandResult(key, 0, "true\n", "")
        if key[:3] == ("git", "remote", "get-url"):
            return rm.CommandResult(key, 0, f"https://github.com/{TOKEN}.git\n", "")
        if key[:3] == ("git", "rev-parse", "--abbrev-ref"):
            return rm.CommandResult(key, 0, "main\n", "")
        if key[:2] == ("git", "status"):
            return rm.CommandResult(key, 0, self.dirty, "")
        if key[:3] == ("git", "rev-parse", "HEAD"):
            return rm.CommandResult(key, 0, f"{self.head_sha}\n", "")
        if key[:2] == ("git", "ls-remote") and "--heads" in key:
            text = f"{self.remote_head}\trefs/heads/main" if self.remote_head else ""
            return rm.CommandResult(key, 0, text + "\n", "")
        if key[:2] == ("git", "ls-remote") and "--tags" in key:
            tag = next((item for item in key if item.startswith("refs/tags/")), "")
            return rm.CommandResult(key, 0, f"{self.head_sha}\t{tag}\n" if self.remote_tag else "", "")
        if key[:3] == ("git", "rev-parse", "-q") and "refs/tags/" in key[-1]:
            return ok if self.local_tag else fail("not found")
        if key[:2] == ("git", "ls-files"):
            names = sorted(
                str(path.relative_to(self.root))
                for path in self.root.rglob("*")
                if path.is_file() and ".git" not in path.parts
            )
            return rm.CommandResult(key, 0, "\0".join(names) + "\0", "")
        if key[:2] == ("gh", "auth"):
            return ok if self.gh_auth_ok else fail("not logged in")
        if key[:3] == ("gh", "repo", "view"):
            return rm.CommandResult(key, 0, json.dumps({"nameWithOwner": TOKEN}), "")
        if "pytest" in key:
            return rm.CommandResult(key, 0, "3 passed in 0.1s\n", "")
        if "compileall" in key:
            return ok
        if any("make_release_manifest.py" in part for part in key):
            output = Path(command[command.index("--output") + 1])
            target = command[command.index("--version") + 1]
            output.write_text(json.dumps(_manifest_json(target)), encoding="utf-8")
            return rm.CommandResult(key, 0, f"Wrote {output}\n", "")
        if "uv" in key and "lock" in key:
            return ok
        if key[:3] == ("gh", "run", "list"):
            first = self.run_states[0] if self.run_states else {
                "databaseId": "99",
                "status": "completed",
                "conclusion": "success",
                "headBranch": self.workflow_tag,
                "displayTitle": f"Release {self.workflow_tag}",
                "url": "https://github.com/actions/runs/99",
            }
            payload = [dict(first)]
            return rm.CommandResult(key, 0, json.dumps(payload), "")
        if key[:3] == ("gh", "run", "view"):
            if "--json" in key and key[key.index("--json") + 1] == "jobs":
                jobs = [
                    {"name": "windows-build", "conclusion": "failure", "url": "https://example.invalid/job"}
                ]
                return rm.CommandResult(key, 0, json.dumps({"jobs": jobs}), "")
            if self.run_states:
                return rm.CommandResult(key, 0, json.dumps(self.run_states.pop(0)), "")
            payload = {
                "databaseId": "99",
                "status": "completed",
                "conclusion": "success",
                "headBranch": self.workflow_tag,
                "displayTitle": f"Release {self.workflow_tag}",
                "url": "https://github.com/actions/runs/99",
            }
            return rm.CommandResult(key, 0, json.dumps(payload), "")
        if key[:3] == ("gh", "release", "view"):
            payload = self.release_json or _release_json(self.version)
            return rm.CommandResult(key, 0, json.dumps(payload), "")
        if key[:3] == ("gh", "release", "list"):
            payload = [
                {
                    "tagName": "v0.1.0-beta.1",
                    "name": "ANIXO v0.1.0-beta.1",
                    "isPrerelease": True,
                    "publishedAt": "2026-11-25T00:00:00Z",
                    "url": "https://github.com/Frederik461/anixo/releases/tag/v0.1.0-beta.1",
                }
            ]
            return rm.CommandResult(key, 0, json.dumps(payload), "")
        return ok


def _manager(
    root: Path,
    runner: FakeRunner,
    *,
    version: str = CURRENT,
    confirm: Callable[..., bool] | None = None,
    output: list[str] | None = None,
    fetcher: Callable[[str], bytes] | None = None,
    head_fetcher: Callable[[str], tuple[int, int]] | None = None,
    release_repo: str = TOKEN,
) -> rm.ReleaseManager:
    config = Config()
    config.update_repo = "tester/fork"
    config.release_repo = release_repo
    config.release_branch = "main"
    config.release_remote = "origin"
    fetched = _fetch_map(version)
    return rm.ReleaseManager(
        root,
        config,
        runner=runner,
        confirmer=confirm or (lambda *_args, **_kwargs: True),
        printer=lambda message: (output if output is not None else []).append(message),
        fetcher=fetcher or (lambda url: fetched[url]),
        head_fetcher=head_fetcher or (lambda _url: (200, 10)),
        sleep=lambda _seconds: None,
        clock=lambda: 0.0,
        timeout_minutes=5,
        uv_available=False,
    )


# ---------------------------------------------------------------------------
# Version discovery and mutation


def test_release_version_targets() -> None:
    assert rm.target_version(CURRENT, "beta") == NEXT_BETA
    assert rm.target_version(CURRENT, "stable") == STABLE
    assert rm.channel_for_version(NEXT_BETA) == "beta"
    assert rm.channel_for_version(STABLE) == "stable"
    assert rm.normalized_version(NEXT_BETA) == "0.1.0b2"
    assert rm.parse_release_version(CURRENT).label == "beta"

    with pytest.raises(rm.ReleaseError):
        rm.target_version(STABLE, "beta")
    with pytest.raises(rm.ReleaseError):
        rm.target_version(STABLE, "stable")
    with pytest.raises(rm.ReleaseError):
        rm.parse_release_version("not-a-version")


def test_version_references_are_discovered_validated_and_updated(tmp_path: Path) -> None:
    root = _repo(tmp_path, line_endings="\r\n")
    tracked = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]
    references = rm.validate_versions(root, CURRENT, tracked_files=tracked)
    names = {str(path.relative_to(root)) for path in references}
    assert {"version.py", "pyproject.toml", "uv.lock", "README.md"} <= names

    changed = rm.apply_version_references(root, CURRENT, NEXT_BETA, references)
    assert set(changed) == set(references)
    assert b'__version__ = "0.1.0-beta.2"\r\n' in (root / "version.py").read_bytes()
    assert 'version = "0.1.0b2"' in (root / "uv.lock").read_text(encoding="utf-8")
    assert (root / "README.md").read_bytes().count(b"\r\n") > 0
    rm.validate_versions(root, NEXT_BETA, tracked_files=tracked)


def test_version_mismatch_is_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    (root / "pyproject.toml").write_text('version = "9.9.9"\n', encoding="utf-8")
    tracked = [path.relative_to(root) for path in root.rglob("*") if path.is_file()]
    with pytest.raises(rm.ReleaseError, match="Version mismatch"):
        rm.validate_versions(root, CURRENT, tracked_files=tracked)


# ---------------------------------------------------------------------------
# Preflight safety


def test_preflight_rejects_dirty_tree(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, dirty=" M app.py\n")
    manager = _manager(root, runner)
    results, _ = manager.preflight(CURRENT, NEXT_BETA)
    assert results["Working tree"].status == rm.FAILED
    assert "uncommitted" in results["Working tree"].detail


def test_preflight_rejects_unpushed_head(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, remote_head="differentcommit")
    manager = _manager(root, runner)
    results, _ = manager.preflight(CURRENT, NEXT_BETA)
    assert results["Pushed commit"].status == rm.FAILED


def test_preflight_rejects_existing_tag(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, local_tag=True)
    manager = _manager(root, runner)
    results, _ = manager.preflight(CURRENT, NEXT_BETA)
    assert results["Release tag"].status == rm.FAILED
    assert "already exists locally" in results["Release tag"].detail


def test_preflight_passes_with_clean_git_and_gh(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root)
    manager = _manager(root, runner)
    results, references = manager.preflight(CURRENT, NEXT_BETA)
    assert not manager._failures(results)
    assert results["Test suite"].status == rm.PASSED
    assert results["Release manifest validation"].status == rm.PASSED
    assert results["Dependency lock"].status == rm.NOT_TESTED
    assert results["Packaging artifacts"].status == rm.NOT_TESTED
    assert references


def test_release_repo_is_derived_from_remote_not_update_repo(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root)
    manager = _manager(root, runner, release_repo="")
    results, _ = manager.preflight(CURRENT, NEXT_BETA)

    assert not manager._failures(results)
    assert manager.repo == TOKEN.lower()
    assert results["Git repository"].status == rm.PASSED
    assert results["GitHub CLI authentication"].status == rm.PASSED


def test_stale_local_packaging_does_not_block_release(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    release_dir = root / "dist" / "release"
    release_dir.mkdir(parents=True)
    (release_dir / "SHA256SUMS.txt").write_text(
        "\n".join(
            f"{_digest()}  {name}" for name in rm.expected_checksum_names(NEXT_BETA)
        ),
        encoding="utf-8",
    )
    manager = _manager(root, FakeRunner(root))
    results, _ = manager.preflight(CURRENT, NEXT_BETA)

    assert not manager._failures(results)
    assert results["Packaging artifacts"].status == rm.NOT_TESTED


def test_current_local_packaging_is_reported_as_checked(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    release_dir = root / "dist" / "release"
    release_dir.mkdir(parents=True)
    (release_dir / "SHA256SUMS.txt").write_text(
        "\n".join(f"{_digest()}  {name}" for name in rm.expected_checksum_names(CURRENT)),
        encoding="utf-8",
    )
    manager = _manager(root, FakeRunner(root))
    results, _ = manager.preflight(CURRENT, NEXT_BETA)

    assert results["Packaging artifacts"].status == rm.PASSED


# ---------------------------------------------------------------------------
# Publishing workflow


def test_beta_release_publishes_only_through_workflow(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, version=NEXT_BETA)
    output: list[str] = []
    manager = _manager(root, runner, version=NEXT_BETA, output=output)
    assert manager.run_release("beta") == 0

    assert f'__version__ = "{NEXT_BETA}"' in (root / "version.py").read_text(encoding="utf-8")
    assert 'version = "0.1.0b2"' in (root / "uv.lock").read_text(encoding="utf-8")
    assert ("git", "commit", "-m", f"Release v{NEXT_BETA}") in runner.calls
    assert ("git", "push", "origin", "main") in runner.calls
    assert ("git", "tag", "-a", f"v{NEXT_BETA}", "-m", f"ANIXO {NEXT_BETA}") in runner.calls
    assert ("git", "push", "origin", f"v{NEXT_BETA}") in runner.calls
    assert not any(call[:3] == ("gh", "release", "create") for call in runner.calls)
    assert any("READY FOR ANIXO UPDATER TEST" in line for line in output)


def test_stable_release_requires_explicit_phrase(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root, workflow_tag=f"v{STABLE}", version=STABLE)
    output: list[str] = []
    manager = _manager(root, runner, confirm=lambda *_a, **_k: False, output=output)
    assert manager.run_release("stable") == 130
    assert f'__version__ = "{CURRENT}"' in (root / "version.py").read_text(encoding="utf-8")
    assert not any(call[:2] == ("git", "tag") for call in runner.calls)

    runner2 = FakeRunner(root, workflow_tag=f"v{STABLE}", version=STABLE)
    phrases: list[str | None] = []

    def confirm(_prompt: str, *, phrase: str | None = None) -> bool:
        phrases.append(phrase)
        return True

    manager2 = _manager(root, runner2, version=STABLE, confirm=confirm, output=[])
    assert manager2.run_release("stable") == 0
    assert phrases == ["stable", "yes"]
    assert f'__version__ = "{STABLE}"' in (root / "version.py").read_text(encoding="utf-8")


def test_workflow_failure_does_not_create_duplicate_tag(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(
        root,
        run_states=[
            {
                "databaseId": "99",
                "status": "completed",
                "conclusion": "failure",
                "headBranch": f"v{NEXT_BETA}",
                "displayTitle": f"Release v{NEXT_BETA}",
                "url": "https://github.com/actions/runs/99",
            }
        ],
    )
    output: list[str] = []
    manager = _manager(root, runner, output=output)
    assert manager.run_release("beta") == 1
    assert runner.calls.count(("git", "push", "origin", f"v{NEXT_BETA}")) == 1
    assert not any(call[:3] == ("gh", "release", "create") for call in runner.calls)
    assert any("Do not re-tag or re-push" in line for line in output)
    assert any("Anixo release check" in line for line in output)


# ---------------------------------------------------------------------------
# Workflow monitoring and verification


def test_check_release_resumes_existing_release(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(
        root,
        workflow_tag=f"v{NEXT_BETA}",
        version=NEXT_BETA,
        local_tag=True,
        remote_tag=True,
    )
    output: list[str] = []
    manager = _manager(root, runner, version=NEXT_BETA, output=output)
    assert manager.check_release(f"v{NEXT_BETA}") == 0
    assert any("READY FOR ANIXO UPDATER TEST" in line for line in output)
    assert any("no files will be changed" in line for line in output)


def test_history_lists_local_tags_and_github_releases(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    runner = FakeRunner(root)
    output: list[str] = []
    manager = _manager(root, runner, output=output)
    assert manager.history(5) == 0
    assert any("Local tags" in line for line in output)
    assert any("GitHub Releases" in line for line in output)


def test_verification_rejects_wrong_manifest_channel(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manifest = _manifest_json(NEXT_BETA)
    manifest["channel"] = "stable"
    fetched = _fetch_map(NEXT_BETA, manifest=manifest)
    runner = FakeRunner(root, version=NEXT_BETA, workflow_tag=f"v{NEXT_BETA}")
    manager = _manager(root, runner, version=NEXT_BETA, fetcher=lambda url: fetched[url])
    results = manager.verify_release(NEXT_BETA, f"v{NEXT_BETA}")
    assert results["Release manifest"].status == rm.FAILED


def test_verification_rejects_checksum_mismatch(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    release = _release_json(NEXT_BETA)
    for asset in release["assets"]:
        if asset["name"] == "ANIXO-Setup-v0.1.0-beta.2.exe":
            asset["digest"] = "sha256:" + "cd" * 32
    runner = FakeRunner(root, version=NEXT_BETA, release_json=release)
    manager = _manager(root, runner, version=NEXT_BETA)
    results = manager.verify_release(NEXT_BETA, f"v{NEXT_BETA}")
    assert results["Checksum agreement"].status == rm.FAILED


def test_untrusted_urls_are_rejected(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    manager = _manager(root, FakeRunner(root))
    for url in (
        "http://github.com/Frederik461/anixo/releases/latest",
        "https://evil.example/release-manifest.json",
    ):
        with pytest.raises(rm.ReleaseError):
            manager._assert_trusted_url(url)
    manager._assert_trusted_url("https://github.com/Frederik461/anixo/releases/latest")


def test_sha256sums_parser_ignores_malformed_lines() -> None:
    parsed = rm.parse_sha256sums(f"{_digest()}  ANIXO.exe\nbad line\n")
    assert parsed == {"anixo.exe": _digest()}
