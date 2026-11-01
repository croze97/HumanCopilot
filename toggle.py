#!/usr/bin/env python3
"""Register / deregister human-copilot for Claude and Codex — the whole lane.

The point is "no hangups": OFF must leave nothing that lets either agent
believe the tool exists. Each client has a registration and a doctrine file:

  * Claude: ~/.claude.json plus ~/.claude/CLAUDE.md
  * Codex:  ~/.codex/config.toml plus ~/.codex/AGENTS.md (and a non-empty
            AGENTS.override.md when that is the active global instruction file)

OFF stashes each registration so ON restores customized commands and
environments exactly. A missing stash falls back to the canonical server.py
beside this file. The same managed doctrine body is installed for both agents;
the OFF body explicitly says the tool is unavailable and what to do instead.

Every write is tmp+rename in the target's own directory, preserving its mode.
The complete set is preflighted before mutation and rolled back on a failed
write. Claude and Codex can also rewrite their config files, so the remaining
read-modify-write race is real but milliseconds wide.

The control record is the runtime authority. OFF writes it first, so resident
servers immediately hide/refuse the tool even if a later config cleanup fails;
ON writes it last, only after both clients' registrations and doctrines agree.
Resident MCP servers publish tools/list_changed when that authority changes.
For clients that do not refresh dynamic tool lists, each real OFF -> ON also
rotates a harmless canonical-server argument so their config refresh creates a
fresh stdio connection instead of retaining a closed transport.

Consumers: zellij-hub's server.py imports this by path (/api/copilot,
/api/copilot/toggle — web topbar + zhub-tui), and it is a CLI:
    python3 toggle.py state|on|off
Moving or renaming this file is therefore a cross-tree change.
"""
import errno
import fcntl
import glob
import hashlib
import json
import os
import pwd
import re
import signal
import stat
import subprocess
import sys
import tomllib
from contextlib import contextmanager
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.realpath(__file__))
SERVER_NAME = "human-copilot"
STATUS_SCHEMA = "human-copilot-status"
STATUS_SCHEMA_VERSION = 1
OVERLAY_UUID = "copilot-overlay@croze"
# gnome-shell misc/extensionUtils.js ExtensionState (GNOME 50), for the
# numeric state GetExtensionInfo returns.
SHELL_EXTENSION_STATES = {1: "ACTIVE", 2: "INACTIVE", 3: "ERROR", 4: "OUT_OF_DATE",
                          5: "DOWNLOADING", 6: "INITIALIZED", 7: "DEACTIVATING",
                          8: "ACTIVATING", 99: "UNINSTALLED"}
OVERLAY_RUNTIME_FILE = "overlay-runtime.json"
OVERLAY_RUNTIME_FRESH_S = 15
TUI_RUNTIME_FILE = "tui-runtime.json"
TUI_RUNTIME_FRESH_S = 15
DELIVERY_FILE = "delivery.json"
CALLER_PROCESS_SCHEMA = "human-copilot-caller-process"
CONTROL_SCHEMA = "human-copilot-control"
CONTROL_SCHEMA_VERSION = 1
CONTROL_FILE_ENV = "HC_CONTROL_FILE"
CONTROL_FILE = "control.json"
CONTROL_LOCK_FILE = "control.lock"
CONTROL_SIGNAL = signal.SIGUSR1
CONNECTION_GENERATION_PREFIX = "--human-copilot-connection-generation="
OVERLAY_FILES = (
    "extension.js",
    "lib.js",
    "metadata.json",
    "schemas/org.gnome.shell.extensions.copilot-overlay.gschema.xml",
    "schemas/gschemas.compiled",
)
MAX_EVIDENCE_FILE_BYTES = 16 * 1024 * 1024
MARK_BEGIN = "<!-- human-copilot:doctrine begin (managed by hubs/human-copilot/toggle.py — edit doctrine-on.md, not this block) -->"
MARK_END = "<!-- human-copilot:doctrine end -->"

def claude_json_path():
    return os.path.expanduser("~/.claude.json")

def doctrine_path():
    return os.path.expanduser("~/.claude/CLAUDE.md")

def stash_path():
    return os.path.expanduser("~/.local/state/human-copilot/registration.json")

def codex_home():
    configured = os.environ.get("CODEX_HOME")
    return (os.path.abspath(os.path.expanduser(configured)) if configured
            else os.path.expanduser("~/.codex"))

def codex_config_path():
    return os.path.join(codex_home(), "config.toml")

def codex_doctrine_path():
    return os.path.join(codex_home(), "AGENTS.md")

def codex_override_path():
    return os.path.join(codex_home(), "AGENTS.override.md")

def codex_stash_path():
    return os.path.expanduser(
        "~/.local/state/human-copilot/codex-registration.toml")

def control_path():
    """Persistent authority read by every resident MCP server.

    HC_CONTROL_FILE exists for isolated tests and QA. The live default is in
    state, not the runtime spool, so OFF survives logout and reboot.
    """
    configured = os.environ.get(CONTROL_FILE_ENV)
    if configured:
        return os.path.abspath(os.path.expanduser(configured))
    return os.path.expanduser(
        os.path.join("~/.local/state/human-copilot", CONTROL_FILE))

def control_lock_path():
    return os.path.join(os.path.dirname(control_path()), CONTROL_LOCK_FILE)

def _read_control():
    """Typed control evidence. Malformed/unreadable records fail closed."""
    path = control_path()
    try:
        with open(path, "rb") as source:
            raw = source.read(8193)
    except FileNotFoundError:
        return {"state": "absent", "enabled": None, "reason": None}, None
    except OSError:
        return {"state": "unreadable", "enabled": False,
                "reason": "io-error"}, None
    if len(raw) > 8192:
        return {"state": "invalid", "enabled": False,
                "reason": "file-too-large"}, raw
    try:
        record = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return {"state": "invalid", "enabled": False,
                "reason": "malformed-json"}, raw
    if (not isinstance(record, dict)
            or record.get("schema") != CONTROL_SCHEMA
            or record.get("schema_version") != CONTROL_SCHEMA_VERSION
            or not isinstance(record.get("enabled"), bool)):
        return {"state": "invalid", "enabled": False,
                "reason": "invalid-contract"}, raw
    return {"state": "explicit", "enabled": record["enabled"],
            "reason": None, "updated_at": record.get("updated_at")}, raw

def _control_text(on):
    evidence, raw = _read_control()
    if evidence["state"] == "explicit" and evidence["enabled"] == bool(on):
        try:
            return raw.decode("utf-8")
        except (AttributeError, UnicodeDecodeError):
            pass
    return json.dumps({
        "schema": CONTROL_SCHEMA,
        "schema_version": CONTROL_SCHEMA_VERSION,
        "enabled": bool(on),
        "updated_at": datetime.now(timezone.utc).isoformat().replace(
            "+00:00", "Z"),
    }, indent=2, sort_keys=True) + "\n"

def _legacy_capability_enabled():
    """Pre-control upgrade verdict from the original four live surfaces."""
    try:
        with open(claude_json_path()) as source:
            claude = SERVER_NAME in (json.load(source).get("mcpServers") or {})
    except (OSError, ValueError, AttributeError):
        claude = False
    try:
        with open(codex_config_path()) as source:
            codex = _codex_usable(tomllib.loads(source.read()))
    except (OSError, tomllib.TOMLDecodeError):
        codex = False
    # Treat the original four surfaces as one upgrade-time authority. A stale
    # registration plus OFF doctrine is not permission to resurrect a prompt.
    return (claude and codex
            and _doctrine_state(doctrine_path()) == "on"
            and _doctrine_state(_effective_codex_doctrine_path()) == "on")

def capability_enabled(default=None):
    """Runtime authority for resident servers; invalid records fail closed.

    ``default`` may override only the pre-upgrade state for a hermetic caller.
    Normally an absent record is derived from the four legacy registration /
    doctrine surfaces, so an upgrade installed after OFF stays fail-closed.
    An explicit ON is permission, not a bypass: all four client surfaces must
    still be ready.  This also makes an out-of-band config rewrite revoke a
    resident server instead of leaving Hub saying ON over a drifted setup.
    """
    evidence, _raw = _read_control()
    if evidence["state"] == "absent":
        return (_legacy_capability_enabled() if default is None
                else bool(default))
    return bool(evidence["enabled"] and _legacy_capability_enabled())

@contextmanager
def _control_lock():
    """Serialize flips and the server's request-publication guard."""
    path = control_lock_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

def default_registration():
    """The entry to write when nothing was stashed. `timeout` is not optional
    dressing: the MCP client aborts a tool call that stays silent for its idle
    timeout (1800s by default), and an operator pass is silence by definition —
    a 2400s request died at exactly 1800s on 2026-08-27. This makes the
    doctrine's "1800-3600 is normal" actually reachable."""
    return {"type": "stdio", "command": os.path.join(HERE, "server.py"),
            "args": [], "env": {}, "timeout": 3600000}

def default_codex_registration():
    """Canonical Codex TOML. The timeout supports a human-speed pass; the
    tool approval is required when the profile's approval policy is ``never``."""
    command = json.dumps(os.path.join(HERE, "server.py"))
    return (f"[mcp_servers.{SERVER_NAME}]\n"
            f"command = {command}\n"
            "args = []\n"
            "tool_timeout_sec = 3600\n\n"
            f"[mcp_servers.{SERVER_NAME}.tools.interactive_feedback]\n"
            'approval_mode = "approve"\n')

