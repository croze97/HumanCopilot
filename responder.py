#!/usr/bin/env python3
"""Transport-neutral Human Copilot responder adapter.

The importable :class:`Responder` API and the strict JSON-lines stdio front
end expose the same local filesystem transaction.  There is deliberately no
socket or network listener: callers choose their own authenticated transport.

Human Copilot remains the semantic authority.  ``server.py`` stamps the
canonical response/step contract below before publishing a request; consumers
never parse prompt prose or GNOME locators themselves.  The GNOME extension
publishes metadata-only target records beside the spool.  No target, lease or
commit record ever contains image/capture bytes.
"""

import fcntl
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone


CONTRACT_SCHEMA = "human-copilot.responder-contract/v1"
REQUEST_SCHEMA = "human-copilot.responder-request/v1"
ANSWER_SCHEMA = "human-copilot.responder-answer/v1"
COMMAND_SCHEMA = "human-copilot.responder-command/v1"
RESULT_SCHEMA = "human-copilot.responder-result/v1"
EVENT_SCHEMA = "human-copilot.responder-event/v1"
LEASE_SCHEMA = "human-copilot.responder-lease/v1"
TARGET_SCHEMA = "human-copilot.target-state/v1"
COMMIT_RECORD_SCHEMA = "human-copilot.responder-commit-record/v1"
COMMIT_RECEIPT_SCHEMA = "human-copilot.responder-commit/v1"
SCHEMA_VERSION = 1
REMOTE_EXACT_FORMAT = "remote-exact/v1"
REMOTE_RESPONSE_SCHEMA = "human-copilot.remote-response/v1"

MAX_COMMAND_BYTES = 256 * 1024
MAX_STATE_BYTES = 1024 * 1024
MAX_NOTE_CHARS = 16 * 1024
MAX_TEXT_CHARS = 64 * 1024
MAX_LEASE_SECONDS = 30.0
MIN_LEASE_SECONDS = 1.0
RECORD_RETENTION_SECONDS = 10 * 60.0

_SAFE_ID = re.compile(r"^[A-Za-z0-9._@+-]{1,128}$")
_STEP_LINE = re.compile(r"^(\d{1,3})[.)]\s+(.*\S)$")
_STEP_CELL = re.compile(r"^\d{1,3}[.)]?$")
_DELIM_CELL = re.compile(r"^:?-+:?$")
_LOOK_ARROW = re.compile(r"\s(?:→|->|=>|⇒)\s+(.+)$")
_SPOT_TAG = re.compile(r"@\{([^}]*)\}")
_INLINE_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_INLINE_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>")
_INLINE_CODE = re.compile(r"`+([^`]+)`+")
_INLINE_STRIKE = re.compile(r"~~([^~]+)~~")
_INLINE_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_INLINE_EM = re.compile(r"(?<![\w*])\*([^*\s](?:[^*]*[^*\s])?)\*(?![\w*])")
_INLINE_ESC = re.compile(r"\\([\\`*_{}\[\]()#+.!|~<>-])")
_INLINE_HIDDEN = re.compile("[\ue000-\ue07f]")
_TASK_MARKER = re.compile(r"^\[([ xX])\]\s+")
_SPOT_RECT = re.compile(
    r"(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?\s*,\s*"
    r"(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?")
_REGIONS = {
    "full", "top-left", "top", "top-right", "left", "center", "right",
    "bottom-left", "bottom", "bottom-right",
}

_REQUEST_REVISION_DOMAIN = b"human-copilot.responder-request/v1\0"
_ANSWER_DIGEST_DOMAIN = b"human-copilot.responder-answer/v1\0"
_SUBMISSION_DIGEST_DOMAIN = b"human-copilot.responder-submission/v1\0"
_COMMIT_DIGEST_DOMAIN = b"human-copilot.responder-commit/v1\0"
_COMMIT_SIGNATURE_DOMAIN = b"human-copilot.responder-commit-signature/v1\0"


class AdapterError(ValueError):
    """A stable typed adapter failure."""

    def __init__(self, code, detail=None):
        self.code = code
        self.detail = detail or code
        super().__init__(self.detail)


def canonical_json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _tagged(domain, value):
    return "sha256:" + hashlib.sha256(domain + canonical_json(value)).hexdigest()


def answer_digest(answer):
    """Digest the exact answer object a responder submitted.

    Callers use this with :func:`submission_digest` to bind verification to
    their own outbound bytes instead of trusting digest fields echoed by a
    receipt.
    """
    return _tagged(_ANSWER_DIGEST_DOMAIN, answer)


def submission_digest(request_id, revision, geometry_generation,
                      submission_id, answer, *, lease_token):
    """Digest every remote-submit binding, including its secret lease token."""
    return _tagged(_SUBMISSION_DIGEST_DOMAIN, {
        "request_id": request_id, "revision": revision,
        "geometry_generation": geometry_generation,
        "submission_id": submission_id,
        "lease_token_digest": _tagged(
            b"human-copilot.responder-lease-token/v1\0", lease_token),
        "answer": answer})


def local_submission_digest(request_id, submission_id, answer, lifecycle=False):
    """Digest the equivalent local-surface transaction bindings."""
    return _tagged(_SUBMISSION_DIGEST_DOMAIN, {
        "request_id": request_id, "submission_id": submission_id,
        "answer": answer, "lifecycle": bool(lifecycle)})


