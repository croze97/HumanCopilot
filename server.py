#!/usr/bin/env python3
"""human-copilot MCP server — execution-blocker requests over the desktop overlay.

Replaced the third-party `mcp-feedback-enhanced` webapp (127.0.0.1:8765) on
2026-08-27. The server and tool names stay stable so Claude and Codex expose the
same `interactive_feedback` lane and all doctrine references keep working.

What it is: a stdlib-only MCP *stdio* server (newline-delimited JSON-RPC — this
process's stdin/stdout ARE the protocol, so nothing here may ever print to
stdout except framed responses; diagnostics go to stderr). It has no UI of its
own. A tool call becomes a request file in the SPOOL; the GNOME overlay
renders it and writes the operator answer. An explicitly operator-launched
`copilot-tui` can also answer. Automatic terminal/pane spawning is disabled by
default; renderer failure never grants permission to open another surface.

Spool: $XDG_RUNTIME_DIR/human-copilot/{requests,answers}/<id>.json — tmpfs, so
request text and screenshot bytes live in RAM only and vanish at logout (the
same posture ocr-clip takes with clipboard images). Some MCP hosts strip
XDG_RUNTIME_DIR; in that case /run/user/$UID is used when it is a private
directory owned by this user. Only a host without either runtime path falls
back to ~/.local/state/human-copilot/spool, mode 0700.

Concurrency: tools/call runs on a worker thread so the read loop keeps
servicing pings and notifications/cancelled while an operator takes their time.
stdout writes are serialized by _OUT_LOCK. A cancelled call stops waiting,
removes its request file (so the TUI drops it) and answers with isError=false
"cancelled" — MCP says a server MAY skip the response after cancellation, but
answering costs nothing and never leaves the client hanging on a race.

Capability: toggle.py's persistent control record is authoritative for every
resident process. initialize advertises tools.listChanged; a watcher publishes
notifications/tools/list_changed on a flip. OFF makes tools/list empty, stale
tools/call attempts return a structured disabled error before touching the
spool, and an in-flight wait is withdrawn promptly.

The operator learns about a request from its SURFACE plus, when the desktop
overlay is not demonstrably rendering, one critical arrival notification. A
TUI lock alone is not enough to suppress that nudge: its terminal may be
behind other windows and curses.beep() is easy to miss. A fresh healthy
overlay heartbeat plus its presence lock does suppress the duplicate (the
operator's 2026-08-27 choice: "I can see the overlay without"). Server and TUI
race for one private per-request marker, so a newly loaded TUI also covers
requests from old resident server processes without sending duplicates.
Nothing here failing fails the call — the request just waits in the spool
until the deadline.

Instance: every request is stamped with {session, pane, server_uuid, attached}
from the zellij environment this process inherited (see "instance" below).
An unattached or non-zellij session keeps today's path by default;
HC_REQUIRE_ATTACHED=1 makes such a call answer with a clear refusal instead.

Colour: from the first call on, this process HOLDS its session's slot in the
palette registry (palette.hold — a shared flock the process keeps for life),
so the number and colour the overlay and the TUI draw for this session are
authoritative while it lives and reclaimed the moment it dies. A palette
failure never fails a request: the surfaces fall back to a provisional slot.
"""
import base64
import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import subprocess
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
import signal
from types import SimpleNamespace

import palette                    # the per-instance colour registry (hcop-0p7.2)
import publisher_lifecycle        # resumable nonblocking publisher ownership
import responder                  # versioned FIFO/lease/answer authority (hcop-4jq)
import toggle                     # one persisted capability authority

PROTOCOL_FALLBACK = "2024-11-05"
DEFAULT_TIMEOUT_S = 1800          # manual passes are slow; doctrine says 1800-3600
MIN_TIMEOUT_S, MAX_TIMEOUT_S = 30, 24 * 3600
POLL_S = 0.3
SPAWN_COOLDOWN_S = 15             # per INSTANCE, shared by that instance's servers via the spool
SURFACE_RECHECK_S = 1.0            # a renderer can fail after its presence probe
OVERLAY_RUNTIME_FILE = "overlay-runtime.json"
OVERLAY_RUNTIME_FRESH_S = 15       # shared with toggle.py's status evidence
OVERLAY_RUNTIME_FUTURE_S = 5       # tolerate only small clock skew
TUI_RUNTIME_FILE = "tui-runtime.json"
TUI_RUNTIME_FRESH_S = 15           # a lock is presence; this proves a TUI painted
PANE_OPEN_TIMEOUT_S = 5           # new-pane / focus-pane-id are local IPC to the zellij server
PANE_FILE = "pane.json"           # spool/instances/<id>/pane.json: the copilot pane new-pane created
DELIVERY_FILE = "delivery.json"   # last pane/terminal delivery attempt, for status
ARRIVAL_NUDGE_SUFFIX = ".arrival-nudge"  # requests/<id><suffix>: cross-process once claim
CALLER_PROCESS_SCHEMA = "human-copilot-caller-process"
ORPHAN_GRACE_S = 30
ORPHAN_GC_LOCK = "orphan-gc.lock"
PANE_NAME = "copilot · {iid}"     # the pane's zellij name = the window-title half the overlay matches
CONTROL_POLL_S = 0.2              # fallback when a process signal cannot be delivered
_CLIENT_LABEL = "Your agent"       # replaced from initialize.clientInfo per process
HERE = os.path.dirname(os.path.realpath(__file__))

# Authenticated checklist verdicts (hcop-d1f).  The MCP content blocks remain
# the human-facing answer; this is an additive, machine-facing authorization
# contract.  HMAC is deliberate: Human Copilot and its local consumers share
# one Unix-account trust boundary, and stdlib-only is an invariant here.
CHECKLIST_REQUEST_SCHEMA = "human-copilot.checklist-request/v1"
CHECKLIST_ANSWER_SCHEMA = "human-copilot.checklist-answer/v1"
CHECKLIST_VERDICT_SCHEMA = "human-copilot.checklist-verdict/v1"
CHECKLIST_SCHEMA_VERSION = 1
CHECKLIST_VERDICTS = ("done", "failed", "not-done")
RECEIPT_ALGORITHM = "hmac-sha256"
RECEIPT_KEY_BYTES = 32
RECEIPT_KEY_ENV = "HC_RECEIPT_KEY_FILE"
RECEIPT_KEY_FILE = "receipt-key"
RECEIPT_KEY_LOCK_FILE = "receipt-key.lock"
MAX_RECEIPT_NOTE_CHARS = 16 * 1024
_REQUEST_DIGEST_DOMAIN = b"human-copilot.checklist-request/v1\0"
_RECEIPT_DIGEST_DOMAIN = b"human-copilot.checklist-verdict/v1\0"
_RECEIPT_SIGNATURE_DOMAIN = b"human-copilot.checklist-signature/v1\0"

TOOL_DESCRIPTION = (
    "Ask ONLY for an actual execution blocker: you cannot complete the needed step "
    "yourself and need a human fact, action or authorization to continue "
    "implementation or operation. "
    "BLOCK for their report. Use shell and API tools first. Prefer autonomous "
    "shell/API, text and browser DOM checks for authorized implementation and verification. "
    "Screenshots and pixel capture are disabled by default. The operator may open a "
    "temporary allowance in Desktop Control (Services -> Agent screen capture: all, "
    "a region, or one window); inside it, screenctx screen_crop/screen_diff may be "
    "used and refuse elsewhere. Never open, edit or request an allowance (including "
    "screenctx capture_policy.py writes), and do not solicit snips. If pixels are needed, "
    "record the observation as unverified and rely on normal app use feedback. "
    "Inspect window handles, geometry and sizes; read button/control metadata and "
    "text through accessibility APIs (a11y) and browser DOM. No computer-vision navigation. "
    "Existing operator-owned snip functionality is preserved for unsolicited "
    "attachments; it is not an invitation to request one. "
    "Make routine technical, implementation and planning decisions yourself. "
    "Before calling, state the specific observation or action needed, the concrete "
    "autonomous routes tried and why each failed or is unavailable. For an "
    "operator-only fact, physical action or required approval, explain why only "
    "the operator can supply it after checking available evidence and existing "
    "authorization. Do not manufacture a decision or approval gate. "
    "Never hand off to save tokens, time or automation effort. A task involving a "
    "UI does not require human visual QA. Investigate uncertainty; do not turn it "
    "into a questionnaire or request automatic sign-off. If the operator says "
    "'I don't know, you decide', use your judgment and continue. "
    "Do not proactively request QA, test checklists, final acceptance or visual "
    "sign-off, even after all autonomous work is finished. Trust normal app use and "
    "unsolicited issue reports to supply operator feedback; do not ask the operator "
    "to test or report back. Run your own tests and verify worker results. Record "
    "physical or unavailable verification in Beads with the exact limitation and "
    "evidence; keep unchecked outcomes unverified. A test you cannot run is not "
    "passed, and missing verification alone is not an execution blocker. Do not "
    "turn awaiting_test into an operator assignment; continue available work. "
    "While enabled and callable, route justified handoffs here instead of "
    "request_user_input / request_user_input_async, including queued follow-up "
    "inputs: no approval bypass and no new reason to ask. If unavailable or "
    "disabled, do not call it repeatedly; state that once and use the supported "
    "direct/native channel for a still-necessary question. "
    "Write for someone who has NOT read this conversation: 1-2 context lines "
    "explaining the human need and what the answer unlocks. For actions needed "
    "to unblock execution, use "
    "1-5 numbered steps, one action each, naming the window/device, exact action "
    "and '-> what should change'. About 20 words per step; clarity comes first. "
    "Do setup and open programs yourself; copilot-open <cmd> opens a terminal "
    "window on the desktop. Every window-specific step needs '@{Window · region}': "
    "a title/class substring matching EXACTLY ONE window initially, and region "
    "top-left..center..bottom-right/full or x,y,w,h fractions. The request follows "
    "that window through title changes; name a popup explicitly when it becomes "
    "the target. Missing/ambiguous matches are errors; never guess a replacement "
    "for a closed target. Decisions use mode:'choice' with "
    "options [{value, label, hint?}]: unique machine value, readable label, "
    "cost/consequence. Recommend an option when supported; the selected value "
    "returns. Modes: checklist (default), text (free prose), choice (2-12 options), "
    "ack (confirmation only; the only mode where an empty send is an answer). "
    "Set timeout 1800-3600s for necessary multi-step assistance. "
    "Reports label steps done / FAILED / NOT DONE, with notes and any unsolicited "
    "operator attachments. NOT DONE is neither failed nor passed. Dismissal is a "
    "decline; timeout/no response leaves the requested action UNVERIFIED. Record it "
    "in Beads and continue available work; do not immediately repeat an unanswered "
    "request. Autonomous evidence may establish a result separately, but never "
    "present it as the operator's verdict."
)

# ----------------------------------------------------------------- response contract
#
# hcop-0gj: a decision must reach the operator as REAL choices, never as prose
# whose only available completion is an empty acknowledgement. The contract is
# additive — legacy calls (summary only) keep working untouched — and it is
# validated HERE, at the boundary, so a malformed choice never reaches a
# surface that would have to guess what it meant.

RESPONSE_MODES = ("checklist", "text", "choice", "ack")
MAX_OPTIONS = 12

def _option_error(i, why):
    return f"options[{i}] {why}"

def _validated_options(options):
    """A list of {value, label, hint?} -> normalised option dicts, or the
    error string. `value` is the stable machine answer, `label` the human
    one; values must be unique."""
    if not isinstance(options, list):
        return (f"`options` must be a list of 1-{MAX_OPTIONS} "
                "{{value, label, hint?}} objects (got "
                f"{type(options).__name__}).")
    if not 1 <= len(options) <= MAX_OPTIONS:
        return (f"`options` must hold 1-{MAX_OPTIONS} options (got "
                f"{len(options)}).")
    out, seen = [], set()
    for i, o in enumerate(options):
        if not isinstance(o, dict):
            return _option_error(i, f"must be an object (got {type(o).__name__})")
        v, l = o.get("value"), o.get("label")
        if not isinstance(v, str) or not v.strip():
            return _option_error(i, "needs a non-empty string `value` — it is "
                                    "the answer the engine reads back")
        if not isinstance(l, str) or not l.strip():
            return _option_error(i, "needs a non-empty string `label` — it is "
                                    "what the operator reads")
        v, l = v.strip(), l.strip()
        if v in seen:
            return _option_error(i, f"duplicate value {v!r}: values are the "
                                    f"machine answer and must be unique")
        hint = o.get("hint")
        if hint is not None and (not isinstance(hint, str) or not hint.strip()):
            return _option_error(i, "`hint`, when given, must be a non-empty "
                                    "string (the cost/consequence text)")
        seen.add(v)
        out.append({"value": v, "label": l,
                    "hint": hint.strip() if isinstance(hint, str) else None})
    return out

def response_contract(args):
    """The call's mode/options -> (response, None) or (None, error text).
    ``response`` is the dict stamped into the request JSON:
    {"mode": <mode or None>, "options": [...]}. mode None is the LEGACY
    shape: the surfaces derive it (numbered steps -> checklist, else prose).
    Options without a mode ARE a choice — the intent is unambiguous."""
    mode = args.get("mode")
    if mode is not None:
        if not isinstance(mode, str) or mode not in RESPONSE_MODES:
            return None, ("`mode` must be one of " +
                          ", ".join(f"'{m}'" for m in RESPONSE_MODES) +
                          f" (got {mode!r}).")
    opts = None
    if args.get("options") is not None:
        if mode in ("checklist", "text", "ack"):
            return None, (f"`options` cannot be combined with mode '{mode}' — "
                          f"choices are mode 'choice'.")
        opts = _validated_options(args["options"])
        if isinstance(opts, str):
            return None, opts
        mode = mode or "choice"
    if mode == "choice":
        if opts is None:
            return None, ("mode 'choice' needs `options` [{value, label, "
                          "hint?}] — the operator picks one, so an empty "
                          "acknowledgement is never the answer.")
        if len(opts) < 2:
            return None, ("a choice needs at least two options — a "
                          "confirm-only request is mode 'ack'.")
    return {"mode": mode, "options": opts or []}, None