def _atomic_write(path, text, mode=None):
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    if mode is None:
        try:
            mode = stat.S_IMODE(os.stat(path).st_mode)
        except OSError:
            mode = 0o600
    tmp = f"{path}.tmp-hc-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w") as f:
            fd = None
            f.write(text)
        os.replace(tmp, path)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

def _apply_changes(changes):
    """Apply [(path, text, new-file-mode)] in order; best-effort rollback."""
    snapshots = []
    try:
        for path, text, mode in changes:
            try:
                with open(path) as f:
                    old = f.read()
                old_mode = stat.S_IMODE(os.stat(path).st_mode)
                existed = True
            except FileNotFoundError:
                old, old_mode, existed = "", mode, False
            if existed and old == text:
                continue
            snapshots.append((path, old, old_mode, existed))
            _atomic_write(path, text, mode if not existed else old_mode)
    except BaseException as original:
        rollback_errors = []
        for path, old, old_mode, existed in reversed(snapshots):
            try:
                if existed:
                    _atomic_write(path, old, old_mode)
                else:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass
            except OSError as e:
                rollback_errors.append(f"{path}: {e}")
        extra = ("; rollback also failed: " + "; ".join(rollback_errors)
                 if rollback_errors else "")
        raise ValueError(f"toggle write failed: {original}{extra}") from original

def _doctrine_text(on):
    with open(os.path.join(HERE, "doctrine-on.md" if on else "doctrine-off.md")) as f:
        return f.read().strip()

def _doctrine_state(path):
    try:
        with open(path) as f:
            body = f.read()
    except OSError:
        return "missing"
    if MARK_BEGIN not in body or MARK_END not in body:
        return "unmarked"
    block = body.split(MARK_BEGIN, 1)[1].split(MARK_END, 1)[0]
    return "off" if "DISABLED" in block[:200] else "on"

def _effective_codex_doctrine_path():
    override = codex_override_path()
    try:
        with open(override) as f:
            if f.read().strip():
                return override
    except OSError:
        pass
    return codex_doctrine_path()

def _render_doctrine(path, on, allow_insert):
    try:
        with open(path) as f:
            body = f.read()
    except FileNotFoundError:
        if not allow_insert:
            raise ValueError(f"cannot read {path}: file does not exist")
        body = ""
    begins, ends = body.count(MARK_BEGIN), body.count(MARK_END)
    if begins != ends or begins > 1:
        raise ValueError(
            f"{path} has incomplete or duplicate human-copilot doctrine "
            "markers — refusing to guess at an edit")
    if not begins:
        if not allow_insert:
            raise ValueError(
                f"{path} has no human-copilot doctrine markers — refusing to "
                f"guess at an edit. Re-add the {MARK_BEGIN.split(' ')[0]}… block.")
        prefix = body
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return (prefix + MARK_BEGIN + "\n" + _doctrine_text(on) + "\n"
                + MARK_END + "\n")
    head, rest = body.split(MARK_BEGIN, 1)
    _, tail = rest.split(MARK_END, 1)
    return (head + MARK_BEGIN + "\n" + _doctrine_text(on) + "\n"
            + MARK_END + tail)

def _doctrine_changes(on):
    targets = [(doctrine_path(), False), (codex_doctrine_path(), True)]
    override = codex_override_path()
    try:
        with open(override) as f:
            if f.read().strip():
                targets.append((override, True))
    except FileNotFoundError:
        pass
    changes = []
    for path, allow_insert in targets:
        changes.append((path, _render_doctrine(path, on, allow_insert), 0o644))
    return changes

_ANY_TOML_TABLE = re.compile(r"^\s*\[\[?")
_HC_TOML_TABLE = re.compile(
    r'''^\s*\[\[?\s*mcp_servers\s*\.\s*'''
    r'''(?:human-copilot|"human-copilot"|'human-copilot')\s*'''
    r'''(?=\.|\]\]?)''')
_HC_TOML_ROOT = re.compile(
    r'''^\s*\[\s*mcp_servers\s*\.\s*'''
    r'''(?:human-copilot|"human-copilot"|'human-copilot')\s*\]'''
    r'''\s*(?:#.*)?$''')
_HC_TOOL_TOML_ROOT = re.compile(
    r'''^\s*\[\s*mcp_servers\s*\.\s*'''
    r'''(?:human-copilot|"human-copilot"|'human-copilot')\s*\.\s*'''
    r'''tools\s*\.\s*(?:interactive_feedback|"interactive_feedback"|'''
    r'''\'interactive_feedback\')\s*\]\s*(?:#.*)?$''')

def _parse_codex_config(text, path=None):
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"cannot parse {path or codex_config_path()}: {e}") from e

def _codex_entry(config):
    servers = config.get("mcp_servers") or {}
    entry = servers.get(SERVER_NAME) if isinstance(servers, dict) else None
    return entry if isinstance(entry, dict) else None

def _codex_active(config):
    entry = _codex_entry(config)
    return bool(entry is not None and entry.get("enabled", True) is not False)

def _codex_usable(config):
    entry = _codex_entry(config)
    if not _codex_active(config) or entry is None:
        return False
    timeout = entry.get("tool_timeout_sec")
    tools = entry.get("tools") or {}
    tool = tools.get("interactive_feedback") if isinstance(tools, dict) else None
    return (isinstance(timeout, (int, float)) and timeout >= 3600
            and isinstance(tool, dict)
            and tool.get("approval_mode") == "approve")

def _canonical_server_command(value):
    if not isinstance(value, str):
        return False
    return os.path.realpath(os.path.abspath(os.path.expanduser(value))) == \
        os.path.realpath(os.path.join(HERE, "server.py"))

def _connection_generation_args(args, generation):
    """Return canonical server args with exactly one fresh connection marker."""
    if not isinstance(args, list) or not all(isinstance(arg, str) for arg in args):
        raise ValueError("canonical Human Copilot registration args must be strings")
    kept = [arg for arg in args
            if not arg.startswith(CONNECTION_GENERATION_PREFIX)]
    return kept + [CONNECTION_GENERATION_PREFIX + generation]

def _entry_connection_generation(entry):
    """Read one canonical connection marker without modifying client config."""
    if not isinstance(entry, dict):
        return None
    args = entry.get("args")
    if not isinstance(args, list):
        return None
    generations = [
        arg[len(CONNECTION_GENERATION_PREFIX):]
        for arg in args
        if isinstance(arg, str)
        and arg.startswith(CONNECTION_GENERATION_PREFIX)
        and arg != CONNECTION_GENERATION_PREFIX
    ]
    return generations[0] if len(generations) == 1 else None

def _rotate_claude_connection(entry, generation):
    """Force supporting Claude hosts to reconnect after a genuine re-enable."""
    if generation is None or not _canonical_server_command(entry.get("command")):
        return
    entry["args"] = _connection_generation_args(entry.get("args", []), generation)

def _rotate_codex_connection(text, generation):
    """Give Codex a changed canonical stdio identity after OFF -> ON.

    Codex currently logs tools/list_changed without refreshing its tool cache.
    A changed configured argument is therefore the portable reconnect trigger.
    The canonical registration is emitted with a one-line args array; refuse a
    lossy rewrite if an operator has changed that syntax unexpectedly.
    """
    if generation is None:
        return text
    config = _parse_codex_config(text)
    entry = _codex_entry(config)
    if entry is None or not _canonical_server_command(entry.get("command")):
        return text
    desired = _connection_generation_args(entry.get("args", []), generation)
    lines = text.splitlines(keepends=True)
    root = next((i for i, line in enumerate(lines)
                 if _HC_TOML_ROOT.match(line.rstrip("\r\n"))), None)
    if root is None:
        raise ValueError("canonical Human Copilot Codex registration has no root table")
    end = root + 1
    while end < len(lines) and not _ANY_TOML_TABLE.match(lines[end]):
        end += 1
    assignment = re.compile(r"^(\s*args\s*=\s*)(.*)$")
    for i in range(root + 1, end):
        raw = lines[i].rstrip("\r\n")
        match = assignment.match(raw)
        if not match:
            continue
        try:
            parsed = tomllib.loads("args = " + match.group(2)).get("args")
        except tomllib.TOMLDecodeError as error:
            raise ValueError(
                "canonical Human Copilot Codex args must be a one-line array") from error
        if parsed != entry.get("args", []):
            raise ValueError("canonical Human Copilot Codex args are ambiguous")
        ending = "\n" if lines[i].endswith("\n") else ""
        lines[i] = match.group(1) + json.dumps(desired) + ending
        break
    else:
        lines.insert(root + 1, "args = " + json.dumps(desired) + "\n")
    out = "".join(lines)
    if _codex_entry(_parse_codex_config(out)).get("args") != desired:
        raise ValueError("could not rotate Human Copilot Codex connection")
    return out

def _remove_codex_registration(text):
    """Remove this server's root and child TOML tables, returning raw stash."""
    before = _parse_codex_config(text)
    if _codex_entry(before) is None:
        return text, None
    lines = text.splitlines(keepends=True)
    kept, removed = [], []
    i = 0
    while i < len(lines):
        if _HC_TOML_TABLE.match(lines[i]):
            j = i + 1
            while j < len(lines) and not _ANY_TOML_TABLE.match(lines[j]):
                j += 1
            removed.extend(lines[i:j])
            i = j
        else:
            kept.append(lines[i])
            i += 1
    if not removed:
        raise ValueError(
            f"{codex_config_path()} contains {SERVER_NAME!r} outside its own "
            "[mcp_servers.*] table; refusing a lossy rewrite")
    result, stash = "".join(kept), "".join(removed)
    if _codex_entry(_parse_codex_config(result)) is not None:
        raise ValueError(f"could not remove {SERVER_NAME!r} from Codex config")
    if _codex_entry(_parse_codex_config(stash, codex_stash_path())) is None:
        raise ValueError("Codex registration stash did not contain the server")
    return result, stash

