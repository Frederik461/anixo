"""Developer-only check for ANIXO's pinned ani-cli exact-provider patch.

This script never installs, self-updates, or executes ani-cli.  It stages a
candidate in a temporary directory, verifies its SHA-256 digest, applies the
production patch to a COPY, and exercises the real argument/environment
builders used by playback.  A URL candidate is fetched over HTTPS with manual
per-hop redirect validation; local files are read-only.  Production code never
imports this module.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from contextlib import nullcontext
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import httpx

from config import Config
from services import ani_cli as ani_cli_module
from services.ani_cli import (
    AniCliInvocation,
    AniCliPatchError,
    AniCliService,
    patch_exact_provider_selection,
)
from services.setup import ANI_CLI_SHA256, ANI_CLI_URL
from services.update_log import redact_update_text
from version import APP_VERSION

_REDIRECT_STATUSES = {301, 302, 303, 307, 308}
_MAX_REDIRECTS = 5
_EPISODE = "7"
_PROVIDER_ID = "pin-check-result"


@dataclass(frozen=True, slots=True)
class PinCheck:
    """One redacted, human-readable check result."""

    name: str
    status: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class PinReport:
    """Complete result of one pin verification run."""

    digest: str
    checks: tuple[PinCheck, ...]
    reason: str = ""

    @property
    def ok(self) -> bool:
        return not self.reason and all(
            item.status == "PASSED" for item in self.checks
        )

    def lines(self) -> tuple[str, ...]:
        outcome = "PASSED" if self.ok else "FAILED"
        result = [f"ani-cli pin check: {outcome}", f"SHA256: {self.digest or '<none>'}"]
        for item in self.checks:
            detail = f" — {redact_update_text(item.detail)}" if item.detail else ""
            result.append(f"[{item.status}] {item.name}{detail}")
        if self.reason:
            result.append(f"Reason: {redact_update_text(self.reason)}")
        return tuple(result)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest().lower()


def _check(name: str, passed: bool, detail: str = "") -> PinCheck:
    return PinCheck(name, "PASSED" if passed else "FAILED", detail)


def _failed(digest: str, checks: list[PinCheck], reason: str) -> PinReport:
    return PinReport(digest, tuple(checks), reason)


def _fetch_https(url: str) -> bytes:
    """Fetch a developer-supplied candidate with HTTPS-only redirects."""
    current = str(url)
    for _ in range(_MAX_REDIRECTS + 1):
        parsed = urlparse(current)
        if (
            parsed.scheme != "https"
            or not parsed.netloc
            or parsed.username
            or parsed.password
        ):
            raise ValueError("Candidate URL must be HTTPS without userinfo.")
        with httpx.Client(
            timeout=30.0,
            follow_redirects=False,
            headers={"User-Agent": f"ANIXO/{APP_VERSION}"},
        ) as client:
            response = client.get(current)
        if response.status_code in _REDIRECT_STATUSES:
            location = str(response.headers.get("location") or "").strip()
            if not location:
                raise ValueError("Redirect response had no location.")
            current = urljoin(current, location)
            continue
        response.raise_for_status()
        return response.content
    raise ValueError("Candidate URL redirected too many times.")


def _read_candidate(
    *,
    source: bytes | None,
    path: Path | None,
    url: str,
    fetcher: Callable[[str], bytes] | None,
) -> bytes:
    if source is not None:
        return bytes(source)
    if path is not None:
        return Path(path).read_bytes()
    if url:
        parsed = urlparse(url)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("Candidate URL must be HTTPS without userinfo.")
        return bytes((fetcher or _fetch_https)(url))
    raise ValueError("A candidate source, path, or URL is required.")


def _arguments(service: AniCliService, **kwargs: Any) -> list[str] | None:
    return service.exact_arguments(**kwargs)


def _command_checks(service: AniCliService, script: Path) -> list[PinCheck]:
    invocation = AniCliInvocation("direct", "ani-cli", (), str(script))
    checks: list[PinCheck] = []
    commands: list[list[str]] = []

    sub_args = _arguments(
        service,
        exact=True,
        provider_id=_PROVIDER_ID,
        episode=_EPISODE,
        dub=False,
        resume=False,
    )
    dub_args = _arguments(
        service,
        exact=True,
        provider_id=_PROVIDER_ID,
        episode=_EPISODE,
        dub=True,
        resume=False,
    )
    resume_args = _arguments(
        service,
        resume=True,
        episode=_EPISODE,
        exact=True,
        provider_id=_PROVIDER_ID,
    )
    title_only = _arguments(
        service, exact=True, title="ignored title", episode=_EPISODE
    )

    if sub_args is None or dub_args is None or resume_args is None:
        checks.append(_check("command.build", False, "argument builder refused a valid request"))
        return checks
    commands.extend(
        service.command_arguments(invocation, args)
        for args in (sub_args, dub_args, resume_args)
    )
    checks.append(_check("command.build", True))

    checks.append(
        _check(
            "command.exact_provider_id",
            _PROVIDER_ID in sub_args,
            f"argv={sub_args!r}",
        )
    )
    checks.append(
        _check(
            "command.exact_selection",
            sub_args[1:3] == ["-S", "1"],
            f"argv={sub_args!r}",
        )
    )
    checks.append(
        _check(
            "command.exact_episode",
            sub_args[-2:] == ["-e", _EPISODE],
            f"argv={sub_args!r}",
        )
    )
    environment = service._environment(invocation, provider_id=_PROVIDER_ID)
    checks.append(
        _check(
            "command.environment",
            environment.get("ANI_CLI_PROVIDER_ID") == _PROVIDER_ID
            and environment.get("ANI_CLI_MENU") == "false",
            "ANI_CLI_PROVIDER_ID and ANI_CLI_MENU=false are set",
        )
    )
    checks.append(
        _check(
            "command.no_continue_with_exact",
            "-c" not in sub_args and "-e" not in resume_args and "-c" in resume_args,
            f"exact={sub_args!r}; resume={resume_args!r}",
        )
    )
    checks.append(
        _check(
            "command.dub_off_default",
            "--dub" not in sub_args,
            f"argv={sub_args!r}",
        )
    )
    checks.append(
        _check(
            "command.dub_only_requested",
            "--dub" in dub_args and "--dub" not in sub_args,
            f"dub={dub_args!r}; sub={sub_args!r}",
        )
    )
    checks.append(
        _check(
            "command.no_title_only_exact",
            title_only is None,
            "title-only exact playback is refused before process creation",
        )
    )
    checks.append(
        _check(
            "command.no_self_update",
            all("-U" not in command for command in commands),
            f"commands={commands!r}",
        )
    )
    return checks


def run_check(
    source: bytes | None = None,
    *,
    expected_sha256: str = ANI_CLI_SHA256,
    path: Path | None = None,
    url: str = "",
    fetcher: Callable[[str], bytes] | None = None,
    work_dir: Path | None = None,
) -> PinReport:
    """Verify one candidate without touching an installed ani-cli."""
    try:
        candidate = _read_candidate(
            source=source, path=path, url=url, fetcher=fetcher
        )
    except (OSError, ValueError, httpx.HTTPError) as error:
        return _failed("", [], f"Candidate could not be read: {error}")

    digest = _sha256(candidate)
    checks = [
        _check(
            "digest",
            digest == str(expected_sha256).strip().lower(),
            f"expected={expected_sha256}; actual={digest}",
        )
    ]
    if checks[0].status != "PASSED":
        return _failed(digest, checks, "SHA-256 mismatch; the candidate was rejected.")

    temporary = nullcontext(work_dir) if work_dir is not None else tempfile.TemporaryDirectory(
        prefix="anixo-pin-"
    )
    with temporary as raw_dir:
        root = Path(raw_dir)
        staged = root / "candidate"
        patched = root / "ani-cli-patched"
        staged.write_bytes(candidate)
        # Read the staged copy; the original path is never modified.
        text = staged.read_text(encoding="utf-8")
        search_anchor_ok = (
            ani_cli_module._SEARCH_ANCHOR in text
            or ani_cli_module._SEARCH_ANCHOR_V2 in text
        )
        checks.append(
            _check(
                "patch.search_anchor",
                search_anchor_ok,
                "search anchor present" if search_anchor_ok else "search anchor missing",
            )
        )
        checks.append(
            _check(
                "patch.menu_anchor",
                ani_cli_module._MENU_ANCHOR in text,
                "menu anchor present" if ani_cli_module._MENU_ANCHOR in text else "menu anchor missing",
            )
        )
        try:
            patched_text = patch_exact_provider_selection(text)
        except AniCliPatchError as error:
            checks.append(_check("patch.apply", False, str(error)))
            return _failed(digest, checks, str(error))
        patched.write_text(patched_text, encoding="utf-8")
        checks.append(
            _check(
                "patch.apply",
                patched.read_text(encoding="utf-8") == patched_text,
                "applied to a staging copy only",
            )
        )
        checks.append(
            _check(
                "patch.marker",
                ani_cli_module._EXACT_MARKER in patched_text,
                "exact-selection marker present",
            )
        )
        service = AniCliService(Config(ani_cli_base_url="https://example.invalid"))
        service.supports_exit_after_play = lambda: False  # type: ignore[method-assign]
        checks.extend(_command_checks(service, patched))
    return PinReport(digest, tuple(checks))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "candidate",
        nargs="?",
        default=ANI_CLI_URL,
        help="HTTPS URL or local path of the ani-cli source to verify",
    )
    parser.add_argument(
        "--expect-sha256",
        default=ANI_CLI_SHA256,
        help="expected SHA-256 digest (defaults to the ANIXO pin)",
    )
    args = parser.parse_args(argv)
    candidate = str(args.candidate)
    try:
        if candidate.startswith("https://") or candidate.startswith("http://"):
            report = run_check(
                url=candidate,
                expected_sha256=str(args.expect_sha256),
                fetcher=_fetch_https,
            )
        else:
            report = run_check(
                path=Path(candidate), expected_sha256=str(args.expect_sha256)
            )
    except (OSError, ValueError) as error:
        report = _failed("", [], f"Candidate could not be read: {error}")
    print("\n".join(report.lines()))
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())