# Structural, CONSERVATIVE decision-syntax detection (hcop-0gj): prose that
# offers a decision must not be completable with an empty ack. Only obvious
# option syntax counts — no NLP guessing; anything subtler is the engine's to
# send properly as mode 'choice'.
_CHOICE_DINGBAT = re.compile(r"[✔✘][^✔✘]*[✔✘]")        # "✔ yes · ✘ no"
_CHOICE_LETTERED = re.compile(r"\(?[A-Za-z][).]\s+\S")  # "a) stay" / "(b) go"
_CHOICE_YESNO = re.compile(r"\byes\s*(?:/|or)\s*no\b", re.I)

def looks_like_choices(summary):
    """True when prose carries obvious decision syntax: a ✔/✘ option line,
    two or more lettered options, or a literal yes/no."""
    lines = [l.strip() for l in (summary or "").splitlines() if l.strip()]
    if any(_CHOICE_DINGBAT.search(l) for l in lines):
        return True
    if sum(1 for l in lines if _CHOICE_LETTERED.match(l)) >= 2:
        return True
    return any(_CHOICE_YESNO.search(l) for l in lines)

def choices_note(summary, response):
    """None unless the summary read as a decision but arrived without
    structured choices — then the nudge to resend with mode/options. Rides
    back on the answer like brevity/clarity; never a refusal."""
    if response and response.get("mode"):
        return None
    if not looks_like_choices(summary):
        return None
    return ("(copilot choices: that summary read as a decision, but the only "
            "completion offered was an acknowledgement. Send decisions as "
            "mode:'choice' with options [{value, label, hint}] — the operator "
            "then picks one and the exact selected value comes back.)")

# A checklist is read on a 460px overlay by someone who has not read the
# conversation. Two nudges ride back to the agent on the answer — same
# session, same call, so the NEXT checklist is better. Never a refusal: the
# operator still sees and answers the request.
#   brevity: too many steps, or steps so wordy the operator has to scroll.
#   clarity: numbered steps with NO context line above them (2026-09-02, the
#   operator: "the past many times in a row I'm just: I don't know what
#   you're talking about"). The intro is measured separately from the steps
#   so writing one never trips the brevity nudge.
BRIEF_MAX_STEPS = 6
BRIEF_MAX_WORDS_PER_STEP = 40
BRIEF_MAX_INTRO_WORDS = 80
BRIEF_MAX_PROSE_WORDS = 120

def _is_step(raw):
    import re
    s = raw.strip()
    if re.match(r"^\d{1,3}[.)]\s+\S", s) and not raw[:1].isspace():
        return True
    if "|" in s:
        cells = [c.strip() for c in s.strip("|").split("|")]
        if cells and re.match(r"^\d{1,3}[.)]?$", cells[0]):
            return True
    return False

def split_summary(summary):
    """(intro_lines, step_lines, n_steps): intro = everything above the first
    step, outside fences, that is not table furniture; steps = the numbered
    lines the overlay walks (top-level numbered lines outside fences + table
    rows numbered in the first cell) and whatever follows them."""
    intro, steps, n, fence, seen = [], [], 0, False, False
    for raw in (summary or "").splitlines():
        s = raw.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            continue
        if fence:
            if seen:
                steps.append(raw)
            continue
        if _is_step(raw):
            n += 1
            seen = True
        if seen:
            steps.append(raw)
        elif s and not s.startswith("|"):
            intro.append(raw)
    return intro, steps, n

def count_steps(summary):
    return split_summary(summary)[2]

def brevity_note(summary):
    """None when the checklist is glanceable, else the nudge text."""
    intro, steps, n = split_summary(summary)
    words = len((summary or "").split())
    step_words = len(" ".join(steps).split())
    intro_words = len(" ".join(intro).split())
    if n and (n > BRIEF_MAX_STEPS or step_words / n > BRIEF_MAX_WORDS_PER_STEP):
        return (f"(copilot brevity: {n} steps, {step_words} words of steps. "
                f"The operator reads this on a 460px overlay — send 1-5 steps, "
                f"one action each, about 20 words, and split the rest into "
                f"the next call. Keep the context line.)")
    if n and intro_words > BRIEF_MAX_INTRO_WORDS:
        return (f"(copilot brevity: {intro_words} words above the steps. The "
                f"context is 1-2 lines: what changed and what their answer "
                f"decides — not a log or a rationale.)")
    if not n and words > BRIEF_MAX_PROSE_WORDS:
        return (f"(copilot brevity: {words} words and no numbered steps. "
                f"Number the steps so the overlay can tick them off, and keep "
                f"the whole thing under ~60 words.)")
    return None

# A step that asks the operator to RUN something is work the agent could have
# done — it has a shell; the operator has the eyes and hands (2026-09-02, the
# operator: "still getting instructions to run commands in terminal"). The
# surface never executes agent text; the agent is told to do it itself, or to
# put the program on the operator's desktop with copilot-open first.
_RUN_VERB = re.compile(r"\b(re-?run|run|re-?launch|launch|re-?start|restart|"
                       r"start|execute|invoke|spawn|kill|quit)\b", re.I)
_SHELLISH = re.compile(r"`[^`]+`|\b(terminal|shell|console|command(?: line)?|"
                       r"cli|prompt)\b", re.I)
_RELAUNCH = re.compile(r"^\W*(?:\d{1,3}[.)]\s*)?(?:quit(?: it)?(?: and|,)\s*)?"
                       r"(?:re-?launch|re-?run|re-?start|relaunch)\b", re.I)

def asks_to_run(text):
    """True when a step hands the operator a command to run."""
    return bool(_RELAUNCH.search(text)
                or (_RUN_VERB.search(text) and _SHELLISH.search(text)))

def handoff_note(summary):
    """None when every step is eyes-and-hands work, else the nudge naming the
    first step that asks the operator to run something."""
    intro, steps, n = split_summary(summary)
    k, fence = 0, False
    for raw in steps:
        st = raw.strip()
        if st.startswith("```") or st.startswith("~~~"):
            fence = not fence
            continue
        if fence or not _is_step(raw):
            continue
        k += 1
        if asks_to_run(st):
            return (f"(copilot handoff: step {k} asks the operator to run "
                    f"something. You have a shell — run it yourself before "
                    f"the call; if they need to see it, `copilot-open <cmd>` "
                    f"opens it in a terminal window on their desktop. Ask "
                    f"them only for what a human must do: look, click, "
                    f"press, judge.)")
    return None

def clarity_note(summary):
    """None when the steps are placed for the operator, else the nudge: a
    numbered checklist arrived with nothing above step 1."""
    intro, steps, n = split_summary(summary)
    if n and not intro:
        return ("(copilot clarity: the checklist had no context line. The "
                "operator has not read this conversation — open the next "
                "call with 1-2 lines saying what changed and what their "
                "answer decides, in their words, then the steps.)")
    return None

# ------------------------------------------------ authenticated checklist receipts

def provider_generation(argv=None):
    """The registration generation this server process was started with.

    toggle.py puts exactly one ignored marker in canonical Claude/Codex
    registrations.  Custom and pre-generation registrations remain usable and
    are named explicitly rather than being mistaken for the current generation.
    """
    args = sys.argv[1:] if argv is None else list(argv)
    prefix = toggle.CONNECTION_GENERATION_PREFIX
    values = [arg[len(prefix):] for arg in args
              if isinstance(arg, str) and arg.startswith(prefix)
              and arg != prefix]
    if len(values) == 1 and len(values[0]) <= 512:
        return values[0]
    return "unversioned"

PROVIDER_GENERATION = provider_generation()

