"""Private, versioned identity and acknowledgement for nonblocking publishers.

The episode ID is supplied by the producer and must identify one semantic task.
Records are write-ahead and private; only acknowledged records are pruned.
"""

import base64
import fcntl
import hashlib
import json
import os
import re
import stat
import time
import uuid
from contextlib import contextmanager
from functools import wraps

SCHEMA = "human-copilot.publisher-lifecycle/v1"
MAX_BYTES = 12 * 1024 * 1024
MAX_ANSWER_BYTES = 8 * 1024 * 1024
ARCHIVE_COUNT = 256
ARCHIVE_BYTES = 128 * 1024 * 1024


class PublisherError(ValueError):
    """Secret-safe lifecycle refusal."""


def _public(operation):
    """Do not let storage/parser/adapter exceptions disclose reply contents."""
    @wraps(operation)
    def call(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except PublisherError:
            raise
        except Exception:
            raise PublisherError("publisher storage unavailable") from None
    return call


def _key(client, episode_id):
    if not isinstance(client, str) or not client.strip() or len(client) > 256:
        raise PublisherError("invalid client")
    if (not isinstance(episode_id, str) or not episode_id
            or len(episode_id) > 256):
        raise PublisherError("invalid episode_id")
    return hashlib.sha256(json.dumps([client.strip(), episode_id],
                                   separators=(",", ":")).encode()).hexdigest()


@contextmanager
def _locked(spool):
    directory = os.path.join(spool, "publisher")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    fd = os.open(os.path.join(directory, "lock"),
                 os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield directory
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _read(path):
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    except FileNotFoundError:
        return None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > MAX_BYTES):
            raise PublisherError("invalid private file")
        raw = b""
        while len(raw) <= MAX_BYTES:
            part = os.read(fd, min(65536, MAX_BYTES + 1 - len(raw)))
            if not part:
                break
            raw += part
        if len(raw) > MAX_BYTES:
            raise PublisherError("private file too large")
        return raw
    finally:
        os.close(fd)


def _json(path):
    raw = _read(path)
    if raw is None:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise PublisherError("invalid private JSON") from None
    if not isinstance(value, dict):
        raise PublisherError("invalid private JSON")
    return value


def _write(path, raw, *, create=False):
    if len(raw) > MAX_BYTES:
        raise PublisherError("private file too large")
    tmp = path + ".tmp-" + uuid.uuid4().hex
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                 getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if create:
            os.link(tmp, path)
            os.unlink(tmp)
        else:
            os.replace(tmp, path)
        dir_fd = os.open(os.path.dirname(path), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass


def _save(path, record):
    _write(path, json.dumps(record, separators=(",", ":"),
                            ensure_ascii=False).encode("utf-8"))


def _record(directory, key):
    path = os.path.join(directory, key + ".json")
    record = _json(path)
    if record is not None and record.get("schema") != SCHEMA:
        raise PublisherError("unsupported publisher contract")
    return path, record


def _prune(directory):
    """Keep unresolved episodes and bounded acknowledged evidence."""
    archive = []
    spool = os.path.dirname(directory)
    for name in os.listdir(directory):
        if not name.endswith(".json"):
            continue
        path = os.path.join(directory, name)
        try:
            record = _json(path)
            if record and record.get("state") == "acknowledged":
                rid = record.get("request_id")
                if (not isinstance(rid, str) or not re.fullmatch(
                        r"[A-Za-z0-9._@+-]{1,128}", rid)
                        or any(os.path.lexists(os.path.join(
                            spool, group, rid + ".json"))
                            for group in ("requests", "answers"))):
                    # Acknowledgement can precede file cleanup by a crash.
                    # Retain its retry key until both paths are gone.
                    continue
                archive.append((record.get("acknowledged_at", 0), path,
                                os.stat(path).st_size))
        except (OSError, PublisherError):
            continue
    archive.sort(reverse=True)
    total = 0
    for index, (_when, path, size) in enumerate(archive):
        total += size
        if index >= ARCHIVE_COUNT or total > ARCHIVE_BYTES:
            os.unlink(path)


@_public
def publish(summary, timeout_s, response, *, client, episode_id,
            server_api, existing_request_id=None):
    """Commit one semantic identity before showing its request card."""
    spool = server_api.spool_dir()
    key = _key(client, episode_id)
    with _locked(spool) as directory:
        path, record = _record(directory, key)
        if record is not None:
            if (record.get("summary") != summary or record.get("response") != response
                    or record.get("client") != client):
                raise PublisherError("episode prompt changed")
            if (existing_request_id is not None
                    and existing_request_id != record.get("request_id")):
                raise PublisherError("episode request changed")
            rid = record["request_id"]
            request_path = os.path.join(spool, "requests", rid + ".json")
            answer_path = os.path.join(spool, "answers", rid + ".json")
            if (record.get("state") == "pending" and _read(request_path) is None
                    and _read(answer_path) is None
                    and time.time() < record["request"]["deadline"]):
                _write(request_path, json.dumps(record["request"]).encode(),
                       create=True)
            return rid
        if existing_request_id is not None:
            if not isinstance(existing_request_id, str) or not re.fullmatch(
                    r"[A-Za-z0-9._@+-]{1,128}", existing_request_id):
                raise PublisherError("invalid request id")
            # A migration cannot assign the same card to independent tasks.
            # The global publisher lock makes this check atomic with adoption.
            for name in os.listdir(directory):
                if name.endswith(".json"):
                    owner = _json(os.path.join(directory, name))
                    if owner and owner.get("request_id") == existing_request_id:
                        raise PublisherError("request already owned by another episode")
            request = _json(os.path.join(
                spool, "requests", existing_request_id + ".json"))
            if (request is None or request.get("id") != existing_request_id
                    or request.get("summary") != summary
                    or request.get("client") != client
                    or request.get("response", {"mode": None, "options": []})
                    != response):
                raise PublisherError("existing request does not match episode")
            if "caller_process" in request:
                # A waiting MCP call still owns consumption and orphan cleanup.
                # Do not race it or alter its immutable request revision.
                raise PublisherError("existing request has a waiting caller")
        else:
            request = server_api._build_request(summary, timeout_s, response,
                                                client=client,
                                                instance=server_api.instance_stamp())
        record = {"schema": SCHEMA, "state": "pending",
                  "request_id": request["id"], "request": request,
                  "summary": summary, "response": response, "client": client,
                  "created_at": time.time()}
        _save(path, record)
        if existing_request_id is None:
            _write(os.path.join(spool, "requests", request["id"] + ".json"),
                   json.dumps(request).encode(), create=True)
        _prune(directory)
        return request["id"]


@_public
def answer(episode_id, *, client, server_api):
    """Return exact answer bytes, retaining them before transport delivery."""
    spool = server_api.spool_dir()
    with _locked(spool) as directory:
        path, record = _record(directory, _key(client, episode_id))
        if record is None:
            raise PublisherError("unknown episode")
        if record["state"] == "acknowledged":
            return None
        if record["state"] == "answered":
            return base64.b64decode(record["answer_bytes"], validate=True)
        raw = _read(os.path.join(spool, "answers", record["request_id"] + ".json"))
        if raw is None:
            return None
        if len(raw) > MAX_ANSWER_BYTES:
            raise PublisherError("answer too large for private recovery")
        try:
            parsed = json.loads(raw)
        except (ValueError, UnicodeDecodeError):
            raise PublisherError("invalid answer") from None
        if not isinstance(parsed, dict):
            raise PublisherError("invalid answer")
        record["answer_bytes"] = base64.b64encode(raw).decode("ascii")
        record["answer_sha256"] = hashlib.sha256(raw).hexdigest()
        record["state"] = "answered"
        _save(path, record)
        return raw


@_public
def acknowledge(episode_id, answer_sha256, *, client, server_api):
    """Mark applied before cleanup; retry completes interrupted cleanup."""
    import responder
    if not isinstance(answer_sha256, str) or not re.fullmatch(
            r"[0-9a-f]{64}", answer_sha256):
        raise PublisherError("invalid answer digest")
    spool = server_api.spool_dir()
    with _locked(spool) as directory:
        path, record = _record(directory, _key(client, episode_id))
        if record is None or record.get("state") == "pending":
            raise PublisherError("answer not recorded")
        if record.get("answer_sha256") != answer_sha256:
            raise PublisherError("answer digest mismatch")
        if record["state"] == "answered":
            visible = _read(os.path.join(
                spool, "answers", record["request_id"] + ".json"))
            if visible is not None and hashlib.sha256(visible).hexdigest() != answer_sha256:
                raise PublisherError("answer storage changed")
            parsed = json.loads(base64.b64decode(record["answer_bytes"]))
            result = responder.Responder(spool).acknowledge_consumed(
                record["request"], parsed)
            if not result.get("ok"):
                raise PublisherError("responder acknowledgement unavailable")
            record.pop("answer_bytes")
            record["state"] = "acknowledged"
            record["acknowledged_at"] = time.time()
            _save(path, record)
        rid = record["request_id"]
        for group in ("requests", "answers"):
            try:
                os.unlink(os.path.join(spool, group, rid + ".json"))
            except FileNotFoundError:
                pass
        server_api.clear_arrival_nudge(spool, rid)
        _prune(directory)
        return True
