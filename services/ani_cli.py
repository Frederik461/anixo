"""ani-cli integration: history import, provider resolution, and playback."""

from __future__ import annotations

import asyncio
import hashlib
import html
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from config import Config, default_cache_dir, expand_path
from platforms import get_platform_adapter
from data.database import Database
from models.anime import Anime, normalize_title
from services.managed_ani_cli import ManagedAniCli

_DEFAULT_HISTORY = Path(".local/state/ani-cli/ani-hsts")
_DEFAULT_PROVIDER = "https://hianime.at"

# Only bytes whose digest is known to come from the official ani-cli project
# are patched and executed.  The v5.1 tag is what the installer downloads; the
# second digest is the official v5.1.8 master revision shipped on this machine.
_KNOWN_PROVIDER_SHA256 = {
    "F99A1DAC807004BECFB732A5518A0F2DED9DC08B467805ABD03DF5D580BECD46",
    "A87F4B0EB178ED5B7856F93670015B2BA8832F1A9E634AD608967535364E77D9",
    "60F10C12F2397811D137A76156555803D0913985FE73499DAFDA1A84771FE917",
}
_EXACT_MARKER = "# ANIXO exact provider selection"
_SEARCH_ANCHOR = '        anime_list=$(hianime_search "$query") || exit 1\n'
_SEARCH_ANCHOR_V2 = '        anime_list=$(miruro_search "$query") || exit 1\n'
_MENU_ANCHOR = 'dep_ch "$menu_program" # TODO: use dep_ch_failover to iterate through options\n'
_EXACT_FILTER = r'''        # ANIXO exact provider selection: keep only the ID chosen in ANIXO.
        if [ -n "$ANI_CLI_PROVIDER_ID" ]; then
            anime_list="$(printf "%s\n" "$anime_list" | awk -F '\t' -v id="$ANI_CLI_PROVIDER_ID" '$1 == id { print; exit }')"
            [ -n "$anime_list" ] || die "Selected provider result is no longer available"
        fi
'''
# ani-cli 5.2 emits ``AniListID:siteID:MALID`` result keys.  ANIXO stores that
# whole key. A legacy ``AniListID:siteID:`` ID still identifies the same exact
# provider result, so both forms are accepted; nothing is ever guess-matched.
_EXACT_FILTER_V2 = r'''        # ANIXO exact provider selection: keep only the exact ID chosen in ANIXO.
         if [ -n "$ANI_CLI_PROVIDER_ID" ]; then
             anime_list="$(printf "%s\n" "$anime_list" | awk -F '\t' -v id="$ANI_CLI_PROVIDER_ID" '{ split($1, part, ":"); if ($1 == id || part[1] ":" part[2] ":" == id) { print; exit } }')"
             [ -n "$anime_list" ] || die "Selected provider result is no longer available"
         fi
'''
_MENU_REPLACEMENT = (
    "# ANIXO: exact playback needs no interactive menu.\n"
    'if [ -z "$ANI_CLI_PROVIDER_ID" ] || [ -z "$ep_no" ]; then '
    'dep_ch "$menu_program"; fi\n'
)


class AniCliPatchError(ValueError):
    """The verified ani-cli source no longer matches ANIXO's exact-ID patch."""

    def __init__(self, message: str = "", *, missing_anchors: tuple[str, ...] = ()) -> None:
        self.missing_anchors = tuple(missing_anchors)
        if not message:
            detail = ", ".join(self.missing_anchors) or "unknown anchor"
            message = (
                "ani-cli patch is out of date for this ani-cli version "
                f"(missing {detail})"
            )
        super().__init__(message)


def patch_exact_provider_selection(source: str) -> str:
    """Apply the tiny exact-ID patch to a verified official ani-cli script."""
    source = source.replace("\r\n", "\n").replace("\r", "\n")
    missing: list[str] = []
    if _SEARCH_ANCHOR in source:
        anchor, exact_filter = _SEARCH_ANCHOR, _EXACT_FILTER
    elif _SEARCH_ANCHOR_V2 in source:
        anchor, exact_filter = _SEARCH_ANCHOR_V2, _EXACT_FILTER_V2
    else:
        missing.append("search anchor")
        anchor, exact_filter = "", ""
    if _MENU_ANCHOR not in source:
        missing.append("menu anchor")
    if missing:
        raise AniCliPatchError(missing_anchors=tuple(missing))
    patched = source.replace(anchor, anchor + exact_filter, 1)
    patched = patched.replace(_MENU_ANCHOR, _MENU_REPLACEMENT, 1)
    if _EXACT_MARKER not in patched:
        raise AniCliPatchError(
            "ani-cli patch is out of date for this ani-cli version "
            "(exact marker was not applied)",
            missing_anchors=("exact marker",),
        )
    return patched