def _canonical_json(value):
    """The one byte representation used by both request and receipt hashes."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")

def _tagged_sha256(domain, value):
    return "sha256:" + hashlib.sha256(domain + _canonical_json(value)).hexdigest()

def _iso_timestamp(epoch):
    value = float(epoch)
    return datetime.fromtimestamp(value, timezone.utc).isoformat(
        timespec="microseconds").replace("+00:00", "Z")

def _valid_timestamp(value):
    if not isinstance(value, str) or not value.endswith("Z"):
        return False
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError:
        return False
    return parsed.tzinfo is not None

def checklist_request_digest(request):
    """Digest every immutable byte of the published request except itself."""
    if not isinstance(request, dict):
        raise ValueError("checklist request must be an object")
    body = {key: value for key, value in request.items()
            if key != "request_digest"}
    return _tagged_sha256(_REQUEST_DIGEST_DOMAIN, body)

def _is_checklist_request(summary, response):
    mode = (response or {}).get("mode")
    return mode == "checklist" or (mode is None and count_steps(summary) > 0)

def stamp_checklist_contract(request, generation=None):
    """Add the v1 request identity consumed by current operator surfaces.

    All fields are additive and live at the request top level: the pinned
    four-field ``instance`` stamp is deliberately untouched.
    """
    if not _is_checklist_request(request.get("summary", ""),
                                 request.get("response")):
        return False
    inst = request.get("instance") if isinstance(request.get("instance"), dict) else {}
    server_uuid = inst.get("server_uuid")
    if not isinstance(server_uuid, str) or not server_uuid:
        server_uuid = SERVER_UUID
    request["provider"] = {
        "name": "human-copilot",
        "instance": server_uuid,
        "generation": PROVIDER_GENERATION if generation is None else str(generation),
    }
    responder_steps = ((request.get("responder") or {}).get("steps")
                       if isinstance(request.get("responder"), dict) else None)
    step_ids = ([row.get("id") for row in responder_steps]
                if isinstance(responder_steps, list)
                and all(isinstance(row, dict) for row in responder_steps)
                else [])
    if not step_ids:
        step_ids = [f"step-{i + 1:03d}"
                    for i in range(count_steps(request.get("summary", "")))]
    request["checklist"] = {
        "schema": CHECKLIST_REQUEST_SCHEMA,
        "schema_version": CHECKLIST_SCHEMA_VERSION,
        "steps": [{"id": step_id} for step_id in step_ids],
    }
    request["request_digest"] = checklist_request_digest(request)
    return True

def receipt_key_path():
    override = os.environ.get(RECEIPT_KEY_ENV)
    if override:
        return os.path.abspath(os.path.expanduser(override))
    state = os.environ.get("XDG_STATE_HOME")
    if not state:
        state = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(state, "human-copilot", RECEIPT_KEY_FILE)

def _private_parent(path, create=True):
    parent = os.path.dirname(path)
    if create:
        os.makedirs(parent, mode=0o700, exist_ok=True)
    st = os.stat(parent, follow_symlinks=False)
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) != 0o700):
        raise PermissionError(f"receipt key directory must be owned 0700: {parent}")
    return parent

def _read_private_key(path):
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                or stat.S_IMODE(st.st_mode) != 0o600):
            raise PermissionError("receipt key must be an owned 0600 regular file")
        body = b""
        while len(body) <= RECEIPT_KEY_BYTES:
            chunk = os.read(fd, RECEIPT_KEY_BYTES + 1 - len(body))
            if not chunk:
                break
            body += chunk
    finally:
        os.close(fd)
    if len(body) != RECEIPT_KEY_BYTES:
        raise ValueError("receipt key has the wrong length")
    return body

def _create_private_key(path):
    """Publish a complete key atomically; caller holds the interprocess lock."""
    key = secrets.token_bytes(RECEIPT_KEY_BYTES)
    tmp = f"{path}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(tmp, flags, 0o600)
    try:
        view = memoryview(key)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("short receipt-key write")
            view = view[written:]
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
        if os.path.lexists(path):
            raise FileExistsError(path)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return key

_RECEIPT_KEY_THREAD_LOCK = threading.Lock()

def receipt_signing_key(path=None, create=True):
    """Load the HMAC key, creating it atomically only for a signer."""
    path = os.path.abspath(path or receipt_key_path())
    parent = _private_parent(path, create=create)
    if not create:
        # Creation publishes through rename, so an existing key is immutable
        # and complete. Verification stays genuinely read-only: no lock or
        # state directory is manufactured when the key is unavailable.
        return _read_private_key(path)
    lock_path = os.path.join(parent, RECEIPT_KEY_LOCK_FILE)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    with _RECEIPT_KEY_THREAD_LOCK:
        lock_fd = os.open(lock_path, flags, 0o600)
        try:
            st = os.fstat(lock_fd)
            if (not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid()
                    or stat.S_IMODE(st.st_mode) != 0o600):
                raise PermissionError("receipt key lock must be an owned 0600 regular file")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            try:
                return _read_private_key(path)
            except FileNotFoundError:
                try:
                    return _create_private_key(path)
                except FileExistsError:
                    return _read_private_key(path)
        finally:
            os.close(lock_fd)

class ChecklistReceiptError(ValueError):
    """Typed fail-closed reason: malformed surface data is never signed."""

    def __init__(self, code, detail=None):
        self.code = code
        super().__init__(detail or code)

def _request_step_ids(request):
    contract = request.get("checklist")
    if (not isinstance(contract, dict)
            or contract.get("schema") != CHECKLIST_REQUEST_SCHEMA
            or contract.get("schema_version") != CHECKLIST_SCHEMA_VERSION):
        raise ChecklistReceiptError("missing-request-contract")
    rows = contract.get("steps")
    if not isinstance(rows, list) or not rows:
        raise ChecklistReceiptError("missing-request-steps")
    ids = []
    for row in rows:
        step_id = row.get("id") if isinstance(row, dict) else None
        if not isinstance(step_id, str) or not step_id:
            raise ChecklistReceiptError("malformed-request-step")
        ids.append(step_id)
    if len(ids) != len(set(ids)):
        raise ChecklistReceiptError("duplicate-request-step")
    return ids

def _validated_answer_steps(request, answer, *, preserve_notes=False):
    expected = _request_step_ids(request)
    if answer.get("cancelled"):
        raise ChecklistReceiptError("dismissed")
    raw = answer.get("checklist")
    if not isinstance(raw, dict):
        raise ChecklistReceiptError("text-only-output")
    if (raw.get("schema") != CHECKLIST_ANSWER_SCHEMA
            or raw.get("schema_version") != CHECKLIST_SCHEMA_VERSION):
        raise ChecklistReceiptError("wrong-answer-schema")
    if raw.get("request_id") != request.get("id"):
        raise ChecklistReceiptError("wrong-request")
    if raw.get("request_digest") != request.get("request_digest"):
        raise ChecklistReceiptError("wrong-request-digest")
    rows = raw.get("steps")
    if not isinstance(rows, list):
        raise ChecklistReceiptError("missing-steps")

    parsed = []
    for row in rows:
        if not isinstance(row, dict):
            raise ChecklistReceiptError("malformed-step")
        step_id = row.get("id")
        verdict = row.get("verdict")
        note = row.get("note", "")
        if not isinstance(step_id, str) or not step_id:
            raise ChecklistReceiptError("malformed-step-id")
        if verdict not in CHECKLIST_VERDICTS:
            raise ChecklistReceiptError("invalid-verdict")
        if not isinstance(note, str):
            raise ChecklistReceiptError("invalid-note")
        if not preserve_notes:
            note = note.strip()
        if len(note) > MAX_RECEIPT_NOTE_CHARS:
            raise ChecklistReceiptError("note-too-long")
        parsed.append({"id": step_id, "verdict": verdict, "note": note})

    got = [row["id"] for row in parsed]
    if len(got) != len(set(got)):
        raise ChecklistReceiptError("duplicate-step")
    unknown = [step_id for step_id in got if step_id not in expected]
    if unknown:
        raise ChecklistReceiptError("unknown-step")
    missing = [step_id for step_id in expected if step_id not in got]
    if missing:
        raise ChecklistReceiptError("missing-step")
    if got != expected:
        raise ChecklistReceiptError("step-order")
    return parsed

def _step_outcome(steps):
    if not steps:
        raise ChecklistReceiptError("missing-steps")
    if any(row["verdict"] == "failed" for row in steps):
        return "failed"
    if any(row["verdict"] == "not-done" for row in steps):
        return "incomplete"
    return "passed"

def _sign_receipt_body(body, signing_key, *,
                       digest_domain=_RECEIPT_DIGEST_DOMAIN,
                       signature_domain=_RECEIPT_SIGNATURE_DOMAIN):
    if not isinstance(signing_key, bytes) or len(signing_key) != RECEIPT_KEY_BYTES:
        raise ValueError(f"receipt signing key must be {RECEIPT_KEY_BYTES} bytes")
    auth_body = {
        "algorithm": RECEIPT_ALGORITHM,
        "key_id": "sha256:" + hashlib.sha256(signing_key).hexdigest(),
    }
    # Algorithm/key identity and any future authentication metadata are part
    # of the signed canonical payload. Only the digest and signature that
    # describe that payload sit outside it (they cannot recursively cover
    # themselves).
    payload = _canonical_json({**body, "authentication": auth_body})
    digest = "sha256:" + hashlib.sha256(digest_domain + payload).hexdigest()
    signature = "hmac-sha256:" + hmac.new(
        signing_key, signature_domain + payload + b"\0" +
        digest.encode("ascii"),
        hashlib.sha256).hexdigest()
    return {**body, "authentication": {**auth_body,
        "digest": digest,
        "signature": signature,
    }}

def build_checklist_receipt(request, answer, answered_at=None, signing_key=None, *, preserve_notes=False):
    """Validate one structured surface answer and sign its exact verdict.

    Invalid, dismissed and text-only answers raise ``ChecklistReceiptError``;
    callers preserve the legacy MCP content but MUST NOT manufacture a receipt.
    """
    if not isinstance(request, dict) or not isinstance(answer, dict):
        raise ChecklistReceiptError("malformed-answer")
    expected_digest = checklist_request_digest(request)
    if (not isinstance(request.get("request_digest"), str)
            or not hmac.compare_digest(request["request_digest"], expected_digest)):
        raise ChecklistReceiptError("mutated-request")
    steps = _validated_answer_steps(request, answer, preserve_notes=preserve_notes)
    outcome = _step_outcome(steps)
    provider = request.get("provider")
    if (not isinstance(provider, dict)
            or not all(isinstance(provider.get(key), str) and provider.get(key)
                       for key in ("name", "instance", "generation"))):
        raise ChecklistReceiptError("missing-provider")
    body = {
        "schema": CHECKLIST_VERDICT_SCHEMA,
        "schema_version": CHECKLIST_SCHEMA_VERSION,
        "request": {
            "id": request.get("id"),
            "digest": request["request_digest"],
            "requested_at": _iso_timestamp(request.get("created")),
            "deadline_at": _iso_timestamp(request.get("deadline")),
        },
        "provider": {
            "name": provider["name"],
            "instance": provider["instance"],
            "generation": provider["generation"],
        },
        "answered_at": _iso_timestamp(time.time() if answered_at is None
                                        else answered_at),
        "steps": steps,
        "outcome": outcome,
        "authorizing": outcome == "passed",
    }
    return _sign_receipt_body(body, signing_key or receipt_signing_key())

def _verify_result(valid, authenticated=False, authorizing=False, reason=None,
                   receipt_digest=None):
    return {"valid": bool(valid), "authenticated": bool(authenticated),
            "authorizing": bool(authorizing), "reason": reason,
            "receipt_digest": receipt_digest}

def verify_checklist_receipt(receipt, *, expected_request_id=None,
                             expected_request_digest=None,
                             expected_provider_instance=None,
                             expected_provider_generation=None,
                             expected_step_ids=None, signing_key=None,
                             replay_cache=None):
    """Authenticate and semantically verify a v1 receipt, fail closed.

    ``replay_cache`` is a caller-owned set of already-consumed receipt digests.
    Cross-process atomic consumption is intentionally left to hcop-4jq's
    responder adapter; this helper still makes in-process replay deterministic.
    """
    if not isinstance(receipt, dict):
        return _verify_result(False, reason="missing-receipt")
    auth = receipt.get("authentication")
    if not isinstance(auth, dict):
        return _verify_result(False, reason="missing-authentication")
    body = {field: value for field, value in receipt.items()
            if field != "authentication"}
    auth_body = {field: value for field, value in auth.items()
                 if field not in ("digest", "signature")}
    try:
        key = signing_key or receipt_signing_key(create=False)
        payload = _canonical_json({**body, "authentication": auth_body})
    except Exception:
        return _verify_result(False, reason="verification-unavailable")
    key_id = "sha256:" + hashlib.sha256(key).hexdigest()
    digest = "sha256:" + hashlib.sha256(
        _RECEIPT_DIGEST_DOMAIN + payload).hexdigest()
    signature = "hmac-sha256:" + hmac.new(
        key, _RECEIPT_SIGNATURE_DOMAIN + payload + b"\0" +
        digest.encode("ascii"), hashlib.sha256).hexdigest()
    if (auth_body.get("algorithm") != RECEIPT_ALGORITHM
            or not isinstance(auth_body.get("key_id"), str)
            or not hmac.compare_digest(auth_body["key_id"], key_id)):
        return _verify_result(False, reason="wrong-key")
    if (not isinstance(auth.get("digest"), str)
            or not hmac.compare_digest(auth["digest"], digest)
            or not isinstance(auth.get("signature"), str)
            or not hmac.compare_digest(auth["signature"], signature)):
        return _verify_result(False, reason="tampered", receipt_digest=digest)
    if set(auth_body) != {"algorithm", "key_id"}:
        return _verify_result(False, True, reason="unsupported-authentication",
                              receipt_digest=digest)

    if (body.get("schema") != CHECKLIST_VERDICT_SCHEMA
            or body.get("schema_version") != CHECKLIST_SCHEMA_VERSION):
        return _verify_result(False, True, reason="wrong-schema",
                              receipt_digest=digest)
    request = body.get("request")
    provider = body.get("provider")
    steps = body.get("steps")
    if (not isinstance(request, dict) or not isinstance(provider, dict)
            or not isinstance(steps, list) or not steps):
        return _verify_result(False, True, reason="malformed-receipt",
                              receipt_digest=digest)
    if (not isinstance(request.get("id"), str)
            or not isinstance(request.get("digest"), str)
            or not _valid_timestamp(request.get("requested_at"))
            or not _valid_timestamp(request.get("deadline_at"))
            or not _valid_timestamp(body.get("answered_at"))):
        return _verify_result(False, True, reason="malformed-request-binding",
                              receipt_digest=digest)
    if expected_request_id is not None and request["id"] != expected_request_id:
        return _verify_result(False, True, reason="wrong-request",
                              receipt_digest=digest)
    if (expected_request_digest is not None
            and request["digest"] != expected_request_digest):
        return _verify_result(False, True, reason="wrong-request-digest",
                              receipt_digest=digest)
    if (not all(isinstance(provider.get(key), str) and provider.get(key)
                for key in ("name", "instance", "generation"))
            or provider.get("name") != "human-copilot"):
        return _verify_result(False, True, reason="malformed-provider",
                              receipt_digest=digest)
    if (expected_provider_instance is not None
            and provider["instance"] != expected_provider_instance):
        return _verify_result(False, True, reason="wrong-provider-instance",
                              receipt_digest=digest)
    if (expected_provider_generation is not None
            and provider["generation"] != expected_provider_generation):
        return _verify_result(False, True, reason="wrong-provider-generation",
                              receipt_digest=digest)

    parsed = []
    for row in steps:
        if not isinstance(row, dict):
            return _verify_result(False, True, reason="malformed-step",
                                  receipt_digest=digest)
        step_id, verdict, note = row.get("id"), row.get("verdict"), row.get("note")
        if (not isinstance(step_id, str) or not step_id
                or verdict not in CHECKLIST_VERDICTS
                or not isinstance(note, str)):
            return _verify_result(False, True, reason="malformed-step",
                                  receipt_digest=digest)
        parsed.append({"id": step_id, "verdict": verdict, "note": note})
    ids = [row["id"] for row in parsed]
    if len(ids) != len(set(ids)):
        return _verify_result(False, True, reason="duplicate-step",
                              receipt_digest=digest)
    if expected_step_ids is not None:
        wanted = list(expected_step_ids)
        if any(step_id not in wanted for step_id in ids):
            return _verify_result(False, True, reason="unknown-step",
                                  receipt_digest=digest)
        if any(step_id not in ids for step_id in wanted):
            return _verify_result(False, True, reason="missing-step",
                                  receipt_digest=digest)
        if ids != wanted:
            return _verify_result(False, True, reason="step-order",
                                  receipt_digest=digest)
    try:
        outcome = _step_outcome(parsed)
    except ChecklistReceiptError as error:
        return _verify_result(False, True, reason=error.code,
                              receipt_digest=digest)
    authorizing = outcome == "passed"
    if body.get("outcome") != outcome or body.get("authorizing") is not authorizing:
        return _verify_result(False, True, reason="inconsistent-outcome",
                              receipt_digest=digest)
    if any(value is None for value in (
            expected_request_id, expected_request_digest,
            expected_provider_instance, expected_provider_generation,
            expected_step_ids)):
        return _verify_result(False, True, reason="missing-expected-binding",
                              receipt_digest=digest)
    if replay_cache is not None:
        if digest in replay_cache:
            return _verify_result(False, True, reason="replay",
                                  receipt_digest=digest)
        replay_cache.add(digest)
    return _verify_result(True, True, authorizing, None, digest)

def verify_checklist_result(result, **expected):
    """Consumer boundary for an MCP tool result: errors/text alone never pass."""
    if not isinstance(result, dict):
        return _verify_result(False, reason="malformed-tool-result")
    if result.get("isError") is True:
        return _verify_result(False, reason="tool-error")
    if not isinstance(result.get("structuredContent"), dict):
        return _verify_result(False, reason="missing-structured-content")
    return verify_checklist_receipt(result["structuredContent"], **expected)

# -------------------------------------------------------- capability control

def capability_enabled():
    """Authoritative live capability; absent upgrade state is legacy-derived."""
    try:
        return toggle.capability_enabled()
    except Exception as error:
        # A broken authority must never turn prompting back on by accident.
        sys.stderr.write(f"[human-copilot] control read failed: {error}\n")
        return False

class CapabilityPollCache:
    """Reuse an idle verdict only while all six authority inputs are unchanged.

    Publication never uses this cache. Stat failures fall back to the full
    authority read; changed-during-read evidence is never cached.
    """

    def __init__(self):
        self._key = None
        self._value = False
        self._lock = threading.Lock()

    @staticmethod
    def signature():
        paths = (toggle.control_path(), toggle.claude_json_path(),
                 toggle.codex_config_path(), toggle.doctrine_path(),
                 toggle.codex_override_path(), toggle.codex_doctrine_path())
        signature = []
        for path in paths:
            try:
                s = os.stat(path)
                stamp = (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns,
                         s.st_ctime_ns, s.st_mode, s.st_uid, s.st_gid)
            except FileNotFoundError:
                stamp = None
            except OSError:
                return None
            signature.append((path, stamp))
        return tuple(signature)

    def enabled(self):
        with self._lock:
            key = self.signature()
            if key is not None and key == self._key:
                return self._value
            value = capability_enabled()
            # Retry denied reads: an I/O failure may recover without changing
            # file metadata. Only a successful enabled verdict is reusable.
            self._key = key if value and key is not None and key == self.signature() else None
            self._value = value
            return value

_CAPABILITY_POLL_CACHE = CapabilityPollCache()

@contextmanager
def capability_guard(*, idle_poll=False):
    """Hold a shared generation lock through initial request side effects.

    toggle.py takes the exclusive side. Thus, after OFF returns, no current
    server can publish a request, open a pane, or emit an arrival nudge based
    on a stale pre-OFF read.
    """
    path = toggle.control_lock_path()
    try:
        os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        os.fchmod(fd, 0o600)
    except OSError as error:
        sys.stderr.write(f"[human-copilot] control lock failed: {error}\n")
        yield False
        return
    try:
        fcntl.flock(fd, fcntl.LOCK_SH)
        yield (_CAPABILITY_POLL_CACHE.enabled() if idle_poll
               else capability_enabled())
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

DISABLED_DETAIL = {
    "code": "human-copilot-disabled",
    "enabled": False,
    "retryable": False,
}

def disabled_content(reason="disabled-by-operator"):
    detail = {**DISABLED_DETAIL, "reason": reason}
    return [{"type": "text", "text": json.dumps(detail,
                                                   separators=(",", ":"))}]

def disabled_result(reason="disabled-by-operator"):
    detail = {**DISABLED_DETAIL, "reason": reason}
    return {"content": disabled_content(reason), "isError": True,
            "structuredContent": detail}

# ----------------------------------------------------------------- spool

def _usable_runtime_dir(path):
    """Return ``path`` only when it is a private directory owned by us.

    MCP subprocess environments are not guaranteed to retain XDG_RUNTIME_DIR,
    while the GNOME extension always resolves the per-user runtime directory.
    Accepting /run/user/$UID only after checking its ownership and mode keeps
    both sides on the same tmpfs without trusting a forged shared directory.
    """
    if not path:
        return None
    try:
        st = os.stat(path)
    except OSError:
        return None
    if (not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid()
            or stat.S_IMODE(st.st_mode) & 0o077):
        return None
    return path


def _uid_runtime_dir():
    """Conventional systemd-logind runtime path; split out for selftests."""
    return f"/run/user/{os.getuid()}"


def spool_dir():
    """The one request/answer exchange directory used by every surface."""
    override = os.environ.get("HC_SPOOL")
    if override:
        base = override
    else:
        run = (_usable_runtime_dir(os.environ.get("XDG_RUNTIME_DIR"))
               or _usable_runtime_dir(_uid_runtime_dir()))
        base = (os.path.join(run, "human-copilot") if run else
                os.path.expanduser("~/.local/state/human-copilot/spool"))
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(base, sub), mode=0o700, exist_ok=True)
    os.chmod(base, 0o700)
    return base

def _write_private(path, data):
    """0600 from the first byte: create-exclusive tmp, then rename over."""
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)

def notify_operator(first_line, client=None):
    """Best-effort desktop arrival nudge when no healthy overlay is known.

    Failure is silent by design; the spool remains the reliable channel. The
    per-request marker, not this subprocess call, provides cross-process
    deduplication.
    """
    # Operator turned desktop arrival popups off (2026-10-22); the spool and
    # overlay remain the delivery channels.
    return

# ----------------------------------------------------------------- auto-open
#
# Legacy pane/terminal helpers, used only when surface_policy() is the
# explicit "auto" opt-in. Default delivery is overlay-only and never opens
# a surface.

# Presence and the cooldown are keyed per INSTANCE (2026-09-03, hcop-0p7.7):
#   spool/tui.lock                       global surface (copilot-tui with no
#                                        --instance, the overlay): serves EVERY
#                                        instance
#   spool/instances/<id>/tui.lock        a surface opened for one instance
#   spool/instances/<id>/spawn.stamp     that instance's auto-open cooldown
# <id> = the zellij session when there is one, else the server's uuid
# (instance_id). One global lock used to suppress auto-open for everyone and
# one stamp told a second session "another server just opened one" about a
# window that was not its own: nine sessions would have collapsed into one
# pane. A request is served when the global lock OR its instance lock is
# held; the storm guard (two calls inside the cooldown open ONE window) now
# stays within one instance.
INSTANCES_DIR = "instances"
_UNSAFE_ID = re.compile(r"[^A-Za-z0-9._@+-]")

def instance_id(inst):
    """The presence/cooldown key of a request's instance stamp: the zellij
    session when set (every server in that session shares the console the
    operator sees), else the server uuid (a non-zellij server is its own
    instance). None when the stamp carries neither (a pre-stamp request: it
    belongs to every surface). Always one safe path segment."""
    raw = (inst or {}).get("session") or (inst or {}).get("server_uuid")
    if not raw:
        return None
    iid = _UNSAFE_ID.sub("_", str(raw)).strip(".")
    return (iid or "_")[:128]

def instance_dir(spool, iid):
    """spool/instances/<iid>, created 0700 race-safely (every server process
    of an instance may create it at once)."""
    path = os.path.join(spool, INSTANCES_DIR, iid)
    os.makedirs(path, mode=0o700, exist_ok=True)
    return path

def _record_delivery(spool, iid, state, surface, reason=None):
    """Best-effort, non-sensitive evidence for the last surface spawn.

    stderr remains the immediate diagnostic; this additive 0600 record lets
    ``human-copilot-status`` explain a missed pane/terminal after the server
    process that attempted it has gone away.
    """
    try:
        base = instance_dir(spool, iid) if iid else spool
        body = {"schema": "human-copilot-delivery", "schema_version": 1,
                "observed": time.time(), "state": state, "surface": surface}
        if reason:
            body["reason"] = reason
        _write_private(os.path.join(base, DELIVERY_FILE), body)
    except OSError as e:
        sys.stderr.write(f"[human-copilot] could not record surface delivery: {e}\n")

def _lock_held(path):
    """Is a SHARED flock held on ``path`` by anyone? Probing with a
    non-blocking EXCLUSIVE lock succeeds only when nobody holds it. A lock,
    not a pgrep: it cannot outlive the process and cannot match the wrong
    thing (the [b]racket lesson)."""
    try:
        fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)

def tui_alive(spool, iid=None):
    """Is a *rendering* surface watching this spool for this instance?

    A raw lock is only a lease. GNOME's helper can keep that lease after the
    extension's JavaScript stopped painting, and any unrelated lock holder can
    look identical. Trust the global lease only with a fresh successful
    overlay or global-TUI heartbeat; trust an instance lease only with that
    pane's fresh TUI heartbeat. This deliberately fails open to another
    operator surface instead of letting a lock-only process hide a request.
    """
    if overlay_rendering(spool) or tui_rendering(spool):
        return True
    if iid is None:
        return False
    return instance_alive(spool, iid)

def _runtime_pid_alive(pid):
    """True only for a live process owned by this user."""
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        proc = os.stat(f"/proc/{pid}")
    except OSError:
        return False
    return proc.st_uid == os.getuid()

def _healthy_runtime_record(path, schema, now=None, instance_marker=False,
                            require_live_pid=False):
    """Validate one small render heartbeat without trusting file presence."""
    try:
        with open(path, "rb") as f:
            raw = f.read(8193)
        if len(raw) > 8192:
            return False
        record = json.loads(raw)
    except (OSError, ValueError, UnicodeDecodeError):
        return False
    if not isinstance(record, dict):
        return False
    stamp = record.get("observed")
    if (record.get("schema") != schema
            or record.get("schema_version") != 1
            or record.get("state") != "healthy"
            or not isinstance(stamp, (int, float))
            or isinstance(stamp, bool)):
        return False
    if instance_marker is not False and record.get("instance") != instance_marker:
        return False
    if require_live_pid and not _runtime_pid_alive(record.get("pid")):
        return False
    try:
        age = (time.time() if now is None else now) - float(stamp)
    except (OverflowError, TypeError, ValueError):
        return False
    return -OVERLAY_RUNTIME_FUTURE_S <= age <= TUI_RUNTIME_FRESH_S

def tui_runtime_path(spool, iid=None):
    """Heartbeat path for the global TUI or one instance-scoped panel."""
    base = os.path.join(spool, INSTANCES_DIR, iid) if iid else spool
    return os.path.join(base, TUI_RUNTIME_FILE)

def record_tui_runtime(spool, iid=None, now=None):
    """Atomically record that a real TUI completed another render cycle."""
    base = instance_dir(spool, iid) if iid else spool
    _write_private(os.path.join(base, TUI_RUNTIME_FILE), {
        "schema": "human-copilot-tui-runtime",
        "schema_version": 1,
        "state": "healthy",
        "observed": time.time() if now is None else float(now),
        "instance": iid,
        "pid": os.getpid(),
    })

def tui_rendering(spool, iid=None, now=None):
    """True only when this TUI's lease and fresh paint heartbeat agree."""
    base = os.path.join(spool, INSTANCES_DIR, iid) if iid else spool
    if not _lock_held(os.path.join(base, "tui.lock")):
        return False
    marker = iid if iid is not None else None
    return _healthy_runtime_record(
        os.path.join(base, TUI_RUNTIME_FILE),
        "human-copilot-tui-runtime", now, marker, require_live_pid=True)

