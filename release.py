#!/usr/bin/env python3
"""Build, validate, promote, and inspect immutable Human Copilot releases.

The build contains both the stable LIVE overlay identity and a release-specific
QA overlay identity.  QA deployments are private directories with their own
stdio MCP configuration, state, and spool.  Promotion copies the already-tested
payload and switches a stable symlink; it never invokes the builder.
"""

from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

import tomllib

RELEASE_SCHEMA = "human-copilot-release-set/v1"
STATUS_SCHEMA = "human-copilot-release-status/v1"
VALIDATION_SCHEMA = "human-copilot-release-validation/v1"
LIVE_UUID = "copilot-overlay@croze"
LIVE_SCHEMA = "org.gnome.shell.extensions.copilot-overlay"

CORE_FILES = (
    "server.py",
    "responder.py",
    "publisher_lifecycle.py",
    "toggle.py",
    "palette.py",
    "open_window.py",
    "doctrine-on.md",
    "doctrine-off.md",
    "release.py",
)
TUI_FILES = ("tui.py", "tuikit.py")
VALIDATION_FILES = (
    "tests/drive_overlay.js",
    "tests/crop_vectors.js",
    "tests/vectors/crop-arithmetic.json",
)
OVERLAY_FILES = (
    "extension.js",
    "lib.js",
    "palette.js",
    "metadata.json",
    f"schemas/{LIVE_SCHEMA}.gschema.xml",
)
EXECUTABLE_FILES = {"server.py", "responder.py", "toggle.py", "open_window.py", "release.py", "tui.py"}
QA_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,48}$")
DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class ReleaseError(RuntimeError):
    """A refusal or integrity failure safe to show to an operator."""