def _iso(epoch):
    return datetime.fromtimestamp(float(epoch), timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")


def _finite_number(value):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return False
    try:
        return math.isfinite(float(value))
    except (OverflowError, ValueError):
        return False


def _safe_id(value):
    return isinstance(value, str) and bool(_SAFE_ID.fullmatch(value))


def _valid_lease_binding(request_id, revision, token):
    """Bound the three values that identify one leased request."""
    return (_safe_id(request_id)
            and isinstance(revision, str) and 0 < len(revision) <= 256
            and isinstance(token, str) and 0 < len(token) <= 512)


def _region_of(value):
    word = re.sub(r"[\s_]+", "-", (value or "").lower().strip())
    word = re.sub(r"^upper-", "top-", word)
    word = re.sub(r"^lower-", "bottom-", word)
    word = re.sub(r"^(centre|middle)$", "center", word)
    word = re.sub(r"^(whole|all|window|everything|entire)$", "full", word)
    return word if word in _REGIONS else None


def parse_spot(inner):
    """Canonical server-side ``@{window · region}`` parser."""
    value = (inner or "").strip()
    if not value:
        return None
    rect = None
    match = _SPOT_RECT.search(value)
    if match:
        rect = [float(v) for v in match.groups()]
        value = value[:match.start()] + " " + value[match.end():]
        if any(v > 1 for v in rect):
            rect = [v / 100 for v in rect]
        if (rect[2] <= 0 or rect[3] <= 0
                or any(not math.isfinite(v) for v in rect)):
            rect = None
    region, words = None, []
    for part in re.split(r"\s*[·|;,]\s*", value):
        tokens = part.split()
        index = 0
        while index < len(tokens):
            two = (_region_of(tokens[index] + "-" + tokens[index + 1])
                   if index + 1 < len(tokens) else None)
            one = _region_of(tokens[index])
            if region is None and two:
                region = two
                index += 2
                continue
            if region is None and one:
                region = one
            else:
                words.append(tokens[index])
            index += 1
    window = " ".join(words).strip()
    if not window and not region and not rect:
        return None
    return {"window": window, "region": region or (None if rect else "full"),
            "rect": rect}


def parse_look(text):
    """Return canonical instruction, observation and target locator."""
    value, target = text or "", None
    match = _SPOT_TAG.search(value)
    if match:
        target = parse_spot(match.group(1))
        value = value[:match.start()] + " " + value[match.end():]
    observation = ""
    match = _LOOK_ARROW.search(value)
    if match:
        observation, value = match.group(1).strip(), value[:match.start()]
    return (re.sub(r"\s{2,}", " ", value).strip(), observation, target)


def _table_cells(row):
    value = (row or "").strip()
    if value.startswith("|"):
        value = value[1:]
    if value.endswith("|") and not value.endswith("\\|"):
        value = value[:-1]
    cells, current, index = [], "", 0
    while index < len(value):
        if value[index:index + 2] == "\\|":
            current += "|"
            index += 2
        elif value[index] == "|":
            cells.append(current.strip())
            current = ""
            index += 1
        else:
            current += value[index]
            index += 1
    cells.append(current.strip())
    return cells


def _delimiter_row(row):
    if "|" not in (row or ""):
        return False
    cells = _table_cells(row)
    return bool(cells) and all(cell and _DELIM_CELL.fullmatch(cell)
                               for cell in cells)


def _plain_inline(value):
    """The canonical twin of the current TUI/overlay inline display pass."""
    def hide(match):
        return chr(0xE000 + ord(match.group(1)))

    def link(match):
        bang, label, url = match.group(1), match.group(2).strip(), match.group(3)
        if bang:
            return label or url
        return url if not label or label == url else f"{label} ({url})"

    value = _INLINE_ESC.sub(hide, value or "")
    value = _INLINE_LINK.sub(link, value)
    value = _INLINE_AUTOLINK.sub(r"\1", value)
    value = _INLINE_CODE.sub(r"\1", value)
    value = _INLINE_STRIKE.sub(r"\1", value)
    value = _INLINE_BOLD.sub(r"\1", value)
    value = _INLINE_EM.sub(r"\1", value)
    return _INLINE_HIDDEN.sub(
        lambda match: chr(ord(match.group()) - 0xE000), value)


def _indent_of(value):
    leading = re.match(r"^[ \t]*", value or "").group()
    return len(leading.replace("\t", "    "))


def canonical_prompt(summary):
    """Parse prompt prose once for every non-UI responder consumer.

    The returned target dictionaries are the same compact representation the
    overlay already understands.  Nested body text remains display context;
    only top-level numbered items and numbered GFM table rows are steps.
    """
    lines = (summary or "").splitlines()
    context, steps = [], []
    fenced, index, seen = False, 0, False
    current_step, base_indent = False, 0
    while index < len(lines):
        raw = lines[index]
        stripped = raw.strip()
        if stripped.startswith(("```", "~~~")):
            fenced = not fenced
            if not seen:
                context.append(raw)
            index += 1
            continue
        if fenced:
            if not seen:
                context.append(raw)
            index += 1
            continue
        if ("|" in raw and index + 1 < len(lines)
                and _delimiter_row(lines[index + 1])):
            current_step = False
            header = [_plain_inline(cell) for cell in _table_cells(raw)]
            cursor = index + 2
            rows = []
            while (cursor < len(lines) and "|" in lines[cursor]
                   and lines[cursor].strip()
                   and not _delimiter_row(lines[cursor])):
                rows.append([_plain_inline(cell)
                             for cell in _table_cells(lines[cursor])])
                cursor += 1
            checklist = any(cells and _STEP_CELL.fullmatch(cells[0])
                            for cells in rows)
            if not checklist and not seen:
                context.append(" · ".join(cell for cell in header if cell))
            for cells in rows:
                if checklist and cells and _STEP_CELL.fullmatch(cells[0]):
                    parts, observation, target = [], "", None
                    for cell in cells[1:]:
                        text, cell_observation, cell_target = parse_look(cell)
                        if text:
                            parts.append(text)
                        observation = observation or cell_observation
                        target = target or cell_target
                    instruction = " · ".join(parts) or cells[0]
                    steps.append({"instruction": instruction,
                                  "observation": observation,
                                  "target": target})
                    seen = True
                elif not seen:
                    context.append(" · ".join(cell for cell in cells if cell))
            index = cursor
            continue
        display = _plain_inline(raw.rstrip())
        match = _STEP_LINE.fullmatch(display.strip())
        indent = _indent_of(raw)
        if match and not (current_step and indent > base_indent):
            body = _TASK_MARKER.sub("", match.group(2))
            instruction, observation, target = parse_look(body)
            steps.append({"instruction": instruction,
                          "observation": observation, "target": target})
            seen = True
            current_step, base_indent = True, indent
        elif current_step and indent > base_indent:
            pass
        else:
            current_step = False
            if not seen and stripped:
                context.append(re.sub(r"^#+\s*", "", display).strip())
        index += 1
    return {"context": "\n".join(context).strip(), "steps": steps}


def request_revision(request):
    """Hash every immutable request field except the two self-descriptors."""
    if not isinstance(request, dict):
        raise AdapterError("malformed", "request must be an object")
    body = {key: value for key, value in request.items()
            if key not in ("request_revision", "request_digest")}
    return _tagged(_REQUEST_REVISION_DOMAIN, body)


def stamp_request_contract(request):
    """Stamp canonical response semantics and revision before publication."""
    parsed = canonical_prompt(request.get("summary", ""))
    response = request.get("response")
    response = response if isinstance(response, dict) else {}
    mode = response.get("mode")
    if mode not in ("checklist", "text", "choice", "ack"):
        mode = "checklist" if parsed["steps"] else "prose"
    options = response.get("options") if mode == "choice" else []
    options = [dict(option) for option in options] if isinstance(options, list) else []
    checklist = request.get("checklist")
    ids = []
    if isinstance(checklist, dict) and isinstance(checklist.get("steps"), list):
        ids = [row.get("id") if isinstance(row, dict) else None
               for row in checklist["steps"]]
    steps = []
    for index, descriptor in enumerate(parsed["steps"]):
        step_id = (ids[index] if index < len(ids) and _safe_id(ids[index])
                   else f"step-{index + 1:03d}")
        steps.append({"id": step_id, "index": index + 1, **descriptor})
    contract = {
        "schema": CONTRACT_SCHEMA, "schema_version": SCHEMA_VERSION,
        "mode": mode, "context": parsed["context"],
        "options": options,
        "choice_cardinality": ({"minimum": 1, "maximum": 1}
                               if mode == "choice" else None),
        "steps": steps,
    }
    request["responder"] = contract
    request["request_revision"] = request_revision(request)
    return contract


def refresh_request_revision(request):
    request["request_revision"] = request_revision(request)
    return request["request_revision"]


def _event(kind, **fields):
    return {"schema": EVENT_SCHEMA, "schema_version": SCHEMA_VERSION,
            "type": kind, **fields}


def _result(ok, kind, **fields):
    return {"schema": RESULT_SCHEMA, "schema_version": SCHEMA_VERSION,
            "ok": bool(ok), "event": _event(kind, **fields)}


def _contains_capture_field(value):
    forbidden = {"image", "images", "capture", "captures", "pixel", "pixels",
                 "b64", "mime", "mimetype", "screenshot", "frame"}
    if isinstance(value, dict):
        return any(str(key).lower().replace("_", "") in forbidden
                   or _contains_capture_field(child)
                   for key, child in value.items())
    if isinstance(value, list):
        return any(_contains_capture_field(child) for child in value)
    return False


def verify_commit_receipt(receipt, *, expected_request_id,
                          expected_request_revision, expected_submission_id,
                          expected_submission_digest, expected_answer_digest,
                          signing_key=None):
    """Authenticate a non-authorizing transaction receipt and caller bindings."""
    import server
    if not isinstance(receipt, dict) or not isinstance(
            receipt.get("authentication"), dict):
        return {"valid": False, "authenticated": False,
                "authorizing": False, "reason": "missing-receipt"}
    auth = receipt["authentication"]
    auth_body = {key: value for key, value in auth.items()
                 if key not in ("digest", "signature")}
    body = {key: value for key, value in receipt.items()
            if key != "authentication"}
    try:
        key = signing_key or server.receipt_signing_key(create=False)
        payload = canonical_json({**body, "authentication": auth_body})
        digest = "sha256:" + hashlib.sha256(
            _COMMIT_DIGEST_DOMAIN + payload).hexdigest()
        signature = "hmac-sha256:" + hmac.new(
            key, _COMMIT_SIGNATURE_DOMAIN + payload + b"\0" +
            digest.encode("ascii"), hashlib.sha256).hexdigest()
        key_id = "sha256:" + hashlib.sha256(key).hexdigest()
    except Exception:
        return {"valid": False, "authenticated": False,
                "authorizing": False, "reason": "verification-unavailable"}
    if (auth_body != {"algorithm": "hmac-sha256", "key_id": key_id}
            or not isinstance(auth.get("digest"), str)
            or not hmac.compare_digest(auth["digest"], digest)
            or not isinstance(auth.get("signature"), str)
            or not hmac.compare_digest(auth["signature"], signature)):
        return {"valid": False, "authenticated": False,
                "authorizing": False, "reason": "tampered"}
    request = body.get("request") if isinstance(body.get("request"), dict) else {}
    submission = (body.get("submission")
                  if isinstance(body.get("submission"), dict) else {})
    if (body.get("schema") != COMMIT_RECEIPT_SCHEMA
            or body.get("schema_version") != SCHEMA_VERSION
            or body.get("outcome") != "committed"
            or body.get("authorizing") is not False
            or request.get("id") != expected_request_id
            or request.get("revision") != expected_request_revision
            or submission.get("id") != expected_submission_id
            or submission.get("digest") != expected_submission_digest
            or submission.get("answer_digest") != expected_answer_digest):
        return {"valid": False, "authenticated": True,
                "authorizing": False, "reason": "wrong-binding"}
    return {"valid": True, "authenticated": True,
            "authorizing": False, "reason": None,
            "receipt_digest": digest}


def _exact_remote_format(commit_receipt):
    """Select mapping only; a successful caller must also authenticate it."""
    if not isinstance(commit_receipt, dict):
        raise AdapterError("malformed", "missing transaction receipt")
    value = commit_receipt.get("answer_format")
    if value is None and "answer_format" not in commit_receipt:
        return False
    if value == REMOTE_EXACT_FORMAT:
        return True
    raise AdapterError("malformed", "unsupported remote answer format")


def verify_remote_receipt(receipt, *, commit_receipt, request,
                          geometry_generation, submission_id, answer,
                          lease_token, signing_key=None):
    """Verify a remote outcome against acquired context and outbound bytes.

    The canonical checklist receipt stays unchanged. A second, non-authorizing
    commit receipt binds it to the exact remote transaction. Nothing is inferred
    from echoed event digests or from private spool records.
    """
    import server
    invalid = {"valid": False, "authenticated": False,
               "authorizing": False, "reason": "missing-expected-binding"}
    try:
        if (not isinstance(request, dict) or request.get("schema") != REQUEST_SCHEMA
                or request.get("schema_version") != SCHEMA_VERSION
                or not _valid_lease_binding(request.get("id"), request.get("revision"), lease_token)
                or not _safe_id(submission_id)
                or not isinstance(geometry_generation, int)
                or isinstance(geometry_generation, bool)):
            return invalid
        exact = _exact_remote_format(commit_receipt)
        internal = Responder._validate_remote_answer({
            "id": request["id"], "request_digest": request.get("request_digest"),
            "responder": request["response"]}, answer, exact=exact)
        verified = verify_commit_receipt(
            commit_receipt, expected_request_id=request["id"],
            expected_request_revision=request["revision"],
            expected_submission_id=submission_id,
            expected_submission_digest=submission_digest(
                request["id"], request["revision"], geometry_generation,
                submission_id, answer, lease_token=lease_token),
            expected_answer_digest=answer_digest(answer), signing_key=signing_key)
        if not verified["valid"]:
            return verified
        if request["response"]["mode"] != "checklist":
            if receipt != commit_receipt:
                return {**invalid, "reason": "wrong-receipt"}
            expected_completion = (internal.get("response_completion") or {}).get("status", "complete")
            if receipt.get("completion", "complete") != expected_completion:
                return {**invalid, "authenticated": True, "reason": "wrong-completion"}
            return {**verified, "exact_text": exact}
        provider = request["provider"]
        digest = request["request_digest"]
        if not isinstance(digest, str) or not digest:
            return invalid
        checked = server.verify_checklist_receipt(
            receipt, expected_request_id=request["id"],
            expected_request_digest=digest,
            expected_provider_instance=provider["instance"],
            expected_provider_generation=provider["generation"],
            expected_step_ids=[row["id"] for row in request["response"]["steps"]],
            signing_key=signing_key)
        if not checked["valid"]:
            return checked
        if receipt["steps"] != internal["checklist"]["steps"]:
            return {**invalid, "authenticated": True, "reason": "wrong-answer"}
        return {**checked, "commit_receipt_digest": verified["receipt_digest"], "exact_text": exact}
    except (KeyError, TypeError, ValueError, AdapterError):
        return invalid


class Responder:
    """One authoritative FIFO/lease/commit transaction over a spool."""

    def __init__(self, spool, *, clock=None, token_factory=None,
                 fault_hook=None):
        self.spool = os.path.abspath(spool)
        self.clock = clock or time.time
        self.token_factory = token_factory or (lambda: secrets.token_urlsafe(32))
        self.fault_hook = fault_hook
        self.state = os.path.join(self.spool, "responder")

    def _fault(self, boundary):
        if self.fault_hook is not None:
            self.fault_hook(boundary)

    def _paths(self):
        return {name: os.path.join(self.state, name)
                for name in ("leases", "commits", "terminal")}

    def _ensure(self):
        paths = (self.spool, self.state, *self._paths().values(),
                 *(os.path.join(self.spool, name) for name in ("requests", "answers", "targets")))
        for path in paths:
            os.makedirs(path, mode=0o700, exist_ok=True)
            # chmod only on drift: an unconditional chmod raises IN_ATTRIB on
            # requests/, the overlay's monitor rescans, and every rescan spawned
            # another --one-shot responder (~15/s, a core of GNOME Shell; host-oq7.6.1).
            if stat.S_IMODE(os.stat(path).st_mode) != 0o700:
                os.chmod(path, 0o700)

    @contextmanager
    def _transaction(self):
        self._ensure()
        path = os.path.join(self.state, "transaction.lock")
        fd = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                     0o600)
        try:
            os.fchmod(fd, 0o600)
            fcntl.flock(fd, fcntl.LOCK_EX)
            self._prune_locked()
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    def _prune_locked(self):
        """Bound metadata without discarding a recoverable prepared WAL."""
        now = self.clock()
        for group in ("leases", "commits", "terminal"):
            directory = self._paths()[group]
            for name in os.listdir(directory):
                if not name.endswith(".json") or ".tmp" in name:
                    continue
                path = os.path.join(directory, name)
                record = self._read(path)
                if group == "leases":
                    expired = (_finite_number(record.get("expires_at"))
                               and float(record["expires_at"]) <= now)
                else:
                    expired = (_finite_number(record.get("retain_until"))
                               and float(record["retain_until"]) <= now)
                    if group == "commits" and record.get("state") == "prepared":
                        request_id = record.get("request_id")
                        recoverable = (_safe_id(request_id) and os.path.exists(
                            os.path.join(self.spool, "requests",
                                         request_id + ".json")))
                        if "recovery_answer" in record and (not recoverable or self._read(
                                self._path("terminal", request_id))):
                            record.pop("recovery_answer")
                            self._write(path, record)
                        expired = expired and not recoverable
                    # A delivered terminal tombstone is what lets an expired
                    # or already-answered stale request stop pinning FIFO. Keep
                    # it only while that request file still exists; once the
                    # server removes the request, normal TTL pruning applies.
                    if group == "terminal":
                        request_id = record.get("request_id")
                        request_present = (_safe_id(request_id) and os.path.exists(
                            os.path.join(self.spool, "requests",
                                         request_id + ".json")))
                        expired = expired and not request_present
                if expired:
                    try:
                        os.unlink(path)
                    except FileNotFoundError:
                        pass

    def _path(self, group, request_id):
        if not _safe_id(request_id):
            raise AdapterError("malformed", "unsafe request id")
        return os.path.join(self._paths()[group], request_id + ".json")

    def _read(self, path, *, required=False):
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        try:
            fd = os.open(path, flags)
        except FileNotFoundError:
            if required:
                raise AdapterError("missing", os.path.basename(path))
            return None
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_STATE_BYTES:
                raise AdapterError("malformed-state", os.path.basename(path))
            raw = b""
            while len(raw) <= MAX_STATE_BYTES:
                part = os.read(fd, min(65536, MAX_STATE_BYTES + 1 - len(raw)))
                if not part:
                    break
                raw += part
        finally:
            os.close(fd)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise AdapterError("malformed-state", str(error)) from error
        if not isinstance(value, dict):
            raise AdapterError("malformed-state", os.path.basename(path))
        return value

    def _write(self, path, value):
        raw = canonical_json(value)
        if len(raw) > MAX_STATE_BYTES:
            raise AdapterError("malformed", "state record too large")
        tmp = path + f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(fd, raw[offset:])
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        else:
            os.close(fd)
        os.replace(tmp, path)

    def _publish_answer(self, path, value):
        """Publish one complete answer with an atomic no-clobber link."""
        raw = canonical_json(value)
        tmp = path + f".tmp-{os.getpid()}-{uuid.uuid4().hex}"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL |
                     getattr(os, "O_NOFOLLOW", 0), 0o600)
        try:
            offset = 0
            while offset < len(raw):
                offset += os.write(fd, raw[offset:])
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        else:
            os.close(fd)
        try:
            os.link(tmp, path, follow_symlinks=False)
        finally:
            os.unlink(tmp)

    def _load_request(self, request_id, *, allow_empty_local_checklist=False):
        path = os.path.join(self.spool, "requests", request_id + ".json")
        request = self._read(path, required=True)
        try:
            expected_revision = request_revision(request)
        except (TypeError, ValueError, OverflowError) as error:
            raise AdapterError("malformed", "request is not canonical JSON") from error
        if (request.get("id") != request_id
                or not _finite_number(request.get("created"))
                or not _finite_number(request.get("deadline"))
                or request.get("request_revision") != expected_revision):
            raise AdapterError("malformed", "request identity/revision invalid")
        contract = request.get("responder")
        if (not isinstance(contract, dict)
                or contract.get("schema") != CONTRACT_SCHEMA
                or contract.get("schema_version") != SCHEMA_VERSION
                or contract.get("mode") not in
                    ("checklist", "choice", "text", "ack", "prose")
                or not isinstance(contract.get("steps"), list)
                or not isinstance(contract.get("options"), list)):
            raise AdapterError("malformed", "request contract invalid")
        steps = contract["steps"]
        step_ids = []
        for index, row in enumerate(steps, 1):
            if (not isinstance(row, dict) or not _safe_id(row.get("id"))
                    or row.get("index") != index
                    or not isinstance(row.get("instruction"), str)
                    or not isinstance(row.get("observation"), str)
                    or (row.get("target") is not None
                        and not isinstance(row.get("target"), dict))):
                raise AdapterError("malformed", "request step contract invalid")
            step_ids.append(row["id"])
        if len(step_ids) != len(set(step_ids)):
            raise AdapterError("malformed", "duplicate request step id")
        options = contract["options"]
        option_values = []
        for option in options:
            if (not isinstance(option, dict)
                    or not isinstance(option.get("value"), str)
                    or not option["value"]
                    or not isinstance(option.get("label"), str)
                    or not option["label"]):
                raise AdapterError("malformed", "request option contract invalid")
            option_values.append(option["value"])
        if len(option_values) != len(set(option_values)):
            raise AdapterError("malformed", "duplicate request option")
        if ((contract["mode"] == "choice" and not options)
                or (contract["mode"] != "choice" and options)
                or (contract["mode"] == "checklist" and not steps
                    and not allow_empty_local_checklist)):
            raise AdapterError("malformed", "request mode cardinality invalid")
        checklist = request.get("checklist")
        if isinstance(checklist, dict):
            import server
            digest = request.get("request_digest")
            try:
                expected_digest = server.checklist_request_digest(request)
            except (TypeError, ValueError, OverflowError) as error:
                raise AdapterError(
                    "malformed", "checklist request is not canonical JSON") from error
            if (not isinstance(digest, str)
                    or not hmac.compare_digest(digest, expected_digest)):
                raise AdapterError("malformed", "checklist request digest invalid")
        return request

    def _requests(self):
        directory = os.path.join(self.spool, "requests")
        rows = []
        for name in os.listdir(directory):
            if not name.endswith(".json") or ".tmp" in name:
                continue
            request_id = name[:-5]
            if not _safe_id(request_id):
                raise AdapterError("malformed", f"unsafe request file {name}")
            rows.append(self._load_request(request_id))
        rows.sort(key=lambda row: (row["created"], row["id"]))
        return rows

    @staticmethod
    def _validate_target_record(request, target):
        if _contains_capture_field(target):
            raise AdapterError("malformed-state", "target contains capture fields")
        rows = target.get("targets")
        expected = [row["id"] for row in request["responder"]["steps"]]
        if (not isinstance(rows, list)
                or [row.get("id") if isinstance(row, dict) else None
                    for row in rows] != expected):
            raise AdapterError("malformed-state", "target step order invalid")
        states = []
        for row in rows:
            state = row.get("state")
            identity = row.get("identity")
            geometry = row.get("geometry")
            if state not in ("resolved", "untargeted", "missing", "ambiguous"):
                raise AdapterError("malformed-state", "target step state invalid")
            states.append(state)
            if state == "untargeted":
                if identity is not None or geometry is not None:
                    raise AdapterError("malformed-state", "untargeted geometry invalid")
                continue
            if state in ("missing", "ambiguous"):
                if geometry is not None:
                    raise AdapterError("malformed-state", "unresolved geometry invalid")
                continue
            if not isinstance(identity, dict) or not isinstance(geometry, dict):
                raise AdapterError("malformed-state", "resolved target incomplete")
            kind = identity.get("kind")
            monitor_index = identity.get("monitor")
            if (kind not in ("mutter-window", "mutter-monitor")
                    or not isinstance(monitor_index, int)
                    or isinstance(monitor_index, bool) or monitor_index < 0):
                raise AdapterError("malformed-state", "target identity invalid")
            if kind == "mutter-window" and (
                    not isinstance(identity.get("stable_sequence"), str)
                    or not identity["stable_sequence"]):
                raise AdapterError("malformed-state", "window identity invalid")
            rect = geometry.get("rect")
            monitor = geometry.get("monitor")
            logical = (monitor.get("logical")
                       if isinstance(monitor, dict) else None)
            if (geometry.get("coordinate_space") !=
                    "monitor-relative-physical-pixels/v1"
                    or not _finite_number(geometry.get("scale"))
                    or float(geometry["scale"]) <= 0
                    or not isinstance(rect, dict)
                    or not isinstance(monitor, dict)
                    or monitor.get("index") != monitor_index
                    or not isinstance(logical, dict)):
                raise AdapterError("malformed-state", "target geometry invalid")
            for key in ("x", "y", "width", "height"):
                if (not isinstance(rect.get(key), int)
                        or isinstance(rect.get(key), bool)):
                    raise AdapterError("malformed-state", "physical rect invalid")
            if rect["x"] < 0 or rect["y"] < 0 or rect["width"] <= 0 or rect["height"] <= 0:
                raise AdapterError("malformed-state", "physical rect empty")
            if (not all(_finite_number(logical.get(key))
                        for key in ("x", "y", "width", "height"))
                    or float(logical["width"]) <= 0
                    or float(logical["height"]) <= 0):
                raise AdapterError("malformed-state", "logical monitor invalid")
        aggregate = "untargeted"
        if "ambiguous" in states:
            aggregate = "ambiguous"
        elif "missing" in states:
            aggregate = "missing"
        elif "resolved" in states:
            aggregate = "resolved"
        if target.get("state") != aggregate:
            raise AdapterError("malformed-state", "aggregate target state invalid")

    def _target(self, request):
        path = os.path.join(self.spool, "targets", request["id"] + ".json")
        target = self._read(path)
        if target is None:
            return {"schema": TARGET_SCHEMA, "schema_version": SCHEMA_VERSION,
                    "request_id": request["id"],
                    "request_revision": request["request_revision"],
                    "generation": 0, "state": "missing",
                    "reason": "target-state-unpublished", "targets": []}
        if (target.get("schema") != TARGET_SCHEMA
                or target.get("schema_version") != SCHEMA_VERSION
                or target.get("request_id") != request["id"]
                or target.get("request_revision") != request["request_revision"]
                or not isinstance(target.get("generation"), int)
                or isinstance(target.get("generation"), bool)
                or target["generation"] < 1
                or target.get("state") not in
                    ("resolved", "untargeted", "missing", "ambiguous")
                or not isinstance(target.get("targets"), list)):
            raise AdapterError("malformed-state", "target record invalid")
        self._validate_target_record(request, target)
        return target

    def _lease(self, request):
        path = self._path("leases", request["id"])
        lease = self._read(path)
        if lease is None:
            return None
        if (lease.get("schema") != LEASE_SCHEMA
                or lease.get("schema_version") != SCHEMA_VERSION
                or lease.get("request_id") != request["id"]
                or lease.get("request_revision") != request["request_revision"]
                or not _finite_number(lease.get("expires_at"))
                or not isinstance(lease.get("token_digest"), str)):
            raise AdapterError("malformed-state", "lease record invalid")
        if float(lease["expires_at"]) <= self.clock():
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            return None
        return lease

    @staticmethod
    def _token_digest(token):
        return _tagged(b"human-copilot.responder-lease-token/v1\0", token)

    @staticmethod
    def _consumer_digest(consumer):
        return _tagged(b"human-copilot.responder-consumer/v1\0", consumer)

    def _envelope(self, request, target, lease_token=None, lease=None,
                  queue_count=1):
        contract = request["responder"]
        envelope = {
            "schema": REQUEST_SCHEMA, "schema_version": SCHEMA_VERSION,
            "id": request["id"], "revision": request["request_revision"],
            "created": request["created"],
            "created_at": _iso(request["created"]),
            "deadline": request["deadline"],
            "deadline_at": _iso(request["deadline"]),
            "context": contract.get("context", ""),
            "response": {key: contract.get(key) for key in
                         ("mode", "choice_cardinality", "options", "steps")},
            "lifecycle": {"state": "leased" if lease else "pending"},
            "target": target,
            "queue": {"position": 1, "count": queue_count},
        }
        if isinstance(request.get("provider"), dict):
            envelope["provider"] = request["provider"]
        if contract["mode"] == "checklist":
            # _load_request has validated this against the canonical request.
            envelope["request_digest"] = request["request_digest"]
        if isinstance(request.get("instance"), dict):
            envelope["instance"] = request["instance"]
        if lease:
            envelope["lease"] = {
                "token": lease_token, "expires_at": float(lease["expires_at"]),
                "maximum_seconds": MAX_LEASE_SECONDS,
            }
        return envelope

    def _terminal_result(self, request_id):
        record = self._read(self._path("terminal", request_id))
        if record:
            return _result(False, record.get("event", "missing"),
                           request_id=request_id,
                           revision=record.get("request_revision"),
                           detail=record.get("detail"))
        return _result(False, "missing", request_id=request_id)

    def acquire(self, consumer, lease_seconds, resume=None,
                acknowledged_geometry_generation=None):
        if not isinstance(consumer, str) or not consumer or len(consumer) > 256:
            return _result(False, "malformed", detail="consumer")
        if not _finite_number(lease_seconds):
            return _result(False, "malformed", detail="lease_seconds")
        if (acknowledged_geometry_generation is not None
                and (not isinstance(acknowledged_geometry_generation, int)
                     or isinstance(acknowledged_geometry_generation, bool)
                     or acknowledged_geometry_generation < 0)):
            return _result(False, "malformed",
                           detail="acknowledged_geometry_generation")
        if resume is not None:
            if (not isinstance(resume, dict)
                    or set(resume) != {"request_id", "revision", "token"}
                    or not _valid_lease_binding(
                        resume.get("request_id"), resume.get("revision"),
                        resume.get("token"))):
                return _result(False, "malformed", detail="resume binding")
        duration = min(MAX_LEASE_SECONDS,
                       max(MIN_LEASE_SECONDS, float(lease_seconds)))
        try:
            with self._transaction():
                # A reconnect names the request it previously held. Terminal
                # knowledge for that exact request wins over queue traversal,
                # even after its stale request file would otherwise be skipped.
                if resume is not None:
                    resumed_id = resume.get("request_id")
                    if not _safe_id(resumed_id):
                        return _result(False, "malformed", detail="resume request_id")
                    terminal = self._read(self._path("terminal", resumed_id))
                    if terminal:
                        return _result(False, terminal.get("event", "missing"),
                                       request_id=resumed_id,
                                       revision=terminal.get("request_revision"),
                                       detail=terminal.get("detail"))
                    commit = self._read(self._path("commits", resumed_id))
                    if commit and commit.get("state") == "prepared":
                        return _result(False, "conflict",
                                       request_id=resumed_id,
                                       revision=commit.get("request_revision"),
                                       detail="commit-prepared")
                    answer_path = os.path.join(
                        self.spool, "answers", resumed_id + ".json")
                    if (os.path.exists(answer_path)
                            or (commit and commit.get("state") in
                                ("published", "acknowledged"))):
                        return _result(False, "already-answered",
                                       request_id=resumed_id,
                                       revision=(commit or {}).get(
                                           "request_revision"))
                requests = self._requests()
                while requests:
                    request = requests[0]
                    terminal_path = self._path("terminal", request["id"])
                    terminal = self._read(terminal_path)
                    if terminal:
                        if terminal.get("delivered") is not True:
                            terminal["delivered"] = True
                            self._write(terminal_path, terminal)
                            return _result(False, terminal.get("event", "missing"),
                                           request_id=request["id"],
                                           revision=request["request_revision"],
                                           detail=terminal.get("detail"))
                        requests.pop(0)
                        continue
                    commit = self._read(self._path("commits", request["id"]))
                    if commit and commit.get("state") == "prepared":
                        if commit.get("origin") == "remote" and "recovery_answer" in commit:
                            recovered = self._recover_locked(request, commit)
                            if not recovered["ok"]:
                                return recovered
                            self._record_terminal_locked(request, "already-answered", delivered=True)
                            return _result(False, "already-answered", request_id=request["id"])
                        return _result(False, "conflict",
                                       request_id=request["id"],
                                       revision=request["request_revision"],
                                       detail="commit-prepared")
                    if os.path.exists(os.path.join(
                            self.spool, "answers", request["id"] + ".json")) \
                            or (commit and commit.get("state") in
                                ("published", "acknowledged")):
                        self._record_terminal_locked(request, "already-answered",
                                                     delivered=True)
                        return _result(False, "already-answered",
                                       request_id=request["id"])
                    if float(request["deadline"]) <= self.clock():
                        self._record_terminal_locked(request, "expired",
                                                     delivered=True)
                        return _result(False, "expired", request_id=request["id"],
                                       revision=request["request_revision"])
                    break
                if not requests:
                    return _result(True, "no-request")
                request = requests[0]
                target = self._target(request)
                lease = self._lease(request)
                token = None
                if lease:
                    token = resume.get("token") if resume else None
                    if (not isinstance(token, str)
                            or not hmac.compare_digest(
                                lease["token_digest"], self._token_digest(token))
                            or resume.get("request_id") != request["id"]
                            or resume.get("revision") != request["request_revision"]):
                        return _result(False, "conflict",
                                       request_id=request["id"],
                                       revision=request["request_revision"],
                                       lease_expires_at=lease["expires_at"])
                elif resume:
                    return _result(False, "lease-expired",
                                   request_id=request["id"],
                                   revision=request["request_revision"])
                else:
                    token = self.token_factory()
                now = self.clock()
                lease = {
                    "schema": LEASE_SCHEMA, "schema_version": SCHEMA_VERSION,
                    "request_id": request["id"],
                    "request_revision": request["request_revision"],
                    "token_digest": self._token_digest(token),
                    "consumer_digest": self._consumer_digest(consumer),
                    "acquired_at": lease.get("acquired_at", now) if lease else now,
                    "expires_at": now + duration,
                }
                self._write(self._path("leases", request["id"]), lease)
                kind = target["state"]
                if kind == "resolved" and acknowledged_geometry_generation not in (
                        None, target["generation"]):
                    kind = "moved"
                elif kind in ("resolved", "untargeted"):
                    kind = "ready"
                return _result(True, kind,
                    request=self._envelope(request, target, token, lease,
                                           len(requests)))
        except AdapterError as error:
            return _result(False, error.code, detail=error.detail)

    def renew(self, request_id, revision, token, lease_seconds,
              acknowledged_geometry_generation=None):
        if not _valid_lease_binding(request_id, revision, token):
            return _result(False, "malformed", detail="renew binding")
        if not _finite_number(lease_seconds):
            return _result(False, "malformed", detail="lease_seconds")
        if (not isinstance(acknowledged_geometry_generation, int)
                or isinstance(acknowledged_geometry_generation, bool)
                or acknowledged_geometry_generation < 0):
            return _result(False, "malformed",
                           detail="acknowledged_geometry_generation")
        duration = min(MAX_LEASE_SECONDS,
                       max(MIN_LEASE_SECONDS, float(lease_seconds)))
        try:
            with self._transaction():
                request = self._load_request(request_id)
                if request["request_revision"] != revision:
                    return _result(False, "stale", request_id=request_id)
                lease = self._lease(request)
                if (not lease or not isinstance(token, str)
                        or not hmac.compare_digest(
                            lease["token_digest"], self._token_digest(token))):
                    return _result(False, "lease-expired", request_id=request_id)
                lease["expires_at"] = self.clock() + duration
                self._write(self._path("leases", request_id), lease)
                target = self._target(request)
                kind = target["state"]
                if kind == "resolved" and acknowledged_geometry_generation != target["generation"]:
                    kind = "moved"
                elif kind in ("resolved", "untargeted"):
                    kind = "ready"
                return _result(True, kind,
                    request=self._envelope(request, target, token, lease))
        except AdapterError as error:
            if error.code == "missing":
                return self._terminal_result(request_id)
            return _result(False, error.code, detail=error.detail)

    def release(self, request_id, revision, token):
        if not _valid_lease_binding(request_id, revision, token):
            return _result(False, "malformed", detail="release binding")
        try:
            with self._transaction():
                request = self._load_request(request_id)
                if request["request_revision"] != revision:
                    return _result(False, "stale", request_id=request_id)
                lease = self._lease(request)
                if (not lease or not isinstance(token, str)
                        or not hmac.compare_digest(
                            lease["token_digest"], self._token_digest(token))):
                    return _result(False, "lease-expired", request_id=request_id)
                os.unlink(self._path("leases", request_id))
                return _result(True, "released", request_id=request_id,
                               revision=revision)
        except AdapterError as error:
            if error.code == "missing":
                return self._terminal_result(request_id)
            return _result(False, error.code, detail=error.detail)

    @staticmethod
    def _validate_remote_answer(request, answer, *, exact=True):
        if not isinstance(answer, dict) or _contains_capture_field(answer):
            raise AdapterError("malformed", "capture/image fields are forbidden")
        common = {"schema", "schema_version", "mode", "note"}
        if (answer.get("schema") != ANSWER_SCHEMA
                or answer.get("schema_version") != SCHEMA_VERSION):
            raise AdapterError("malformed", "answer schema")
        mode = request["responder"]["mode"]
        if answer.get("mode") != mode:
            raise AdapterError("malformed", "answer mode")
        note = answer.get("note", "")
        if not isinstance(note, str) or len(note) > MAX_NOTE_CHARS:
            raise AdapterError("malformed", "answer note")
        internal = {"text": note if exact else note.strip(), "cancelled": False, "images": []}
        if exact:
            encoded = canonical_json(answer)
            if len(encoded) > MAX_COMMAND_BYTES:
                raise AdapterError("malformed", "remote answer too large")
            internal["remote_response"] = {
                "schema": REMOTE_RESPONSE_SCHEMA, "schema_version": SCHEMA_VERSION,
                "answer": json.loads(encoded)}
        incomplete = False
        if mode != "checklist":
            common.add("completion")
            completion = answer.get("completion", "complete")
            if completion not in ("complete", "not_done"):
                raise AdapterError("malformed", "answer completion")
            incomplete = completion == "not_done"
            if incomplete:
                internal["response_completion"] = {
                    "schema": "human-copilot.response-completion/v1", "schema_version": 1,
                    "mode": mode, "status": "not_done"}
        if mode == "choice":
            if set(answer) - (common | {"choices"}):
                raise AdapterError("malformed", "unknown choice field")
            choices = answer.get("choices")
            if not isinstance(choices, list) or len(choices) != (0 if incomplete else 1):
                raise AdapterError("malformed", "choice cardinality")
            if incomplete:
                return internal
            options = request["responder"]["options"]
            matches = [row for row in options
                       if isinstance(row, dict) and row.get("value") == choices[0]]
            if len(matches) != 1:
                raise AdapterError("malformed", "unknown choice")
            internal["choice"] = {"value": matches[0]["value"],
                                  "label": matches[0]["label"]}
        elif mode == "checklist":
            if set(answer) - (common | {"steps"}):
                raise AdapterError("malformed", "unknown checklist field")
            rows = answer.get("steps")
            specs = request["responder"]["steps"]
            if not isinstance(rows, list) or len(rows) != len(specs) or not rows:
                raise AdapterError("malformed", "checklist cardinality")
            mapped, report = [], []
            glyph = {"pass": "✔", "fail": "✘", "not_done": "○"}
            verdict = {"pass": "done", "fail": "failed",
                       "not_done": "not-done"}
            for index, (row, spec) in enumerate(zip(rows, specs), 1):
                if (not isinstance(row, dict)
                        or set(row) - {"id", "verdict", "note"}
                        or row.get("id") != spec.get("id")
                        or row.get("verdict") not in verdict
                        or not isinstance(row.get("note", ""), str)
                        or len(row.get("note", "")) > MAX_NOTE_CHARS):
                    raise AdapterError("malformed", "ordered checklist verdict")
                row_note = row.get("note", "")
                if not exact:
                    row_note = row_note.strip()
                mapped.append({"id": spec["id"],
                               "verdict": verdict[row["verdict"]],
                               "note": row_note})
                report.append(f"{glyph[row['verdict']]} {index}. " +
                              ("NOT DONE" if row["verdict"] == "not_done"
                               else row["verdict"].upper()) +
                              ((" — " + row_note) if row_note else ""))
            global_note = note if exact else note.strip()
            internal["text"] = "\n".join(report + ([global_note] if global_note else []))
            internal["checklist"] = {
                "schema": "human-copilot.checklist-answer/v1",
                "schema_version": 1, "request_id": request["id"],
                "request_digest": request.get("request_digest"),
                "steps": mapped,
            }
        elif mode in ("text", "prose"):
            if set(answer) - (common | {"text"}):
                raise AdapterError("malformed", "unknown text field")
            text = answer.get("text")
            if (not isinstance(text, str) or len(text) > MAX_TEXT_CHARS
                    or bool(text.strip()) == incomplete):
                raise AdapterError("malformed", "answer text")
            if not incomplete:
                internal["text"] = text
        elif mode == "ack":
            if set(answer) - (common | {"confirmed"}):
                raise AdapterError("malformed", "unknown ack field")
            if answer.get("confirmed") is not (False if incomplete else True):
                raise AdapterError("malformed", "ack confirmation")
        else:
            raise AdapterError("malformed", "unsupported mode")
        return internal

    def _commit_receipt(self, request, submission_id, submission_hash,
                        submitted_answer_hash, answered_at, *, completion=None, answer_format=None):
        import server
        body = {
            "schema": COMMIT_RECEIPT_SCHEMA, "schema_version": SCHEMA_VERSION,
            "request": {"id": request["id"],
                        "revision": request["request_revision"],
                        "requested_at": _iso(request["created"]),
                        "deadline_at": _iso(request["deadline"])},
            "submission": {"id": submission_id, "digest": submission_hash,
                           "answer_digest": submitted_answer_hash},
            "answered_at": _iso(answered_at),
            "mode": request["responder"]["mode"],
            "outcome": "committed", "authorizing": False,
        }
        if completion == "not_done":
            body["completion"] = "not_done"
        if answer_format is not None:
            body["answer_format"] = answer_format
        return server._sign_receipt_body(
            body, server.receipt_signing_key(),
            digest_domain=_COMMIT_DIGEST_DOMAIN,
            signature_domain=_COMMIT_SIGNATURE_DOMAIN)

    def _make_receipt(self, request, answer, submission_id, submission_hash,
                      submitted_answer_hash, answered_at):
        import server
        if isinstance(answer.get("checklist"), dict):
            try:
                return server.build_checklist_receipt(
                    request, answer, answered_at=answered_at,
                    preserve_notes="remote_response" in answer)
            except server.ChecklistReceiptError as error:
                raise AdapterError("malformed", error.code) from error
        return self._commit_receipt(request, submission_id, submission_hash,
                                    submitted_answer_hash, answered_at,
                                    completion=(answer.get("response_completion") or {}).get("status"),
                                    answer_format=REMOTE_EXACT_FORMAT if "remote_response" in answer else None)

    def _commit_locked(self, request, answer, submission_id, submission_hash,
                       submitted_answer_hash,
                       *, lifecycle=False, origin="remote", remote_answer=None):
        request_id = request["id"]
        commit_path = self._path("commits", request_id)
        existing = self._read(commit_path)
        if existing:
            if (existing.get("submission_id") == submission_id
                    and existing.get("submission_digest") == submission_hash):
                if existing.get("state") == "prepared":
                    if existing.get("origin", "remote") != origin:
                        return _result(False, "conflict",
                                       request_id=request_id,
                                       revision=request["request_revision"],
                                       detail="commit-prepared")
                    resumed = self._resume_prepared_locked(
                        request, answer, existing, lifecycle=lifecycle)
                    if resumed.get("ok"):
                        resumed["event"]["type"] = "committed"
                    return resumed
                return _result(True, "idempotent", request_id=request_id,
                               revision=request["request_revision"],
                               receipt=existing.get("receipt"),
                               commit_receipt=existing.get("commit_receipt"),
                               submission_digest=existing.get("submission_digest"),
                               answer_digest=existing.get(
                                   "submitted_answer_digest"))
            kind = ("conflict" if existing.get("state") == "prepared"
                    else "already-answered")
            return _result(False, kind, request_id=request_id,
                           revision=request["request_revision"],
                           detail=("commit-prepared" if kind == "conflict"
                                   else None))
        internal_answer_digest = answer_digest(answer)
        answered_at = self.clock()
        receipt = self._make_receipt(request, answer, submission_id,
                                     submission_hash, submitted_answer_hash,
                                     answered_at)
        commit_receipt = (receipt if receipt.get("schema") == COMMIT_RECEIPT_SCHEMA
                          else self._commit_receipt(
                              request, submission_id, submission_hash,
                              submitted_answer_hash, answered_at,
                              answer_format=REMOTE_EXACT_FORMAT if "remote_response" in answer else None))
        final_answer = {**answer, "responder_commit": {
            "schema": COMMIT_RECORD_SCHEMA, "schema_version": SCHEMA_VERSION,
            "request_revision": request["request_revision"],
            "submission_id": submission_id,
            "answer_digest": internal_answer_digest,
        }, "receipt": receipt}
        if "remote_response" in answer:
            final_answer["commit_receipt"] = commit_receipt
        file_digest = _tagged(_ANSWER_DIGEST_DOMAIN, final_answer)
        record = {
            "schema": COMMIT_RECORD_SCHEMA, "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "request_revision": request["request_revision"],
            "submission_id": submission_id,
            "submission_digest": submission_hash,
            "submitted_answer_digest": submitted_answer_hash,
            "answer_digest": internal_answer_digest,
            "answer_file_digest": file_digest,
            "receipt": receipt, "commit_receipt": commit_receipt,
            "state": "prepared", "origin": origin,
            "prepared_at": answered_at,
            "retain_until": answered_at + RECORD_RETENTION_SECONDS,
        }
        if origin == "remote" and remote_answer is not None:
            if (_contains_capture_field(remote_answer)
                    or len(canonical_json(remote_answer)) > MAX_COMMAND_BYTES
                    or answer_digest(remote_answer) != submitted_answer_hash):
                raise AdapterError("malformed", "remote recovery payload invalid")
            # Exact typed answer only: no lease token, captures or second spool.
            # The existing signed commit receipt binds its digest.
            record["recovery_answer"] = remote_answer
        self._write(commit_path, record)
        self._fault("after-prepare")
        result = self._resume_prepared_locked(request, answer, record,
                                              lifecycle=lifecycle)
        if result.get("ok"):
            result["event"]["type"] = "committed"
        return result

    def _resume_prepared_locked(self, request, answer, record, *,
                                lifecycle=False):
        """Finish the exact prepared transaction after an interruption."""
        request_id = request["id"]
        internal_answer_digest = answer_digest(answer)
        if internal_answer_digest != record.get("answer_digest"):
            return _result(False, "conflict", request_id=request_id,
                           revision=request["request_revision"])
        final_answer = {**answer, "responder_commit": {
            "schema": COMMIT_RECORD_SCHEMA, "schema_version": SCHEMA_VERSION,
            "request_revision": request["request_revision"],
            "submission_id": record["submission_id"],
            "answer_digest": internal_answer_digest,
        }, "receipt": record.get("receipt")}
        if "remote_response" in answer:
            final_answer["commit_receipt"] = record.get("commit_receipt")
        file_digest = _tagged(_ANSWER_DIGEST_DOMAIN, final_answer)
        if file_digest != record.get("answer_file_digest"):
            return _result(False, "conflict", request_id=request_id,
                           revision=request["request_revision"])
        answer_path = os.path.join(self.spool, "answers", request_id + ".json")
        try:
            self._publish_answer(answer_path, final_answer)
        except FileExistsError:
            current = self._read(answer_path, required=True)
            if _tagged(_ANSWER_DIGEST_DOMAIN, current) != file_digest:
                return _result(False, "conflict", request_id=request_id,
                               revision=request["request_revision"])
        self._fault("after-publish")
        record["state"] = "published"
        record["published_at"] = self.clock()
        record["retain_until"] = self.clock() + RECORD_RETENTION_SECONDS
        record.pop("recovery_answer", None)
        self._write(self._path("commits", request_id), record)
        try:
            os.unlink(self._path("leases", request_id))
        except FileNotFoundError:
            pass
        if lifecycle:
            reason = ((answer.get("error") or {}).get("reason")
                      if isinstance(answer.get("error"), dict) else None)
            event = "ambiguous" if str(reason).startswith("ambiguous") else "missing"
            self._record_terminal_locked(
                request, event, detail="target lifecycle termination")
        return _result(True, "idempotent", request_id=request_id,
                       revision=request["request_revision"],
                       receipt=record.get("receipt"),
                       commit_receipt=record.get("commit_receipt"),
                       submission_digest=record.get("submission_digest"),
                       answer_digest=record.get("submitted_answer_digest"))

    def submit(self, *, request_id, revision, lease_token,
               geometry_generation, submission_id, answer):
        if (not _valid_lease_binding(request_id, revision, lease_token)
                or not isinstance(geometry_generation, int)
                or isinstance(geometry_generation, bool)
                or not _safe_id(submission_id)):
            return _result(False, "malformed", detail="submit bindings")
        if _contains_capture_field(answer):
            return _result(False, "malformed",
                           detail="remote image/capture fields forbidden")
        submission_hash = submission_digest(
            request_id, revision, geometry_generation, submission_id, answer,
            lease_token=lease_token)
        submitted_answer_hash = answer_digest(answer)
        try:
            with self._transaction():
                prior = self._read(self._path("commits", request_id))
                if prior:
                    exact = (prior.get("submission_id") == submission_id
                             and prior.get("submission_digest") == submission_hash)
                    if exact and prior.get("state") != "prepared":
                        return _result(True, "idempotent", request_id=request_id,
                                       revision=prior.get("request_revision"),
                                       receipt=prior.get("receipt"),
                                       commit_receipt=prior.get("commit_receipt"),
                                       submission_digest=prior.get(
                                           "submission_digest"),
                                       answer_digest=prior.get(
                                           "submitted_answer_digest"))
                    if not exact:
                        kind = ("conflict" if prior.get("state") == "prepared"
                                else "already-answered")
                        return _result(False, kind, request_id=request_id,
                                       detail=("commit-prepared"
                                               if kind == "conflict" else None))
                try:
                    request = self._load_request(request_id)
                except AdapterError as error:
                    if error.code == "missing":
                        return self._terminal_result(request_id)
                    raise
                if request["request_revision"] != revision:
                    return _result(False, "stale", request_id=request_id)
                if prior and prior.get("state") == "prepared":
                    # Validation and the lease were already proven before the
                    # durable prepare boundary. The exact token-bound digest
                    # may finish after a process crash or lease/deadline expiry.
                    receipt = prior.get("commit_receipt")
                    exact = _exact_remote_format(receipt) if receipt else False
                    internal = self._validate_remote_answer(request, answer, exact=exact)
                    return self._commit_locked(
                        request, internal, submission_id, submission_hash,
                        submitted_answer_hash, remote_answer=answer)
                if float(request["deadline"]) <= self.clock():
                    self._record_terminal_locked(request, "expired")
                    return _result(False, "expired", request_id=request_id)
                lease = self._lease(request)
                if not lease:
                    # A terminal outcome (a cancellation, most often) also
                    # unlinks the lease, so a lost race must report WHY the
                    # lease went away — reporting `conflict` would tell a
                    # consumer to retry a request that is already over. The
                    # terminal record outlives the lease; the request file
                    # may not have been cleaned up yet. Either way the
                    # submission is refused.
                    if self._read(self._path("terminal", request_id)):
                        return self._terminal_result(request_id)
                    return _result(False, "conflict", request_id=request_id)
                if not hmac.compare_digest(
                        lease["token_digest"], self._token_digest(lease_token)):
                    return _result(False, "conflict", request_id=request_id)
                target = self._target(request)
                if geometry_generation != target["generation"]:
                    return _result(False, "moved", request_id=request_id,
                                   geometry_generation=target["generation"])
                if target["state"] in ("missing", "ambiguous"):
                    return _result(False, target["state"], request_id=request_id)
                internal = self._validate_remote_answer(request, answer)
                return self._commit_locked(request, internal, submission_id,
                                           submission_hash,
                                           submitted_answer_hash, remote_answer=answer)
        except AdapterError as error:
            return _result(False, error.code, detail=error.detail)
        except Exception as error:
            return _result(False, "unavailable", detail=str(error))

    def _recover_locked(self, request, record):
        """Complete authenticated original bytes; never abandon or supersede."""
        import server
        request_id = request["id"]
        if (record.get("schema") != COMMIT_RECORD_SCHEMA
                or record.get("schema_version") != SCHEMA_VERSION
                or record.get("request_id") != request_id
                or record.get("request_revision") != request["request_revision"]):
            return _result(False, "malformed-state", detail="recovery binding")
        if record.get("state") != "prepared":
            return _result(False, "malformed-state", detail="recovery state")
        if record.get("origin") != "remote":
            return _result(False, "recovery-unavailable", request_id=request_id)
        if self._lease(request):
            return _result(False, "conflict", request_id=request_id,
                           detail="commit-prepared")
        answer = record.get("recovery_answer")
        if (not isinstance(answer, dict) or _contains_capture_field(answer)
                or len(canonical_json(answer)) > MAX_COMMAND_BYTES):
            return _result(False, "recovery-unavailable", request_id=request_id)
        exact = _exact_remote_format(record.get("commit_receipt"))
        internal = self._validate_remote_answer(request, answer, exact=exact)
        checked = verify_commit_receipt(
            record.get("commit_receipt"), expected_request_id=request_id,
            expected_request_revision=request["request_revision"],
            expected_submission_id=record.get("submission_id"),
            expected_submission_digest=record.get("submission_digest"),
            expected_answer_digest=answer_digest(answer))
        if not checked["valid"]:
            return _result(False, "recovery-unavailable", request_id=request_id)
        receipt = record.get("receipt")
        if request["responder"]["mode"] == "checklist":
            provider = request["provider"]
            checked = server.verify_checklist_receipt(
                receipt, expected_request_id=request_id,
                expected_request_digest=request["request_digest"],
                expected_provider_instance=provider["instance"],
                expected_provider_generation=provider["generation"],
                expected_step_ids=[row["id"] for row in request["responder"]["steps"]])
            if not checked["valid"] or receipt["steps"] != internal["checklist"]["steps"]:
                return _result(False, "recovery-unavailable", request_id=request_id)
        elif (receipt != record.get("commit_receipt")
              or receipt.get("completion", "complete") !=
              (internal.get("response_completion") or {}).get("status", "complete")):
            return _result(False, "recovery-unavailable", request_id=request_id)
        result = self._resume_prepared_locked(request, internal, record)
        if result["ok"]:
            result["event"]["type"] = "committed"
        return result

    def recover(self, request_id, revision):
        """Same-account recovery after the original remote lease expires.

        Caller supplies no answer or new authority. Terminal cleanup wins;
        otherwise the previously accepted exact transaction may complete after
        deadline expiry, just like its original token-bound submit retry.
        """
        if not _safe_id(request_id) or not isinstance(revision, str):
            return _result(False, "malformed", detail="recovery binding")
        try:
            with self._transaction():
                terminal = self._read(self._path("terminal", request_id))
                if terminal:
                    return self._terminal_result(request_id)
                record = self._read(self._path("commits", request_id))
                if not record:
                    return _result(False, "recovery-unavailable", request_id=request_id)
                if record.get("request_revision") != revision:
                    return _result(False, "stale", request_id=request_id)
                if record.get("state") in ("published", "acknowledged"):
                    return _result(False, "already-answered", request_id=request_id)
                request = self._load_request(request_id)
                return self._recover_locked(request, record)
        except AdapterError as error:
            return _result(False, error.code, detail=error.detail)
        except Exception:
            return _result(False, "unavailable", detail="recovery unavailable")

    def submit_local(self, request_id, answer, *, submission_id=None,
                     lifecycle=False):
        if not _safe_id(request_id) or not isinstance(answer, dict):
            return _result(False, "malformed", detail="local answer")
        submission_id = submission_id or (
            "local-" + _tagged(_SUBMISSION_DIGEST_DOMAIN, answer).split(":", 1)[1][:24])
        submission_hash = local_submission_digest(
            request_id, submission_id, answer, lifecycle)
        submitted_answer_hash = answer_digest(answer)
        try:
            with self._transaction():
                prior = self._read(self._path("commits", request_id))
                exact = False
                replace_local_prepare = False
                if prior:
                    exact = (prior.get("submission_id") == submission_id
                             and prior.get("submission_digest") == submission_hash)
                    if exact and prior.get("state") != "prepared":
                        return _result(True, "idempotent", request_id=request_id,
                                       revision=prior.get("request_revision"),
                                       receipt=prior.get("receipt"),
                                       commit_receipt=prior.get("commit_receipt"),
                                       submission_digest=prior.get(
                                           "submission_digest"),
                                       answer_digest=prior.get(
                                           "submitted_answer_digest"))
                    if not exact:
                        if (prior.get("state") == "prepared"
                                and prior.get("origin") == "local"):
                            replace_local_prepare = True
                        elif prior.get("state") == "prepared":
                            return _result(False, "conflict",
                                           request_id=request_id,
                                           detail="commit-prepared")
                        else:
                            return _result(False, "already-answered",
                                           request_id=request_id)
                request_path = os.path.join(
                    self.spool, "requests", request_id + ".json")
                raw_request = self._read(request_path, required=True)
                current = (isinstance(raw_request.get("responder"), dict)
                           and raw_request["responder"].get("schema") ==
                           CONTRACT_SCHEMA
                           and isinstance(raw_request.get("request_revision"), str))
                if not current:
                    # A resident pre-4jq server cannot participate in remote
                    # leasing. Preserve its old answer bytes exactly, but still
                    # serialize and publish no-clobber with every current writer.
                    if ("responder" in raw_request
                            or "request_revision" in raw_request):
                        return _result(False, "malformed",
                                       detail="partial responder contract")
                    if (raw_request.get("id") != request_id
                            or not isinstance(raw_request.get("summary"), str)):
                        return _result(False, "malformed",
                                       detail="legacy request identity")
                    deadline = raw_request.get("deadline")
                    if _finite_number(deadline) and float(deadline) <= self.clock():
                        return _result(False, "expired", request_id=request_id)
                    answer_path = os.path.join(
                        self.spool, "answers", request_id + ".json")
                    try:
                        self._publish_answer(answer_path, answer)
                    except FileExistsError:
                        return _result(False, "already-answered",
                                       request_id=request_id)
                    return _result(True, "committed", request_id=request_id,
                                   legacy=True, receipt=None)
                # Resident servers could publish an explicit checklist with
                # no steps. Let the human reply in prose or decline it through
                # the existing receipt-free compatibility path. Remote callers
                # and structured checklist answers keep strict cardinality.
                request = self._load_request(
                    request_id, allow_empty_local_checklist=(
                        not lifecycle and not isinstance(answer.get("checklist"), dict)))
                lease = self._lease(request)
                if prior and prior.get("state") == "prepared":
                    # A prepared record reserves this FIFO item. An exact
                    # local retry can finish it; a restarted surface may
                    # replace its own metadata-only prepare because the lost
                    # answer bytes cannot be reconstructed. Neither path may
                    # race a live remote lease.
                    if lease:
                        return _result(False, "conflict", request_id=request_id,
                                       lease_expires_at=lease["expires_at"])
                    if exact:
                        return self._commit_locked(
                            request, answer, submission_id, submission_hash,
                            submitted_answer_hash, lifecycle=lifecycle,
                            origin="local")
                if float(request["deadline"]) <= self.clock():
                    self._record_terminal_locked(request, "expired")
                    return _result(False, "expired", request_id=request_id)
                if lease and (replace_local_prepare or not lifecycle):
                    return _result(False, "conflict", request_id=request_id,
                                   lease_expires_at=lease["expires_at"])
                if not lifecycle:
                    mode = request["responder"]["mode"]
                    # A dismissal is lifecycle information, not a choice.
                    # Current local surfaces have always been able to decline
                    # every response mode without manufacturing a selection.
                    if mode == "choice" and not answer.get("cancelled"):
                        choice = answer.get("choice")
                        completion = answer.get("response_completion")
                        incomplete = completion == {
                            "schema": "human-copilot.response-completion/v1",
                            "schema_version": 1, "mode": "choice",
                            "status": "not_done"}
                        options = request["responder"]["options"]
                        valid = [row for row in options if isinstance(row, dict)
                                 and isinstance(choice, dict)
                                 and row.get("value") == choice.get("value")]
                        if incomplete:
                            note = answer.get("text")
                            if (choice is not None or not isinstance(note, str)
                                    or not note.strip() or len(note) > MAX_NOTE_CHARS):
                                return _result(False, "malformed",
                                               detail="local incomplete choice")
                        elif completion is not None or len(valid) != 1:
                            return _result(False, "malformed",
                                           detail="local choice")
                    if mode == "checklist":
                        if not isinstance(answer.get("checklist"), dict):
                            # Backward-compatible free prose from an old/current
                            # local surface remains human-visible, but receives
                            # no adapter or checklist receipt and cannot authorize.
                            answer_path = os.path.join(
                                self.spool, "answers", request_id + ".json")
                            try:
                                if replace_local_prepare:
                                    os.unlink(self._path("commits", request_id))
                                self._publish_answer(answer_path, answer)
                            except FileExistsError:
                                return _result(False, "already-answered",
                                               request_id=request_id)
                            return _result(True, "committed",
                                           request_id=request_id,
                                           revision=request["request_revision"],
                                           compatibility=True, receipt=None)
                        import server
                        try:
                            server._validated_answer_steps(request, answer)
                        except server.ChecklistReceiptError as error:
                            return _result(False, "malformed", detail=error.code)
                if replace_local_prepare:
                    os.unlink(self._path("commits", request_id))
                return self._commit_locked(request, answer, submission_id,
                                           submission_hash,
                                           submitted_answer_hash,
                                           lifecycle=lifecycle, origin="local")
        except AdapterError as error:
            if error.code == "missing":
                return self._terminal_result(request_id)
            return _result(False, error.code, detail=error.detail)
        except Exception as error:
            return _result(False, "unavailable", detail=str(error))

    def _record_terminal_locked(self, request, event, detail=None,
                                delivered=False):
        now = self.clock()
        record = {
            "schema": "human-copilot.responder-terminal/v1",
            "schema_version": SCHEMA_VERSION, "request_id": request["id"],
            "request_revision": request.get("request_revision"),
            "event": event, "detail": detail, "observed_at": now,
            "delivered": bool(delivered),
            "retain_until": now + RECORD_RETENTION_SECONDS,
        }
        self._write(self._path("terminal", request["id"]), record)
        commit_path = self._path("commits", request["id"])
        commit = self._read(commit_path)
        if commit and "recovery_answer" in commit:
            commit.pop("recovery_answer")
            self._write(commit_path, commit)
        try:
            os.unlink(self._path("leases", request["id"]))
        except FileNotFoundError:
            pass
        return record

    def record_terminal(self, request, event, detail=None):
        if not isinstance(request, dict) or not _safe_id(request.get("id")):
            return _result(False, "malformed")
        try:
            with self._transaction():
                self._record_terminal_locked(request, event, detail)
                return _result(True, event, request_id=request["id"])
        except Exception as error:
            return _result(False, "unavailable", detail=str(error))

    def expire(self, request_id, revision=None):
        """Retire a request whose deadline has elapsed.

        Operator surfaces use this when a caller disappeared or its normal
        cleanup lost the race with a repaint.  It is deliberately a full
        transaction: an answer already committed wins, a prepared WAL is
        preserved for recovery, and only an actually expired request is
        tombstoned and removed from the actionable spool.
        """
        if not _safe_id(request_id) or (
                revision is not None and not isinstance(revision, str)):
            return _result(False, "malformed", detail="expiry binding")
        try:
            with self._transaction():
                terminal = self._read(self._path("terminal", request_id))
                if terminal:
                    if terminal.get("event") == "expired":
                        self._remove_expired_files_locked(request_id)
                    return self._terminal_result(request_id)
                commit = self._read(self._path("commits", request_id))
                answer_path = os.path.join(
                    self.spool, "answers", request_id + ".json")
                if os.path.exists(answer_path) or (
                        commit and commit.get("state") in
                        ("published", "acknowledged")):
                    return _result(False, "already-answered",
                                   request_id=request_id)
                if commit and commit.get("state") == "prepared":
                    return _result(False, "conflict", request_id=request_id,
                                   detail="commit-prepared")
                path = os.path.join(
                    self.spool, "requests", request_id + ".json")
                request = self._read(path, required=True)
                actual_revision = request.get("request_revision")
                if revision is not None and actual_revision != revision:
                    return _result(False, "stale", request_id=request_id)
                if (request.get("id") != request_id
                        or not _finite_number(request.get("deadline"))):
                    return _result(False, "malformed", detail="request expiry")
                if float(request["deadline"]) > self.clock():
                    return _result(False, "not-expired", request_id=request_id,
                                   revision=actual_revision)
                self._record_terminal_locked(
                    request, "expired", "deadline elapsed")
                self._remove_expired_files_locked(request_id)
                return _result(False, "expired", request_id=request_id,
                               revision=actual_revision)
        except AdapterError as error:
            if error.code == "missing":
                return self._terminal_result(request_id)
            return _result(False, error.code, detail=error.detail)
        except Exception as error:
            return _result(False, "unavailable", detail=str(error))

    def _remove_expired_files_locked(self, request_id):
        """Remove only the request-side files behind an expired tombstone."""
        for path in (
                os.path.join(self.spool, "requests", request_id + ".json"),
                os.path.join(self.spool, "requests",
                             request_id + ".arrival-nudge")):
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass

    def acknowledge_consumed(self, request, answer, event="already-answered"):
        """Advance a prepared/published WAL before server cleanup."""
        if not isinstance(request, dict) or not isinstance(answer, dict):
            return _result(False, "malformed")
        try:
            with self._transaction():
                record = self._read(self._path("commits", request["id"]))
                if record:
                    digest = _tagged(_ANSWER_DIGEST_DOMAIN, answer)
                    if digest != record.get("answer_file_digest"):
                        return _result(False, "conflict",
                                       request_id=request["id"])
                    record["state"] = "acknowledged"
                    record["acknowledged_at"] = self.clock()
                    record["retain_until"] = self.clock() + RECORD_RETENTION_SECONDS
                    record.pop("recovery_answer", None)
                    self._write(self._path("commits", request["id"]), record)
                self._record_terminal_locked(request, event)
                return _result(True, event, request_id=request["id"],
                               receipt=record.get("receipt") if record else None)
        except Exception as error:
            return _result(False, "unavailable", detail=str(error))

    def local_available(self, request):
        """Advisory surface filter; commit still rechecks under the lock."""
        try:
            with self._transaction():
                if (not isinstance(request.get("responder"), dict)
                        and "request_revision" not in request):
                    return True
                current = self._load_request(
                    request["id"], allow_empty_local_checklist=True)
                return self._lease(current) is None
        except Exception:
            return False

    def dispatch(self, command):
        if not isinstance(command, dict) or command.get("schema") != COMMAND_SCHEMA:
            return _result(False, "malformed", detail="command schema")
        if command.get("schema_version") != SCHEMA_VERSION:
            return _result(False, "malformed", detail="command version")
        op = command.get("op")
        fields = {
            "acquire": {"consumer", "lease_seconds", "resume",
                        "acknowledged_geometry_generation"},
            "renew": {"request_id", "revision", "lease_token",
                      "lease_seconds", "acknowledged_geometry_generation"},
            "release": {"request_id", "revision", "lease_token"},
            "submit": {"request_id", "revision", "lease_token",
                       "geometry_generation", "submission_id", "answer"},
            "recover": {"request_id", "revision"},
            "expire": {"request_id", "revision"},
            "submit-local": {"request_id", "submission_id", "answer"},
            "submit-system": {"request_id", "submission_id", "answer"},
        }
        if op not in fields:
            return _result(False, "malformed", detail="unknown operation")
        allowed = {"schema", "schema_version", "op"} | fields[op]
        if set(command) - allowed:
            return _result(False, "malformed", detail="unknown command field")
        if op == "acquire":
            return self.acquire(command.get("consumer"),
                                command.get("lease_seconds"),
                                command.get("resume"),
                                command.get("acknowledged_geometry_generation"))
        if op == "renew":
            return self.renew(command.get("request_id"), command.get("revision"),
                              command.get("lease_token"),
                              command.get("lease_seconds"),
                              command.get("acknowledged_geometry_generation"))
        if op == "release":
            return self.release(command.get("request_id"), command.get("revision"),
                                command.get("lease_token"))
        if op == "submit":
            return self.submit(
                request_id=command.get("request_id"),
                revision=command.get("revision"),
                lease_token=command.get("lease_token"),
                geometry_generation=command.get("geometry_generation"),
                submission_id=command.get("submission_id"),
                answer=command.get("answer"))
        if op == "recover":
            return self.recover(command.get("request_id"), command.get("revision"))
        if op == "expire":
            return self.expire(command.get("request_id"), command.get("revision"))
        if op in ("submit-local", "submit-system"):
            return self.submit_local(
                command.get("request_id"), command.get("answer"),
                submission_id=command.get("submission_id"),
                lifecycle=op == "submit-system")
        return _result(False, "malformed", detail="unknown operation")