def overlay_rendering(spool, now=None):
    """Does a held overlay lease have a fresh successful render heartbeat?

    ``tui.lock`` is shared by the overlay and the global TUI, so the lock by
    itself cannot distinguish an always-visible desktop card from a terminal
    hidden behind other windows. The overlay's small atomic runtime record is
    the discriminator. Missing, malformed, stale, future-skewed, or explicit
    error evidence fails open to an arrival notification.
    """
    if not _lock_held(os.path.join(spool, "tui.lock")):
        return False
    return _healthy_runtime_record(
        os.path.join(spool, OVERLAY_RUNTIME_FILE),
        "human-copilot-overlay-runtime", now)

def _request_id_segment(req_id):
    """A request id that is safe to use beside its JSON spool file."""
    raw = str(req_id or "")
    if (not raw or len(raw) > 128 or raw in (".", "..")
            or _UNSAFE_ID.search(raw)):
        return None
    return raw


# A request can outlive the MCP subprocess that published it (for example,
# when a desktop logout closes the client while the overlay remains long
# enough for the operator to answer).  PID alone is not ownership proof: it
# can be reused.  The private request therefore carries the caller's kernel
# boot id plus /proc start ticks.  Surfaces use all three fields and preserve
# anything ambiguous.  This is deliberately OUTSIDE ``instance``: that stamp
# has a pinned four-key protocol used for colour/routing compatibility.

# ``toggle.py`` owns the /proc identity parsers; ``copilot-toggle state``
# classifies the same ownership records with them.
_proc_stat_identity = toggle._proc_stat_identity
_boot_id = toggle._boot_id


def caller_process_identity(pid=None, proc_root="/proc"):
    """Identity of this caller that remains safe across PID reuse.

    None is a supported fail-safe result: the request still works, but an
    eventual answer is classified as ownership-ambiguous and never reclaimed
    automatically.
    """
    pid = os.getpid() if pid is None else pid
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return None
    boot = _boot_id(proc_root)
    base = os.path.join(proc_root, str(pid))
    try:
        if os.stat(base).st_uid != os.getuid():
            return None
        with open(os.path.join(base, "stat"), "rb") as source:
            raw = source.read(8193)
    except OSError:
        return None
    if len(raw) > 8192:
        return None
    parsed = _proc_stat_identity(raw)
    if boot is None or parsed is None:
        return None
    _state, start_ticks = parsed
    return {"schema": CALLER_PROCESS_SCHEMA, "schema_version": 1,
            "pid": pid, "boot_id": boot, "start_ticks": start_ticks}


def caller_process_state(record, proc_root="/proc"):
    """Return ``alive``, ``dead`` or ``unknown`` for a request owner.

    Only conclusive kernel evidence returns dead.  Missing/malformed legacy
    metadata, unreadable procfs, wrong ownership, and oversized records all
    remain unknown so cleanup can never guess its way into deleting a live
    caller's answer.
    """
    return toggle._caller_process_state(record, proc_root)


def answered_request_ids(spool):
    """Complete answer ids currently visible in the private spool."""
    try:
        names = os.listdir(os.path.join(spool, "answers"))
    except OSError:
        return set()
    return {name[:-5] for name in names
            if name.endswith(".json") and ".tmp" not in name}


def _private_json(path, max_bytes=4 * 1024 * 1024):
    """Read one owned 0600 regular JSON file without following a symlink."""
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None, None
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                or stat.S_IMODE(info.st_mode) & 0o077
                or info.st_size > max_bytes):
            return None, None
        chunks, left = [], max_bytes + 1
        while left:
            chunk = os.read(fd, min(65536, left))
            if not chunk:
                break
            chunks.append(chunk)
            left -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > max_bytes:
            return None, None
        body = json.loads(raw.decode("utf-8"))
        return (body, info) if isinstance(body, dict) else (None, None)
    except (OSError, UnicodeDecodeError, ValueError):
        return None, None
    finally:
        os.close(fd)