def _utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _sha_bytes(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _canonical(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def _manifest_digest(manifest: dict) -> str:
    return _sha_bytes(_canonical(manifest))


def _json_read(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ReleaseError(f"{path} must contain a JSON object")
    return value


def _atomic_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.parent / f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()


def _atomic_json(path: Path, value: object, mode: int = 0o600) -> None:
    _atomic_bytes(
        path, json.dumps(value, indent=2, sort_keys=True).encode() + b"\n", mode
    )


@contextlib.contextmanager
def _locked(state_root: Path):
    state_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = state_root / ".release.lock"
    with lock_path.open("a+b") as handle:
        os.chmod(lock_path, 0o600)
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


@contextlib.contextmanager
def _read_locked(state_root: Path, dry_run: bool):
    """_locked for a mutating run; for a rehearsal, a lock that CREATES nothing.

    _locked mkdirs the state root and opens .release.lock 'a+b', so calling it
    from --dry-run would leave a directory behind on a desktop that has never
    built a release — a rehearsal whose first act is a write. When the lock
    file already exists a rehearsal still takes it, so it cannot read a
    candidate mid-promotion; when it does not, there is no state root, hence no
    candidate, and the run refuses on the next line anyway."""
    if not dry_run:
        with _locked(state_root):
            yield
        return
    lock_path = state_root / ".release.lock"
    if not lock_path.exists():
        yield
        return
    with lock_path.open("rb") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def _default_state_root() -> Path:
    base = os.environ.get("XDG_STATE_HOME")
    return (
        Path(base) / "human-copilot-release"
        if base
        else Path.home() / ".local/state/human-copilot-release"
    )


def _default_live_root() -> Path:
    base = os.environ.get("XDG_DATA_HOME")
    return (
        Path(base) / "human-copilot"
        if base
        else Path.home() / ".local/share/human-copilot"
    )


def _default_legacy_source_root(source_file: Path | None = None) -> Path:
    """Return the installed source root trusted for a first promotion.

    Release commands normally run from ``.qa/worktrees/qa`` while the legacy
    launchers intentionally still point at the registered LIVE workspace. A
    default of ``release.py``'s own directory therefore rejects the exact
    symlinks the documented source gate creates. ``HC_LIVE_ROOT`` is the
    explicit override used by portfolio tooling; otherwise recognise only the
    managed ``<root>/.qa/worktrees/<name>`` shape and escape to ``<root>``.
    """
    configured = os.environ.get("HC_LIVE_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    here = Path(source_file or __file__).resolve().parent
    if here.parent.name == "worktrees" and here.parent.parent.name == ".qa":
        return here.parent.parent.parent.resolve()
    return here


def _run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    input_text: str | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            argv,
            cwd=cwd,
            env=env,
            input=input_text,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ReleaseError(f"could not run {argv[0]}: {exc}") from exc


def _require_success(result: subprocess.CompletedProcess, label: str) -> None:
    if result.returncode:
        detail = (result.stderr or result.stdout or "no output").strip()[-1200:]
        raise ReleaseError(f"{label} failed ({result.returncode}): {detail}")


def _source_git(source: Path, allow_dirty: bool) -> dict:
    rev = _run(["git", "rev-parse", "HEAD"], cwd=source)
    _require_success(rev, "source revision lookup")
    status_result = _run(
        ["git", "status", "--porcelain", "--untracked-files=normal"], cwd=source
    )
    _require_success(status_result, "source cleanliness check")
    dirty_lines = [line for line in status_result.stdout.splitlines() if line]
    if dirty_lines and not allow_dirty:
        raise ReleaseError(
            "source is dirty; commit the release inputs or use --allow-dirty for a non-promotable QA build"
        )
    return {
        "revision": rev.stdout.strip(),
        "dirty": bool(dirty_lines),
        "dirty_paths": [line[3:] if len(line) > 3 else line for line in dirty_lines],
    }


def _source_inputs(source: Path) -> dict[str, bytes]:
    paths = list(CORE_FILES + TUI_FILES + VALIDATION_FILES)
    paths.extend(f"overlay/{LIVE_UUID}/{item}" for item in OVERLAY_FILES)
    result = {}
    for rel in paths:
        path = source / rel
        try:
            result[rel] = path.read_bytes()
        except OSError as exc:
            raise ReleaseError(f"missing release input {path}: {exc}") from exc
    return result


def _tree_digest(files: dict[str, bytes]) -> str:
    records = {name: _sha_bytes(data) for name, data in sorted(files.items())}
    return _sha_bytes(_canonical(records))


def _write_payload_file(root: Path, rel: str, data: bytes, mode: int) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    path.write_bytes(data)
    path.chmod(mode)


def _qa_overlay(inputs: dict[str, bytes], token: str) -> tuple[dict[str, bytes], dict]:
    qa_uuid = f"copilot-overlay-qa-{token}@croze"
    qa_schema = f"org.gnome.shell.extensions.copilot-overlay.qa{token}"
    qa_spool = f"human-copilot-qa-{token}"
    prefix = f"overlay/{LIVE_UUID}/"
    metadata = json.loads(inputs[prefix + "metadata.json"].decode())
    metadata["uuid"] = qa_uuid
    metadata["settings-schema"] = qa_schema
    metadata["name"] = f"Human Copilot QA {token}"

    extension = inputs[prefix + "extension.js"].decode()
    old_spool = "GLib.get_user_runtime_dir(), 'human-copilot'"
    if extension.count(old_spool) != 1:
        raise ReleaseError("extension.js no longer has exactly one LIVE spool literal")
    extension = extension.replace(
        old_spool, f"GLib.get_user_runtime_dir(), '{qa_spool}'"
    )

    xml = inputs[prefix + f"schemas/{LIVE_SCHEMA}.gschema.xml"].decode()
    if xml.count(f'id="{LIVE_SCHEMA}"') != 1:
        raise ReleaseError("LIVE schema id was not found exactly once")
    xml = xml.replace(f'id="{LIVE_SCHEMA}"', f'id="{qa_schema}"')
    live_path = 'path="/org/gnome/shell/extensions/copilot-overlay/"'
    qa_path = f'path="/org/gnome/shell/extensions/copilot-overlay/qa{token}/"'
    if xml.count(live_path) != 1:
        raise ReleaseError("LIVE schema path was not found exactly once")
    xml = xml.replace(live_path, qa_path)

    files = {
        "extension.js": extension.encode(),
        "lib.js": inputs[prefix + "lib.js"],
        "palette.js": inputs[prefix + "palette.js"],
        "metadata.json": (
            json.dumps(metadata, indent=2, sort_keys=True) + "\n"
        ).encode(),
        f"schemas/{qa_schema}.gschema.xml": xml.encode(),
    }
    return files, {
        "extension_uuid": qa_uuid,
        "settings_schema": qa_schema,
        "spool_name": qa_spool,
    }


def _compile_schema(schema_dir: Path) -> None:
    result = _run(["glib-compile-schemas", "--strict", str(schema_dir)])
    _require_success(result, f"schema compilation in {schema_dir}")


def _payload_records(root: Path) -> dict[str, dict]:
    records = {}
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        rel = path.relative_to(root).as_posix()
        records[rel] = {
            "digest": _sha_file(path),
            "mode": stat.S_IMODE(path.stat().st_mode),
            "size": path.stat().st_size,
        }
    return records


def _member_digest(records: dict[str, dict], files: list[str]) -> str:
    return _sha_bytes(_canonical({name: records[name] for name in sorted(files)}))


def _build_tar(payload: Path, destination: Path, records: dict[str, dict]) -> None:
    with tarfile.open(destination, mode="w", format=tarfile.PAX_FORMAT) as archive:
        for rel, record in sorted(records.items()):
            data = (payload / rel).read_bytes()
            info = tarfile.TarInfo(rel)
            info.size = len(data)
            info.mode = record["mode"]
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = 0
            import io

            archive.addfile(info, io.BytesIO(data))


def build_release(source: Path, state_root: Path, *, allow_dirty: bool = False) -> dict:
    source = source.resolve()
    # Dirty/incomplete-source refusals should not create a state root or lock.
    preflight_git = _source_git(source, allow_dirty)
    preflight_inputs = _source_inputs(source)
    preflight_digest = _tree_digest(preflight_inputs)
    started_at = _utc_now()
    with _locked(state_root):
        git = _source_git(source, allow_dirty)
        inputs = _source_inputs(source)
        source_digest = _tree_digest(inputs)
        if (
            git["revision"] != preflight_git["revision"]
            or git["dirty"] != preflight_git["dirty"]
            or source_digest != preflight_digest
        ):
            raise ReleaseError("source changed while the build was acquiring its lock")
        token = source_digest.split(":", 1)[1][:12]
        work = Path(tempfile.mkdtemp(prefix="build-", dir=state_root))
        try:
            payload = work / "payload"
            for rel in CORE_FILES + TUI_FILES + VALIDATION_FILES:
                mode = 0o755 if rel in EXECUTABLE_FILES else 0o644
                _write_payload_file(payload, rel, inputs[rel], mode)

            live_base = f"overlay/{LIVE_UUID}"
            for item in OVERLAY_FILES:
                _write_payload_file(
                    payload, f"{live_base}/{item}", inputs[f"{live_base}/{item}"], 0o644
                )
            _compile_schema(payload / live_base / "schemas")

            qa_files, qa = _qa_overlay(inputs, token)
            qa_base = f"overlay/{qa['extension_uuid']}"
            for rel, data in qa_files.items():
                _write_payload_file(payload, f"{qa_base}/{rel}", data, 0o644)
            _compile_schema(payload / qa_base / "schemas")

            records = _payload_records(payload)
            groups = {
                "core": list(CORE_FILES),
                "tui": list(TUI_FILES),
                "validation": list(VALIDATION_FILES),
                "overlay_live": sorted(
                    name for name in records if name.startswith(live_base + "/")
                ),
                "overlay_qa": sorted(
                    name for name in records if name.startswith(qa_base + "/")
                ),
            }
            members = {
                name: {"digest": _member_digest(records, files), "files": files}
                for name, files in groups.items()
            }
            release_basis = {
                "schema": RELEASE_SCHEMA,
                "source_revision": git["revision"],
                "source_tree_digest": source_digest,
                "source_dirty": git["dirty"],
                "promotable": not git["dirty"],
                "members": {
                    name: value["digest"] for name, value in sorted(members.items())
                },
                "live": {
                    "extension_uuid": LIVE_UUID,
                    "settings_schema": LIVE_SCHEMA,
                    "spool_name": "human-copilot",
                },
                "qa": qa,
            }
            release_digest = _sha_bytes(_canonical(release_basis))
            release_id = release_digest.split(":", 1)[1]
            tar_path = work / "release.tar"
            _build_tar(payload, tar_path, records)
            finished_at = _utc_now()
            manifest = {
                "schema": RELEASE_SCHEMA,
                "release_id": release_id,
                "release_digest": release_digest,
                "source_revision": git["revision"],
                "source_tree_digest": source_digest,
                "source_root": str(source),
                "source_dirty": git["dirty"],
                "promotable": not git["dirty"],
                "build_started_at": started_at,
                "build_finished_at": finished_at,
                "archive": {
                    "file": "release.tar",
                    "digest": _sha_file(tar_path),
                    "size": tar_path.stat().st_size,
                    "format": "normalized-pax-tar",
                },
                "files": records,
                "members": members,
                "live": {
                    "extension_uuid": LIVE_UUID,
                    "settings_schema": LIVE_SCHEMA,
                    "spool_name": "human-copilot",
                },
                "qa": qa,
            }
            object_dir = state_root / "artifacts" / release_id
            reused = object_dir.exists()
            if reused:
                existing = _json_read(object_dir / "manifest.json")
                _verify_manifest(existing)
                existing_archive_digest = _sha_file(object_dir / "release.tar")
                if (
                    existing.get("release_digest") != release_digest
                    or existing_archive_digest != manifest["archive"]["digest"]
                    or existing_archive_digest
                    != existing.get("archive", {}).get("digest")
                ):
                    raise ReleaseError(f"immutable artifact collision at {object_dir}")
                manifest = existing
            else:
                candidate = work / "object"
                candidate.mkdir()
                shutil.copy2(tar_path, candidate / "release.tar")
                _atomic_json(candidate / "manifest.json", manifest)
                object_dir.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                try:
                    os.rename(candidate, object_dir)
                except FileExistsError:
                    existing = _json_read(object_dir / "manifest.json")
                    _verify_manifest(existing)
                    existing_archive_digest = _sha_file(object_dir / "release.tar")
                    if (
                        existing.get("release_digest") != release_digest
                        or existing_archive_digest != manifest["archive"]["digest"]
                        or existing_archive_digest
                        != existing.get("archive", {}).get("digest")
                    ):
                        raise ReleaseError(
                            f"concurrent artifact collision at {object_dir}"
                        )
                    manifest = existing
                    reused = True
            receipt = {
                "schema": "human-copilot-build-receipt/v1",
                "release_digest": release_digest,
                "source_revision": git["revision"],
                "source_tree_digest": source_digest,
                "source_dirty": git["dirty"],
                "promotable": not git["dirty"],
                "build_started_at": started_at,
                "build_finished_at": finished_at,
                "artifact_dir": str(object_dir),
                "artifact_reused": reused,
                "archive_digest": manifest["archive"]["digest"],
                "manifest_digest": _manifest_digest(manifest),
            }
            event = (
                state_root
                / "builds"
                / f"{finished_at.replace(':', '')}-{uuid.uuid4().hex}.json"
            )
            _atomic_json(event, receipt)
            _atomic_json(state_root / "last-build.json", receipt)
            return receipt
        finally:
            shutil.rmtree(work, ignore_errors=True)


def _verify_manifest(manifest: dict) -> None:
    if manifest.get("schema") != RELEASE_SCHEMA:
        raise ReleaseError(f"unsupported manifest schema {manifest.get('schema')!r}")
    required = (
        "release_digest",
        "source_revision",
        "source_tree_digest",
        "source_root",
        "source_dirty",
        "promotable",
        "files",
        "members",
        "archive",
        "live",
        "qa",
    )
    if any(key not in manifest for key in required):
        raise ReleaseError("release manifest is incomplete")
    if not DIGEST_RE.fullmatch(str(manifest["release_digest"])):
        raise ReleaseError("manifest release digest is invalid")
    release_id = manifest["release_digest"].split(":", 1)[1]
    if manifest.get("release_id") != release_id:
        raise ReleaseError("manifest release id does not match its digest")
    if not isinstance(manifest["source_dirty"], bool) or not isinstance(
        manifest["promotable"], bool
    ):
        raise ReleaseError("manifest cleanliness fields must be booleans")
    if not all(
        isinstance(manifest[key], dict)
        for key in ("files", "members", "archive", "live", "qa")
    ):
        raise ReleaseError("manifest structured fields have invalid types")
    if (
        manifest["archive"].get("file") != "release.tar"
        or not DIGEST_RE.fullmatch(str(manifest["archive"].get("digest")))
        or not isinstance(manifest["archive"].get("size"), int)
    ):
        raise ReleaseError("manifest archive record is invalid")
    for channel, fields in {
        "live": ("extension_uuid", "settings_schema", "spool_name"),
        "qa": ("extension_uuid", "settings_schema", "spool_name"),
    }.items():
        if not all(isinstance(manifest[channel].get(field), str) for field in fields):
            raise ReleaseError(f"manifest {channel} channel identity is invalid")
    for rel, record in manifest["files"].items():
        if (
            not isinstance(rel, str)
            or rel.startswith("/")
            or ".." in Path(rel).parts
            or not isinstance(record, dict)
            or not DIGEST_RE.fullmatch(str(record.get("digest")))
            or not isinstance(record.get("mode"), int)
            or not 0 <= record["mode"] <= 0o777
            or not isinstance(record.get("size"), int)
            or record["size"] < 0
        ):
            raise ReleaseError(f"manifest file record is invalid: {rel!r}")
    if manifest["promotable"] is not (not manifest["source_dirty"]):
        raise ReleaseError(
            "manifest promotion eligibility disagrees with source cleanliness"
        )
    covered = []
    for name, member in manifest["members"].items():
        if not isinstance(name, str) or not isinstance(member, dict):
            raise ReleaseError("manifest member record has an invalid type")
        files = member.get("files")
        if not isinstance(files, list) or not all(
            isinstance(item, str) and item in manifest["files"] for item in files
        ):
            raise ReleaseError(f"manifest member {name!r} has an invalid file set")
        if _member_digest(manifest["files"], files) != member.get("digest"):
            raise ReleaseError(f"manifest member {name!r} digest mismatch")
        covered.extend(files)
    if len(covered) != len(set(covered)) or set(covered) != set(manifest["files"]):
        raise ReleaseError(
            "manifest members do not cover each payload file exactly once"
        )
    basis = {
        "schema": RELEASE_SCHEMA,
        "source_revision": manifest["source_revision"],
        "source_tree_digest": manifest["source_tree_digest"],
        "source_dirty": manifest["source_dirty"],
        "promotable": manifest["promotable"],
        "members": {
            name: value["digest"] for name, value in sorted(manifest["members"].items())
        },
        "live": manifest["live"],
        "qa": manifest["qa"],
    }
    if _sha_bytes(_canonical(basis)) != manifest["release_digest"]:
        raise ReleaseError(
            "release manifest digest does not match its immutable members"
        )


def _load_artifact(path: Path) -> tuple[dict, Path]:
    path = path.resolve()
    manifest = _json_read(path / "manifest.json")
    _verify_manifest(manifest)
    archive = path / manifest["archive"]["file"]
    if _sha_file(archive) != manifest["archive"]["digest"]:
        raise ReleaseError("release archive digest mismatch")
    return manifest, archive


def _safe_extract(archive_path: Path, destination: Path, manifest: dict) -> None:
    expected = manifest["files"]
    seen = set()
    with tarfile.open(archive_path, mode="r:") as archive:
        for member in archive.getmembers():
            if (
                not member.isfile()
                or member.name.startswith("/")
                or ".." in Path(member.name).parts
            ):
                raise ReleaseError(f"unsafe archive member {member.name!r}")
            if member.name not in expected or member.name in seen:
                raise ReleaseError(f"unexpected archive member {member.name!r}")
            source = archive.extractfile(member)
            if source is None:
                raise ReleaseError(f"cannot read archive member {member.name!r}")
            data = source.read()
            record = expected[member.name]
            if len(data) != record["size"] or _sha_bytes(data) != record["digest"]:
                raise ReleaseError(f"archive member digest mismatch: {member.name}")
            _write_payload_file(destination, member.name, data, int(record["mode"]))
            seen.add(member.name)
    if seen != set(expected):
        missing = sorted(set(expected) - seen)
        raise ReleaseError(f"archive lacks manifest members: {missing}")


def _verify_release_dir(release_dir: Path, manifest: dict) -> None:
    expected = manifest["files"]
    actual = set()
    for path in release_dir.rglob("*"):
        if path.is_symlink():
            raise ReleaseError(f"release contains symlink {path}")
        if "__pycache__" in path.relative_to(release_dir).parts:
            # Derived bytecode, never a member: a deployed server that imports
            # a sibling module (palette.py) may write it even though every
            # launcher sets PYTHONDONTWRITEBYTECODE. Any other stray file is
            # still a mismatch.
            continue
        if path.is_file():
            rel = path.relative_to(release_dir).as_posix()
            actual.add(rel)
            record = expected.get(rel)
            if (
                not record
                or path.stat().st_size != record["size"]
                or _sha_file(path) != record["digest"]
            ):
                raise ReleaseError(f"installed release member mismatch: {rel}")
    if actual != set(expected):
        raise ReleaseError("installed release file set does not match manifest")


def _artifact_from_last_build(state_root: Path) -> Path:
    receipt = _json_read(state_root / "last-build.json")
    return Path(receipt["artifact_dir"])


def _mcp_handshake(server: Path, spool: Path, state: Path | None = None) -> dict:
    env = os.environ.copy()
    env["HC_SPOOL"] = str(spool)
    env["PYTHONDONTWRITEBYTECODE"] = "1"   # the release tree must stay byte-exact
    if state is not None:
        env["XDG_STATE_HOME"] = str(state)
    messages = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "release-validator", "version": "1"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
    ]
    payload = "".join(
        json.dumps(item, separators=(",", ":")) + "\n" for item in messages
    )
    result = _run(
        [sys.executable, str(server)], env=env, input_text=payload, timeout=15
    )
    _require_success(result, "QA MCP handshake")
    replies = []
    for line in result.stdout.splitlines():
        with contextlib.suppress(json.JSONDecodeError):
            replies.append(json.loads(line))
    ids = {item.get("id") for item in replies if isinstance(item, dict)}
    if not {1, 2}.issubset(ids):
        raise ReleaseError(
            "QA MCP handshake did not return initialize and tools/list replies"
        )
    return {"state": "passed", "reply_ids": [1, 2]}


def _qa_paths(state_root: Path, qa_root: Path | None) -> Path:
    return qa_root.resolve() if qa_root else state_root / "qa"


def deploy_qa(
    state_root: Path,
    *,
    artifact_dir: Path | None = None,
    qa_root: Path | None = None,
    qa_id: str | None = None,
) -> dict:
    qa_root = _qa_paths(state_root, qa_root)
    with _locked(state_root):
        selected_build = None
        if artifact_dir is None:
            selected_build = _json_read(state_root / "last-build.json")
            artifact_dir = Path(selected_build["artifact_dir"])
        manifest, archive = _load_artifact(artifact_dir)
        manifest_digest = _manifest_digest(manifest)
        if (
            selected_build is not None
            and selected_build.get("manifest_digest") != manifest_digest
        ):
            raise ReleaseError("last-build receipt does not bind the selected manifest")
        digest_token = manifest["release_digest"].split(":", 1)[1][:12]
        qa_id = qa_id or f"qa-{digest_token}"
        if not QA_ID_RE.fullmatch(qa_id):
            raise ReleaseError(
                "QA id must be 1-49 lowercase letters, numbers, or hyphens"
            )
        final = qa_root / "deployments" / qa_id
        if final.exists():
            receipt = _json_read(final / "deployment.json")
            if receipt.get("release_digest") != manifest["release_digest"]:
                raise ReleaseError(
                    f"QA id {qa_id!r} is already bound to a different release"
                )
            _verify_release_dir(final / "release", manifest)
            return {**receipt, "idempotent": True}
        final.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        work = Path(tempfile.mkdtemp(prefix=f".{qa_id}-", dir=final.parent))
        try:
            release_dir = work / "release"
            _safe_extract(archive, release_dir, manifest)
            _verify_release_dir(release_dir, manifest)
            for rel in ("state", "spool", "xdg-data/gnome-shell/extensions"):
                (work / rel).mkdir(parents=True, mode=0o700)
            server_name = f"human-copilot-{qa_id}"
            final_server = final / "release/server.py"
            final_spool = final / "spool"
            final_state = final / "state"
            final_data = final / "xdg-data"
            qa_env = {
                "HC_SPOOL": str(final_spool),
                "XDG_STATE_HOME": str(final_state),
                "XDG_DATA_HOME": str(final_data),
                "PYTHONDONTWRITEBYTECODE": "1",
            }
            claude = {
                "mcpServers": {
                    server_name: {
                        "type": "stdio",
                        "command": str(final_server),
                        "args": [],
                        "env": qa_env,
                        "timeout": 3600000,
                    }
                }
            }
            _atomic_json(work / "mcp/claude.json", claude)
            codex = (
                f'[mcp_servers."{server_name}"]\n'
                f"command = {json.dumps(str(final_server))}\n"
                f"env = {{ HC_SPOOL = {json.dumps(str(final_spool))}, XDG_STATE_HOME = {json.dumps(str(final_state))}, XDG_DATA_HOME = {json.dumps(str(final_data))} }}\n"
                "tool_timeout_sec = 3600\nstartup_timeout_sec = 10\n"
            )
            _atomic_bytes(work / "mcp/codex.toml", codex.encode(), 0o600)
            qa_extension_link = (
                work
                / "xdg-data/gnome-shell/extensions"
                / manifest["qa"]["extension_uuid"]
            )
            os.symlink(
                final / "release/overlay" / manifest["qa"]["extension_uuid"],
                qa_extension_link,
            )
            handshake = _mcp_handshake(
                release_dir / "server.py", work / "spool", work / "state"
            )
            deployed_at = _utc_now()
            receipt = {
                "schema": "human-copilot-qa-deployment/v1",
                "qa_id": qa_id,
                "release_digest": manifest["release_digest"],
                "manifest_digest": manifest_digest,
                "source_revision": manifest["source_revision"],
                "qa_deployed_at": deployed_at,
                "deployed_at": deployed_at,
                "deployed_digest": manifest["release_digest"],
                "deployed_revision": manifest["source_revision"],
                "deployment_status": f"isolated QA {qa_id}",
                "deployment_root": str(final),
                "release_root": str(final / "release"),
                "state_root": str(final_state),
                "spool_root": str(final_spool),
                "mcp_address": f"stdio:{final_server}",
                "mcp_server_name": server_name,
                "claude_config": str(final / "mcp/claude.json"),
                "codex_config": str(final / "mcp/codex.toml"),
                "extension_root": str(
                    final / "release/overlay" / manifest["qa"]["extension_uuid"]
                ),
                "extension_install_root": str(
                    final_data
                    / "gnome-shell/extensions"
                    / manifest["qa"]["extension_uuid"]
                ),
                "extension_uuid": manifest["qa"]["extension_uuid"],
                "settings_schema": manifest["qa"]["settings_schema"],
                "handshake": handshake,
            }
            _atomic_json(work / "deployment.json", receipt)
            os.rename(work, final)
            _atomic_json(state_root / "last-qa.json", receipt)
            return {**receipt, "idempotent": False}
        finally:
            if work.exists():
                shutil.rmtree(work, ignore_errors=True)


def _deployment(
    state_root: Path, qa_root: Path | None, qa_id: str | None
) -> tuple[Path, dict, dict]:
    root = _qa_paths(state_root, qa_root)
    if qa_id is None:
        qa_id = _json_read(state_root / "last-qa.json").get("qa_id")
    if not isinstance(qa_id, str) or not QA_ID_RE.fullmatch(qa_id):
        raise ReleaseError("a valid QA id is required")
    deployment = root / "deployments" / qa_id
    receipt = _json_read(deployment / "deployment.json")
    manifest_path = (
        state_root
        / "artifacts"
        / receipt["release_digest"].split(":", 1)[1]
        / "manifest.json"
    )
    manifest = _json_read(manifest_path)
    _verify_manifest(manifest)
    if manifest["release_digest"] != receipt["release_digest"]:
        raise ReleaseError("QA deployment and artifact manifest digests disagree")
    if receipt.get("manifest_digest") != _manifest_digest(manifest):
        raise ReleaseError("QA deployment does not bind the exact artifact manifest")
    return deployment, receipt, manifest


_JS_RELATIVE_IMPORT_RE = re.compile(
    r"""(?:\bfrom|\bimport)\s*\(?\s*['"](\.\.?/[^'"]*)['"]"""
)


def _relative_js_imports(text: str) -> list[str]:
    """Every './x.js' or '../x.js' an ES module asks for, in source order."""
    return [match.group(1) for match in _JS_RELATIVE_IMPORT_RE.finditer(text)]


def _check_qa_imports(qa_base: Path) -> dict:
    """Assert every relative import of the QA extension names a payload file.

    ``qa-extension-parse`` cannot do this: gjs resolves imports in order and
    aborts at the FIRST failure, always ``resource:///org/gnome/shell/ui/main.js``,
    reporting an ImportError that this project documents as the clean signal.
    A genuinely missing sibling module is therefore invisible there, so the
    payload is checked statically instead -- no gjs, no display.
    """
    before = time.monotonic()
    missing: list[str] = []
    scanned = 0
    try:
        base = qa_base.resolve()
        for path in sorted(qa_base.rglob("*.js")):
            scanned += 1
            text = path.read_text(encoding="utf-8", errors="replace")
            for target in _relative_js_imports(text):
                resolved = (path.parent / target).resolve()
                inside = resolved == base or base in resolved.parents
                if not resolved.is_file() or not inside:
                    missing.append(
                        f"{path.relative_to(qa_base).as_posix()} -> {target}"
                    )
    except OSError as exc:
        return {
            "name": "qa-extension-imports",
            "state": "failed",
            "duration_ms": round((time.monotonic() - before) * 1000),
            "detail": f"QA extension payload could not be read: {exc}",
        }
    record = {
        "name": "qa-extension-imports",
        "duration_ms": round((time.monotonic() - before) * 1000),
        "scanned_files": scanned,
    }
    if not scanned:
        record["state"] = "failed"
        record["detail"] = "the QA extension payload contains no JavaScript"
    elif missing:
        record["state"] = "failed"
        record["detail"] = (
            "QA extension imports a module absent from its payload: "
            + ", ".join(missing)
        )
    else:
        record["state"] = "passed"
    return record


def _check(
    name: str,
    argv: list[str],
    *,
    cwd: Path | None = None,
    env: dict | None = None,
    allow_nonzero: bool = False,
) -> dict:
    before = time.monotonic()
    result = _run(argv, cwd=cwd, env=env, timeout=60)
    output = ((result.stdout or "") + (result.stderr or "")).strip()[-800:]
    passed = result.returncode == 0
    if allow_nonzero:
        passed = "SyntaxError" not in output and "SyntaxError" not in (
            result.stderr or ""
        )
    return {
        "name": name,
        "state": "passed" if passed else "failed",
        "exit_code": result.returncode,
        "duration_ms": round((time.monotonic() - before) * 1000),
        "output_tail": output,
    }


def validate_qa(
    state_root: Path, *, qa_root: Path | None = None, qa_id: str | None = None
) -> dict:
    with _locked(state_root):
        deployment, receipt, manifest = _deployment(state_root, qa_root, qa_id)
        release_dir = deployment / "release"
        checks = []
        try:
            _verify_release_dir(release_dir, manifest)
            checks.append({"name": "release-integrity", "state": "passed"})
        except ReleaseError as exc:
            checks.append(
                {"name": "release-integrity", "state": "failed", "detail": str(exc)}
            )

        with tempfile.TemporaryDirectory(prefix="hcop-pycache-") as cache:
            env = os.environ.copy()
            env["PYTHONPYCACHEPREFIX"] = cache
            py_files = [
                str(release_dir / rel)
                for rel in CORE_FILES + TUI_FILES
                if rel.endswith(".py")
            ]
            checks.append(
                _check(
                    "python-compile",
                    [sys.executable, "-m", "py_compile", *py_files],
                    env=env,
                )
            )

        live_schema_dir = release_dir / f"overlay/{LIVE_UUID}/schemas"
        qa_schema_dir = (
            release_dir / "overlay" / manifest["qa"]["extension_uuid"] / "schemas"
        )
        checks.append(
            _check(
                "live-schema",
                ["glib-compile-schemas", "--strict", "--dry-run", str(live_schema_dir)],
            )
        )
        checks.append(
            _check(
                "qa-schema",
                ["glib-compile-schemas", "--strict", "--dry-run", str(qa_schema_dir)],
            )
        )
        checks.append(
            _check(
                "overlay-logic",
                ["gjs", "-m", "tests/drive_overlay.js"],
                cwd=release_dir,
            )
        )
        qa_extension = qa_schema_dir.parent / "extension.js"
        checks.append(
            _check(
                "qa-extension-parse",
                ["gjs", "-m", str(qa_extension)],
                allow_nonzero=True,
            )
        )
        checks.append(_check_qa_imports(qa_schema_dir.parent))

        try:
            handshake = _mcp_handshake(
                release_dir / "server.py", deployment / "spool", deployment / "state"
            )
            checks.append({"name": "mcp-handshake", **handshake})
        except ReleaseError as exc:
            checks.append(
                {"name": "mcp-handshake", "state": "failed", "detail": str(exc)}
            )

        try:
            live_base = release_dir / f"overlay/{LIVE_UUID}"
            qa_base = release_dir / "overlay" / manifest["qa"]["extension_uuid"]
            metadata = json.loads((qa_base / "metadata.json").read_text())
            extension = (qa_base / "extension.js").read_text()
            if metadata.get("uuid") != manifest["qa"]["extension_uuid"]:
                raise ReleaseError("QA UUID does not match manifest")
            if metadata.get("settings-schema") != manifest["qa"]["settings_schema"]:
                raise ReleaseError("QA settings schema does not match manifest")
            if manifest["qa"]["spool_name"] not in extension:
                raise ReleaseError("QA extension is not bound to its QA spool")
            for shared in ("lib.js", "palette.js"):
                live_bytes = (live_base / shared).read_bytes()
                if live_bytes != (qa_base / shared).read_bytes():
                    raise ReleaseError(f"QA changes runtime library bytes ({shared})")
            checks.append({"name": "channel-isolation", "state": "passed"})
        except (OSError, ValueError, ReleaseError) as exc:
            checks.append(
                {"name": "channel-isolation", "state": "failed", "detail": str(exc)}
            )

        passed = all(item["state"] == "passed" for item in checks)
        validation_path = deployment / "validation.json"
        visual = {"state": "pending", "attested_at": None}
        overall = "failed" if not passed else "pending-visual"
        record = {
            "schema": VALIDATION_SCHEMA,
            "qa_id": receipt["qa_id"],
            "release_digest": manifest["release_digest"],
            "artifact_digest": manifest["release_digest"],
            "manifest_digest": receipt["manifest_digest"],
            "source_revision": receipt["source_revision"],
            "automated": {
                "state": "passed" if passed else "failed",
                "checked_at": _utc_now(),
                "checks": checks,
            },
            "visual": visual,
            "state": overall,
            "status": "failed" if not passed else "pending",
            "passed": False,
            "validated_at": None,
            "test_run_id": f"hcop-{uuid.uuid4().hex}",
        }
        _atomic_json(validation_path, record)
        _atomic_json(state_root / "last-qa.json", receipt)
        candidate_path = state_root / "candidate.json"
        if candidate_path.exists():
            candidate = _json_read(candidate_path)
            if candidate.get("qa_id") == receipt["qa_id"]:
                candidate_path.unlink()
        return record


def attest_visual(
    state_root: Path,
    *,
    qa_root: Path | None,
    qa_id: str | None,
    release_digest: str,
    result: str,
    actor: str,
    note: str,
) -> dict:
    if not actor.strip() or not note.strip():
        raise ReleaseError(
            "visual attestation requires a named actor and concrete note"
        )
    with _locked(state_root):
        deployment, receipt, manifest = _deployment(state_root, qa_root, qa_id)
        if (
            release_digest != receipt["release_digest"]
            or release_digest != manifest["release_digest"]
        ):
            raise ReleaseError(
                "visual attestation digest does not match the QA deployment"
            )
        validation = _json_read(deployment / "validation.json")
        if validation.get("release_digest") != release_digest:
            raise ReleaseError("automated validation is bound to another digest")
        if validation.get("manifest_digest") != receipt["manifest_digest"]:
            raise ReleaseError("automated validation is bound to another manifest")
        if validation.get("automated", {}).get("state") != "passed":
            raise ReleaseError(
                "automated validation must pass before visual attestation"
            )
        visual = {
            "state": result,
            "actor": actor.strip(),
            "note": note.strip(),
            "attested_at": _utc_now(),
            "release_digest": release_digest,
            "manifest_digest": receipt["manifest_digest"],
        }
        validation["visual"] = visual
        validation["state"] = "passed" if result == "passed" else "failed"
        validation["status"] = "passed" if result == "passed" else "failed"
        validation["passed"] = result == "passed"
        validation["validated_at"] = _utc_now() if result == "passed" else None
        _atomic_json(deployment / "validation.json", validation)
        _atomic_json(state_root / "last-qa.json", receipt)
        candidate = state_root / "candidate.json"
        if result == "passed":
            _atomic_json(
                candidate,
                {
                    "schema": "human-copilot-promotion-candidate/v1",
                    "qa_id": receipt["qa_id"],
                    "release_digest": release_digest,
                    "manifest_digest": receipt["manifest_digest"],
                    "validated_at": validation["validated_at"],
                    "test_run_id": validation["test_run_id"],
                    "validation_path": str(deployment / "validation.json"),
                },
            )
        elif candidate.exists():
            current = _json_read(candidate)
            if current.get("qa_id") == receipt["qa_id"]:
                candidate.unlink()
        return validation


def _link_state(path: Path) -> dict:
    if path.is_symlink():
        return {"kind": "symlink", "target": os.readlink(path)}
    if path.exists():
        return {"kind": "other"}
    return {"kind": "absent"}


def _restore_link(path: Path, state: dict) -> None:
    if state["kind"] == "other":
        return
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        raise ReleaseError(f"cannot restore over non-symlink {path}")
    if state["kind"] == "symlink":
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(state["target"], path)


def _atomic_link(path: Path, target: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.parent / f".{path.name}.{uuid.uuid4().hex}.link"
    os.symlink(target, temp)
    try:
        os.replace(temp, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            temp.unlink()


def _file_snapshot(path: Path) -> dict:
    if not path.exists():
        return {"exists": False}
    if not path.is_file() or path.is_symlink():
        raise ReleaseError(f"registration path is not a regular file: {path}")
    return {
        "exists": True,
        "data": path.read_bytes(),
        "mode": stat.S_IMODE(path.stat().st_mode),
    }


def _restore_file(path: Path, snapshot: dict) -> None:
    if snapshot["exists"]:
        _atomic_bytes(path, snapshot["data"], snapshot["mode"])
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


def _rewrite_claude(data: bytes, command: str) -> tuple[bytes, dict]:
    try:
        value = json.loads(data.decode())
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ReleaseError(f"Claude config is invalid JSON: {exc}") from exc
    entry = (
        value.get("mcpServers", {}).get("human-copilot")
        if isinstance(value, dict)
        else None
    )
    if not isinstance(entry, dict):
        return data, {"state": "disabled", "previous_command": None}
    previous = entry.get("command")
    entry["command"] = command
    return (
        json.dumps(value, indent=2, sort_keys=True).encode() + b"\n",
        {"state": "updated", "previous_command": previous},
    )


_CODEX_TABLE = re.compile(
    r'^\s*\[\s*mcp_servers\s*\.\s*(?:human-copilot|"human-copilot"|\'human-copilot\')\s*\]\s*(?:#.*)?$'
)
_TOML_TABLE = re.compile(r"^\s*\[")
_COMMAND_LINE = re.compile(r"^(\s*)command\s*=.*$")


def _rewrite_codex(data: bytes, command: str) -> tuple[bytes, dict]:
    try:
        text = data.decode()
        parsed = tomllib.loads(text)
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ReleaseError(f"Codex config is invalid TOML: {exc}") from exc
    entry = parsed.get("mcp_servers", {}).get("human-copilot")
    if not isinstance(entry, dict):
        return data, {"state": "disabled", "previous_command": None}
    lines = text.splitlines(keepends=True)
    start = next(
        (i for i, line in enumerate(lines) if _CODEX_TABLE.match(line.rstrip("\r\n"))),
        None,
    )
    if start is None:
        raise ReleaseError(
            "Codex human-copilot table could not be located without rewriting unrelated TOML"
        )
    end = next(
        (i for i in range(start + 1, len(lines)) if _TOML_TABLE.match(lines[i])),
        len(lines),
    )
    command_index = next(
        (
            i
            for i in range(start + 1, end)
            if _COMMAND_LINE.match(lines[i].rstrip("\r\n"))
        ),
        None,
    )
    newline = "\r\n" if "\r\n" in text else "\n"
    replacement = f"command = {json.dumps(command)}{newline}"
    if command_index is None:
        lines.insert(start + 1, replacement)
    else:
        indent = _COMMAND_LINE.match(lines[command_index].rstrip("\r\n")).group(1)
        lines[command_index] = indent + replacement
    updated = "".join(lines).encode()
    try:
        checked = tomllib.loads(updated.decode())
    except tomllib.TOMLDecodeError as exc:
        raise ReleaseError(f"Codex config rewrite would be invalid: {exc}") from exc
    if checked["mcp_servers"]["human-copilot"].get("command") != command:
        raise ReleaseError("Codex config rewrite did not set the exact release command")
    return updated, {"state": "updated", "previous_command": entry.get("command")}


def _managed_links(home: Path, live_root: Path) -> dict[Path, str]:
    current = live_root / "current"
    return {
        home / "bin/copilot-tui": str(current / "tui.py"),
        home / "bin/copilot-toggle": str(current / "toggle.py"),
        home / "bin/copilot-open": str(current / "open_window.py"),
        home / ".local/share/gnome-shell/responder.py": str(current / "responder.py"),
        home / f".local/share/gnome-shell/extensions/{LIVE_UUID}": str(
            current / f"overlay/{LIVE_UUID}"
        ),
    }


def _preflight_links(
    links: dict[Path, str], allowed_roots: list[Path]
) -> dict[str, dict]:
    snapshots = {}
    roots = [root.resolve() for root in allowed_roots]
    for path in links:
        state = _link_state(path)
        if state["kind"] == "other":
            raise ReleaseError(f"refusing to replace unrelated non-symlink {path}")
        if state["kind"] == "symlink":
            resolved = (path.parent / state["target"]).resolve()
            if not any(resolved == root or root in resolved.parents for root in roots):
                raise ReleaseError(
                    f"refusing unrelated symlink {path} -> {state['target']}"
                )
        snapshots[str(path)] = state
    return snapshots


def _rewrite_registrations(
    home: Path, codex_home: Path, command: str
) -> tuple[dict, dict]:
    claude_path = home / ".claude.json"
    codex_path = codex_home / "config.toml"
    snapshots = {str(path): _file_snapshot(path) for path in (claude_path, codex_path)}
    states = {}
    try:
        if snapshots[str(claude_path)]["exists"]:
            body, states["claude"] = _rewrite_claude(
                snapshots[str(claude_path)]["data"], command
            )
            if body != snapshots[str(claude_path)]["data"]:
                _atomic_bytes(claude_path, body, snapshots[str(claude_path)]["mode"])
        else:
            states["claude"] = {"state": "disabled", "previous_command": None}
        if snapshots[str(codex_path)]["exists"]:
            body, states["codex"] = _rewrite_codex(
                snapshots[str(codex_path)]["data"], command
            )
            if body != snapshots[str(codex_path)]["data"]:
                _atomic_bytes(codex_path, body, snapshots[str(codex_path)]["mode"])
        else:
            states["codex"] = {"state": "disabled", "previous_command": None}
    except Exception:
        for raw, snapshot in snapshots.items():
            _restore_file(Path(raw), snapshot)
        raise
    return states, snapshots


# Every refusal a rehearsal actually exercises, in the order promote_live and
# rollback_live raise them. Reported by --dry-run so the reader can see WHICH
# gates were proven, not merely that nothing blew up: a rehearsal that silently
# skipped a check would look exactly like one that passed it.
_PROMOTE_REFUSALS = (
    "confirm-token is the literal 'attested'",
    "a passed, digest-bound QA candidate exists (build/deploy/validate/attest)",
    "the release bytes verify against the manifest, member by member",
    "no owned launcher or extension path is a non-symlink",
    "no owned symlink points outside the trusted source tree or the LIVE root",
    "the LIVE current path is a symlink, not a directory or file",
    "the LIVE current symlink resolves inside the trusted roots",
    "both client registrations parse (Claude JSON, Codex TOML)",
)
_ROLLBACK_REFUSALS = (
    "confirm-token is the literal 'previous'",
    "the current LIVE receipt records a previous digest",
    "the previous manifest is well formed and its digest matches",
    "the previous release bytes verify against that manifest",
    "the previous release has a recorded tested LIVE deployment",
    "that recorded evidence binds the exact manifest digest",
    "the LIVE current path is a symlink, so the swap is reversible",
)


def _registration_plan(home: Path, codex_home: Path, command: str) -> dict:
    """What _rewrite_registrations WOULD do, computed without writing a byte.

    Same readers, same rewriters, same refusals (invalid JSON/TOML raises here
    exactly as it would there) — only the _atomic_bytes calls are missing, and
    `changes` reports whether that call would have happened at all."""
    plan = {}
    for name, path, rewrite in (
        ("claude", home / ".claude.json", _rewrite_claude),
        ("codex", codex_home / "config.toml", _rewrite_codex),
    ):
        snapshot = _file_snapshot(path)
        if not snapshot["exists"]:
            plan[name] = {
                "path": str(path),
                "state": "disabled",
                "previous_command": None,
                "changes": False,
            }
            continue
        body, state = rewrite(snapshot["data"], command)
        plan[name] = {
            "path": str(path),
            "changes": body != snapshot["data"],
            **state,
        }
    return plan


def _reload_overlay(mode: str) -> dict:
    if mode == "never":
        return {
            "state": "not-requested",
            "sessions": "new MCP/TUI sessions use the release immediately",
        }
    executable = shutil.which("gnome-extensions")
    if not executable:
        return {"state": "next-login", "reason": "gnome-extensions unavailable"}
    info = _run([executable, "info", LIVE_UUID])
    if info.returncode or "State: ACTIVE" not in info.stdout:
        return {"state": "not-running", "reason": "overlay was not active"}
    # GNOME Shell 50 on Wayland keeps the extension's ESM module cached. A
    # disable/enable cycle can return success while reusing the old JavaScript,
    # as the live prompting outage demonstrated. Do not mutate the active
    # extension and never turn that cycle into false deployment evidence.
    return {
        "state": "next-login",
        "reason": (
            "GNOME Shell caches extension modules; disable/enable does not "
            "prove that the selected JavaScript was loaded"
        ),
        "sessions": "new MCP/TUI sessions use the release immediately",
    }


def _selection_status(operation: str, reload_result: dict) -> str:
    prefix = "selected (rolled back)" if operation == "rollback" else "selected"
    state = reload_result.get("state")
    if state == "next-login":
        return prefix + "; overlay activates next login"
    if state == "not-running":
        return prefix + "; overlay is not running"
    if state == "not-requested":
        return prefix + "; overlay reload not requested"
    return prefix + "; overlay activation unknown"


def _candidate(
    state_root: Path,
    qa_root: Path | None,
    qa_id: str | None,
    release_digest: str | None,
) -> tuple[Path, dict, dict, dict]:
    candidate = _json_read(state_root / "candidate.json")
    if qa_id is not None and candidate.get("qa_id") != qa_id:
        raise ReleaseError("requested QA id is not the attested promotion candidate")
    if release_digest is not None and candidate.get("release_digest") != release_digest:
        raise ReleaseError(
            "requested release digest is not the attested promotion candidate"
        )
    deployment, receipt, manifest = _deployment(
        state_root, qa_root, candidate.get("qa_id")
    )
    validation = _json_read(deployment / "validation.json")
    digest = candidate.get("release_digest")
    if not (
        digest
        == receipt.get("release_digest")
        == manifest.get("release_digest")
        == validation.get("release_digest")
        == validation.get("visual", {}).get("release_digest")
    ):
        raise ReleaseError(
            "promotion candidate, QA deployment, manifest, and validation digests do not match"
        )
    exact_manifest = _manifest_digest(manifest)
    if not (
        exact_manifest
        == candidate.get("manifest_digest")
        == receipt.get("manifest_digest")
        == validation.get("manifest_digest")
        == validation.get("visual", {}).get("manifest_digest")
    ):
        raise ReleaseError(
            "promotion candidate, QA deployment, and validation do not bind the same manifest"
        )
    if validation.get("state") != "passed" or not validation.get("validated_at"):
        raise ReleaseError(
            "promotion candidate lacks a completed digest-bound validation"
        )
    if not validation.get("test_run_id") or candidate.get(
        "test_run_id"
    ) != validation.get("test_run_id"):
        raise ReleaseError("promotion candidate does not bind the current QA test run")
    if not manifest.get("promotable"):
        raise ReleaseError("dirty-source QA releases are deliberately non-promotable")
    _verify_release_dir(deployment / "release", manifest)
    return deployment, receipt, manifest, validation


def promote_live(
    state_root: Path,
    *,
    qa_root: Path | None = None,
    qa_id: str | None = None,
    release_digest: str | None = None,
    confirm: str,
    home: Path,
    codex_home: Path,
    live_root: Path,
    legacy_source_root: Path | None = None,
    reload_overlay: str = "auto",
    dry_run: bool = False,
) -> dict:
    if confirm != "attested":
        raise ReleaseError("promotion requires --confirm-live attested")
    home, codex_home, live_root = (
        home.resolve(),
        codex_home.resolve(),
        live_root.resolve(),
    )
    trusted_legacy_root = (
        legacy_source_root or _default_legacy_source_root()
    ).resolve()
    with _read_locked(state_root, dry_run):
        deployment, qa_receipt, manifest, validation = _candidate(
            state_root, qa_root, qa_id, release_digest
        )
        digest = manifest["release_digest"]
        release_id = digest.split(":", 1)[1]
        source_release = deployment / "release"
        release_target = live_root / "releases" / release_id
        manifest_target = live_root / "manifests" / f"{release_id}.json"
        release_present = release_target.exists()
        if dry_run:
            # Verify the exact bytes that WOULD be copied. Same check, one
            # directory earlier: the real path verifies the copy, so a
            # rehearsal that verified nothing would be the weaker instrument.
            _verify_release_dir(
                release_target if release_present else source_release, manifest
            )
        else:
            live_root.mkdir(parents=True, exist_ok=True, mode=0o755)
            if release_present:
                _verify_release_dir(release_target, manifest)
            else:
                release_target.parent.mkdir(parents=True, exist_ok=True)
                temp_release = (
                    release_target.parent / f".{release_id}.{uuid.uuid4().hex}.tmp"
                )
                shutil.copytree(source_release, temp_release, symlinks=False)
                _verify_release_dir(temp_release, manifest)
                try:
                    os.rename(temp_release, release_target)
                except FileExistsError:
                    shutil.rmtree(temp_release)
                    _verify_release_dir(release_target, manifest)
            _atomic_json(manifest_target, manifest)

        links = _managed_links(home, live_root)
        allowed = [trusted_legacy_root, live_root]
        link_snapshots = _preflight_links(links, allowed)
        current = live_root / "current"
        current_snapshot = _link_state(current)
        if current_snapshot["kind"] == "other":
            raise ReleaseError(f"LIVE current path is not a symlink: {current}")
        if current_snapshot["kind"] == "symlink":
            selected = (current.parent / current_snapshot["target"]).resolve()
            allowed_current = [trusted_legacy_root, live_root]
            if not any(
                selected == root or root in selected.parents for root in allowed_current
            ):
                raise ReleaseError(
                    f"refusing unrelated LIVE current link {current} -> {current_snapshot['target']}"
                )
        prior_receipt = (
            _json_read(state_root / "live/current.json")
            if (state_root / "live/current.json").exists()
            else None
        )
        previous_digest = (
            prior_receipt.get("deployed_digest") if prior_receipt else None
        )
        claude_path, codex_path = home / ".claude.json", codex_home / "config.toml"
        command = str(live_root / "current/server.py")
        if dry_run:
            return {
                "schema": "human-copilot-live-plan/v1",
                "operation": "promote",
                "dry_run": True,
                "planned_at": _utc_now(),
                "refusals_checked": _PROMOTE_REFUSALS,
                "would_deploy_digest": digest,
                "manifest_digest": _manifest_digest(manifest),
                "source_revision": manifest["source_revision"],
                "validated_at": validation["validated_at"],
                "qa_id": qa_receipt["qa_id"],
                "previous_digest": previous_digest,
                "release_dir": {
                    "path": str(release_target),
                    "state": "present-and-verified"
                    if release_present
                    else "would-create",
                    "source": str(source_release),
                },
                "manifest_file": {
                    "path": str(manifest_target),
                    "state": "would-overwrite"
                    if manifest_target.exists()
                    else "would-create",
                },
                "current_link": {
                    "path": str(current),
                    "from": current_snapshot,
                    "to": f"releases/{release_id}",
                },
                "links": [
                    {
                        "path": str(path),
                        "from": link_snapshots[str(path)],
                        "to": target,
                        "changes": link_snapshots[str(path)] != {
                            "kind": "symlink",
                            "target": target,
                        },
                    }
                    for path, target in links.items()
                ],
                "registrations": _registration_plan(home, codex_home, command),
                "reload": {"mode": reload_overlay, "state": "not-performed"},
                "state_writes": [
                    str(state_root / "live/current.json"),
                    str(state_root / "live/history/<deployed_at>.json"),
                ]
                + (
                    []
                    if (state_root / "live/legacy.json").exists()
                    else [str(state_root / "live/legacy.json")]
                ),
                "wrote_nothing": True,
            }
        config_snapshots = {
            str(claude_path): _file_snapshot(claude_path),
            str(codex_path): _file_snapshot(codex_path),
        }
        changed_links = []
        try:
            _atomic_link(current, f"releases/{release_id}")
            for path, target in links.items():
                _atomic_link(path, target)
                changed_links.append(path)
            registrations, _ = _rewrite_registrations(home, codex_home, command)
            reload_result = _reload_overlay(reload_overlay)
            deployed_at = _utc_now()
            receipt = {
                "schema": "human-copilot-live-deployment/v1",
                "operation": "promote",
                "deployed_at": deployed_at,
                "deployed_digest": digest,
                "manifest_digest": _manifest_digest(manifest),
                "source_revision": manifest["source_revision"],
                "deployed_revision": manifest["source_revision"],
                "deployment_status": _selection_status("promote", reload_result),
                "validated_at": validation["validated_at"],
                "qa_id": qa_receipt["qa_id"],
                "previous_digest": previous_digest,
                "release_root": str(release_target),
                "current_link": str(current),
                "registrations": registrations,
                "reload": reload_result,
                "promotion_build_invocations": 0,
            }
            history = (
                state_root
                / "live/history"
                / f"{deployed_at.replace(':', '')}-{uuid.uuid4().hex}.json"
            )
            _atomic_json(history, receipt)
            _atomic_json(state_root / "live/current.json", receipt)
            if not (state_root / "live/legacy.json").exists():
                _atomic_json(
                    state_root / "live/legacy.json",
                    {
                        "schema": "human-copilot-legacy-live-snapshot/v1",
                        "captured_at": deployed_at,
                        "current": current_snapshot,
                        "links": link_snapshots,
                        "registration_commands": {
                            "claude": registrations["claude"].get("previous_command"),
                            "codex": registrations["codex"].get("previous_command"),
                        },
                    },
                )
            return receipt
        except Exception:
            for path in reversed(changed_links):
                _restore_link(path, link_snapshots[str(path)])
            _restore_link(current, current_snapshot)
            for raw, snapshot in config_snapshots.items():
                _restore_file(Path(raw), snapshot)
            raise


def rollback_live(
    state_root: Path,
    *,
    confirm: str,
    home: Path,
    codex_home: Path,
    live_root: Path,
    reload_overlay: str = "auto",
    dry_run: bool = False,
) -> dict:
    if confirm != "previous":
        raise ReleaseError("rollback requires --confirm-rollback previous")
    home, codex_home, live_root = (
        home.resolve(),
        codex_home.resolve(),
        live_root.resolve(),
    )
    with _read_locked(state_root, dry_run):
        current_receipt = _json_read(state_root / "live/current.json")
        current_digest = current_receipt.get("deployed_digest")
        previous_digest = current_receipt.get("previous_digest")
        if not previous_digest:
            raise ReleaseError("there is no previous immutable release to roll back to")
        previous_id = previous_digest.split(":", 1)[1]
        manifest = _json_read(live_root / "manifests" / f"{previous_id}.json")
        _verify_manifest(manifest)
        if manifest["release_digest"] != previous_digest:
            raise ReleaseError("previous LIVE manifest digest mismatch")
        release_dir = live_root / "releases" / previous_id
        _verify_release_dir(release_dir, manifest)
        previous_receipt = None
        history_root = state_root / "live/history"
        if history_root.exists():
            for path in sorted(history_root.glob("*.json"), reverse=True):
                with contextlib.suppress(ReleaseError, KeyError):
                    item = _json_read(path)
                    if item.get("deployed_digest") == previous_digest:
                        previous_receipt = item
                        break
        if previous_receipt is None:
            raise ReleaseError(
                "previous release has no recorded tested LIVE deployment"
            )
        if previous_receipt.get("manifest_digest") != _manifest_digest(manifest):
            raise ReleaseError(
                "previous LIVE evidence does not bind its exact manifest"
            )
        current_link = live_root / "current"
        snapshot = _link_state(current_link)
        if snapshot["kind"] != "symlink":
            raise ReleaseError("LIVE current link is not safely roll-backable")
        if dry_run:
            return {
                "schema": "human-copilot-live-plan/v1",
                "operation": "rollback",
                "dry_run": True,
                "planned_at": _utc_now(),
                "refusals_checked": _ROLLBACK_REFUSALS,
                "would_deploy_digest": previous_digest,
                "manifest_digest": _manifest_digest(manifest),
                "source_revision": manifest["source_revision"],
                "rolled_back_from": current_digest,
                "validated_at": previous_receipt.get("validated_at"),
                "qa_id": previous_receipt.get("qa_id"),
                "release_dir": {"path": str(release_dir), "state": "present-and-verified"},
                "current_link": {
                    "path": str(current_link),
                    "from": snapshot,
                    "to": f"releases/{previous_id}",
                },
                "links": [],
                "registrations": {
                    "note": "rollback moves the current link only; both "
                    "registrations already point through it and are not rewritten"
                },
                "reload": {"mode": reload_overlay, "state": "not-performed"},
                "state_writes": [
                    str(state_root / "live/current.json"),
                    str(state_root / "live/history/<deployed_at>.json"),
                ],
                "wrote_nothing": True,
            }
        try:
            _atomic_link(current_link, f"releases/{previous_id}")
            reload_result = _reload_overlay(reload_overlay)
            deployed_at = _utc_now()
            receipt = {
                "schema": "human-copilot-live-deployment/v1",
                "operation": "rollback",
                "deployed_at": deployed_at,
                "deployed_digest": previous_digest,
                "manifest_digest": _manifest_digest(manifest),
                "source_revision": manifest["source_revision"],
                "deployed_revision": manifest["source_revision"],
                "deployment_status": _selection_status("rollback", reload_result),
                "validated_at": previous_receipt.get("validated_at"),
                "qa_id": previous_receipt.get("qa_id"),
                "previous_digest": current_digest,
                "rolled_back_from": current_digest,
                "release_root": str(release_dir),
                "current_link": str(current_link),
                "reload": reload_result,
                "rollback_build_invocations": 0,
            }
            history = (
                state_root
                / "live/history"
                / f"{deployed_at.replace(':', '')}-{uuid.uuid4().hex}.json"
            )
            _atomic_json(history, receipt)
            _atomic_json(state_root / "live/current.json", receipt)
            return receipt
        except Exception:
            _restore_link(current_link, snapshot)
            raise


def release_status(
    state_root: Path, *, qa_root: Path | None = None, live_root: Path | None = None
) -> dict:
    """Return evidence without creating, locking, touching, or repairing paths."""
    observed = _utc_now()
    artifact: dict = {"state": "absent"}
    last_build = state_root / "last-build.json"
    if last_build.exists():
        try:
            receipt = _json_read(last_build)
            manifest, _ = _load_artifact(Path(receipt["artifact_dir"]))
            if receipt.get("manifest_digest") != _manifest_digest(manifest):
                raise ReleaseError("last-build receipt does not bind its manifest")
            artifact = {
                "state": "current",
                "type": "human-copilot-release-set",
                "digest": manifest["release_digest"],
                "release_digest": manifest["release_digest"],
                "manifest_digest": receipt["manifest_digest"],
                "source_revision": manifest["source_revision"],
                "source_dirty": manifest["source_dirty"],
                "promotable": manifest["promotable"],
                "build_started_at": receipt["build_started_at"],
                "build_finished_at": receipt["build_finished_at"],
                "archive_digest": receipt["archive_digest"],
            }
        except (ReleaseError, KeyError, OSError) as exc:
            artifact = {"state": "unreadable", "explanation": str(exc)}

    qa: dict = {"state": "absent"}
    last_qa = state_root / "last-qa.json"
    if last_qa.exists():
        try:
            selected = _json_read(last_qa)
            root, receipt, manifest = _deployment(
                state_root, qa_root, selected["qa_id"]
            )
            if selected.get("release_digest") != receipt.get("release_digest"):
                raise ReleaseError("last-QA receipt selects a different deployment")
            _verify_release_dir(root / "release", manifest)
            validation_path = root / "validation.json"
            validation = (
                _json_read(validation_path) if validation_path.exists() else None
            )
            if validation and (
                validation.get("release_digest") != receipt["release_digest"]
                or validation.get("artifact_digest") != receipt["release_digest"]
                or validation.get("manifest_digest") != receipt["manifest_digest"]
                or validation.get("source_revision") != receipt["source_revision"]
            ):
                raise ReleaseError(
                    "QA validation is not bound to the deployed artifact"
                )
            if (
                validation
                and validation.get("state") == "passed"
                and (
                    not validation.get("validated_at")
                    or not validation.get("test_run_id")
                )
            ):
                raise ReleaseError("passed QA validation lacks canonical evidence")
            qa_state = "deployed"
            if validation:
                qa_state = (
                    "validated"
                    if validation.get("state") == "passed"
                    else (
                        "failed"
                        if validation.get("state") == "failed"
                        else "awaiting-visual"
                    )
                )
            qa = {
                "state": qa_state,
                "qa_id": receipt["qa_id"],
                "release_digest": receipt["release_digest"],
                "manifest_digest": receipt["manifest_digest"],
                "source_revision": receipt.get(
                    "deployed_revision", receipt["source_revision"]
                ),
                "qa_deployed_at": receipt.get("deployed_at", receipt["qa_deployed_at"]),
                "mcp_address": receipt["mcp_address"],
                "spool_root": receipt["spool_root"],
                "extension_uuid": receipt["extension_uuid"],
                "validation_state": validation.get("state") if validation else None,
                "validation_status": validation.get("status") if validation else None,
                "validation_passed": validation.get("passed") if validation else False,
                "validated_at": validation.get("validated_at") if validation else None,
                "test_run_id": validation.get("test_run_id") if validation else None,
            }
        except (ReleaseError, KeyError, OSError) as exc:
            qa = {"state": "unreadable", "explanation": str(exc)}

    live: dict = {"state": "absent"}
    current_receipt = state_root / "live/current.json"
    if current_receipt.exists():
        try:
            receipt = _json_read(current_receipt)
            live_root = (live_root or _default_live_root()).resolve()
            digest = receipt["deployed_digest"]
            release_id = digest.split(":", 1)[1]
            manifest = _json_read(live_root / "manifests" / f"{release_id}.json")
            _verify_manifest(manifest)
            if receipt.get("manifest_digest") != _manifest_digest(manifest):
                raise ReleaseError("LIVE receipt does not bind its manifest")
            link = live_root / "current"
            expected_target = f"releases/{release_id}"
            if not link.is_symlink() or os.readlink(link) != expected_target:
                raise ReleaseError(
                    "LIVE current link does not select the recorded release"
                )
            _verify_release_dir(live_root / expected_target, manifest)
            live = {
                "state": "current",
                "deployment_status": receipt.get("deployment_status")
                or (
                    "active (rolled back)"
                    if receipt.get("operation") == "rollback"
                    else "active"
                ),
                "deployed_at": receipt["deployed_at"],
                "deployed_digest": digest,
                "manifest_digest": receipt["manifest_digest"],
                "source_revision": receipt["source_revision"],
                "deployed_revision": receipt.get(
                    "deployed_revision", receipt["source_revision"]
                ),
                "previous_digest": receipt.get("previous_digest"),
                "reload": receipt.get("reload"),
            }
        except (ReleaseError, KeyError, OSError) as exc:
            live = {"state": "drift", "explanation": str(exc)}
    return {
        "schema": STATUS_SCHEMA,
        "read_only": True,
        "observed_at": observed,
        "artifact": artifact,
        "qa": qa,
        "live": live,
    }


def component_status(aggregate: dict, component: str) -> dict:
    """Project Manager release_pipeline evidence for one environment probe."""
    artifact = aggregate["artifact"]
    qa = aggregate["qa"]
    live = aggregate["live"]

    if component == "artifact":
        current = artifact.get("state") == "current"
        return {
            "artifact": {
                "type": artifact.get("type") if current else None,
                "digest": artifact.get("digest") if current else None,
                "source_revision": artifact.get("source_revision") if current else None,
                "build_started_at": artifact.get("build_started_at")
                if current
                else None,
                "build_finished_at": artifact.get("build_finished_at")
                if current
                else None,
            }
        }
    if component == "qa":
        current = qa.get("state") not in {"absent", "unreadable"}
        digest = qa.get("release_digest") if current else None
        revision = qa.get("source_revision") if current else None
        return {
            "artifact": {
                "type": "human-copilot-release-set" if current else None,
                "digest": digest,
                "source_revision": revision,
            },
            "deployed_at": qa.get("qa_deployed_at") if current else None,
            "deployed_digest": digest,
            "deployed_revision": revision,
            "deployment_status": (
                f"isolated QA {qa.get('qa_id')}" if current else "absent"
            ),
        }
    if component == "attestation":
        has_run = bool(qa.get("test_run_id"))
        raw_status = qa.get("validation_status") if has_run else None
        status = {
            "passed": "passed",
            "failed": "failed",
            "pending": "pending",
        }.get(raw_status, raw_status)
        return {
            "validation": {
                "status": status,
                "passed": status == "passed",
                "artifact_digest": qa.get("release_digest") if has_run else None,
                "source_revision": qa.get("source_revision") if has_run else None,
                "validated_at": qa.get("validated_at") if has_run else None,
                "test_run_id": qa.get("test_run_id") if has_run else None,
            }
        }
    if component == "production":
        current = live.get("state") == "current"
        digest = live.get("deployed_digest") if current else None
        revision = live.get("deployed_revision") if current else None
        return {
            "artifact": {
                "type": "human-copilot-release-set" if current else None,
                "digest": digest,
                "source_revision": revision,
            },
            "deployed_at": live.get("deployed_at") if current else None,
            "deployed_digest": digest,
            "deployed_revision": revision,
            "deployment_status": live.get("deployment_status") if current else "absent",
        }
    if component == "aggregate":
        return aggregate
    raise ReleaseError(f"unknown status component {component!r}")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=str(_default_state_root()))
    parser.add_argument(
        "--json",
        action="store_true",
        help="JSON is always emitted; retained for cockpit clarity",
    )
    subs = parser.add_subparsers(dest="command", required=True)

    build = subs.add_parser("build")
    build.add_argument("--source", default=str(Path(__file__).resolve().parent))
    build.add_argument("--allow-dirty", action="store_true")

    deploy = subs.add_parser("deploy-qa")
    deploy.add_argument("--artifact")
    deploy.add_argument("--qa-root")
    deploy.add_argument("--qa-id")

    validate = subs.add_parser("validate-qa")
    validate.add_argument("--qa-root")
    validate.add_argument("--qa-id")

    visual = subs.add_parser("attest-visual")
    visual.add_argument("--qa-root")
    visual.add_argument("--qa-id")
    visual.add_argument("--release-digest", required=True)
    visual.add_argument("--result", required=True, choices=("passed", "failed"))
    visual.add_argument("--actor", required=True)
    visual.add_argument("--note", required=True)

    promote = subs.add_parser("promote")
    promote.add_argument("--qa-root")
    promote.add_argument("--qa-id")
    promote.add_argument("--release-digest")
    promote.add_argument("--confirm-live", required=True)
    promote.add_argument("--home", default=str(Path.home()))
    promote.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
    )
    promote.add_argument("--live-root", default=str(_default_live_root()))
    promote.add_argument(
        "--legacy-source-root",
        default=str(_default_legacy_source_root()),
        help="only this source tree's existing symlinks may be adopted on first promotion",
    )
    promote.add_argument("--reload-overlay", choices=("auto", "never"), default="auto")
    promote.add_argument(
        "--dry-run",
        action="store_true",
        help="rehearse: run every refusal and print the exact transaction plan, "
        "then write nothing. Add it to the command you are about to run, so the "
        "rehearsal is that command and not an approximation of it.",
    )

    rollback = subs.add_parser("rollback")
    rollback.add_argument("--confirm-rollback", required=True)
    rollback.add_argument("--home", default=str(Path.home()))
    rollback.add_argument(
        "--codex-home",
        default=os.environ.get("CODEX_HOME", str(Path.home() / ".codex")),
    )
    rollback.add_argument("--live-root", default=str(_default_live_root()))
    rollback.add_argument("--reload-overlay", choices=("auto", "never"), default="auto")
    rollback.add_argument(
        "--dry-run",
        action="store_true",
        help="rehearse: run every refusal and print the exact transaction plan, "
        "then write nothing.",
    )

    status_parser = subs.add_parser("status")
    status_parser.add_argument(
        "component",
        nargs="?",
        default="aggregate",
        choices=("aggregate", "artifact", "qa", "attestation", "production"),
    )
    status_parser.add_argument("--qa-root")
    status_parser.add_argument("--live-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    # Cockpit actions conventionally put --json after the subcommand.  Output is
    # always JSON, so accept the clarity flag in either position.
    raw_argv = [item for item in raw_argv if item != "--json"]
    args = _parser().parse_args(raw_argv)
    state_root = Path(args.state_root).expanduser().resolve()
    try:
        if args.command == "build":
            result = build_release(
                Path(args.source), state_root, allow_dirty=args.allow_dirty
            )
        elif args.command == "deploy-qa":
            result = deploy_qa(
                state_root,
                artifact_dir=Path(args.artifact) if args.artifact else None,
                qa_root=Path(args.qa_root) if args.qa_root else None,
                qa_id=args.qa_id,
            )
        elif args.command == "validate-qa":
            result = validate_qa(
                state_root,
                qa_root=Path(args.qa_root) if args.qa_root else None,
                qa_id=args.qa_id,
            )
            if result["automated"]["state"] != "passed":
                print(json.dumps(result, indent=2, sort_keys=True))
                return 1
        elif args.command == "attest-visual":
            result = attest_visual(
                state_root,
                qa_root=Path(args.qa_root) if args.qa_root else None,
                qa_id=args.qa_id,
                release_digest=args.release_digest,
                result=args.result,
                actor=args.actor,
                note=args.note,
            )
        elif args.command == "promote":
            result = promote_live(
                state_root,
                qa_root=Path(args.qa_root) if args.qa_root else None,
                qa_id=args.qa_id,
                release_digest=args.release_digest,
                confirm=args.confirm_live,
                home=Path(args.home),
                codex_home=Path(args.codex_home),
                live_root=Path(args.live_root),
                legacy_source_root=Path(args.legacy_source_root),
                reload_overlay=args.reload_overlay,
                dry_run=args.dry_run,
            )
        elif args.command == "rollback":
            result = rollback_live(
                state_root,
                confirm=args.confirm_rollback,
                home=Path(args.home),
                codex_home=Path(args.codex_home),
                live_root=Path(args.live_root),
                reload_overlay=args.reload_overlay,
                dry_run=args.dry_run,
            )
        else:
            aggregate = release_status(
                state_root,
                qa_root=Path(args.qa_root) if args.qa_root else None,
                live_root=Path(args.live_root) if args.live_root else None,
            )
            result = component_status(aggregate, args.component)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except ReleaseError as exc:
        print(
            json.dumps(
                {"schema": "human-copilot-release-error/v1", "error": str(exc)},
                indent=2,
            )
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