def _stdio(spool, one_shot=False):
    responder = Responder(spool)
    source = sys.stdin.buffer
    while True:
        raw = source.readline(MAX_COMMAND_BYTES + 2)
        if not raw:
            return 0
        if len(raw) > MAX_COMMAND_BYTES or not raw.endswith(b"\n"):
            while raw and not raw.endswith(b"\n"):
                raw = source.readline(MAX_COMMAND_BYTES + 2)
            result = _result(False, "malformed", detail="command line too large")
        else:
            try:
                command = json.loads(raw)
                if not isinstance(command, dict):
                    raise AdapterError("malformed", "command must be an object")
                if _contains_capture_field(command) and command.get("op") not in (
                        "submit-local", "submit-system"):
                    raise AdapterError("malformed", "capture fields forbidden")
                result = responder.dispatch(command)
            except (UnicodeDecodeError, json.JSONDecodeError) as error:
                result = _result(False, "malformed", detail=str(error))
            except AdapterError as error:
                result = _result(False, error.code, detail=error.detail)
            except Exception as error:
                sys.stderr.write(f"responder: {error}\n")
                result = _result(False, "unavailable")
        sys.stdout.buffer.write(canonical_json(result) + b"\n")
        sys.stdout.buffer.flush()
        if one_shot:
            return 0


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    one_shot = False
    spool = os.environ.get("HC_SPOOL")
    while args:
        arg = args.pop(0)
        if arg == "--one-shot":
            one_shot = True
        elif arg == "--spool" and args:
            spool = args.pop(0)
        elif arg.startswith("--spool="):
            spool = arg.split("=", 1)[1]
        else:
            sys.stderr.write("usage: responder.py [--one-shot] --spool DIR\n")
            return 2
    if not spool:
        # Import lazily: server imports this module to stamp requests.
        import server
        spool = server.spool_dir()
    return _stdio(spool, one_shot)


if __name__ == "__main__":
    raise SystemExit(main())