def reclaim_answered_orphans(spool, now=None, grace_s=ORPHAN_GRACE_S,
                             proc_root="/proc"):
    """Conservatively reclaim answered requests whose caller is gone.

    Multiple current surfaces may scan together, so a private global GC flock
    serialises deletion.  An answer is eligible only when both immutable JSON
    files are valid/owned/private, the request has the current PID+boot+start
    identity, procfs conclusively says that identity is dead, and the answer
    has aged past the recovery grace.  Legacy or unreadable ownership is
    reported ambiguous and preserved.  The request is unlinked first so a
    half-finished cleanup can never resurrect an actionable card; only then
    are its now-unreachable answer and nudge removed.
    """
    result = {"state": "complete", "pairs": 0, "reclaimed": 0,
              "live": 0, "ambiguous": 0, "young": 0, "errors": []}
    lock_path = os.path.join(spool, ORPHAN_GC_LOCK)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        lock_fd = os.open(lock_path, flags, 0o600)
        os.fchmod(lock_fd, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as error:
        try:
            os.close(lock_fd)
        except (NameError, OSError):
            pass
        if error.errno in (errno.EACCES, errno.EAGAIN):
            result["state"] = "busy"
        else:
            result["state"] = "unavailable"
            result["errors"].append("gc-lock-unavailable")
        return result
    try:
        answer_dir = os.path.join(spool, "answers")
        request_dir = os.path.join(spool, "requests")
        try:
            names = sorted(os.listdir(answer_dir))
        except OSError:
            return result
        observed = time.time() if now is None else float(now)
        grace = max(0.0, float(grace_s))
        for name in names:
            if not name.endswith(".json") or ".tmp" in name:
                continue
            rid = _request_id_segment(name[:-5])
            if rid is None:
                continue
            answer_path = os.path.join(answer_dir, name)
            request_path = os.path.join(request_dir, rid + ".json")
            answer, answer_info = _private_json(answer_path)
            request, _request_info = _private_json(request_path)
            if answer is None or request is None or request.get("id") != rid:
                continue
            result["pairs"] += 1
            owner_state = caller_process_state(
                request.get("caller_process"), proc_root=proc_root)
            if owner_state == "alive":
                result["live"] += 1
                continue
            if owner_state != "dead":
                result["ambiguous"] += 1
                continue
            age = observed - answer_info.st_mtime
            if age < grace:
                result["young"] += 1
                continue
            failed = False
            try:
                os.unlink(request_path)
            except FileNotFoundError:
                pass
            except OSError:
                result["errors"].append(f"{rid}-request-unlink-failed")
                continue
            for path, label in (
                    (arrival_nudge_path(spool, rid), "nudge"),
                    (answer_path, "answer")):
                if path is None:
                    continue
                try:
                    os.unlink(path)
                except FileNotFoundError:
                    pass
                except OSError:
                    result["errors"].append(f"{rid}-{label}-unlink-failed")
                    failed = True
            if not failed:
                result["reclaimed"] += 1
        if result["errors"]:
            result["state"] = "degraded"
        return result
    finally:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)

def arrival_nudge_path(spool, req_id):
    """Private cross-process once marker for one request, or None."""
    rid = _request_id_segment(req_id)
    if rid is None:
        return None
    return os.path.join(spool, "requests", rid + ARRIVAL_NUDGE_SUFFIX)

def _claim_arrival_nudge(spool, req_id):
    """Atomically claim a request's desktop nudge.

    Returns ``claimed``, ``already``, or ``error``. O_EXCL is the arbitration:
    a server and any number of global/scoped TUIs can discover the same
    request together, but exactly one invokes notify-send. The marker contains
    no request text and is mode 0600 from its first visible instant.
    """
    path = arrival_nudge_path(spool, req_id)
    if path is None:
        return "error"
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return "already"
    except OSError as e:
        sys.stderr.write(f"[human-copilot] could not claim arrival nudge "
                         f"for {req_id}: {e}\n")
        return "error"
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"schema": "human-copilot-arrival-nudge",
                       "schema_version": 1, "observed": time.time()}, f)
    except Exception as e:
        try:
            os.unlink(path)
        except OSError:
            pass
        sys.stderr.write(f"[human-copilot] could not record arrival nudge "
                         f"for {req_id}: {e}\n")
        return "error"
    return "claimed"

def arrival_nudge(spool, request, now=None):
    """Notify once if ``request`` lacks a demonstrably healthy overlay.

    This is deliberately shared by server.py and tui.py. A TUI loaded after a
    long-lived MCP server can therefore bridge that old process: it scans the
    request, claims the same marker a new server would use, and emits the
    desktop arrival notification. The marker is left for the request's whole
    lifetime so later scans and sibling panels cannot duplicate the nudge.

    Returns ``overlay``, ``notified``, ``already``, ``gone``, or ``error``.
    """
    if not capability_enabled():
        return "disabled"
    rendering = (overlay_rendering(spool) if now is None
                 else overlay_rendering(spool, now))
    if rendering:
        return "overlay"
    rid = request.get("id") if isinstance(request, dict) else None
    marker = arrival_nudge_path(spool, rid)
    if marker is None:
        return "error"
    # A TUI can retain a row for the fraction of a tick in which its server
    # removes the request. Do not notify for a request that is already gone.
    if not os.path.exists(os.path.join(spool, "requests", str(rid) + ".json")):
        return "gone"
    claim = _claim_arrival_nudge(spool, rid)
    if claim != "claimed":
        return claim
    summary = request.get("summary") if isinstance(request.get("summary"), str) else ""
    first = summary.strip().splitlines()[0] if summary.strip() else "(no summary)"
    notify_operator("Human Copilot request waiting — focus it, or open one: "
                    "tui → c.  " + first,
                    client=request.get("client"))
    return "notified"

def clear_arrival_nudge(spool, req_id):
    """Remove one request's marker after its request file is gone."""
    path = arrival_nudge_path(spool, req_id)
    if path is not None:
        try:
            os.unlink(path)
        except OSError:
            pass

def prune_arrival_nudges(spool):
    """Clean orphan markers left by older servers unaware of this protocol.

    A newly loaded TUI performs this lifecycle cleanup. Existing request JSON
    files, including temporarily unreadable ones, always retain their marker.
    """
    rdir = os.path.join(spool, "requests")
    try:
        names = os.listdir(rdir)
    except OSError:
        return
    for name in names:
        if not name.endswith(ARRIVAL_NUDGE_SUFFIX):
            continue
        rid = name[:-len(ARRIVAL_NUDGE_SUFFIX)]
        if os.path.exists(os.path.join(rdir, rid + ".json")):
            continue
        try:
            os.unlink(os.path.join(rdir, name))
        except OSError:
            pass

def instance_alive(spool, iid):
    """Is this instance's own pane both leased and freshly rendering?"""
    if not iid:
        return False
    try:
        # Preserve the established per-instance lock-file lifecycle: an
        # operational server probe seeds the 0600 lock path, while read-only
        # status still creates nothing.
        instance_dir(spool, iid)
        return tui_rendering(spool, iid)
    except OSError:
        return False

def _terminal_command(cmd):
    """Run ``cmd`` with capture-only color suppression removed.

    This second boundary matters for single-instance terminals: an existing
    primary process can ignore the environment of the client asking for a new
    window and spawn the command from its own older environment instead.
    """
    return ["env", "-u", "NO_COLOR", "-u", "NODE_DISABLE_COLORS", "--"] + list(cmd)

def terminal_argv(cmd, title=None):
    """The argv that opens ``cmd`` (a list) in a desktop terminal window, or
    None when no emulator is known. One choice for the whole lane — the TUI
    auto-open and ``copilot-open`` share it: ``$HC_TERMINAL`` verbatim, else
    ptyxis, else x-terminal-emulator.

    ``title`` names the window so a checklist step can point at it (the
    operator cannot look at a window called ``env``; hcop-22h). Only ptyxis
    takes it here: ``$HC_TERMINAL`` is the operator's own verbatim command
    and x-terminal-emulator's title flag is not portable across the
    alternatives, so both are opened untitled rather than guessed at.
    """
    cmd = _terminal_command(cmd)
    term = os.environ.get("HC_TERMINAL")
    if term:
        return shlex.split(term) + list(cmd)
    if shutil.which("ptyxis"):
        # A request sent to the existing single-instance Ptyxis process can
        # return success without opening the requested command (observed on
        # 50.1). A standalone instance owns this one fallback window and
        # cannot silently drop the TUI argv during GApplication handoff.
        head = ["ptyxis", "--standalone"]
        if title:
            head += ["-T", title]
        return head + ["-x", shlex.join(cmd)]
    if shutil.which("x-terminal-emulator"):
        return ["x-terminal-emulator", "-e"] + list(cmd)
    return None

def _terminal_env():
    """Environment for a program opened on the operator's desktop.

    Agent command runners set these variables to keep captured tool output
    plain.  Letting them cross the desktop boundary disables highlighting in
    terminal applications and, with a single-instance emulator such as
    Ptyxis, can contaminate later ordinary windows too.
    """
    env = os.environ.copy()
    env.pop("NO_COLOR", None)
    env.pop("NODE_DISABLE_COLORS", None)
    return env

def open_in_terminal(cmd, title=None):
    """Spawn ``cmd`` in a desktop terminal, detached. Returns the argv used
    or None when nothing could be opened."""
    argv = terminal_argv(cmd, title=title)
    if argv is None:
        return None
    try:
        subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True,
                         env=_terminal_env())
    except OSError:
        return None
    return argv

# ----------------------------------------------------------- the sidepanel
#
# An ATTACHED zellij session gets its copilot as a floating pane INSIDE that
# session (2026-09-03, hcop-0p7.4) — the operator is looking at that console,
# so that is where the checklist belongs. Everything the pane needs rides as
# ARGUMENTS: the pane inherits the zellij SERVER's environment, frozen when
# the session was created, not this process's — an HC_SPOOL or --instance
# passed through env would silently be the wrong one (or absent).
# `--name` pins the pane's title, which zellij puts in the window title as
# "<session> | copilot · <id>"; the overlay routes hotkeys by that title.
# `--close-on-exit` lets the pane vanish with the TUI instead of parking on
# an "exit code" banner. Any failure here falls back to today's terminal
# window and says so on stderr — never a silent no-surface.
#
# The pane is TILED, not floating (2026-09-04, hcop-goq). A floating pane is
# drawn OVER the agent's pane: the terminal underneath keeps its old bounds
# and its text is simply covered, so nothing re-wraps and the covered lines
# are gone until zellij's floating layer is hidden again — which is what the
# operator saw and asked to change ("it should instead merge with it,
# becoming an integrated box that doesn't overlap but rather creates new
# bounds for word wrap"). `--direction down` splits the focused pane instead,
# so the session re-flows around the copilot and gets the rows back when it
# closes. zellij 0.44.3 takes --width/--height/-x/-y only for FLOATING panes,
# so a tiled pane takes its half and cannot be sized at creation.

def pane_layout():
    """(new-pane flags, is_floating) for the copilot pane. $HC_PANE_DIRECTION
    is `down` (default), `right`, or `floating` to restore the old covering
    pane exactly; anything else reads as `down`."""
    d = (os.environ.get("HC_PANE_DIRECTION") or "down").strip().lower()
    if d == "floating":
        return ["--floating"], True
    return ["--direction", d if d in ("down", "right") else "down"], False

def zellij_bin():
    """The zellij binary: $HC_ZELLIJ (tests point it at a stub) else `zellij`
    on PATH; None when neither resolves."""
    return shutil.which(os.environ.get("HC_ZELLIJ") or "zellij")

def pane_argv(session, iid, spool):
    """The argv that opens this instance's copilot pane inside ``session``,
    or None when there is no zellij to run."""
    zb = zellij_bin()
    if zb is None:
        return None
    return [zb, "--session", session, "action", "new-pane", *pane_layout()[0],
            "--close-on-exit", "--name", PANE_NAME.format(iid=iid), "--",
            os.path.join(HERE, "tui.py"), "--instance", iid, "--spool", spool]

_PANE_ID = re.compile(r"\b((?:terminal|plugin)_\d+)\b")

def _zellij_action(argv):
    """Run one zellij action with the protocol stdio kept out of reach:
    stdin=/dev/null, stdout/stderr captured, a short timeout. Returns the
    CompletedProcess or None (the reason is written to stderr)."""
    try:
        return subprocess.run(argv, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, timeout=PANE_OPEN_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as e:
        sys.stderr.write(f"[human-copilot] {shlex.join(argv[1:5])}… failed: {e}\n")
        return None

def open_pane_in_session(session, iid, spool):
    """Open the instance's copilot pane inside its zellij session. Returns the
    argv used, or None when it could not be opened (said on stderr) so the
    caller falls back to a terminal window. The pane id zellij prints
    (terminal_N) is kept in the instance dir so a later call can focus it."""
    argv = pane_argv(session, iid, spool)
    if argv is None:
        sys.stderr.write("[human-copilot] no zellij binary; falling back to a terminal window\n")
        return None
    r = _zellij_action(argv)
    if r is None:
        return None
    if r.returncode != 0:
        sys.stderr.write(f"[human-copilot] new-pane in {session!r} exited "
                         f"{r.returncode}: {(r.stderr or r.stdout).strip()[:200]}; "
                         f"falling back to a terminal window\n")
        return None
    # Zellij focuses a newly-created pane. Restore the operator's prior pane
    # immediately: the card/OSD remains visible and answerable by its global
    # chords, but receiving a request never steals their current work focus.
    _zellij_action([zellij_bin(), "--session", session, "action", "focus-previous-pane"])
    m = _PANE_ID.search(r.stdout or "")
    if m:
        try:
            _write_private(os.path.join(instance_dir(spool, iid), PANE_FILE),
                           {"pane": m.group(1), "session": session, "at": time.time(),
                            "floating": pane_layout()[1]})
        except OSError as e:
            sys.stderr.write(f"[human-copilot] could not record the pane id: {e}\n")
    return argv

def focus_pane_in_session(session, iid, spool):
    """The instance already has a pane holding its lock: bring it to the front
    instead of opening a second one. Best effort — a pane id we recorded may
    be gone; zellij's answer is logged, never trusted as a surface."""
    try:
        with open(os.path.join(spool, INSTANCES_DIR, iid, PANE_FILE)) as f:
            rec = json.load(f) or {}
        pane = rec.get("pane")
    except (OSError, ValueError):
        return False
    zb = zellij_bin()
    if not pane or not zb:
        return False
    # A tiled pane is already in the layout; only a floating one has to be
    # revealed first. A record written before hcop-goq has no `floating` key
    # and was floating, so that is the default for a legacy record.
    actions = [["focus-pane-id", pane]]
    if rec.get("floating", True):
        actions.insert(0, ["show-floating-panes"])
    for action in actions:
        r = _zellij_action([zb, "--session", session, "action", *action])
        if r is None or r.returncode != 0:
            return False
    return True

def surface_policy():
    """Read the persisted operator preference on every delivery check.

    Missing, malformed or unreadable configuration means overlay-only: a
    renderer failure must never grant permission to open intrusive windows.
    Explicit ``auto`` retains the legacy pane/terminal route for opt-in use.
    """
    path = os.environ.get("HC_SURFACE_POLICY_FILE") or os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "human-copilot", "surface.json")
    try:
        with open(path) as stream:
            policy = json.load(stream)
        return "auto" if isinstance(policy, dict) and policy.get("mode") == "auto" else "overlay"
    except (OSError, ValueError):
        return "overlay"