def _uses_composite_provider_ids(source: str) -> bool:
    """True when this script emits ``AniListID:siteID:MALID`` result keys."""
    return _SEARCH_ANCHOR_V2 in source or "miruro_search()" in source


_PROVIDER_BASE_RE = re.compile(
    r'^\s*(?:base_api|miruro_base)=["\']([^"\']+)["\']',
    re.IGNORECASE | re.MULTILINE,
)


def provider_base_url_from_source(source: str) -> str:
    """Return the provider URL declared by a verified ani-cli script."""
    match = _PROVIDER_BASE_RE.search(source)
    if not match:
        return ""
    url = match.group(1).strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return ""
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return ""
    return url


def provider_family_from_source(source: str) -> str:
    """Return the provider family used by a verified ani-cli script."""
    if "miruro_base=" in source or "miruro_search()" in source:
        return "miruro"
    if "base_api=" in source or "hianime_search" in source:
        return "hianime"
    return ""


def sync_provider_url_from_script(
    config: Config, script: Path | None
) -> tuple[str, str, bool] | None:
    """Persist the provider declared by *script* into *config*.

    Returns ``(url, family, config_changed)``. An unreadable or unrecognised
    script fails closed and returns ``None`` without changing persisted state.
    """
    if not script or not script.exists():
        return None
    try:
        source = script.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    url = provider_base_url_from_source(source)
    if not url:
        return None
    changed = config.ani_cli_base_url.rstrip("/") != url
    if changed:
        config.ani_cli_base_url = url
    return url, provider_family_from_source(source) or "hianime", changed


@dataclass(frozen=True, slots=True)
class AniCliRecord:
    episode: str
    source_id: str
    title: str


@dataclass(frozen=True, slots=True)
class AniCliInvocation:
    kind: str
    command: str
    base_args: tuple[str, ...]
    display_path: str


def parse_cmd_wrapper(contents: str) -> dict[str, str]:
    """Extract ANI_CLI and GIT_BASH from an ani-cli.cmd wrapper."""
    ani_cli = re.search(r'set\s+"ANI_CLI=([^"]+)"', contents, re.IGNORECASE)
    git_bash = re.search(r'set\s+"GIT_BASH=([^"]+)"', contents, re.IGNORECASE)
    return {
        **({"ani_cli": ani_cli.group(1)} if ani_cli else {}),
        **({"git_bash": git_bash.group(1)} if git_bash else {}),
    }


def windows_path_to_posix(value: str) -> str:
    normalized = value.replace("\\", "/")
    drive = re.match(r"^([A-Za-z]):/(.*)$", normalized)
    if drive:
        return f"/{drive.group(1).lower()}/{drive.group(2)}"
    return normalized


def find_matching_record(
    records: list[AniCliRecord],
    title: str,
    history_id: str | None = None,
) -> AniCliRecord | None:
    """Resolve by canonical source ID, or by title only when unambiguous."""
    if history_id and history_id.startswith("ani-cli:"):
        source_id = history_id.split(":", 1)[1]
        return next((record for record in records if record.source_id == source_id), None)
    normalized = normalize_title(title)
    matches = [
        record for record in records if normalize_title(record.title) == normalized
    ]
    return matches[0] if len(matches) == 1 else None