def _append_codex_registration(text, registration):
    if _codex_entry(_parse_codex_config(text)) is not None:
        return text
    if not registration.endswith("\n"):
        registration += "\n"
    out = text
    if out and not out.endswith("\n"):
        out += "\n"
    if out and not out.endswith("\n\n"):
        out += "\n"
    out += registration
    if _codex_entry(_parse_codex_config(out)) is None:
        raise ValueError("Codex registration did not create the MCP server")
    return out

def _enable_codex_registration(text):
    """Enable an entry, give it an hour, and pre-approve its one safe tool."""
    config = _parse_codex_config(text)
    entry = _codex_entry(config)
    if entry is None:
        return text
    lines = text.splitlines(keepends=True)
    root = next((i for i, line in enumerate(lines)
                 if _HC_TOML_ROOT.match(line.rstrip("\r\n"))), None)
    if root is None:
        raise ValueError(
            f"{codex_config_path()} contains {SERVER_NAME!r} without a normal "
            "root table; refusing a lossy rewrite")
    end = root + 1
    while end < len(lines) and not _ANY_TOML_TABLE.match(lines[end]):
        end += 1

    def replace_value(key, value):
        pattern = re.compile(rf"^(\s*{re.escape(key)}\s*=\s*)([^#\r\n]*)(.*)$")
        for i in range(root + 1, end):
            ending = "\n" if lines[i].endswith("\n") else ""
            raw = lines[i].rstrip("\r\n")
            match = pattern.match(raw)
            if match:
                lines[i] = match.group(1) + value + match.group(3) + ending
                return True
        return False

    additions = []
    if entry.get("enabled", True) is False:
        if not replace_value("enabled", "true"):
            additions.append("enabled = true\n")
    timeout = entry.get("tool_timeout_sec")
    if not isinstance(timeout, (int, float)) or timeout < 3600:
        if not replace_value("tool_timeout_sec", "3600"):
            additions.append("tool_timeout_sec = 3600\n")
    if additions:
        lines[root + 1:root + 1] = additions
    out = "".join(lines)

    config = _parse_codex_config(out)
    entry = _codex_entry(config)
    tools = entry.get("tools") or {}
    tool = tools.get("interactive_feedback") if isinstance(tools, dict) else None
    if not isinstance(tool, dict) or tool.get("approval_mode") != "approve":
        lines = out.splitlines(keepends=True)
        tool_root = next((i for i, line in enumerate(lines)
                          if _HC_TOOL_TOML_ROOT.match(
                              line.rstrip("\r\n"))), None)
        if tool_root is None:
            if out and not out.endswith("\n"):
                out += "\n"
            if out and not out.endswith("\n\n"):
                out += "\n"
            out += (f"[mcp_servers.{SERVER_NAME}.tools.interactive_feedback]\n"
                    'approval_mode = "approve"\n')
        else:
            tool_end = tool_root + 1
            while (tool_end < len(lines)
                   and not _ANY_TOML_TABLE.match(lines[tool_end])):
                tool_end += 1
            approval = re.compile(
                r'^(\s*approval_mode\s*=\s*)([^#\r\n]*)(.*)$')
            for i in range(tool_root + 1, tool_end):
                ending = "\n" if lines[i].endswith("\n") else ""
                match = approval.match(lines[i].rstrip("\r\n"))
                if match:
                    lines[i] = (match.group(1) + '"approve"'
                                + match.group(3) + ending)
                    break
            else:
                lines.insert(tool_root + 1, 'approval_mode = "approve"\n')
            out = "".join(lines)

    enabled = _parse_codex_config(out)
    if not _codex_usable(enabled):
        raise ValueError("Codex registration could not be enabled safely")
    return out

# ------------------------------------------------------- read-only evidence

def _utc_timestamp(value=None):
    value = value or datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _io_reason(error):
    """Stable error classes only: never echo paths or file contents."""
    if isinstance(error, PermissionError):
        return "permission-denied"
    if isinstance(error, NotADirectoryError):
        return "not-a-directory"
    return "io-error"

def _read_regular_file(path):
    """Return digest metadata and bounded bytes without following a file link."""
    empty = {"state": "absent", "digest": None, "size_bytes": None}
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return empty, None
    except OSError as error:
        return {**empty, "state": "unreadable",
                "reason": _io_reason(error)}, None
    if not stat.S_ISREG(info.st_mode):
        return {**empty, "state": "unreadable",
                "reason": "not-regular-file"}, None

    flags = (os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
             | getattr(os, "O_NOATIME", 0))
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return empty, None
    except OSError as error:
        return {**empty, "state": "unreadable",
                "reason": _io_reason(error)}, None
    try:
        before = os.fstat(fd)
        if (not stat.S_ISREG(before.st_mode)
                or before.st_size > MAX_EVIDENCE_FILE_BYTES):
            reason = ("not-regular-file" if not stat.S_ISREG(before.st_mode)
                      else "file-too-large")
            return {**empty, "state": "unreadable", "reason": reason}, None
        body = bytearray()
        digest = hashlib.sha256()
        while True:
            block = os.read(fd, 1024 * 1024)
            if not block:
                break
            body.extend(block)
            digest.update(block)
            if len(body) > MAX_EVIDENCE_FILE_BYTES:
                return {**empty, "state": "unreadable",
                        "reason": "file-too-large"}, None
        after = os.fstat(fd)
        if (before.st_size != after.st_size
                or before.st_mtime_ns != after.st_mtime_ns):
            return {**empty, "state": "unreadable",
                    "reason": "changed-during-read"}, None
        return {
            "state": "present",
            "digest": "sha256:" + digest.hexdigest(),
            "size_bytes": after.st_size,
        }, bytes(body)
    except OSError as error:
        return {**empty, "state": "unreadable",
                "reason": _io_reason(error)}, None
    finally:
        os.close(fd)

def _file_evidence(path):
    evidence, _ = _read_regular_file(path)
    return evidence

def _package_evidence(root, relative_files):
    result = {
        "state": "absent",
        "digest": None,
        "file_count": len(relative_files),
        "observed_files": 0,
        "size_bytes": None,
    }
    try:
        root_info = os.stat(root)
    except FileNotFoundError:
        return result
    except OSError as error:
        return {**result, "state": "unreadable",
                "reason": _io_reason(error)}
    if not stat.S_ISDIR(root_info.st_mode):
        return {**result, "state": "unreadable",
                "reason": "not-a-directory"}

    aggregate = hashlib.sha256()
    total = 0
    states = []
    for relative in sorted(relative_files):
        item = _file_evidence(os.path.join(root, *relative.split("/")))
        states.append(item["state"])
        if item["state"] != "present":
            continue
        result["observed_files"] += 1
        total += item["size_bytes"]
        aggregate.update(relative.encode("utf-8") + b"\0")
        aggregate.update(item["digest"].encode("ascii") + b"\0")
    result["size_bytes"] = total
    if "unreadable" in states:
        return {**result, "state": "unreadable",
                "reason": "required-file-unreadable"}
    if "absent" in states:
        return {**result, "reason": "required-file-absent"}
    return {**result, "state": "present",
            "digest": "sha256:" + aggregate.hexdigest()}

def _overlay_manifest(source_root):
    path = os.path.join(source_root, "metadata.json")
    file_evidence, body = _read_regular_file(path)
    result = {
        "state": file_evidence["state"],
        "version": None,
        "uuid_matches": False,
        "shell_50_declared": False,
    }
    if body is None:
        return result
    try:
        metadata = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return {**result, "state": "unreadable",
                "reason": "invalid-json"}
    if not isinstance(metadata, dict):
        return {**result, "state": "unreadable",
                "reason": "invalid-manifest"}
    version = metadata.get("version")
    shells = metadata.get("shell-version")
    return {
        "state": "present",
        "version": (version if isinstance(version, int)
                    and not isinstance(version, bool) else None),
        "uuid_matches": metadata.get("uuid") == OVERLAY_UUID,
        "shell_50_declared": (isinstance(shells, list) and "50" in shells),
    }

def _overlay_evidence(root, installed_root):
    source_root = os.path.join(root, "overlay", OVERLAY_UUID)
    source = _package_evidence(source_root, OVERLAY_FILES)
    installed = _package_evidence(installed_root, OVERLAY_FILES)
    manifest = _overlay_manifest(source_root)
    install_type = "absent"
    install_target = None
    try:
        installed_info = os.lstat(installed_root)
        if stat.S_ISLNK(installed_info.st_mode):
            install_type = "symlink"
            try:
                install_target = ("source" if os.path.samefile(
                    installed_root, source_root) else "other")
            except OSError:
                install_target = "unreadable"
        elif stat.S_ISDIR(installed_info.st_mode):
            install_type = "directory"
            try:
                install_target = ("source" if os.path.samefile(
                    installed_root, source_root) else "other")
            except OSError:
                install_target = "unreadable"
        else:
            install_type = "other"
    except FileNotFoundError:
        pass
    except OSError:
        install_type = "unreadable"

    digests_match = (source["digest"] == installed["digest"]
                     if source["digest"] and installed["digest"] else None)
    prerequisites_met = (
        manifest["state"] == "present"
        and manifest["version"] == 1
        and manifest["uuid_matches"]
        and manifest["shell_50_declared"]
        and install_type == "symlink"
        and install_target == "source"
    )
    states = {source["state"], installed["state"], manifest["state"]}
    if "unreadable" in states or install_type == "unreadable":
        state_value = "unreadable"
    elif "absent" in states or install_type == "absent":
        state_value = "absent"
    elif prerequisites_met and digests_match:
        state_value = "current"
    else:
        state_value = "drift"
    return {
        "state": state_value,
        "digest": source["digest"],
        "source_digest": source["digest"],
        "installed_digest": installed["digest"],
        "digests_match": digests_match,
        "source_state": source["state"],
        "installed_state": installed["state"],
        "install_type": install_type,
        "install_target": install_target,
        "manifest_state": manifest["state"],
        "version": manifest["version"],
        "prerequisites_met": prerequisites_met,
    }