def overlay_delivery(spool, iid=None):
    """Record real render health without spawning or manufacturing an answer."""
    healthy = overlay_rendering(spool)
    _record_delivery(spool, iid, "healthy" if healthy else "degraded", "overlay",
                     None if healthy else "overlay-unavailable")
    return healthy


def maybe_spawn_tui(spool, iid=None, inst=None):
    """Honor the persisted surface preference, defaulting to overlay-only.

    Overlay mode reports actual render health and never spawns. Only explicit
    auto mode opens the instance pane when attached, else a terminal.
    In auto mode returns True if a spawn was issued —
    or if THIS INSTANCE just issued one (the cooldown arm). The cooldown
    stamp lives IN THE SPOOL, per instance, and is checked under its own
    flock because every agent session runs its own copy of this server —
    simultaneous calls from one instance must open one window, not a storm.
    Another instance's stamp is never consulted: a window opened for someone
    else is not evidence that this instance has one. The stamp file exists
    from the first probe; size > 0, read under the lock, is the only
    "a spawn happened" signal — its visibility means nothing on its own.
    A pane that could not be opened falls back to the terminal window unless
    a global surface (the overlay / a global panel) already shows it."""
    if surface_policy() == "overlay":
        return overlay_delivery(spool, iid)
    try:
        stamp = os.path.join(instance_dir(spool, iid) if iid else spool,
                             "spawn.stamp")
        fd = os.open(stamp, os.O_RDWR | os.O_CREAT, 0o600)
    except OSError:
        return False
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        st = os.fstat(fd)
        if st.st_size and time.time() - st.st_mtime < SPAWN_COOLDOWN_S:
            return True                    # this instance just opened one
        inst = inst or {}
        session = inst.get("session") if inst.get("attached") is True else None
        opened = None
        if session and iid:
            opened = open_pane_in_session(session, iid, spool)
            if opened is None and tui_alive(spool):
                sys.stderr.write("[human-copilot] a healthy global surface is up; no terminal opened\n")
                _record_delivery(spool, iid, "degraded", "global-fallback",
                                 "pane-spawn-failed")
                return True                # shown there; nothing spawned, no stamp
        tui = os.path.join(HERE, "tui.py")
        if opened is None:
            if open_in_terminal([tui]) is None:
                _record_delivery(spool, iid, "error", "none",
                                 "surface-spawn-failed")
                return False
            _record_delivery(spool, iid, "spawn-issued", "terminal",
                             "pane-spawn-failed" if session else None)
        else:
            _record_delivery(spool, iid, "spawn-issued", "pane")
        os.write(fd, b"x")                 # size>0 = a real spawn happened
        return True
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)

# ----------------------------------------------------------------- instance
#
# Which agent session is asking? This process inherits the agent's environment,
# so ZELLIJ_SESSION_NAME / ZELLIJ_PANE_ID name the zellij session (and pane)
# that spawned it — when there is one. The session alone is NOT an identity:
# two live servers were measured in one session, both pane 0 (2026-09-02), so
# every request also carries a uuid minted once per server process. Every
# request file is stamped with all three plus the attached verdict; the
# surfaces ignore the field today and the multi-instance work (hcop-0p7) keys
# on it.
SERVER_UUID = uuid.uuid4().hex
ATTACH_PROBE_TIMEOUT_S = 3        # list-clients is local IPC; anything slower is a wedge
# Attached and unattached calls both use the overlay unless the operator
# explicitly enables legacy auto routing. Refusing unattached requests is a
# separate opt-in policy via HC_REQUIRE_ATTACHED=1.
REQUIRE_ATTACHED_DEFAULT = False
REQUIRE_ATTACHED_ENV = "HC_REQUIRE_ATTACHED"

def _client_rows(output):
    """Rows of `zellij action list-clients` that are clients. The command
    prints a header (CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND) then one row
    per attached client — and exits 0 EVEN for an unknown session, printing
    prose instead (measured, 0.44.3), so the exit code proves nothing: no
    header means no verdict. Only the first token is read; the
    RUNNING_COMMAND column carries the agent's whole prompt and is never
    parsed."""
    lines = [ln for ln in (output or "").splitlines() if ln.strip()]
    if not lines or not lines[0].split()[:1] == ["CLIENT_ID"]:
        return None
    return [ln for ln in lines[1:] if ln.split()[0].isdigit()]