def parse_provider_page_title(raw: str) -> str | None:
    """Clean a provider page title into the anime title."""
    value = html.unescape(raw or "").strip()
    value = re.sub(r"^Watch\s+", "", value, flags=re.IGNORECASE)
    value = re.split(r"\s*\|\s*HiAnime.*$", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.split(r"\s+[–—]\s+.*$", value, maxsplit=1)[0]
    return value.strip() or None


def record_to_anime(record: AniCliRecord) -> dict[str, Any]:
    canonical = f"ani-cli:{record.source_id}"
    return {
        "id": canonical,
        "title": {"romaji": record.title},
        "source": "ani-cli",
        "sourceId": record.source_id,
        "provider": "ani-cli",
        "providerTitle": record.title,
    }


class AniCliService:
    def __init__(
        self, config: Config, managed_dir: Path | str | None = None
    ) -> None:
        self.config = config
        self.managed = ManagedAniCli(managed_dir) if managed_dir is not None else None
        self._provider_url: str | None = None
        self._provider_family: str | None = None
        self._exit_after_play: bool | None = None

    # ------------------------------------------------------------------
    # History

    def history_path(self) -> Path:
        if self.config.ani_cli_history_path:
            return Path(expand_path(self.config.ani_cli_history_path))
        hist_dir = os.environ.get("ANI_CLI_HIST_DIR")
        if hist_dir:
            return Path(expand_path(hist_dir)) / "ani-hsts"
        state_home = os.environ.get("XDG_STATE_HOME")
        if state_home:
            return Path(expand_path(state_home)) / "ani-cli" / "ani-hsts"
        return Path.home() / _DEFAULT_HISTORY

    def parse_history(self) -> list[AniCliRecord]:
        """Parse ani-cli 3+ tab-separated history rows."""
        path = self.history_path()
        if not path.exists():
            return []
        records: list[AniCliRecord] = []
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) < 3:
                continue
            episode = parts[0].strip()
            source_id = parts[1].strip()
            title = "\t".join(parts[2:]).strip()
            if episode and source_id and title:
                records.append(AniCliRecord(episode, source_id, title))
        return records

    def import_history(self, database: Database) -> int:
        """Import provider history once, preserving canonical source identity."""
        ignored = database.ignored_sources()
        imported = 0
        for record in self.parse_history():
            if record.source_id in ignored:
                continue
            canonical = f"ani-cli:{record.source_id}"
            existing = database.get_history_entry(canonical)
            if existing:
                if existing.episode != record.episode:
                    database.update_history_episode(canonical, record.episode)
                continue
            anime = Anime.from_mapping(record_to_anime(record))
            database.record_history(
                anime,
                record.episode,
                entry_id=canonical,
                source_id=record.source_id,
                source="ani-cli",
            )
            imported += 1
        return imported

    # ------------------------------------------------------------------
    # Invocation

    def _find_git_bash(self) -> str | None:
        """Return a usable bash from the active platform adapter."""
        shell = get_platform_adapter().bash_executable(self.config.git_bash_path)
        return str(shell) if shell else None

    def installed_script(self) -> Path | None:
        invocation = self.resolve_invocation()
        if not invocation:
            return None
        if invocation.kind in {"bash", "direct", "posix"}:
            path = Path(invocation.display_path)
            return path if path.exists() else None
        if invocation.kind == "cmd":
            try:
                wrapper = parse_cmd_wrapper(
                    Path(invocation.display_path).read_text(
                        encoding="utf-8", errors="replace"
                    )
                )
            except OSError:
                return None
            script = wrapper.get("ani_cli")
            if script:
                path = Path(expand_path(script))
                return path if path.exists() else None
        return None

    def resolve_invocation(self) -> AniCliInvocation | None:
        adapter = get_platform_adapter()
        if self.managed is not None:
            state = self.managed.read()
            if state and state.script.exists():
                script = state.script.resolve()
                wrapper = state.wrapper
                if (
                    wrapper.exists()
                    and adapter.supports_cmd_wrappers()
                    and not adapter.bash_executable(self.config.git_bash_path)
                ):
                    return AniCliInvocation(
                        "cmd", "cmd.exe", (), str(wrapper.resolve())
                    )
                bash = self._find_git_bash()
                if bash:
                    return AniCliInvocation(
                        "bash",
                        bash,
                        (adapter.to_shell_path(script),),
                        str(script),
                    )
                if os.access(script, os.X_OK):
                    return AniCliInvocation("direct", str(script), (), str(script))
        configured = (
            Path(expand_path(self.config.ani_cli_path))
            if self.config.ani_cli_path
            else None
        )
        if configured:
            resolved = configured.resolve()
            suffix = configured.suffix.lower()
            if suffix in {".cmd", ".bat"}:
                if not adapter.supports_cmd_wrappers():
                    return None
                try:
                    wrapper = parse_cmd_wrapper(
                        configured.read_text(encoding="utf-8", errors="replace")
                    )
                    script = wrapper.get("ani_cli")
                    bash = wrapper.get("git_bash")
                    if script and bash:
                        script_path = Path(expand_path(script))
                        bash_path = Path(expand_path(bash))
                        if script_path.exists() and bash_path.exists():
                            return AniCliInvocation(
                                "bash",
                                str(bash_path.resolve()),
                                (adapter.to_shell_path(script_path.resolve()),),
                                str(script_path.resolve()),
                            )
                except OSError:
                    pass
                return AniCliInvocation("cmd", "cmd.exe", (), str(resolved))
            if suffix in {".exe", ".com"}:
                return AniCliInvocation("direct", str(resolved), (), str(resolved))

            bash = self._find_git_bash()
            if bash:
                return AniCliInvocation(
                    "bash",
                    bash,
                    (adapter.to_shell_path(resolved),),
                    str(resolved),
                )
            if resolved.exists() and os.access(resolved, os.X_OK):
                return AniCliInvocation("direct", str(resolved), (), str(resolved))

        bash = self._find_git_bash()
        candidates = [
            Path.home() / "ani-cli" / "ani-cli",
            adapter.tools_dir(default_cache_dir()) / "ani-cli",
            Path.home() / ".local" / "bin" / "ani-cli",
        ]
        if adapter.is_macos:
            candidates.extend(
                (Path("/opt/homebrew/bin/ani-cli"), Path("/usr/local/bin/ani-cli"))
            )
        if adapter.is_windows:
            candidates.extend(
                (
                    Path.home() / "scoop" / "apps" / "ani-cli" / "current" / "ani-cli",
                    Path.home() / "bin" / "ani-cli.cmd",
                    Path.home() / "scoop" / "shims" / "ani-cli.cmd",
                )
            )
        found = shutil.which("ani-cli")
        if found:
            candidates.append(Path(found))

        for candidate in candidates:
            if not candidate.exists():
                continue
            if candidate.suffix.lower() in {".cmd", ".bat"}:
                if adapter.supports_cmd_wrappers():
                    return AniCliInvocation(
                        "cmd", "cmd.exe", (), str(candidate.resolve())
                    )
                continue
            if bash:
                return AniCliInvocation(
                    "bash",
                    bash,
                    (adapter.to_shell_path(candidate.resolve()),),
                    str(candidate.resolve()),
                )
            if os.access(candidate, os.X_OK):
                return AniCliInvocation(
                    "direct", str(candidate.resolve()), (), str(candidate.resolve())
                )
        return None

    @staticmethod
    def _quote_cmd(value: str) -> str:
        escaped = (
            value.replace("^", "^^")
            .replace("&", "^&")
            .replace("|", "^|")
            .replace("<", "^<")
            .replace(">", "^>")
            .replace('"', '""')
        )
        return f'"{escaped}"'

    def command_arguments(
        self, invocation: AniCliInvocation, arguments: list[str]
    ) -> list[str]:
        if invocation.kind == "cmd":
            joined = " ".join(
                self._quote_cmd(item) for item in (invocation.display_path, *arguments)
            )
            return ["/d", "/s", "/c", joined]
        return [*invocation.base_args, *arguments]

    def _command(self) -> list[str]:
        """Compatibility helper used by older callers/tests."""
        invocation = self.resolve_invocation()
        if not invocation:
            return []
        return [invocation.command, *self.command_arguments(invocation, [])]

    def supports_exit_after_play(self) -> bool:
        """True only when the installed script advertises the flag.

        This avoids passing an unknown option to older ani-cli releases.
        """
        if self._exit_after_play is None:
            script = self.installed_script()
            supported = False
            if script:
                try:
                    supported = "--exit-after-play" in script.read_text(
                        encoding="utf-8", errors="replace"
                    )
                except OSError:
                    supported = False
            self._exit_after_play = supported
        return self._exit_after_play

    # ------------------------------------------------------------------
    # Exact provider playback

    def exact_script(self) -> Path | None:
        """Write and return a verified, exact-ID-patched ani-cli copy."""
        source = self.installed_script()
        if not source:
            return None
        try:
            payload = source.read_bytes()
        except OSError:
            return None
        digest = hashlib.sha256(payload).hexdigest().upper()
        managed_digest = ""
        if self.managed is not None:
            state = self.managed.read()
            if state and state.script.resolve() == source.resolve():
                managed_digest = state.sha256.upper()
        if digest not in _KNOWN_PROVIDER_SHA256 and digest != managed_digest:
            return None
        try:
            patched = patch_exact_provider_selection(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None
        encoded = patched.encode("utf-8")
        target_dir = default_cache_dir() / "runtime"
        target = target_dir / f"ani-cli-exact-{digest[:8]}"
        try:
            if not target.exists() or target.read_bytes() != encoded:
                target_dir.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(f".{os.getpid()}.tmp")
                temporary.write_bytes(encoded)
                temporary.replace(target)
                try:
                    target.chmod(0o700)
                except OSError:
                    pass
        except OSError:
            return None
        return target

    def exact_invocation(self, script: Path) -> AniCliInvocation | None:
        """Return an invocation that runs the verified exact-ID script."""
        adapter = get_platform_adapter()
        bash = adapter.bash_executable(self.config.git_bash_path)
        if not bash:
            if adapter.is_windows:
                return None
            resolved = script.resolve()
            return AniCliInvocation("direct", str(resolved), (), str(resolved))
        resolved = script.resolve()
        return AniCliInvocation(
            "bash",
            str(bash),
            (adapter.to_shell_path(resolved),),
            str(resolved),
        )

    # ------------------------------------------------------------------
    # Provider title resolution

    def provider_base_url(self) -> str:
        """Return the provider URL declared by the installed ani-cli revision."""
        if self._provider_url:
            return self._provider_url
        declared = ""
        script = self.installed_script()
        if script:
            try:
                declared = provider_base_url_from_source(
                    script.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                declared = ""
        self._provider_url = (
            declared or self.config.ani_cli_base_url or _DEFAULT_PROVIDER
        ).rstrip("/")
        return self._provider_url

    def provider_family(self) -> str:
        """Return ``miruro`` or ``hianime`` for the installed ani-cli revision."""
        if self._provider_family:
            return self._provider_family
        declared = ""
        script = self.installed_script()
        if script:
            try:
                declared = provider_family_from_source(
                    script.read_text(encoding="utf-8", errors="replace")
                )
            except OSError:
                declared = ""
        self._provider_family = declared or "hianime"
        return self._provider_family

    def sync_provider_url(self, script: Path | None = None) -> bool:
        """Adopt the provider declared by the installed ani-cli revision.

        The installed script is authoritative, so this runs at startup and is
        never gated on a network update check. Returns ``True`` only when the
        persisted config actually changed.
        """
        result = sync_provider_url_from_script(
            self.config, script or self.installed_script()
        )
        if not result:
            return False
        url, family, changed = result
        self._provider_url = url
        self._provider_family = family
        return changed

    async def resolve_source_title(self, source_id: str) -> str | None:
        numeric = re.search(r"(\d+)$", source_id)
        if not numeric:
            return None
        url = f"{self.provider_base_url()}/{numeric.group(1)}"
        try:
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
                response = await client.get(
                    url, headers={"User-Agent": "ANIXO/0.1.0"}
                )
                response.raise_for_status()
        except httpx.HTTPError:
            return None
        html_text = response.text
        og = re.search(
            r'<meta\s+property=["\']og:title["\']\s+content=["\']([^"\']+)["\']',
            html_text,
            re.IGNORECASE,
        )
        match = og or re.search(
            r"<title[^>]*>(.*?)</title>", html_text, re.IGNORECASE | re.DOTALL
        )
        return parse_provider_page_title(html.unescape(match.group(1))) if match else None

    # ------------------------------------------------------------------
    # Process control

    def _environment(
        self,
        invocation: AniCliInvocation,
        *,
        provider_id: str | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ)
        environment["TERM"] = environment.get("TERM", "xterm-256color")
        player = expand_path(self.config.mpv_path)
        if player:
            environment["ANI_CLI_PLAYER"] = (
                get_platform_adapter().to_shell_path(player)
                if invocation.kind == "bash"
                else player
            )
        if provider_id:
            composite = False
            script = self.installed_script()
            if script:
                try:
                    script_text = script.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    script_text = ""
                composite = _uses_composite_provider_ids(script_text)
            # The provider row ID is exact for both layouts: a HiAnime slug or a
            # composite AniListID:siteID:MALID key.  Never weaken it to a title match.
            environment["ANI_CLI_PROVIDER_ID"] = provider_id
            if composite:
                # ANIXO's ID filter is authoritative; title matching stays off.
                environment["ANI_CLI_EXACT"] = "0"
            environment["ANI_CLI_MENU"] = "false"
            environment["ANI_CLI_NO_DETACH"] = "1"
        return environment

    def exact_arguments(
        self,
        *,
        title: str | None = None,
        source_id: str | None = None,
        episode: str | None = None,
        resume: bool = False,
        dub: bool = False,
        exact: bool = False,
        provider_id: str | None = None,
    ) -> list[str] | None:
        """Build the real argument list for one playback request.

        ``None`` means the request is refused before any process is created:
        exact playback without a provider ID would let ani-cli open its picker.
        The dev pin checker uses this same builder for its assertions.
        """
        if exact and not resume and not provider_id:
            return None
        arguments: list[str] = []
        if resume:
            arguments.append("-c")
        else:
            arguments.append(title or provider_id or source_id or "")
            if provider_id:
                arguments.extend(["-S", "1"])
        if dub:
            arguments.append("--dub")
        if episode and not resume:
            arguments.extend(["-e", str(episode)])
        if exact and self.supports_exit_after_play():
            arguments.append("--exit-after-play")
        return arguments

    async def spawn(
        self,
        *,
        title: str | None = None,
        source_id: str | None = None,
        episode: str | None = None,
        resume: bool = False,
        dub: bool = False,
        exact: bool = False,
        provider_id: str | None = None,
    ) -> asyncio.subprocess.Process | None:
        """Spawn ani-cli with inherited stdio so the terminal stays visible.

        With ``provider_id``, a checksum-verified official script is patched in
        ANIXO's runtime cache to accept that exact result ID.  ``-S 1`` pins the
        single filtered result and ``ANI_CLI_MENU=false`` makes any attempt to
        open ani-cli's picker fail instead of prompting the user.  ``-c`` and
        ``-e`` are mutually exclusive.
        """
        invocation = self.resolve_invocation()
        if not invocation:
            return None
        arguments = self.exact_arguments(
            title=title,
            source_id=source_id,
            episode=episode,
            resume=resume,
            dub=dub,
            exact=exact,
            provider_id=provider_id,
        )
        if arguments is None:
            return None
        if provider_id and not resume:
            script = self.exact_script()
            if script is None:
                return None
            exact_invocation = self.exact_invocation(script)
            if exact_invocation is None:
                return None
            invocation = exact_invocation

        command = self.command_arguments(invocation, arguments)
        try:
            return await asyncio.create_subprocess_exec(
                invocation.command,
                *command,
                cwd=str(Path.home()),
                env=self._environment(invocation, provider_id=provider_id),
                stdin=None,
                stdout=None,
                stderr=None,
            )
        except OSError:
            return None

    async def launch(
        self,
        source_id: str | None,
        episode: str | None = None,
        *,
        title: str | None = None,
        resume: bool = False,
        dub: bool = False,
        exact: bool = False,
        provider_id: str | None = None,
    ) -> bool:
        """Launch and wait without suspending Textual (for non-interactive use)."""
        process = await self.spawn(
            title=title,
            source_id=source_id,
            episode=episode,
            resume=resume,
            dub=dub,
            exact=exact,
            provider_id=provider_id,
        )
        if process is None:
            return False
        await process.wait()
        return process.returncode == 0

    async def update(self) -> bool:
        """Refuse ani-cli's self-updater.

        ``ani-cli -U`` can replace the checksum-verified script with an
        unpatched build and silently disable ANIXO's exact-provider support.
        Updates are handled by the checksum-pinned dependency manager instead.
        """
        return False