def _rollback_evidence(tool_root):
    result = {
        "state": "absent",
        "digest": None,
        "version": None,
        "prerequisites_met": False,
    }
    try:
        root_info = os.stat(tool_root)
    except FileNotFoundError:
        return result
    except OSError as error:
        return {**result, "state": "unreadable",
                "reason": _io_reason(error)}
    if not stat.S_ISDIR(root_info.st_mode):
        return {**result, "state": "unreadable",
                "reason": "not-a-directory"}

    pattern = os.path.join(
        tool_root, "lib", "python*", "site-packages",
        "mcp_feedback_enhanced-*.dist-info")
    distributions = sorted(path for path in glob.glob(pattern)
                           if os.path.isdir(path))
    if not distributions:
        return {**result, "reason": "distribution-absent"}
    if len(distributions) != 1:
        return {**result, "state": "unreadable",
                "reason": "ambiguous-distribution"}
    dist = distributions[0]
    name = os.path.basename(dist)
    match = re.fullmatch(
        r"mcp_feedback_enhanced-([0-9A-Za-z.+_-]{1,64})\.dist-info", name)
    if not match:
        return {**result, "state": "unreadable",
                "reason": "invalid-distribution-name"}
    dist_relative = os.path.relpath(dist, tool_root).replace(os.sep, "/")
    package = _package_evidence(tool_root, (
        "uv-receipt.toml",
        "bin/mcp-feedback-enhanced",
        "bin/interactive-feedback-mcp",
        dist_relative + "/METADATA",
        dist_relative + "/RECORD",
    ))
    return {
        "state": package["state"],
        "digest": package["digest"],
        "version": match.group(1),
        "prerequisites_met": package["state"] == "present",
    }

def _tuikit_evidence(local_path, canonical_path):
    local = _file_evidence(local_path)
    canonical = _file_evidence(canonical_path)
    matches = (local["digest"] == canonical["digest"]
               if local["digest"] and canonical["digest"] else None)
    states = {local["state"], canonical["state"]}
    if "unreadable" in states:
        state_value = "unreadable"
    elif "absent" in states:
        state_value = "absent"
    else:
        state_value = "current" if matches else "drift"
    return {
        "state": state_value,
        "digest": local["digest"],
        "local_digest": local["digest"],
        "canonical_digest": canonical["digest"],
        "digests_match": matches,
        "local_state": local["state"],
        "canonical_state": canonical["state"],
        "prerequisites_met": state_value == "current",
    }

def _private_runtime_dir(path):
    try:
        info = os.stat(path) if path else None
    except OSError:
        return False
    return bool(info and stat.S_ISDIR(info.st_mode)
                and info.st_uid == os.getuid()
                and not stat.S_IMODE(info.st_mode) & 0o077)

def _read_only_spool_path():
    override = os.environ.get("HC_SPOOL")
    if override:
        return override, "HC_SPOOL"
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if _private_runtime_dir(runtime):
        return os.path.join(runtime, "human-copilot"), "XDG_RUNTIME_DIR"
    runtime = f"/run/user/{os.getuid()}"
    if _private_runtime_dir(runtime):
        return os.path.join(runtime, "human-copilot"), "uid-runtime"
    return (os.path.expanduser("~/.local/state/human-copilot/spool"),
            "state-fallback")

def _lock_holder_state(path):
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return "unreadable"
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno in (errno.EACCES, errno.EAGAIN):
                return "held"
            return "unreadable"
        fcntl.flock(fd, fcntl.LOCK_UN)
        return "unheld"
    finally:
        os.close(fd)

def _proc_stat_identity(raw):
    if isinstance(raw, bytes):
        try:
            raw = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if not isinstance(raw, str):
        return None
    end = raw.rfind(")")
    if end < 0:
        return None
    fields = raw[end + 1:].strip().split()
    if len(fields) <= 19 or len(fields[0]) != 1 or not fields[19].isdigit():
        return None
    return fields[0], fields[19]

def _boot_id(proc_root="/proc"):
    try:
        with open(os.path.join(proc_root, "sys", "kernel", "random", "boot_id"),
                  encoding="ascii") as source:
            value = source.read(129).strip()
    except (OSError, UnicodeError):
        return None
    return value if value and len(value) <= 128 else None

def _caller_process_state(record, proc_root="/proc"):
    """alive/dead/unknown using PID + boot id + process start ticks."""
    if not isinstance(record, dict):
        return "unknown"
    pid = record.get("pid")
    boot = record.get("boot_id")
    start = record.get("start_ticks")
    if (record.get("schema") != CALLER_PROCESS_SCHEMA
            or record.get("schema_version") != 1
            or not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0
            or not isinstance(boot, str) or not boot or len(boot) > 128
            or not isinstance(start, str) or not start.isdigit()):
        return "unknown"
    current_boot = _boot_id(proc_root)
    if current_boot is None:
        return "unknown"
    if current_boot != boot:
        return "dead"
    base = os.path.join(proc_root, str(pid))
    try:
        if os.stat(base).st_uid != os.getuid():
            return "unknown"
        with open(os.path.join(base, "stat"), "rb") as source:
            raw = source.read(8193)
    except FileNotFoundError:
        return "dead"
    except OSError:
        return "unknown"
    parsed = _proc_stat_identity(raw) if len(raw) <= 8192 else None
    if parsed is None:
        return "unknown"
    state, actual_start = parsed
    return ("dead" if actual_start != start or state in ("Z", "X", "x")
            else "alive")

def _request_queue_evidence(spool):
    """Read-only actionable/answered request counts and caller liveness."""
    request_path = os.path.join(spool, "requests")
    try:
        request_names = {name for name in os.listdir(request_path)
                         if name.endswith(".json") and ".tmp" not in name}
    except FileNotFoundError:
        return {"state": "absent", "spooled": 0, "actionable": 0,
                "answered": 0, "answered_live": 0,
                "answered_orphans": 0, "answered_ambiguous": 0}
    except OSError:
        return {"state": "unreadable", "spooled": None,
                "actionable": None, "answered": None,
                "answered_live": None, "answered_orphans": None,
                "answered_ambiguous": None}
    answer_path = os.path.join(spool, "answers")
    try:
        answer_names = {name for name in os.listdir(answer_path)
                        if name.endswith(".json") and ".tmp" not in name}
    except FileNotFoundError:
        answer_names = set()
    except OSError:
        return {"state": "unreadable", "spooled": len(request_names),
                "actionable": None, "answered": None,
                "answered_live": None, "answered_orphans": None,
                "answered_ambiguous": None}
    answered_names = request_names & answer_names
    live = orphaned = ambiguous = 0
    for name in answered_names:
        _evidence, body = _read_regular_file(os.path.join(request_path, name))
        try:
            request = json.loads(body.decode("utf-8")) if body is not None else None
        except (UnicodeDecodeError, ValueError):
            request = None
        owner_state = _caller_process_state(
            request.get("caller_process") if isinstance(request, dict) else None)
        if owner_state == "alive":
            live += 1
        elif owner_state == "dead":
            orphaned += 1
        else:
            ambiguous += 1
    return {"state": "present", "spooled": len(request_names),
            "actionable": len(request_names) - len(answered_names),
            "answered": len(answered_names), "answered_live": live,
            "answered_orphans": orphaned,
            "answered_ambiguous": ambiguous}

def _run_probe(argv):
    """Live command runner for shell/extension probes. Never raises OSError
    upward on a missing binary; callers still guard timeouts explicitly."""
    return subprocess.run(argv, capture_output=True, text=True, timeout=3)