def zellij_attached(session):
    """True: the session has >=1 attached client. False: zero clients, no
    zellij on PATH, or the probe failed/timed out/answered prose. None: not
    under zellij (no session name). Never touches this process's stdio —
    stdout is the protocol and a child reading stdin would eat JSON-RPC."""
    if not session:
        return None
    zb = zellij_bin()
    if not zb:
        return False
    try:
        r = subprocess.run(
            [zb, "--session", session, "action", "list-clients"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True,
            timeout=ATTACH_PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return False
    if r.returncode != 0:
        return False
    rows = _client_rows(r.stdout)
    return bool(rows)

def instance_stamp():
    """The identity stamped on every request: session, pane, per-process
    uuid, and this call's attached verdict (probed per call — the operator
    attaches and detaches between calls)."""
    session = os.environ.get("ZELLIJ_SESSION_NAME") or None
    return {"session": session,
            "pane": os.environ.get("ZELLIJ_PANE_ID") or None,
            "server_uuid": SERVER_UUID,
            "attached": zellij_attached(session)}

def require_attached():
    """The opt-in: HC_REQUIRE_ATTACHED=1/true/yes/on refuses unattached
    calls; 0/false/no/off keeps today's path; unset = the named default."""
    raw = os.environ.get(REQUIRE_ATTACHED_ENV)
    if raw is None or not raw.strip():
        return REQUIRE_ATTACHED_DEFAULT
    return raw.strip().lower() in ("1", "true", "yes", "on")

def unattached_refusal(inst):
    """None when the call may proceed, else the TextContent list that answers
    it: a clear result the agent can act on, never a hang, never a silent
    pass."""
    if inst["attached"] is True or not require_attached():
        return None
    where = (f"zellij session {inst['session']!r} has no attached client"
             if inst["session"] else "this session is not running under zellij")
    return [{"type": "text",
             "text": f"interactive_feedback refused: {where}, so no operator "
                     f"console would show the checklist ({REQUIRE_ATTACHED_ENV} "
                     f"is set). Nothing was sent to the operator. Record the "
                     f"blocker and continue autonomous work; do not open another "
                     f"surface or assume the steps passed."}]

# ----------------------------------------------------------------- colour lease
#
# The palette registry (palette.py, spool/palette.json) gives each SESSION a
# slot; the overlay's strip number and the TUI's group number read it. A slot
# the surfaces assigned on their own is PROVISIONAL (evicted first when the
# table fills, and never proof the session is alive); a slot a SERVER holds
# is authoritative and lives exactly as long as the holder's shared flock —
# this process's, released by the kernel when it exits, however it exits.
# One lease per process, taken at the FIRST call (a session that never asks
# for eyes takes no colour), for the stamp's session key. Failing to lease is
# said on stderr and changes nothing about the request: colour is a
# convenience, the checklist is the point.
_LEASE = None
_LEASE_LOCK = threading.Lock()

def hold_palette(inst, spool):
    """Hold this session's palette slot for the life of the process (idempotent:
    the first call leases, later calls return the same Assignment). None when
    the palette could not be held — the reason is on stderr."""
    global _LEASE
    with _LEASE_LOCK:
        if _LEASE is not None:
            return _LEASE
        try:
            _LEASE = palette.hold(inst, holder=SERVER_UUID, spool=spool)
        except Exception as e:                      # registry unwritable, odd stamp…
            sys.stderr.write(f"[human-copilot] palette lease failed: {e}; "
                             f"surfaces fall back to a provisional colour\n")
            return None
        return _LEASE

# ----------------------------------------------------------------- the wait

_CANCELLED = set()                 # JSON-RPC ids of cancelled tools/call requests
_CANCELLED_LOCK = threading.Lock()

class FeedbackError:
    """Content that must leave the call as isError: True (hcop-453): an
    unresolved target is an ERROR to the calling engine, not a quiet result
    that reads as a pass."""

    def __init__(self, content, structured=None):
        self.content = content
        self.structured = structured

class FeedbackResult(list):
    """Legacy content-list behaviour plus optional MCP structuredContent."""

    def __init__(self, content, structured=None):
        super().__init__(content)
        self.structured = structured

def structured_response(ans, request):
    """Typed human reply; rendered prose can never manufacture a choice.

    Only use after answer storage authentication/acknowledgement. Checklist
    consumers keep their existing separately verified verdict receipt.
    """
    mode = request.get("responder", {}).get("mode")
    if mode == "checklist":
        return None
    if mode not in ("choice", "text", "prose", "ack"):
        raise ValueError("unsupported response mode")
    if "cancelled" in ans and not isinstance(ans["cancelled"], bool):
        raise ValueError("invalid cancellation marker")
    remote = verified_remote_response(ans, request) if "remote_response" in ans else None
    if "remote_response" in ans and remote is None:
        raise ValueError("invalid remote response")
    text = ans.get("text", "")
    if not isinstance(text, str) or len(text) > responder.MAX_TEXT_CHARS:
        raise ValueError("invalid reply text")
    note = text if mode in ("choice", "ack") else ""
    if remote is not None:
        note = remote.get("note", "")
        text = remote.get("text", note)
    completion = "cancelled" if ans.get("cancelled") is True else "complete"
    marker = ans.get("response_completion")
    if marker is not None and completion != "cancelled":
        expected = {"schema": "human-copilot.response-completion/v1",
                    "schema_version": 1, "mode": mode, "status": "not_done"}
        if marker != expected:
            raise ValueError("invalid reply completion")
        completion = "not_done"
    choice = None
    if mode == "choice" and completion == "complete":
        selected = ans.get("choice")
        options = request["responder"].get("options", [])
        matches = [row for row in options if isinstance(selected, dict)
                   and row["value"] == selected.get("value")]
        if len(matches) != 1:
            raise ValueError("missing explicit choice")
        choice = {"value": matches[0]["value"], "label": matches[0]["label"]}
    elif ans.get("choice") is not None and completion != "cancelled":
        raise ValueError("choice conflicts with reply mode or completion")
    if mode in ("text", "prose") and completion == "complete" and not text.strip():
        raise ValueError("empty text reply")
    return {"schema": "human-copilot.response/v1", "schema_version": 1,
            "request_id": request["id"], "mode": mode,
            "completion": completion, "choice": choice, "text": text, "note": note}

def disabled_feedback_error(reason):
    detail = {**DISABLED_DETAIL, "reason": reason}
    return FeedbackError(disabled_content(reason), detail)

# ----------------------------------------------------- publication (shared)

class PublishRefused(ValueError):
    """Arguments refused BEFORE any surface saw a request. ``text`` is the
    exact operator-facing sentence the tools/call path returns, so the MCP
    lane and the public publish_request refuse identically."""

    def __init__(self, text):
        self.text = text
        super().__init__(text)

def feedback_arguments(summary, timeout, response_args):
    """(summary, timeout_s, response) for one call, or raise PublishRefused.

    The single argument gate: `handle_tools_call` and `publish_request` both
    go through it, so an external publisher cannot enqueue a request the MCP
    lane would have refused. ``response_args`` is the RAW argument object
    (mode/options), never an already-stamped contract."""
    if not isinstance(summary, str) or not summary.strip():
        raise PublishRefused("interactive_feedback needs a non-empty "
                             "`summary` (a numbered checklist).")
    try:
        timeout_s = float(timeout or DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout_s = DEFAULT_TIMEOUT_S
    timeout_s = max(MIN_TIMEOUT_S, min(MAX_TIMEOUT_S, timeout_s))
    if response_args is None:
        response_args = {}
    if not isinstance(response_args, dict):
        raise PublishRefused("interactive_feedback arguments invalid: "
                             "`response` must be an object with `mode` and "
                             "optional `options`.")
    response, err = response_contract(response_args)
    if (err is None and response["mode"] == "checklist"
            and not responder.canonical_prompt(summary)["steps"]):
        err = ("mode 'checklist' needs at least one numbered action in `summary`. "
               "Resend with the specific action and expected observation, or "
               "use mode 'text' for a necessary free-text question. "
               "No request was shown to the operator.")
    if err is not None:
        raise PublishRefused(f"interactive_feedback arguments invalid: {err}")
    return summary, timeout_s, response

def _build_request(summary, timeout_s, response, *, client, instance,
                   caller_process=None, request_id=None):
    """Build and stamp one immutable request before making it visible.

    The single publication sequence — `run_feedback` and `publish_request`
    share it so a future field, stamp or ordering change cannot land on only
    one path. Server publication is the semantic boundary: every consumer
    gets a canonical mode/step/target description and immutable revision, so
    a transport never has to recreate the prompt grammar. The checklist stamp
    reuses those ordered ids; revision/digest are refreshed only once every
    additive immutable field is present."""
    req_id = request_id or uuid.uuid4().hex[:12]
    created = time.time()
    request = {
        "id": req_id, "summary": summary, "created": created,
        "deadline": created + timeout_s, "timeout": timeout_s,
        "client": client, "instance": instance,
        "response": response or {"mode": None, "options": []},
    }
    if caller_process is not None:
        request["caller_process"] = caller_process
    responder.stamp_request_contract(request)
    stamp_checklist_contract(request)
    responder.refresh_request_revision(request)
    if request.get("checklist") is not None:
        request["request_digest"] = checklist_request_digest(request)
    return request

def _publish(spool, summary, timeout_s, response, *, client, instance,
             caller_process=None):
    request = _build_request(summary, timeout_s, response, client=client,
                             instance=instance, caller_process=caller_process)
    _write_private(os.path.join(spool, "requests", request["id"] + ".json"),
                   request)
    return request

PUBLISHER_CONTRACT_SCHEMA = publisher_lifecycle.SCHEMA
PublisherError = publisher_lifecycle.PublisherError

def _publisher_api():
    # Hosts may import this file by path without registering it in sys.modules.
    return SimpleNamespace(spool_dir=spool_dir, _build_request=_build_request,
                           instance_stamp=instance_stamp,
                           clear_arrival_nudge=clear_arrival_nudge)

def publish_request(summary, timeout=None, response=None, *, client,
                    episode_id=None, existing_request_id=None):
    """Publish one request for an alternate answering surface; return its id.

    The public, NON-BLOCKING publication entry point (hcop-da5): an external
    same-account surface (e.g. the Graphene Copilot host request verb) enqueues
    through this instead of rebuilding the sequence around `_write_private`.
    ``response`` is the raw {mode, options} argument object; malformed
    summary/timeout/response are refused with PublishRefused carrying exactly
    the text the MCP lane returns. ``client`` names the publisher in every
    surface's card and is required.

    It publishes and returns without waiting, so unlike `run_feedback` it
    stamps no `caller_process` ownership: orphan GC never mistakes a restarted
    nonblocking producer for a dead waiting MCP caller. With ``episode_id``, a
    private lifecycle record reuses one request across producer restarts; the
    caller reads with ``published_answer`` and acknowledges after durable
    application. Publication shares the capability guard with blocking calls
    so OFF cannot race a late write."""
    if not isinstance(client, str) or not client.strip() or len(client) > 256:
        raise PublishRefused("interactive_feedback arguments invalid: "
                             "`client` must be a short non-empty name for the "
                             "publishing surface.")
    summary, timeout_s, response = feedback_arguments(summary, timeout, response)
    if episode_id is None and existing_request_id is not None:
        raise PublisherError("episode_id required for adoption")
    with capability_guard() as enabled:
        if not enabled:
            raise PublishRefused("interactive_feedback is disabled; no request was published.")
        if episode_id is not None:
            return publisher_lifecycle.publish(
                summary, timeout_s, response, client=client.strip(),
                episode_id=episode_id, existing_request_id=existing_request_id,
                server_api=_publisher_api())
        request = _publish(spool_dir(), summary, timeout_s, response,
                           client=client.strip(), instance=instance_stamp())
        return request["id"]

def published_answer(episode_id, *, client):
    """Return exact JSON bytes after private capture; None while unanswered."""
    return publisher_lifecycle.answer(
        episode_id, client=client, server_api=_publisher_api())

def acknowledge_published(episode_id, answer_sha256, *, client):
    """Acknowledge only after the caller durably applies these answer bytes."""
    return publisher_lifecycle.acknowledge(
        episode_id, answer_sha256, client=client,
        server_api=_publisher_api())

def run_feedback(summary, timeout_s, rpc_id, response=None):
    """Spool the request, wait for the TUI's answer (or deadline/cancel).
    ``response`` is the validated mode/options contract (response_contract),
    stamped into the request so every surface renders the same shape; None is
    the legacy checklist/prose call. Returns the MCP content list."""
    # Publication and every initial operator-facing side effect share-lock the
    # same authority OFF changes exclusively. The second check closes the race
    # after handle_tools_call's fast-path check.
    with capability_guard() as enabled:
        if not enabled:
            return disabled_feedback_error("disabled-before-publication")
        inst = instance_stamp()
        refusal = unattached_refusal(inst)
        if refusal is not None:
            sys.stderr.write(f"[human-copilot] refused unattached call: {inst}\n")
            return refusal
        spool = spool_dir()
        # A current call is also a cheap maintenance opportunity for an
        # overlay-only desktop: reclaim only conclusively dead answered
        # callers, never a live or legacy/ambiguous request.
        reclaim_answered_orphans(spool)
        hold_palette(inst, spool)  # name the session before a surface sees it
        # One shared publication sequence with publish_request (_publish);
        # this path additionally stamps caller ownership because it stays
        # alive to consume the answer.
        request = _publish(spool, summary, timeout_s, response,
                           client=_CLIENT_LABEL, instance=inst,
                           caller_process=caller_process_identity())
        req_id = request["id"]
        deadline = request["deadline"]
        req_path = os.path.join(spool, "requests", req_id + ".json")
        ans_path = os.path.join(spool, "answers", req_id + ".json")
        # A healthy overlay beats a notification: its desktop card and arrival
        # OSD are already visible. A TUI alone does not — its terminal may be
        # behind other windows and its curses beep is not a desktop cue.
        iid = instance_id(inst)
        attached = inst.get("attached") is True and bool(inst.get("session"))

        def ensure_surface(*, focus=False):
            """Ensure this still-waiting request has a live delivery path."""
            if not capability_enabled():
                return False
            arrival_nudge(spool, request)
            if surface_policy() == "overlay":
                return overlay_delivery(spool, iid)
            if attached:
                if instance_alive(spool, iid):
                    if focus:
                        focus_pane_in_session(inst["session"], iid, spool)
                    return True
            elif tui_alive(spool, iid):
                return True
            if maybe_spawn_tui(spool, iid, inst):
                return True
            # Cover a renderer dropping its lease between the two probes.
            arrival_nudge(spool, request)
            return False

        # Persisted overlay-only preference applies to every caller. Explicit
        # auto mode retains legacy pane/terminal routing. OFF cannot interleave
        # until this initial delivery block has completed.
        ensure_surface()
    next_surface_check = time.monotonic() + SURFACE_RECHECK_S
    terminal_event = None
    terminal_detail = None
    terminal_recorded = False
    preserve_answer = False
    try:
        while time.time() < deadline:
            if not capability_enabled():
                terminal_event = "cancelled"
                terminal_detail = "capability revoked while waiting"
                return disabled_feedback_error("revoked-while-waiting")
            with _CANCELLED_LOCK:
                if rpc_id in _CANCELLED:
                    terminal_event = "cancelled"
                    terminal_detail = "caller cancelled"
                    return [{"type": "text",
                             "text": "The call was cancelled before the operator answered."}]
            if os.path.exists(ans_path):
                # Every current surface publishes through responder.py's
                # complete-temp + no-clobber hard link; legacy files were also
                # complete before visibility. A visible answer is safe to read.
                with open(ans_path) as f:
                    ans = json.load(f)
                err = ans.get("error")
                if isinstance(err, dict):
                    event = ("ambiguous" if err.get("reason") == "ambiguous"
                             else "missing")
                elif ans.get("cancelled"):
                    event = "cancelled"
                else:
                    event = "already-answered"
                acknowledged = responder.Responder(spool).acknowledge_consumed(
                    request, ans, event=event)
                terminal_recorded = acknowledged.get("ok") is True
                # The owning WAL is authoritative even if an answer's remote
                # markers were stripped to resemble a legacy surface answer.
                if not terminal_recorded:
                    # The human has already answered. A transient storage
                    # failure must not erase that response or forge completion.
                    preserve_answer = True
                    return FeedbackError([{"type": "text", "text":
                        "[NOT DONE: answer storage acknowledgement failed.]"}])
                # A request whose REQUIRED target never resolved is an error
                # to the calling engine (hcop-453) — never an empty pass.
                if isinstance(err, dict):
                    return FeedbackError(target_error_content(err))
                if "remote_response" in ans and verified_remote_response(ans, request) is None:
                    return FeedbackError([{"type": "text", "text":
                        "[NOT DONE: remote answer authentication or storage binding failed.]"}])
                content = answer_content(ans, request=request)
                if not ans.get("cancelled"):
                    for note in (brevity_note(summary), clarity_note(summary),
                                 handoff_note(summary),
                                 choices_note(summary, response)):
                        if note:
                            content.append({"type": "text", "text": note})
                structured = None
                if request.get("checklist") is None:
                    try:
                        structured = structured_response(ans, request)
                    except (ValueError, TypeError, KeyError):
                        return FeedbackError([{"type": "text", "text":
                            "[NOT DONE: malformed response contract.]"}])
                if request.get("checklist") is not None:
                    persisted = ans.get("receipt")
                    if (isinstance(persisted, dict)
                            and persisted.get("schema") == CHECKLIST_VERDICT_SCHEMA):
                        expected_steps = [row["id"] for row in
                                          request["checklist"]["steps"]]
                        verdict = verify_checklist_receipt(
                            persisted, expected_request_id=request["id"],
                            expected_request_digest=request["request_digest"],
                            expected_provider_instance=request["provider"]["instance"],
                            expected_provider_generation=request["provider"]["generation"],
                            expected_step_ids=expected_steps)
                        if verdict["valid"]:
                            structured = persisted
                        else:
                            sys.stderr.write("[human-copilot] persisted checklist "
                                             f"receipt rejected: {verdict['reason']}\n")
                    elif persisted is not None:
                        # A supplied-but-wrong receipt is not replaced with a
                        # fresh signature: doing so would bless tampered state.
                        sys.stderr.write("[human-copilot] persisted checklist "
                                         "receipt has the wrong schema\n")
                    else:
                        try:
                            structured = build_checklist_receipt(
                                request, ans, answered_at=time.time())
                        except ChecklistReceiptError as error:
                            # Backward compatibility is intentional: an old/text
                            # surface still returns its prose and images, but an
                            # authorization consumer sees no signed receipt.
                            sys.stderr.write("[human-copilot] checklist receipt "
                                             f"withheld: {error.code}\n")
                        except Exception as error:
                            # Key/storage trouble also fails closed without eating
                            # the operator's already-completed human-facing answer.
                            sys.stderr.write("[human-copilot] checklist receipt "
                                             f"unavailable: {error}\n")
                return FeedbackResult(content, structured)
            now_mono = time.monotonic()
            if now_mono >= next_surface_check:
                ensure_surface()
                next_surface_check = now_mono + SURFACE_RECHECK_S
            time.sleep(POLL_S)
        terminal_event = "expired"
        terminal_detail = "deadline elapsed"
        return [{"type": "text",
                 "text": f"No operator response within {timeout_s}s. Do not "
                         f"assume the steps passed — say the pass is "
                         f"unverified, record the blocker, and continue available "
                         f"autonomous work. Do not repeat the unanswered request "
                         f"or open another answering surface."}]
    finally:
        if terminal_event and not terminal_recorded:
            responder.Responder(spool).record_terminal(
                request, terminal_event, terminal_detail)
        for p in (() if preserve_answer else (req_path, ans_path)):
            try:
                os.unlink(p)
            except OSError:
                pass
        if not preserve_answer:
            clear_arrival_nudge(spool, req_id)

def target_error_content(err):
    """A request whose required target never resolved (hcop-453): an explicit
    machine-readable ERROR to the calling engine — never an empty pass, never
    a request moved to an unrelated screen corner and answered there. reason
    is 'no-match' or 'ambiguous'; the engine reviews and corrects the
    locator."""
    reason = err.get("reason") or "no-match"
    query = err.get("query") or ""
    if reason == "ambiguous":
        detail = (f"the locator '{query}' matched {err.get('matches')} "
                  f"windows — it must match exactly one")
    else:
        detail = f"no window matching '{query}'"
    return [{"type": "text",
             "text": (f"interactive_feedback target error ({reason}): {detail}. "
                      f"The request was never shown to the operator — nothing "
                      f"was asked and no answer exists. Review or correct the "
                      f"@{{Window · region}} locator (use a window substring "
                      f"that matches exactly one window) and resend.")}]

def verified_remote_response(ans, request=None):
    """Authenticate the exact remote payload before presenting its strings.

    The live MCP path supplies its own request; standalone consumers may also
    use the already signed request binding without asserting caller identity.
    """
    try:
        wrapper, receipt, commit = ans["remote_response"], ans["commit_receipt"], ans["responder_commit"]
        if (wrapper.get("schema") != responder.REMOTE_RESPONSE_SCHEMA
                or wrapper.get("schema_version") != 1
                or receipt.get("answer_format") != responder.REMOTE_EXACT_FORMAT):
            return None
        answer = wrapper["answer"]
        if (not isinstance(answer, dict) or responder._contains_capture_field(answer)
                or len(responder.canonical_json(answer)) > responder.MAX_COMMAND_BYTES):
            return None
        checked = responder.verify_commit_receipt(
            receipt, expected_request_id=request["id"] if request else receipt["request"]["id"],
            expected_request_revision=request["request_revision"] if request else commit["request_revision"],
            expected_submission_id=commit["submission_id"],
            expected_submission_digest=receipt["submission"]["digest"],
            expected_answer_digest=responder.answer_digest(answer))
        if not checked["valid"]:
            return None
        if receipt.get("completion", "complete") != answer.get("completion", "complete"):
            return None
        if request:
            expected = responder.Responder._validate_remote_answer(request, answer, exact=True)
            stored = {key: value for key, value in ans.items()
                      if key not in ("responder_commit", "receipt", "commit_receipt")}
            if stored != expected:
                return None
        return answer
    except (KeyError, TypeError, ValueError, AttributeError, responder.AdapterError):
        return None


def _remote_answer_content(ans, request=None):
    """Present the canonical exact remote fields without normalizing strings."""
    answer = verified_remote_response(ans, request)
    if answer is None:
        return [{"type": "text", "text": "[NOT DONE: invalid stored remote response.]"}]
    mode = answer.get("mode")
    out = []
    def block(value):
        out.append({"type": "text", "text": value})
    def field(label, value):
        # Even whitespace-only strings are explicit notes/text, not absence.
        if value != "":
            block(label)
            block(value)
    incomplete = answer.get("completion") == "not_done"
    if incomplete:
        descriptions = {"choice": "no option was selected", "text": "no text answer was provided",
                        "prose": "no text answer was provided", "ack": "the request was not acknowledged"}
        block("[NOT DONE: " + descriptions.get(mode, "the response was not completed") + ".]")
    if mode == "checklist":
        for index, row in enumerate(answer["steps"], 1):
            status = "NOT DONE" if row["verdict"] == "not_done" else row["verdict"].upper()
            block(f"{index}. {status} ({row['id']})")
            field(f"[Step {index} note]", row.get("note", ""))
    elif mode == "choice" and not incomplete:
        choice = ans["choice"]
        block(f'[selected: "{choice["label"]}" (value: "{choice["value"]}")]')
    elif mode in ("text", "prose"):
        field("[Text answer]", answer["text"])
    elif mode == "ack" and not incomplete:
        block("[Acknowledged]")
    field("[Global note]", answer.get("note", ""))
    return out


def answer_content(ans, request=None):
    """Answer file -> MCP content blocks. The TUI/overlay write
    {text, images: [{mime, b64}], cancelled}, plus `choice` for a
    mode:'choice' answer (hcop-0gj) and `error` for an unresolved target
    (handled by target_error_content). A choice is returned as its own
    unambiguous block: the exact value and the label the operator read."""
    if ans.get("cancelled"):
        return [{"type": "text",
                 "text": "The operator dismissed this request without answering."}]
    if "remote_response" in ans:
        return _remote_answer_content(ans, request)
    out = []
    text = (ans.get("text") or "").strip()
    completion = ans.get("response_completion")
    if isinstance(completion, dict) and completion.get("status") == "not_done":
        # An explicit remote incomplete response is neither an empty ack nor
        # a choice. Keep its status separate from the operator's note.
        descriptions = {"choice": "no option was selected",
                        "text": "no text answer was provided",
                        "prose": "no text answer was provided",
                        "ack": "the request was not acknowledged"}
        description = descriptions.get(completion.get("mode"), "the response was not completed")
        out.append({"type": "text", "text": f"[NOT DONE: {description}.]"})
        if text:
            out.append({"type": "text", "text": text})
        return out
    choice = ans.get("choice")
    if isinstance(choice, dict) and choice.get("value"):
        label = choice.get("label") or choice["value"]
        out.append({"type": "text",
                    "text": f'[selected: "{label}" (value: "{choice["value"]}")]'})
        if text:
            out.append({"type": "text", "text": text})
    else:
        out.append({"type": "text", "text": text or "(operator sent no text)"})
    for img in ans.get("images") or []:
        b64, mime = img.get("b64"), img.get("mime") or "image/png"
        if not b64:
            continue
        try:                                  # refuse to forward junk as an image
            base64.b64decode(b64, validate=True)
        except Exception:
            out.append({"type": "text",
                        "text": "(an attached image was unreadable and was dropped)"})
            continue
        out.append({"type": "image", "data": b64, "mimeType": mime})
    return out

# ----------------------------------------------------------------- JSON-RPC

_OUT_LOCK = threading.Lock()

def send(msg):
    with _OUT_LOCK:
        sys.stdout.write(json.dumps(msg, separators=(",", ":")) + "\n")
        sys.stdout.flush()

def send_result(rpc_id, result):
    send({"jsonrpc": "2.0", "id": rpc_id, "result": result})

def send_error(rpc_id, code, message):
    send({"jsonrpc": "2.0", "id": rpc_id,
          "error": {"code": code, "message": message}})

_CONTROL_WAKE = threading.Event()
_CONTROL_WATCH_LOCK = threading.Lock()
_CONTROL_WATCH_STARTED = False

def _watch_control(initial):
    """Publish MCP's list-changed notification on each effective flip."""
    previous = bool(initial)
    while True:
        _CONTROL_WAKE.wait(CONTROL_POLL_S)
        _CONTROL_WAKE.clear()
        # Observe only a committed transition.  toggle.py holds the exclusive
        # side while it prepares all client/doctrine surfaces, so this shared
        # read cannot leak its fail-closed migration sentinel as a second,
        # transient list change during the first ON after an upgrade.
        with capability_guard(idle_poll=True) as current:
            pass
        if current == previous:
            continue
        previous = current
        send({"jsonrpc": "2.0",
              "method": "notifications/tools/list_changed"})

def start_control_watcher(initial=None):
    global _CONTROL_WATCH_STARTED
    with _CONTROL_WATCH_LOCK:
        if _CONTROL_WATCH_STARTED:
            return
        _CONTROL_WATCH_STARTED = True
        threading.Thread(target=_watch_control,
                         args=(capability_enabled() if initial is None else initial,),
                         name="human-copilot-control", daemon=True).start()

def _control_signal(_signum, _frame):
    _CONTROL_WAKE.set()

def handle_tools_call(rpc_id, params):
    if not isinstance(params, dict):
        return send_error(rpc_id, -32602, "tool params must be an object")
    name = params.get("name")
    args = params.get("arguments", {})
    if not isinstance(args, dict):
        return send_error(rpc_id, -32602, "tool arguments must be an object")
    if name != "interactive_feedback":
        return send_error(rpc_id, -32602, f"unknown tool {name!r}")
    if not capability_enabled():
        return send_result(rpc_id, disabled_result())
    try:
        summary, timeout_s, response = feedback_arguments(
            args.get("summary"), args.get("timeout"), args)
    except PublishRefused as refusal:
        return send_result(rpc_id, {
            "content": [{"type": "text", "text": refusal.text}],
            "isError": True})
    try:
        content = run_feedback(summary, timeout_s, rpc_id, response)
        if isinstance(content, FeedbackError):
            result = {"content": content.content, "isError": True}
            if content.structured is not None:
                result["structuredContent"] = content.structured
            send_result(rpc_id, result)
            return
        result = {"content": content, "isError": False}
        structured = getattr(content, "structured", None)
        if structured is not None:
            result["structuredContent"] = structured
        send_result(rpc_id, result)
    except Exception as e:                       # spool unwritable, disk gone…
        sys.stderr.write(f"[human-copilot] tools/call failed: {e}\n")
        send_result(rpc_id, {
            "content": [{"type": "text",
                         "text": f"interactive_feedback failed on the host: {e}. "
                                 f"Record the delivery failure and continue available autonomous "
                                 f"work; do not open another answering surface."}],
            "isError": True})
    finally:
        with _CANCELLED_LOCK:
            _CANCELLED.discard(rpc_id)

def handle(msg):
    global _CLIENT_LABEL
    method = msg.get("method")
    rpc_id = msg.get("id")
    if method == "initialize":
        params = msg.get("params") or {}
        client_ver = params.get("protocolVersion")
        info = params.get("clientInfo") or {}
        raw_name = info.get("name", "") if isinstance(info, dict) else ""
        lowered = raw_name.lower() if isinstance(raw_name, str) else ""
        if "codex" in lowered:
            _CLIENT_LABEL = "Codex"
        elif "claude" in lowered:
            _CLIENT_LABEL = "Claude"
        else:
            _CLIENT_LABEL = "Your agent"
        initial_enabled = capability_enabled()
        send_result(rpc_id, {
            "protocolVersion": client_ver or PROTOCOL_FALLBACK,
            "capabilities": {"tools": {"listChanged": True}},
            "serverInfo": {"name": "human-copilot", "version": "2.0-tui"},
        })
        start_control_watcher(initial_enabled)
        return
    if method == "tools/list":
        if not capability_enabled():
            return send_result(rpc_id, {"tools": []})
        return send_result(rpc_id, {"tools": [{
            "name": "interactive_feedback",
            "description": TOOL_DESCRIPTION,
            "inputSchema": {
                "type": "object",
                "properties": {
                    "summary": {
                        "type": "string",
                        "description": "Describe the actual execution blocker and "
                                       "the human fact, action or authorization "
                                       "needed to continue implementation or operation. "
                                       "Do not proactively request QA, test checklists, "
                                       "final acceptance or visual sign-off. "
                                       "Screenshots and pixel capture are disabled by default; "
                                       "screenctx screen_crop/screen_diff may be used only inside "
                                       "an operator-open temporary allowance in Desktop Control. "
                                       "Never open, edit or request an allowance (including "
                                       "screenctx capture_policy.py writes), or solicit snips. "
                                       "Prefer window handles, geometry/sizes, control metadata, "
                                       "text, accessibility APIs and browser DOM; no computer-vision navigation. "
                                       "State concrete autonomous routes tried and "
                                       "why each failed or is unavailable, or why "
                                       "an operator-only fact, physical action or "
                                       "required approval needs the operator after "
                                       "checking evidence and existing authorization. "
                                       "Markdown for a human who has NOT read this "
                                       "conversation: 1-2 context lines (what changed, "
                                       "what their answer decides), then 1-5 numbered "
                                       "steps, one action each, self-contained. "
                                       "'1. Click Save @{Firefox · top-right} -> toast "
                                       "says Saved' (-> = what to see, highlighted; "
                                       "@{window · region} = framed on screen — the "
                                       "window substring must match exactly one "
                                       "window). Table checklists put the step number "
                                       "in the FIRST cell. A decision is sent with "
                                       "mode:'choice' + options, never as prose that "
                                       "can only be acknowledged."},
                    "mode": {
                        "type": "string",
                        "enum": ["checklist", "text", "choice", "ack"],
                        "description": "Response mode. 'checklist' (default): numbered "
                                       "steps, reported done/FAILED/NOT DONE. 'text': "
                                       "free prose. 'choice': the operator picks ONE "
                                       "option — requires `options`, and the exact "
                                       "value comes back. 'ack': confirmation only "
                                       "(the only mode where an empty send is an "
                                       "answer)."},
                    "options": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "value": {"type": "string",
                                          "description": "Stable machine value "
                                                         "returned to you; unique."},
                                "label": {"type": "string",
                                          "description": "What the operator reads."},
                                "hint": {"type": "string",
                                         "description": "Short cost/consequence of "
                                                        "this option."},
                            },
                            "required": ["value", "label"],
                        },
                        "description": "The choices for mode:'choice' (2-12): each a "
                                       "real selectable control, never an empty ack.",
                    },
                    "timeout": {
                        "type": "number",
                        "description": f"Seconds to wait for the operator "
                                       f"(default {DEFAULT_TIMEOUT_S}; use 1800-3600 "
                                       f"for a multi-step pass)."},
                },
                "required": ["summary"],
            },
        }]})
    if method == "ping":
        return send_result(rpc_id, {})
    if method == "notifications/cancelled":
        cancelled = (msg.get("params") or {}).get("requestId")
        if not valid_rpc_id(cancelled):
            return
        with _CANCELLED_LOCK:
            _CANCELLED.add(cancelled)
        return
    if method in ("notifications/initialized", "notifications/roots/list_changed"):
        return
    # Anything else with an id deserves an answer, or the client waits forever.
    if rpc_id is not None:
        send_error(rpc_id, -32601, f"method not found: {method}")