def _overlay_shell_evidence(runner=_run_probe):
    """Best-effort read of whether GNOME actually has the overlay ACTIVE.

    A copilot-toggle state that only shows overlay_runtime.state=absent
    cannot distinguish 'the overlay crashed' from 'GNOME never activated
    it' (e.g. org.gnome.shell disable-user-extensions=true left it stuck
    INITIALIZED on 2026-09-09). This probe names the cause so the fix is
    obvious. It only reads; it never enables/disables anything.
    """
    user_extensions_disabled = None
    enabled = None
    shell_state = None
    gsettings_reason = None
    extinfo_reason = None

    try:
        proc = runner(["gsettings", "get", "org.gnome.shell",
                       "disable-user-extensions"])
        if proc.returncode == 0:
            out = proc.stdout.strip()
            if out == "true":
                user_extensions_disabled = True
            elif out == "false":
                user_extensions_disabled = False
            else:
                gsettings_reason = (
                    "gsettings get org.gnome.shell disable-user-extensions "
                    f"returned unparsable output: {out!r}")
        else:
            gsettings_reason = (
                "gsettings get org.gnome.shell disable-user-extensions "
                f"exited {proc.returncode}: {(proc.stderr or '').strip()}")
    except FileNotFoundError:
        gsettings_reason = "gsettings binary not found"
    except subprocess.TimeoutExpired:
        gsettings_reason = "gsettings probe timed out"
    except OSError as exc:
        gsettings_reason = f"gsettings probe failed: {exc}"

    # Ask gnome-shell itself. `gnome-extensions info` D-Bus-activates a fresh
    # gjs org.gnome.Shell.Extensions helper per call, and each helper's
    # Settings.ReadAll leaks ~14 KB in xdg-desktop-portal-gtk; the Hub polls
    # this every few seconds (1.7 GB portal heap after 13 days, 2026-10-19).
    try:
        proc = runner(["gdbus", "call", "--session", "--dest", "org.gnome.Shell",
                       "--object-path", "/org/gnome/Shell", "--method",
                       "org.gnome.Shell.Extensions.GetExtensionInfo", OVERLAY_UUID])
        if proc.returncode == 0:
            m = re.search(r"'enabled': <(true|false)>", proc.stdout)
            if m:
                enabled = m.group(1) == "true"
            m = re.search(r"'state': <(\d+)(?:\.0)?>", proc.stdout)
            if m:
                shell_state = SHELL_EXTENSION_STATES.get(int(m.group(1)), f"STATE_{m.group(1)}")
            if enabled is None and shell_state is None:
                extinfo_reason = (
                    f"gnome-shell GetExtensionInfo has no enabled/state for "
                    f"{OVERLAY_UUID} (not installed?)")
        else:
            extinfo_reason = (
                f"gnome-shell GetExtensionInfo {OVERLAY_UUID} exited "
                f"{proc.returncode}: {(proc.stderr or '').strip()}")
    except FileNotFoundError:
        extinfo_reason = "gdbus binary not found"
    except subprocess.TimeoutExpired:
        extinfo_reason = "gnome-shell GetExtensionInfo probe timed out"
    except OSError as exc:
        extinfo_reason = f"gnome-shell GetExtensionInfo probe failed: {exc}"

    if user_extensions_disabled is True:
        state = "disabled-globally"
        reason = ("org.gnome.shell disable-user-extensions is true; run: "
                   "gsettings set org.gnome.shell disable-user-extensions "
                   "false")
    elif shell_state == "ACTIVE":
        state = "current"
        reason = None
    elif shell_state is not None:
        state = "inactive"
        reason = (f"extension {OVERLAY_UUID} is {shell_state}, not ACTIVE; "
                   f"enable it (gnome-extensions enable {OVERLAY_UUID}) or "
                   "check the shell journal")
    else:
        state = "unknown"
        reason = gsettings_reason or extinfo_reason

    return {
        "state": state,
        "enabled": enabled,
        "shell_state": shell_state,
        "user_extensions_disabled": user_extensions_disabled,
        "reason": reason,
        "prerequisites_met": state == "current",
    }

def _runtime_health_reason(runtime_health, overlay_shell):
    """Name why the render surface is unhealthy when the shell explains it."""
    if (runtime_health in ("unhealthy", "stopped")
            and overlay_shell["state"] in ("disabled-globally", "inactive")):
        return overlay_shell["reason"]
    return None

def _overlay_runtime_evidence(spool, location, observed_at):
    """Read the overlay's render heartbeat, never just its presence lock.

    The lock proves a process exists. This record proves its latest card
    construction completed. A stale/malformed record is never upgraded to
    healthy, so a crashing renderer cannot hide behind its helper process
    while requests wait unseen.
    """
    path = os.path.join(spool, OVERLAY_RUNTIME_FILE)
    file_evidence, body = _read_regular_file(path)
    queue = _request_queue_evidence(spool)
    result = {
        "state": file_evidence["state"],
        "digest": file_evidence["digest"],
        "size_bytes": file_evidence["size_bytes"],
        "location_source": location,
        "runtime_state": "unknown",
        "fresh": None,
        "observed": None,
        "reported_pending_requests": None,
        "rendered_cards": None,
        "answer_filter_version": None,
        "window_binding_version": None,
        "request_queue_state": queue["state"],
        "spooled_requests": queue["spooled"],
        "actionable_requests": queue["actionable"],
        "answered_requests": queue["answered"],
        "answered_live": queue["answered_live"],
        "answered_orphans": queue["answered_orphans"],
        "answered_ambiguous": queue["answered_ambiguous"],
        "prerequisites_met": False,
    }
    if "reason" in file_evidence:
        result["reason"] = file_evidence["reason"]
    if body is None:
        return result
    try:
        record = json.loads(body.decode("utf-8"))
        if not isinstance(record, dict):
            raise ValueError("runtime record is not an object")
        runtime_state = record.get("state")
        stamp = record.get("observed")
        if (record.get("schema") != "human-copilot-overlay-runtime"
                or record.get("schema_version") != 1
                or runtime_state not in ("healthy", "error")
                or not isinstance(stamp, (int, float))
                or isinstance(stamp, bool)):
            raise ValueError("invalid overlay runtime record")
        now = (observed_at if isinstance(observed_at, datetime)
               else datetime.now(timezone.utc))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = now.astimezone(timezone.utc).timestamp() - float(stamp)
        fresh = -5 <= age <= OVERLAY_RUNTIME_FRESH_S
        pending = record.get("pending_requests")
        rendered = record.get("rendered_cards")
        if not isinstance(pending, int) or isinstance(pending, bool) or pending < 0:
            pending = None
        if not isinstance(rendered, int) or isinstance(rendered, bool) or rendered < 0:
            rendered = None
        answer_filter_version = record.get("answer_filter_version")
        if (not isinstance(answer_filter_version, int)
                or isinstance(answer_filter_version, bool)
                or answer_filter_version < 1):
            answer_filter_version = None
        window_binding_version = record.get("window_binding_version")
        if (not isinstance(window_binding_version, int)
                or isinstance(window_binding_version, bool)
                or window_binding_version < 1):
            window_binding_version = None
    except (TypeError, ValueError, UnicodeDecodeError):
        return {**result, "state": "unreadable",
                "reason": "invalid-runtime-record"}
    result.update({
        "state": ("stale" if not fresh else
                  "current" if runtime_state == "healthy" else "error"),
        "runtime_state": runtime_state,
        "fresh": fresh,
        "observed": float(stamp),
        "reported_pending_requests": pending,
        "rendered_cards": rendered,
        "answer_filter_version": answer_filter_version,
        "window_binding_version": window_binding_version,
        "prerequisites_met": fresh,
    })
    error = record.get("error")
    if runtime_state == "error" and isinstance(error, dict):
        name = error.get("name")
        if isinstance(name, str) and name:
            result["error_name"] = name[:80]
    return result

def _tui_runtime_evidence(spool, location, observed_at, lease_held):
    """Read the global terminal TUI's paint heartbeat.

    A global lock may belong to the overlay's helper, so the lock alone does
    not establish that a terminal panel exists. The TUI writes this separate
    record after painting; status reports both signals without creating or
    repairing either one.
    """
    path = os.path.join(spool, TUI_RUNTIME_FILE)
    file_evidence, body = _read_regular_file(path)
    result = {
        "state": file_evidence["state"],
        "digest": file_evidence["digest"],
        "size_bytes": file_evidence["size_bytes"],
        "location_source": location,
        "runtime_state": "unknown",
        "fresh": None,
        "observed": None,
        "process_pid": None,
        "process_alive": None,
        "lease_held": lease_held,
        "prerequisites_met": False,
    }
    if "reason" in file_evidence:
        result["reason"] = file_evidence["reason"]
    if body is None:
        return result
    try:
        record = json.loads(body.decode("utf-8"))
        stamp = record.get("observed") if isinstance(record, dict) else None
        pid = record.get("pid") if isinstance(record, dict) else None
        if (not isinstance(record, dict)
                or record.get("schema") != "human-copilot-tui-runtime"
                or record.get("schema_version") != 1
                or record.get("state") != "healthy"
                or record.get("instance") is not None
                or not isinstance(stamp, (int, float))
                or isinstance(stamp, bool)
                or not isinstance(pid, int) or isinstance(pid, bool)
                or pid <= 0):
            raise ValueError("invalid TUI runtime record")
        now = (observed_at if isinstance(observed_at, datetime)
               else datetime.now(timezone.utc))
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)
        age = now.astimezone(timezone.utc).timestamp() - float(stamp)
        fresh = -5 <= age <= TUI_RUNTIME_FRESH_S
        try:
            os.kill(pid, 0)
            process_alive = os.stat(f"/proc/{pid}").st_uid == os.getuid()
        except OSError:
            process_alive = False
    except (TypeError, ValueError, UnicodeDecodeError):
        return {**result, "state": "unreadable",
                "reason": "invalid-runtime-record"}
    result.update({
        "state": "current" if fresh and process_alive else "stale",
        "runtime_state": "healthy",
        "fresh": fresh,
        "observed": float(stamp),
        "process_pid": pid,
        "process_alive": process_alive,
        "prerequisites_met": (fresh and process_alive
                              and lease_held is True),
    })
    return result

def _delivery_evidence(path):
    """Typed evidence for one instance's last pane/terminal spawn attempt."""
    file_evidence, body = _read_regular_file(path)
    result = {"state": file_evidence["state"],
              "digest": file_evidence["digest"],
              "size_bytes": file_evidence["size_bytes"],
              "attempt_state": None, "surface": None, "reason": None,
              "observed": None}
    if body is None:
        return result
    try:
        record = json.loads(body.decode("utf-8"))
        if (not isinstance(record, dict)
                or record.get("schema") != "human-copilot-delivery"
                or record.get("schema_version") != 1
                or record.get("state") not in
                    ("spawn-issued", "degraded", "error")):
            raise ValueError("invalid delivery record")
    except (TypeError, ValueError, UnicodeDecodeError):
        return {**result, "state": "unreadable",
                "reason": "invalid-delivery-record"}
    return {**result, "state": "present",
            "attempt_state": record["state"],
            "surface": record.get("surface"),
            "reason": record.get("reason"),
            "observed": record.get("observed")}

def _instance_locks_evidence(spool):
    """Per-instance presence locks (spool/instances/<id>/tui.lock,
    hcop-0p7.7), observed without creating anything. Returns
    (instances_state, [ {id, state, holder_state, held}, ... ])."""
    base = os.path.join(spool, "instances")
    try:
        names = sorted(os.listdir(base))
    except FileNotFoundError:
        return "absent", []
    except OSError as error:
        return "unreadable", [{"id": None, "state": "unreadable",
                               "reason": _io_reason(error),
                               "holder_state": "unknown", "held": None}]
    rows = []
    for name in names:
        path = os.path.join(base, name, "tui.lock")
        lock = _file_evidence(path)
        holder = (_lock_holder_state(path) if lock["state"] == "present"
                  else "unknown")
        rows.append({
            "id": name,
            "state": lock["state"],
            "holder_state": holder,
            "held": (True if holder == "held" else
                     False if holder == "unheld" else None),
            "delivery": _delivery_evidence(
                os.path.join(base, name, DELIVERY_FILE)),
        })
    return "present", rows

PALETTE_REGISTRY = "palette.json"          # palette.py::REGISTRY
PALETTE_HOLDERS = "palette-holders"        # palette.py::HOLDERS

def _palette_table():
    """palette.py's colour table, loaded BY PATH (this file is itself loaded
    by path from zellij-hub, where a bare `import palette` would not
    resolve). Returns None when the sibling is missing or broken: the status
    then reports slots without colours instead of failing."""
    try:
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "hc_palette_ro", os.path.join(HERE, "palette.py"))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.PALETTE
    except Exception:                       # any breakage: no colours, not no status
        return None

def _colour_for_slot(table, slot):
    if not table or not isinstance(slot, int) or slot < 1:
        return None
    entry = table[(slot - 1) % len(table)]
    return {"name": entry["name"], "hex": entry["hex"], "xterm": entry["xterm"]}

def _holder_lock_evidence(spool, holder):
    """The lease lock a server holds on palette-holders/<uuid>.lock
    (palette.py::hold). Observed only: an absent file cannot be held."""
    path = os.path.join(spool, PALETTE_HOLDERS, f"{holder}.lock")
    lock = _file_evidence(path)
    if lock["state"] == "absent":
        return "absent", False
    if lock["state"] != "present":
        return "unreadable", None
    holder_state = _lock_holder_state(path)
    return holder_state, (True if holder_state == "held" else
                          False if holder_state == "unheld" else None)

def _instances_evidence(spool, location, presence_rows):
    """The colour registry (spool/palette.json, hcop-0p7.2/0p7.5) read as
    recorded: one row per registered session with its slot number, palette
    colour, whether a live server still holds its lease and whether its own
    presence lock (instances/<id>/tui.lock) is held. Never reclaims, never
    writes, never creates the registry or the holders directory — a session
    whose server died and was not yet reclaimed by a lookup shows as
    `lock_held: false`, which is the truth about it."""
    registry, body = _read_regular_file(os.path.join(spool, PALETTE_REGISTRY))
    result = {
        "state": registry["state"],
        "digest": registry["digest"],
        "size_bytes": registry["size_bytes"],
        "location_source": location,
        "palette_registry_state": registry["state"],
        "count": 0,
        "live_count": 0,
        "sessions": [],
        "prerequisites_met": registry["state"] == "present",
    }
    if "reason" in registry:
        result["reason"] = registry["reason"]
    if body is None:
        return result
    try:
        parsed = json.loads(body.decode("utf-8"))
        sessions = parsed["sessions"]
        if not isinstance(sessions, dict):
            raise ValueError("sessions is not an object")
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return {**result, "state": "unreadable", "palette_registry_state": "unreadable",
                "reason": "corrupt-registry", "prerequisites_met": False}
    table = _palette_table()
    presence = {row["id"]: row for row in presence_rows if row.get("id")}
    rows = []
    for key in sorted(sessions):
        rec = sessions[key] if isinstance(sessions[key], dict) else {}
        slot = rec.get("slot")
        holders = rec.get("holders") if isinstance(rec.get("holders"), dict) else {}
        holder_rows = []
        for holder in sorted(holders):
            h = holders[holder] if isinstance(holders[holder], dict) else {}
            leased = bool(h.get("leased"))
            lock_state, held = (_holder_lock_evidence(spool, holder) if leased
                                else ("provisional", False))
            holder_rows.append({"holder": holder, "leased": leased,
                                "lock_state": lock_state, "held": held})
        helds = [h["held"] for h in holder_rows if h["leased"]]
        lock_held = (True if any(h is True for h in helds) else
                     None if any(h is None for h in helds) else False)
        presence_row = presence.get(key)
        rows.append({
            "id": key,
            "number": slot,
            "colour": _colour_for_slot(table, slot),
            "wrapped": (isinstance(slot, int) and table is not None
                        and slot > len(table)),
            "holders": holder_rows,
            "lock_held": lock_held,
            "presence_state": (presence_row["state"] if presence_row else "absent"),
            "presence_held": (presence_row["held"] if presence_row else False),
        })
    result["sessions"] = rows
    result["count"] = len(rows)
    result["live_count"] = sum(1 for r in rows if r["lock_held"] is True)
    return result

def _tui_lock_evidence(spool, location):
    lock = _file_evidence(os.path.join(spool, "tui.lock"))
    holder = (_lock_holder_state(os.path.join(spool, "tui.lock"))
              if lock["state"] == "present" else "unknown")
    instances_state, instances = _instance_locks_evidence(spool)
    return {
        "state": lock["state"],
        "digest": lock["digest"],
        "size_bytes": lock["size_bytes"],
        "location_source": location,
        "holder_state": holder,
        "surface_state": ("active" if holder == "held" else
                          "inactive" if holder == "unheld" else "unknown"),
        "prerequisites_met": lock["state"] == "present",
        # additive (hcop-0p7.7): the global lock above serves EVERY instance;
        # these are the per-instance surfaces. v1 fields keep their meaning.
        "instances_state": instances_state,
        "instances": instances,
        "instances_held": [row["id"] for row in instances if row["held"]],
    }

def build_status(*, root=HERE, home=None, installed_overlay=None,
                 rollback_tool=None, canonical_tuikit=None, spool=None,
                 observed_at=None, overlay_shell_runner=None):
    """Build the versioned evidence contract without writing or spawning."""
    root = os.path.abspath(os.fspath(root))
    home = os.path.abspath(os.path.expanduser(os.fspath(
        home if home is not None else "~")))
    installed_overlay = os.fspath(installed_overlay) if installed_overlay else (
        os.path.join(home, ".local", "share", "gnome-shell", "extensions",
                     OVERLAY_UUID))
    rollback_tool = os.fspath(rollback_tool) if rollback_tool else os.path.join(
        home, ".local", "share", "uv", "tools", "mcp-feedback-enhanced")
    canonical_tuikit_provided = canonical_tuikit is not None
    canonical_tuikit = (os.fspath(canonical_tuikit) if canonical_tuikit else
                        os.path.join(root, os.pardir, "zellij-hub", "tuikit.py"))
    if not canonical_tuikit_provided and not os.path.isfile(canonical_tuikit):
        canonical_tuikit = os.path.join(root, "tuikit.py")
    if spool is None:
        spool, spool_source = _read_only_spool_path()
    else:
        spool, spool_source = os.fspath(spool), "explicit"

    overlay = _overlay_evidence(root, installed_overlay)
    rollback = _rollback_evidence(rollback_tool)
    tuikit = _tuikit_evidence(os.path.join(root, "tuikit.py"), canonical_tuikit)
    tui_lock = _tui_lock_evidence(spool, spool_source)
    instances = _instances_evidence(spool, spool_source, tui_lock["instances"])
    overlay_runtime = _overlay_runtime_evidence(spool, spool_source, observed_at)
    overlay_shell = _overlay_shell_evidence(
        runner=overlay_shell_runner or _run_probe)
    tui_runtime = _tui_runtime_evidence(
        spool, spool_source, observed_at,
        tui_lock["holder_state"] == "held")
    if overlay_runtime["state"] == "error":
        runtime_health = "unhealthy"
    elif (tui_lock["surface_state"] == "active"
          and (overlay_runtime["state"] == "current"
               or tui_runtime["prerequisites_met"])):
        runtime_health = "healthy"
    elif ((overlay_runtime["actionable_requests"]
           if overlay_runtime["actionable_requests"] is not None
           else overlay_runtime["spooled_requests"]) or 0) > 0:
        # A live ACTIONABLE request plus no proven renderer is an outage even
        # when a lock-only helper makes the old presence field read active.
        # A complete answer awaiting normal caller cleanup is no longer an
        # operator prompt and must not manufacture an outage.
        runtime_health = "unhealthy"
    elif tui_lock["surface_state"] == "active":
        runtime_health = "unknown"
    else:
        runtime_health = ({"inactive": "stopped"}
                          .get(tui_lock["surface_state"], "unknown"))
    deployment_status = {
        "current": "installed-current",
        "drift": "installed-drift",
        "absent": "not-installed",
        "unreadable": "unknown",
    }[overlay["state"]]
    runtime_health_reason = _runtime_health_reason(runtime_health, overlay_shell)
    return {
        "schema": STATUS_SCHEMA,
        "schema_version": STATUS_SCHEMA_VERSION,
        "read_only": True,
        "source": "human-copilot:toggle.py state",
        "observed_at": _utc_timestamp(observed_at),
        "artifact": {
            "type": "gnome-shell-extension",
            "version": overlay["version"],
            "digest": overlay["source_digest"],
        },
        "deployed_digest": overlay["installed_digest"],
        "runtime_health": runtime_health,
        # additive (hcop-1o0): names the GNOME-side cause when the render
        # surface is unhealthy/stopped because the extension is not ACTIVE.
        "runtime_health_reason": runtime_health_reason,
        "deployment_status": deployment_status,
        "prerequisites": {
            "overlay": {"uuid": OVERLAY_UUID, "manifest_version": 1,
                        "shell_version": "50", "install_type": "symlink"},
            "rollback_tool": {"manager": "uv",
                              "package": "mcp-feedback-enhanced",
                              "entrypoints": ["mcp-feedback-enhanced",
                                              "interactive-feedback-mcp"]},
            "tuikit": {"canonical": "../zellij-hub/tuikit.py"},
            "tui_lock": {"name": "tui.lock",
                         "meaning": "operator-surface flock",
                         "instance_lock": "instances/<id>/tui.lock",
                         "instance_lock_meaning":
                             "one instance's operator-surface flock; the "
                             "global lock serves every instance",
                         # additive (hcop-0p7.6): the colour registry and
                         # the lease a server holds on its session's slot
                         "palette_registry": PALETTE_REGISTRY,
                         "palette_registry_meaning":
                             "per-session colour slots (palette.py); read as "
                             "recorded, never reclaimed or created here",
                         "palette_holder_lock": PALETTE_HOLDERS + "/<uuid>.lock",
                         "palette_holder_lock_meaning":
                             "shared flock a server keeps while its session "
                             "owns a slot; unheld = that server is gone",
                         "overlay_runtime": OVERLAY_RUNTIME_FILE,
                         "overlay_runtime_meaning":
                             "render heartbeat; a presence lock alone does "
                             "not prove cards were constructed",
                         "tui_runtime": TUI_RUNTIME_FILE,
                         "tui_runtime_meaning":
                             "global terminal paint heartbeat; a presence "
                             "lock alone does not prove a panel rendered"},
        },
        "evidence": {
            "overlay": overlay,
            "rollback_tool": rollback,
            "tuikit": tuikit,
            "tui_lock": tui_lock,
            # additive (hcop-0p7.6): every registered copilot instance with
            # its number, colour, lease and presence lock. v1 keys above are
            # untouched; a v1 reader ignoring this key reads exactly what it did.
            "instances": instances,
            "overlay_runtime": overlay_runtime,
            "tui_runtime": tui_runtime,
            # additive (hcop-1o0): GNOME-side enablement evidence for the
            # overlay extension, read best-effort via gsettings/
            # gnome-extensions; never repairs anything.
            "overlay_shell": overlay_shell,
        },
    }