def valid_rpc_id(value):
    return isinstance(value, str) or type(value) is int

def valid_rpc_message(msg):
    """Reject malformed envelopes before dispatch or worker side effects."""
    if not isinstance(msg, dict):
        send_error(None, -32600, "request must be an object")
        return False
    has_id = "id" in msg
    rpc_id = msg.get("id")
    if has_id and not valid_rpc_id(rpc_id):
        send_error(None, -32600, "request id must be a string or integer")
        return False
    if msg.get("jsonrpc") != "2.0" or not isinstance(msg.get("method"), str):
        send_error(rpc_id, -32600, "request needs jsonrpc '2.0' and a method string")
        return False
    if "params" in msg and not isinstance(msg["params"], dict):
        if has_id:
            send_error(rpc_id, -32602, "params must be an object")
        return False
    # A request accidentally sent as a notification must not open a UI whose
    # reply has no owner. Only notifications are dispatched without an id.
    return has_id or msg["method"].startswith("notifications/")

def main():
    # SIGUSR1 wakes current binaries; its default action terminates a resident
    # pre-revocation binary, making upgrade-time OFF fail closed too.
    signal.signal(toggle.CONTROL_SIGNAL, _control_signal)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, RecursionError):
            send_error(None, -32700, "invalid JSON")
            continue
        if not valid_rpc_message(msg):
            continue
        if msg.get("method") == "tools/call":
            threading.Thread(target=handle_tools_call,
                             args=(msg.get("id"), msg.get("params")),
                             daemon=True).start()
        else:
            handle(msg)
    # stdin closed: the client is gone; in-flight waits die with the process.

if __name__ == "__main__":
    main()