# --------------------------------------------------------- live revocation

_REQUEST_FILE = re.compile(r"^[0-9a-f]{12}\.json(?:\.tmp-[0-9]+)?$")
_NUDGE_FILE = re.compile(
    r"^[0-9a-f]{12}" + re.escape(".arrival-nudge") + r"$")

def _private_owned_directory(path):
    """True only for a real, user-owned directory inaccessible to peers."""
    try:
        info = os.lstat(path)
    except OSError:
        return False
    return (stat.S_ISDIR(info.st_mode) and info.st_uid == os.getuid()
            and not stat.S_IMODE(info.st_mode) & 0o077)

def _purge_pending(spool=None, passes=3):
    """Remove only validated request/answer artifacts from a private spool.

    OFF runs this after resident servers are signalled. A few bounded passes
    close the race with a legacy process that was already publishing as the
    signal arrived; current servers cannot enter publication once the control
    record is false because they share control.lock.
    """
    if spool is None:
        spool, source = _read_only_spool_path()
    else:
        spool, source = os.fspath(spool), "explicit"
    result = {"state": "absent", "source": source, "removed": 0,
              "remaining": 0, "errors": []}
    if not os.path.exists(spool):
        return result
    if not _private_owned_directory(spool):
        return {**result, "state": "refused",
                "errors": ["spool-is-not-a-private-owned-directory"]}
    result["state"] = "purged"
    targets = (("requests", (_REQUEST_FILE, _NUDGE_FILE)),
               ("answers", (_REQUEST_FILE,)))
    for _ in range(max(1, min(int(passes), 5))):
        removed_this_pass = 0
        for dirname, patterns in targets:
            directory = os.path.join(spool, dirname)
            if not os.path.exists(directory):
                continue
            if not _private_owned_directory(directory):
                result["errors"].append(
                    f"{dirname}-is-not-a-private-owned-directory")
                continue
            try:
                entries = list(os.scandir(directory))
            except OSError:
                result["errors"].append(f"{dirname}-unreadable")
                continue
            for entry in entries:
                if not any(pattern.fullmatch(entry.name) for pattern in patterns):
                    continue
                try:
                    if entry.is_dir(follow_symlinks=False):
                        result["errors"].append(
                            f"{dirname}/{entry.name}-is-directory")
                        continue
                    os.unlink(entry.path)
                    result["removed"] += 1
                    removed_this_pass += 1
                except FileNotFoundError:
                    pass
                except OSError:
                    result["errors"].append(
                        f"{dirname}/{entry.name}-unlink-failed")
        if not removed_this_pass:
            break
    for dirname, patterns in targets:
        directory = os.path.join(spool, dirname)
        try:
            result["remaining"] += sum(
                1 for entry in os.scandir(directory)
                if any(pattern.fullmatch(entry.name) for pattern in patterns))
        except FileNotFoundError:
            pass
        except OSError:
            if f"{dirname}-unreadable" not in result["errors"]:
                result["errors"].append(f"{dirname}-unreadable")
    if result["errors"] or result["remaining"]:
        result["state"] = "degraded"
    return result

def _live_default_control_scope():
    """Legacy /proc signalling is allowed only for the real user's HOME.

    QA and selftests use a private HOME or HC_CONTROL_FILE and must never
    signal a server belonging to the live desktop.
    """
    if os.environ.get(CONTROL_FILE_ENV):
        return False
    try:
        actual = pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return False
    return os.path.realpath(os.path.expanduser("~")) == os.path.realpath(actual)

def _process_runs_server(pid, proc_root="/proc"):
    """Exact owner/cmdline proof for this tree's server.py, never substring.

    The path must be the executable itself or Python's script operand.  Merely
    opening server.py in an editor, mentioning it in ``python -c``, or passing
    it as some later program argument is not a resident MCP server and must
    never receive our signal.
    """
    if pid == os.getpid() or not isinstance(pid, int) or pid <= 1:
        return False
    base = os.path.join(proc_root, str(pid))
    try:
        if os.stat(base).st_uid != os.getuid():
            return False
        with open(os.path.join(base, "cmdline"), "rb") as source:
            raw = source.read(1024 * 1024 + 1)
        if not raw or len(raw) > 1024 * 1024:
            return False
        cwd = os.readlink(os.path.join(base, "cwd"))
    except OSError:
        return False
    wanted = os.path.realpath(os.path.join(HERE, "server.py"))

    argv = []
    for token in raw.rstrip(b"\0").split(b"\0"):
        try:
            argv.append(os.fsdecode(token))
        except (TypeError, UnicodeDecodeError):
            return False
    if not argv or not argv[0]:
        return False

    def resolved(arg):
        candidate = arg if os.path.isabs(arg) else os.path.join(cwd, arg)
        try:
            return os.path.realpath(candidate)
        except OSError:
            return None

    if resolved(argv[0]) == wanted:
        return True
    if not re.fullmatch(r"python(?:3(?:\.\d+)?)?", os.path.basename(argv[0])):
        return False

    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            index += 1
            break
        if arg in ("-c", "-m"):
            return False
        if arg in ("-W", "-X", "--check-hash-based-pycs"):
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        break
    return index < len(argv) and resolved(argv[index]) == wanted

def _resident_server_pids(proc_root="/proc"):
    try:
        names = os.listdir(proc_root)
    except OSError:
        return []
    return sorted(pid for name in names if name.isdigit()
                  for pid in (int(name),)
                  if _process_runs_server(pid, proc_root))

def _wake_resident_servers(proc_root="/proc", kill=os.kill):
    """Wake current servers; pre-revocation binaries terminate on SIGUSR1."""
    result = {"state": "isolated", "matched": 0, "signalled": 0,
              "errors": []}
    if proc_root == "/proc" and not _live_default_control_scope():
        return result
    result["state"] = "sent"
    for pid in _resident_server_pids(proc_root):
        result["matched"] += 1
        # Close the PID-reuse window between discovery and delivery.
        if not _process_runs_server(pid, proc_root):
            continue
        try:
            kill(pid, CONTROL_SIGNAL)
            result["signalled"] += 1
        except ProcessLookupError:
            pass
        except OSError:
            result["errors"].append(f"pid-{pid}-signal-failed")
    if result["errors"]:
        result["state"] = "degraded"
    return result

# ----------------------------------------------------------------- state

def state():
    """Versioned read-only evidence plus legacy registration state.

    ``registered`` remains a bool and is true only when both clients can call;
    raw per-client registration evidence remains under ``clients``.
    """
    claude_registered = False
    claude_entry = None
    try:
        with open(claude_json_path()) as f:
            claude_servers = json.load(f).get("mcpServers") or {}
            claude_registered = SERVER_NAME in claude_servers
            claude_entry = claude_servers.get(SERVER_NAME)
    except (OSError, ValueError):
        pass
    codex_registered = False
    codex_entry = None
    try:
        with open(codex_config_path()) as f:
            codex_config = tomllib.loads(f.read())
            codex_entry = _codex_entry(codex_config)
            codex_registered = _codex_usable(codex_config)
    except (OSError, tomllib.TOMLDecodeError):
        pass
    claude_doctrine = _doctrine_state(doctrine_path())
    codex_doctrine = _doctrine_state(_effective_codex_doctrine_path())
    doctrine = (claude_doctrine if claude_doctrine == codex_doctrine
                else "drift")
    registrations_ready = claude_registered and codex_registered
    control, _raw = _read_control()
    # An absent record is the one-time upgrade state. Derive it from the old
    # four-surface contract so installing this version does not unexpectedly
    # flip a coherent existing setup. Invalid records fail closed.
    enabled = capability_enabled()
    registered = registrations_ready and enabled
    consistent = (claude_registered == codex_registered
                  and claude_doctrine == codex_doctrine
                  and claude_doctrine in ("on", "off")
                  and claude_registered == (claude_doctrine == "on")
                  and control["state"] in ("absent", "explicit")
                  and enabled == registrations_ready
                  and (control["state"] != "explicit"
                       or control["enabled"] == enabled))
    status = build_status()
    # Runtime health is the capability contract, not merely whether an idle
    # overlay helper holds a lease. Intentional OFF is coherently stopped.
    if not enabled:
        status["runtime_health"] = "stopped"
        status["runtime_health_reason"] = _runtime_health_reason(
            "stopped", status["evidence"]["overlay_shell"])
    claude_generation = _entry_connection_generation(claude_entry)
    codex_generation = _entry_connection_generation(codex_entry)
    shared_generation = (claude_generation
                         if claude_generation == codex_generation else None)
    if not enabled:
        refresh_state = "disabled"
    elif shared_generation:
        refresh_state = "new-sessions-ready"
    else:
        refresh_state = "registration-generation-unavailable"
    return {
        **status,
        "registered": registered,
        "registrations_ready": registrations_ready,
        "enabled": enabled,
        "doctrine": doctrine,
        "consistent": consistent,
        "capability": {
            "state": control["state"],
            "enabled": enabled,
            "requested_enabled": control.get("enabled"),
            "explicit": control["state"] == "explicit",
            "reason": control.get("reason"),
            "updated_at": control.get("updated_at"),
        },
        # Additive client-host boundary evidence. Config changes reliably
        # affect new hosts, but Codex currently exposes no supported runtime
        # MCP reload for a host that survived an OFF -> ON transition.
        "client_refresh": {
            "state": refresh_state,
            "connection_generation": shared_generation,
            "new_sessions": "ready" if registered else "not-ready",
            "codex": {
                "runtime_reload_supported": False,
                "affected_scope": (
                    "sessions-started-before-connection-generation"),
                "required_boundary": (
                    "restart-codex-session" if enabled else None),
                "reason": (
                    "Codex has no supported runtime MCP reload; registration "
                    "changes are loaded by a new client host"),
            },
        },
        "clients": {
            "claude": {"registered": claude_registered,
                       "doctrine": claude_doctrine,
                       "connection_generation": claude_generation},
            "codex": {"registered": codex_registered,
                      "doctrine": codex_doctrine,
                      "connection_generation": codex_generation},
        },
    }

# ----------------------------------------------------------------- flip

def _swap_doctrine(on):
    """Re-sync both clients' managed doctrine blocks."""
    _apply_changes(_doctrine_changes(on))

def _client_changes(on, connection_generation=None):
    """Preflight and render every client/doctrine change without writing."""
    cj = claude_json_path()
    try:
        with open(cj) as source:
            cfg = json.load(source)
    except (OSError, ValueError) as error:
        raise ValueError(f"cannot read {cj}: {error}") from error
    servers = cfg.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise ValueError(f"cannot read {cj}: mcpServers is not an object")
    cc = codex_config_path()
    try:
        with open(cc) as source:
            codex_text = source.read()
    except FileNotFoundError:
        codex_text = ""
    codex_cfg = _parse_codex_config(codex_text, cc)

    # Both doctrine bodies are first in this ordered list; either can refuse
    # during this preflight without a partial doctrine/config write.
    changes = _doctrine_changes(on)
    labels = []
    if on and SERVER_NAME not in servers:
        entry = default_registration()
        try:
            with open(stash_path()) as source:
                entry = json.load(source)
        except (OSError, ValueError):
            pass
        _rotate_claude_connection(entry, connection_generation)
        servers[SERVER_NAME] = entry
        changes.append((cj, json.dumps(cfg, indent=2), 0o600))
        labels.append("claude registered")
    elif not on and SERVER_NAME in servers:
        entry = servers.pop(SERVER_NAME)
        changes.append((stash_path(), json.dumps(entry, indent=2), 0o600))
        changes.append((cj, json.dumps(cfg, indent=2), 0o600))
        labels.append("claude deregistered")

    codex_entry = _codex_entry(codex_cfg)
    if on:
        if codex_entry is None:
            registration = default_codex_registration()
            try:
                with open(codex_stash_path()) as source:
                    candidate = source.read()
                if _codex_entry(_parse_codex_config(
                        candidate, codex_stash_path())) is not None:
                    registration = candidate
            except (OSError, ValueError):
                pass
            new_codex = _enable_codex_registration(
                _append_codex_registration(codex_text, registration))
            new_codex = _rotate_codex_connection(
                new_codex, connection_generation)
            changes.append((cc, new_codex, 0o600))
            labels.append("codex registered")
        else:
            new_codex = _enable_codex_registration(codex_text)
            new_codex = _rotate_codex_connection(
                new_codex, connection_generation)
            if new_codex != codex_text:
                changes.append((cc, new_codex, 0o600))
                labels.append("codex enabled")
    elif codex_entry is not None:
        new_codex, raw = _remove_codex_registration(codex_text)
        changes.append((codex_stash_path(), raw, 0o600))
        changes.append((cc, new_codex, 0o600))
        labels.append("codex deregistered")
    return changes, labels

def set_enabled(on):
    """Flip clients plus the resident-server authority, always fail-closed."""
    revoked = False
    cleanup = None
    wake = None
    error = None
    changed = []
    with _control_lock():
        was_enabled = capability_enabled()
        # OFF is authoritative even when config/doctrine preflight or writing
        # fails. ON also starts from explicit false when upgrading an absent
        # control record, so an unsuccessful first enable cannot expose a
        # partially prepared lane.
        control, _raw = _read_control()
        if not on or control["state"] != "explicit":
            try:
                _atomic_write(control_path(), _control_text(False), 0o600)
            except OSError as exc:
                raise ValueError(f"cannot publish disabled control: {exc}") from exc
            revoked = True
        try:
            connection_generation = (
                datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                if on and not was_enabled else None)
            changes, changed = _client_changes(on, connection_generation)
            if on:
                # Expose only after both doctrines and both registrations.
                changes.append((control_path(), _control_text(True), 0o600))
            _apply_changes(changes)
            changed.append("resident capability " + ("enabled" if on else
                                                       "revoked"))
        except ValueError as exc:
            error = exc

    if revoked or not on:
        wake = _wake_resident_servers()
        # Current servers cannot publish across control.lock. SIGUSR1 also
        # terminates pre-revocation binaries; bounded exact-name passes remove
        # any artifact a legacy publisher had already prepared.
        cleanup = _purge_pending()
    elif on:
        wake = _wake_resident_servers()
    if error is not None:
        raise ValueError(
            f"{error}; Human Copilot remains runtime-disabled (client state "
            "may be drifted; run `copilot-toggle off` again after fixing it)")
    changed.append("claude + codex doctrine " + ("on" if on else "off"))
    if not on:
        note = ("revoked immediately in current and new Claude/Codex sessions; "
                "stale calls are refused without prompting")
    elif connection_generation:
        note = (
            "restored for new client sessions at connection generation "
            f"{connection_generation}; restart any Codex session opened before "
            "that generation because Codex has no supported runtime MCP reload")
    else:
        note = ("already enabled; no connection generation changed and existing "
                "client sessions keep their current MCP transport")
    result = {**state(), "changed": changed, "note": note}
    result["resident_servers"] = wake
    if cleanup is not None:
        result["revoked_spool"] = cleanup
    if on:
        # The flip succeeded either way; this only warns when GNOME itself
        # will keep the overlay off the operator's screen (nothing here
        # repairs it — see hcop-1o0).
        overlay_shell = result.get("evidence", {}).get("overlay_shell", {})
        if overlay_shell.get("state") in ("disabled-globally", "inactive"):
            print(f"warning: {overlay_shell.get('reason')}", file=sys.stderr)
    return result

# ----------------------------------------------------------------- CLI

def main(argv):
    cmd = argv[1] if len(argv) > 1 else "state"
    if cmd == "state":
        print(json.dumps(state(), indent=2))
    elif cmd in ("on", "off"):
        try:
            print(json.dumps(set_enabled(cmd == "on"), indent=2))
        except ValueError as e:
            print(f"refused: {e}", file=sys.stderr)
            return 1
    else:
        print("usage: toggle.py [state|on|off]", file=sys.stderr)
        return 2
    return 0

if __name__ == "__main__":
    sys.exit(main(sys.argv))
