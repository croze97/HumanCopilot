#!/usr/bin/env python3
"""copilot-tui — the operator's side of the human-copilot MCP lane.

A Claude or Codex `interactive_feedback` call becomes a request in the spool
(server.py, same directory, owns the other half); this panel renders the
checklist, collects a typed answer, and hands back screenshots. Run it in any
terminal (`copilot-tui`, or via `tui` → copilot) and leave it up: it idles on
the spool and rings the bell when a request lands.

Clipboard is the whole screenshot story, and it is SEAMLESS by design: while a
request is open, a `wl-paste --watch` sidecar reports every clipboard change;
a NEW image (one that appeared after the request opened) is attached
automatically and a small overlay says so — "📸 screenshot on clipboard —
attached". PrintScreen is the entire gesture. An image that was already on the
clipboard when the request opened is only hinted at (^V attaches it): auto-
attaching stale clipboard contents would ship whatever the operator last
copied, which is exactly the kind of quiet wrong-thing this estate documents.
^X removes the last attachment. Bytes are held in RAM and written only into
the answer file on the tmpfs spool.

Keys in a request: type freely (multi-line) · ^D send · ^T insert a per-step
report template (every step pre-filled "pass" — change the ones that did not)
· ^Y copy the request text · Esc dismiss · ^V attach clipboard image · ^X remove last attachment ·
PgUp/PgDn or wheel scroll the checklist · ^B back to the list (the draft is
kept). Idle screen: T toggles the server registration (same flip as
zellij-hub's button), R refresh, q quit.

Several requests may be open at once (2026-09-03, hcop-0p7.3): the panel then
shows a LIST — every open request it serves, grouped by instance (a global
panel: all instances; `--instance` panel: its own plus unstamped ones), each
with its palette number, ↑↓/j k move, Enter opens one, answering or backing
out returns to the list. Exactly one open request still takes the screen by
itself, as it always did, unless the operator backed out of it with ^B.

Every scan also races server.py for a private per-request arrival-nudge marker.
When no fresh healthy desktop overlay is rendering, exactly one server/global
panel/scoped panel emits a critical desktop notification. This surface-side
bridge matters for requests written by MCP server processes loaded before a
delivery fix; the terminal bell remains an additional local cue, not the only
one.

A step's "→ what to see" is drawn highlighted on its own line under the step
(👁), and an "@{Window · region}" locator as a ⌖ line — the TUI cannot draw
on the screen, so it names the place; the GNOME overlay draws the frame.
Both annotations are parsed by parse_look(), the Python twin of
lib.js::parseLook — keep the two grammars identical.

The checklist is rendered by md_lines(): headings, bullet/ordered/task lists,
GFM pipe tables (drawn as boxed columns, degraded to "header: value" records
when the terminal is too narrow), quotes, rules, fenced code and a flattening
inline pass. It never DROPS a line it doesn't understand — an unrenderable
line still has to be readable, because it may be the one that says what to do.

Like every house TUI: stdlib only, built on tuikit (vendored copy — canonical
lives in zellij-hub/). Logic that tests drive (spool scan, answer write,
markdown-lite, clipboard grab/decide) is module-level and curses-free.
"""
import base64
import curses
import fcntl
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import textwrap
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
from server import (spool_dir, instance_id, instance_dir,   # ONE spool resolution, not two
                    looks_like_choices, arrival_nudge,
                    prune_arrival_nudges, capability_enabled,
                    record_tui_runtime, answered_request_ids,
                    reclaim_answered_orphans) # ONE delivery/control protocol
import palette
import responder as responder_adapter
import toggle
from tuikit import (BUTTON5, C, Command, ListView, Regions, ScrollView, TICK_MS,
                    TuiApp, bfmt, confirm, dur, help_modal, homely, keybar,
                    norm_key, put, read_key, spinner, trunc)

OVERLAY_S = 5.0
TUI_RUNTIME_REFRESH_S = 3.0

# How long an instance-scoped sidepanel sits with nothing open before it
# closes itself and gives the pane's rows back to the session (2026-09-04,
# hcop-emd: one was parked over the operator's terminal having answered
# nothing, and one accumulates per zellij session). 0 or less disables it.
# A GLOBAL copilot-tui is never closed this way: it is a window the operator
# opened and it is the surface for every instance.
try:
    PANE_IDLE_S = float(os.environ.get("HC_PANE_IDLE_S") or 60)
except ValueError:
    PANE_IDLE_S = 60.0

# ----------------------------------------------------------------- spool side

def scan_requests(spool):
    """(requests sorted oldest-first, unreadable-count). Unreadable files are
    COUNTED, not skipped silently — a corrupt request that vanished from the
    panel while an agent blocks on it would be undiagnosable from here.

    A complete answer makes its request non-actionable immediately, even if
    the caller dies before performing its normal two-file cleanup.  This
    prevents a fresh panel from resurrecting a question the operator already
    answered or overwriting that answer from a second surface.
    """
    reqs, bad = [], 0
    answered = answered_request_ids(spool)
    rdir = os.path.join(spool, "requests")
    try:
        names = os.listdir(rdir)
    except OSError:
        return [], 0
    for n in names:
        # Arrival markers and any future sidecars live beside requests but
        # are not requests; only complete JSON files enter the panel.
        if not n.endswith(".json") or n.endswith(".tmp") or ".tmp-" in n:
            continue
        try:
            with open(os.path.join(rdir, n)) as f:
                r = json.load(f)
            if not r.get("id") or not isinstance(r.get("summary"), str):
                raise ValueError("missing id/summary")
            if r["id"] in answered:
                continue
            if request_expired(r):
                # A dead caller may have missed its own finally block. Retire
                # the request through the same transaction used by the
                # overlay, so every answering surface converges on one
                # expired tombstone instead of merely hiding a stale row.
                try:
                    responder_adapter.Responder(spool).expire(
                        r["id"], r.get("request_revision"))
                except Exception:
                    pass
                continue
            # A current mobile lease owns response rights. The advisory
            # filter hides it promptly; submit_local() repeats the decision
            # under the transaction lock, so a stale screen still loses safe.
            contract = r.get("responder")
            if (isinstance(contract, dict)
                    and contract.get("schema") == responder_adapter.CONTRACT_SCHEMA
                    and not responder_adapter.Responder(spool).local_available(r)):
                continue
            reqs.append(r)
        except (OSError, ValueError):
            bad += 1
    def fifo_key(row):
        created = row.get("created")
        if not isinstance(created, (int, float)) or isinstance(created, bool):
            created = float("inf")
        return float(created), str(row.get("id", ""))
    reqs.sort(key=fifo_key)
    return reqs, bad


def request_expired(request, now=None):
    """True only for a request carrying a conclusively elapsed deadline."""
    deadline = request.get("deadline") if isinstance(request, dict) else None
    if not isinstance(deadline, (int, float)) or isinstance(deadline, bool):
        return False
    now = time.time() if now is None else now
    try:
        return float(deadline) <= float(now)
    except (TypeError, ValueError, OverflowError):
        return False


def prune_drafts(spool, drafts, visible_ids):
    """Drop drafts only after real completion/withdrawal, not lease hiding."""
    visible = set(visible_ids)
    for request_id in list(drafts):
        if request_id in visible:
            continue
        request_path = os.path.join(spool, "requests", request_id + ".json")
        answer_path = os.path.join(spool, "answers", request_id + ".json")
        if os.path.exists(request_path) and not os.path.exists(answer_path):
            continue
        del drafts[request_id]
    return drafts

def hold_presence(spool, instance=None):
    """Advertise 'a copilot-tui is watching this spool' by holding a SHARED
    flock on tui.lock for the process's whole life — several panels may hold
    it at once. The server requires both this lease and a fresh paint heartbeat
    before suppressing a fallback: a lock cannot outlive its process, while
    the heartbeat proves that process is really a TUI rather than a lock-only
    helper. Returns the fd, which the caller must keep referenced. With no
    ``instance`` the GLOBAL lock
    (spool/tui.lock) is held: this panel serves every instance. With one,
    only spool/instances/<id>/tui.lock is held: the server keeps auto-opening
    for every other instance (hcop-0p7.7)."""
    where = instance_dir(spool, instance) if instance else spool
    fd = os.open(os.path.join(where, "tui.lock"), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_SH)
    try:
        record_tui_runtime(spool, instance)
    except OSError as e:
        # The panel can still render. Without proof the server may open one
        # redundant fallback, which is safer than hiding a request.
        sys.stderr.write(f"copilot-tui: could not record render heartbeat: {e}\n")
    return fd

def write_answer(spool, req_id, text, images, cancelled=False, choice=None,
                 error=None, checklist=None, response_completion=None):
    """Commit through the one lease-aware, immutable answer transaction.

    Returns the responder result. A caller must claim success only when
    ``result['ok']`` is true; conflicts retain the draft for retry/reload.
    """
    body = {"text": text, "cancelled": cancelled,
            "images": [{"mime": i["mime"], "b64": i["b64"]} for i in images]}
    if choice is not None:
        body["choice"] = choice
    if error is not None:
        body["error"] = error
    if checklist is not None:
        body["checklist"] = checklist
    if response_completion is not None:
        body["response_completion"] = response_completion
    return responder_adapter.Responder(spool).submit_local(req_id, body)


def commit_problem(result):
    """Short operator-facing text for a failed responder transaction."""
    event = ((result or {}).get("event") or {}).get("type")
    if event == "conflict":
        return "another responder owns this request — your draft was not sent"
    if event == "already-answered":
        return "another responder already answered — your draft was not overwritten"
    if event in ("expired", "cancelled", "missing", "stale"):
        return f"request {event.replace('-', ' ')} — your draft was not sent"
    detail = ((result or {}).get("event") or {}).get("detail")
    return "answer could not be committed" + (f": {detail}" if detail else "")

# ---- a closed sidepanel declines what it was showing (hcop-0p7.4). An
# instance-scoped panel IS its session's surface; when the operator closes the
# pane (zellij hangs the pty up -> SIGHUP), quits it, or it dies, an agent
# blocked on a request nobody will now see must be told — with the same answer
# JSON the Esc path writes, so the server needs no new case. Only requests
# STAMPED for this instance are declined: an unstamped request belongs to
# every surface and the global overlay still shows it.

def decline_targets(reqs, instance, answered_ids=()):
    """The ids a closing instance-scoped panel declines: open requests whose
    stamp keys to ``instance`` and that have no answer yet. Nothing for a
    global panel (its requests wait in the spool, as they always did)."""
    if not instance:
        return []
    return [r["id"] for r in reqs
            if instance_id(r.get("instance")) == instance
            and r["id"] not in set(answered_ids)]

# ---- the response contract, surface-side (hcop-0gj). server.py validates the
# call's mode/options at the boundary; the surfaces render what survived. A
# legacy request (no `response` key, or mode null) keeps today's behaviour
# exactly: numbered steps -> checklist, anything else -> prose. "prose" is
# NOT a mode an engine can send — it is the derived shape of a legacy summary
# with no steps, and the only place the choice-like guard can fire.

def response_of(req):
    """request -> {"mode": "checklist"|"text"|"choice"|"ack"|"prose",
    "options": [...]}. Options ride along only for mode 'choice'."""
    stamped = req.get("responder")
    if (isinstance(stamped, dict)
            and stamped.get("schema") == responder_adapter.CONTRACT_SCHEMA
            and stamped.get("schema_version") == 1):
        resp = stamped
    else:
        resp = req.get("response")
    resp = resp if isinstance(resp, dict) else {}
    mode = resp.get("mode")
    options = [o for o in (resp.get("options") or []) if isinstance(o, dict)]
    if mode not in ("checklist", "text", "choice", "ack", "prose"):
        mode = "checklist" if step_texts(req.get("summary", "")) else "prose"
        options = []
    return {"mode": mode, "options": options}

def empty_send(mode, text, images, choice_like=False):
    """Verdict for a ^D with nothing typed and nothing attached: 'send' — it
    goes out as it is; anything else is the refusal message shown instead.
    An EMPTY acknowledgement is an answer ONLY when the request explicitly
    declared itself confirmation-only (hcop-0gj): a checklist, prose and a
    choice all refuse — an omitted report must never read as a pass, and
    choice-like prose must never silently become an ack."""
    if (text or "").strip() or images:
        return "send"
    if mode == "ack":
        return "send"
    if mode == "choice":
        return "choose an option first — ↑↓ or 1-9, then Enter sends it"
    if mode == "text":
        return "type your answer first — ^D sends the text you typed"
    if mode == "prose" and choice_like:
        return ("this reads as a decision — answer in your own words; the "
                "engine is asked to resend real options")
    return "an empty send is not an answer — type what you saw, attach a " \
           "snip, or Esc declines"

def decline_open(spool, instance):
    """Write a declined answer for every decline_targets() request of this
    instance. Returns the ids written. Failures are collected, not raised —
    this runs on the way out, after SIGHUP, and must never wedge the exit."""
    reqs, _bad = scan_requests(spool)
    adir = os.path.join(spool, "answers")
    try:
        answered = {n[:-5] for n in os.listdir(adir) if n.endswith(".json")}
    except OSError:
        answered = set()
    done = []
    for rid in decline_targets(reqs, instance, answered):
        try:
            result = write_answer(spool, rid, "", [], cancelled=True)
            if result.get("ok"):
                done.append(rid)
            else:
                sys.stderr.write(f"copilot-tui: decline lost for {rid}: "
                                 f"{commit_problem(result)}\n")
        except OSError as e:
            sys.stderr.write(f"copilot-tui: could not decline {rid}: {e}\n")
    return done

# ----------------------------------------------------------------- markdown-lite

# Inline grammar. Deliberately conservative about `_`: snake_case and dunder
# names (`__init__`, `MALLOC_ARENA_MAX`) are all over this estate's checklists
# and CommonMark would eat them as emphasis, so underscores are never touched.
# `*` emphasis is stripped only when the run holds no star and no edge space,
# which is what keeps a glob like `rm *.log *.tmp` intact.
_MD_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_MD_AUTOLINK = re.compile(r"<((?:https?|ftp|mailto):[^>\s]+)>")
_MD_CODE = re.compile(r"`+([^`]+)`+")
_MD_STRIKE = re.compile(r"~~([^~]+)~~")
_MD_BOLD = re.compile(r"\*\*([^*]+)\*\*")
_MD_EM = re.compile(r"(?<![\w*])\*([^*\s](?:[^*]*[^*\s])?)\*(?![\w*])")
_MD_ESC = re.compile(r"\\([\\`*_{}\[\]()#+.!|~<>-])")
_MD_HIDDEN = re.compile("[\ue000-\ue07f]")

# A backslash-escaped marker is parked in the private-use plane for the length
# of the pass: leaving the backslash in place is not enough — `\*literal\*`
# would still read as an emphasis pair and lose its stars.
def _md_hide(m):
    return chr(0xE000 + ord(m.group(1)))

def md_inline(s):
    """Inline markdown -> plain text: emphasis/code/strike markers dropped,
    links flattened to "text (url)", backslash escapes resolved."""
    s = _MD_ESC.sub(_md_hide, s or "")
    def link(m):
        bang, text, url = m.group(1), m.group(2).strip(), m.group(3)
        if bang:                       # an image can only be named, not shown
            return text or url
        return url if (not text or text == url) else f"{text} ({url})"
    s = _MD_LINK.sub(link, s)
    s = _MD_AUTOLINK.sub(r"\1", s)
    s = _MD_CODE.sub(r"\1", s)
    s = _MD_STRIKE.sub(r"\1", s)
    s = _MD_BOLD.sub(r"\1", s)
    s = _MD_EM.sub(r"\1", s)
    return _MD_HIDDEN.sub(lambda m: chr(ord(m.group()) - 0xE000), s)

# ---- pipe tables (GFM). A table is a pipe row whose NEXT line is a delimiter
# row; that two-line handshake is what keeps a lone "---" a horizontal rule and
# a prose line with a stray "|" a paragraph.

_MD_DELIM_CELL = re.compile(r":?-+:?$")
TABLE_MIN_COL = 8                      # a column squeezed below this breaks
                                       # words mid-token; the record form below
                                       # says the same thing and stays readable

def md_cells(row):
    r"""A pipe-table row -> its cells. Outer pipes are optional, \| is a
    literal pipe inside a cell."""
    s = row.strip()
    if s.startswith("|"):
        s = s[1:]
    if s.endswith("|") and not s.endswith("\\|"):
        s = s[:-1]
    out, cur, i = [], "", 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s) and s[i + 1] == "|":
            cur += "|"
            i += 2
        elif c == "|":
            out.append(cur.strip())
            cur = ""
            i += 1
        else:
            cur += c
            i += 1
    out.append(cur.strip())
    return out

def md_is_delim(row):
    """True for a table's ---|:--:|--- line (pipes required, so a bare '---'
    stays a horizontal rule)."""
    if "|" not in row:
        return False
    cells = md_cells(row)
    return bool(cells) and all(c and _MD_DELIM_CELL.match(c) for c in cells)

def _md_aligns(delim_cells):
    out = []
    for c in delim_cells:
        left, right = c.startswith(":"), c.endswith(":")
        out.append("c" if left and right else "r" if right else "l")
    return out

def _md_pad(text, w, align):
    pad = w - len(text)
    if pad <= 0:
        return text
    if align == "r":
        return " " * pad + text
    if align == "c":
        return " " * (pad // 2) + text + " " * (pad - pad // 2)
    return text + " " * pad

def md_table(rows, aligns, width):
    """Rows of cells -> boxed, column-aligned [(line, attr)]. Columns shrink
    (widest first, never below TABLE_MIN_COL) to fit `width` and cells wrap
    inside them; when that still doesn't fit, the table degrades to one
    "header: value" record per row, which stays readable at any width."""
    ncols = max(len(r) for r in rows)
    rows = [[md_inline(c) for c in r] + [""] * (ncols - len(r)) for r in rows]
    aligns = (list(aligns) + ["l"] * ncols)[:ncols]
    nat = [max(1, max(len(r[i]) for r in rows)) for i in range(ncols)]
    budget = width - (3 * ncols + 1)           # "| " + cell + " " per column
    w = list(nat)
    while sum(w) > budget:
        i = max(range(ncols), key=lambda k: w[k])
        if w[i] <= TABLE_MIN_COL:
            break
        w[i] -= 1
    if sum(w) > budget:
        return _md_records(rows, width)
    def rule(l, m, r):
        return (l + m.join("─" * (c + 2) for c in w) + r, curses.A_DIM)
    out = [rule("┌", "┬", "┐")]
    for n, row in enumerate(rows):
        cellw = [textwrap.wrap(row[i], w[i]) or [""] for i in range(ncols)]
        for ln in range(max(len(c) for c in cellw)):
            body = " │ ".join(
                _md_pad(cellw[i][ln] if ln < len(cellw[i]) else "",
                        w[i], aligns[i]) for i in range(ncols))
            out.append(("│ " + body + " │",
                        curses.A_BOLD if n == 0 else 0))
        if n == 0:
            out.append(rule("├", "┼", "┤"))
    out.append(rule("└", "┴", "┘"))
    return out

def _md_records(rows, width):
    """Too narrow for columns: one "header: value" block per body row. The
    table's information survives even in a 30-column terminal."""
    head = rows[0]
    out = []
    for row in rows[1:]:
        for i, cell in enumerate(row):
            key = head[i] if i < len(head) and head[i] else f"col {i + 1}"
            for k, ln in enumerate(textwrap.wrap(f"{key}: {cell}", max(8, width),
                                                 subsequent_indent="  ") or [""]):
                out.append((ln, curses.A_BOLD if k == 0 and i == 0 else 0))
        out.append(("", 0))
    return out[:-1] if out else [(" | ".join(head), curses.A_BOLD)]

# ---- pointing the eyes. Two inline step annotations (the overlay's lib.js
# has the same grammar — keep them identical):
#   "1. Click Save @{Firefox · top-right} → toast says Saved"
# "→ what to see" (also -> => ⇒) is the OBSERVATION, drawn highlighted under
# the step; "@{Window · region}" is a spotlight LOCATOR (window title/class
# substring, region top-left…center…bottom-right/full or x,y,w,h fractions)
# that the overlay frames on screen and this TUI can only name.

_LOOK_ARROW = re.compile(r"\s(?:→|->|=>|⇒)\s+(.+)$")
_SPOT_TAG = re.compile(r"@\{([^}]*)\}")
_SPOT_RECT = re.compile(r"(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?")
REGIONS = ("full", "top-left", "top", "top-right", "left", "center", "right",
           "bottom-left", "bottom", "bottom-right")

def region_of(word):
    w = re.sub(r"[\s_]+", "-", (word or "").lower().strip())
    w = re.sub(r"^upper-", "top-", w)
    w = re.sub(r"^lower-", "bottom-", w)
    w = re.sub(r"^(centre|middle)$", "center", w)
    w = re.sub(r"^(whole|all|window|everything|entire)$", "full", w)
    return w if w in REGIONS else None

def parse_spot(inner):
    """Inside of an @{…} tag -> {'window','region','rect'} or None."""
    s = (inner or "").strip()
    if not s:
        return None
    rect = None
    m = _SPOT_RECT.search(s)
    if m:
        rect = [float(v) for v in m.groups()]
        s = s[:m.start()] + " " + s[m.end():]
        if any(v > 1 for v in rect):
            rect = [v / 100 for v in rect]
        if rect[2] <= 0 or rect[3] <= 0:
            rect = None
    region, words = None, []
    for part in re.split(r"\s*[·|;,]\s*", s):
        toks = part.split()
        i = 0
        while i < len(toks):
            two = region_of(f"{toks[i]}-{toks[i + 1]}") if i + 1 < len(toks) else None
            one = region_of(toks[i])
            if not region and two:
                region = two; i += 2; continue
            if not region and one:
                region = one
            else:
                words.append(toks[i])
            i += 1
    win = " ".join(words).strip()
    if not win and not region and not rect:
        return None
    return {"window": win, "region": region or (None if rect else "full"),
            "rect": rect}

def parse_look(text):
    """Step text -> (action, see, spot): both annotations lifted out."""
    s, spot = text or "", None
    m = _SPOT_TAG.search(s)
    if m:
        spot = parse_spot(m.group(1))
        s = s[:m.start()] + " " + s[m.end():]
    see = ""
    m = _LOOK_ARROW.search(s)
    if m:
        see, s = m.group(1).strip(), s[:m.start()]
    return re.sub(r"\s{2,}", " ", s).strip(), see, spot

def spot_label(spot):
    if not spot:
        return ""
    parts = []
    if spot["window"]:
        parts.append(spot["window"])
    if spot["rect"]:
        parts.append(",".join(f"{round(v * 100)}%" for v in spot["rect"]))
    elif spot["region"] and (spot["region"] != "full" or not spot["window"]):
        parts.append("whole screen" if spot["region"] == "full" else spot["region"])
    return " · ".join(parts)

def _look_lines(indent, see, spot, width):
    """The 👁 / ⌖ lines drawn under a step (or after a table)."""
    out = []
    if see:
        w = textwrap.wrap("👁 " + see, max(4, width - indent),
                          subsequent_indent="  ") or [""]
        out.extend((" " * indent + ln, C.ATT | curses.A_BOLD) for ln in w)
    if spot:
        out.append((" " * indent + "⌖ " + spot_label(spot)
                    + "  (framed on screen by the overlay)", C.ACC))
    return out

_STEP_LINE = re.compile(r"^\s*(\d{1,3})[.)]\s+(.*\S)")
_STEP_CELL = re.compile(r"^\d{1,3}[.)]?$")

def step_texts(summary):
    """The numbered steps of a summary, as the overlay counts them: top-level
    numbered lines outside fences, and table rows whose first cell is the
    number. Used for the ^T report template."""
    out, fence = [], False
    src = (summary or "").splitlines()
    i = 0
    while i < len(src):
        s = src[i].strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence; i += 1; continue
        if fence:
            i += 1; continue
        if "|" in src[i] and i + 1 < len(src) and md_is_delim(src[i + 1]):
            j = i + 2
            while j < len(src) and "|" in src[j] and src[j].strip() \
                    and not md_is_delim(src[j]):
                cells = md_cells(src[j])
                if cells and _STEP_CELL.match(cells[0]):
                    out.append(" · ".join(parse_look(c)[0] for c in cells[1:] if c))
                j += 1
            i = j; continue
        m = _STEP_LINE.match(src[i])
        if m and not (src[i][0] in " \t" and out):
            out.append(parse_look(md_inline(m.group(2)))[0])
        i += 1
    return out

def report_template(summary):
    """"1. pass\n2. pass\n…" — the operator edits the ones that did not."""
    steps = step_texts(summary)
    if not steps:
        return ""
    return "\n".join(f"{i + 1}. pass" for i in range(len(steps))) + "\n"

_REPORT_VERDICT = re.compile(
    r"^\s*(\d{1,3})[.)]\s+(not(?:[ -])done|fail(?:ed)?|pass(?:ed)?|done)\b"
    r"(?:\s*(?:[-:—]\s*)?(.*))?$", re.I)

def checklist_answer(req, report):
    """Strict TUI report -> the surface half of the v1 receipt contract.

    The existing free-form editor remains free-form. Only an entirely
    parseable numbered verdict report becomes authorization evidence; arbitrary
    prose still returns to the agent but the server correctly withholds a
    receipt. ``^T``'s ``N. pass`` template is deliberately accepted.
    """
    contract = req.get("checklist") if isinstance(req, dict) else None
    specs = contract.get("steps") if isinstance(contract, dict) else None
    if (not isinstance(contract, dict)
            or contract.get("schema") != "human-copilot.checklist-request/v1"
            or contract.get("schema_version") != 1
            or not isinstance(specs, list) or not specs
            or not isinstance(req.get("id"), str) or not req.get("id")
            or not isinstance(req.get("request_digest"), str)
            or not req.get("request_digest")
            or not isinstance(report, str)):
        return None
    rows = []
    for raw in report.splitlines():
        if not raw.strip():
            continue
        match = _REPORT_VERDICT.match(raw)
        if match is None:
            # An indented continuation is a note on the preceding verdict;
            # an unnumbered top-level line makes the whole report prose-only.
            if raw[:1].isspace() and rows:
                note = raw.strip()
                if note.lower().startswith("note:"):
                    note = note[5:].strip()
                rows[-1]["note"] = (rows[-1]["note"] + " " + note).strip()
                continue
            return None
        number = int(match.group(1))
        token = match.group(2).lower().replace("-", " ")
        verdict = ("not-done" if token == "not done" else
                   "failed" if token.startswith("fail") else "done")
        if 1 <= number <= len(specs) and isinstance(specs[number - 1], dict):
            step_id = specs[number - 1].get("id")
        else:
            # Preserve bad cardinality for the server's typed unknown-step
            # rejection; never silently remap it to a real step.
            step_id = f"step-{number:03d}"
        rows.append({"id": step_id, "verdict": verdict,
                     "note": (match.group(3) or "").strip()})
    if not rows or any(not isinstance(row.get("id"), str) or not row["id"]
                       for row in rows):
        return None
    return {
        "schema": "human-copilot.checklist-answer/v1",
        "schema_version": 1,
        "request_id": req["id"],
        "request_digest": req["request_digest"],
        "steps": rows,
    }

# ---- block rendering

_MD_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_MD_BULLET = re.compile(r"^([-*+])\s+(.*)$")
_MD_NUM = re.compile(r"^(\d{1,3}[.)])\s+(.*)$")
_MD_TASK = re.compile(r"^\[([ xX])\]\s+(.*)$")

def md_lines(text, width):
    """Checklist markdown -> [(line, attr)] for the ScrollView.

    Handles headings, bullet/ordered/task lists with hanging indents, GFM pipe
    tables, block quotes, horizontal rules, fenced code (verbatim, dim) and the
    inline grammar in md_inline(). Anything it doesn't recognise is rendered as
    wrapped prose rather than dropped — an unrenderable line must still be
    readable, because it may be the one that tells the operator what to do."""
    out, fence = [], False
    width = max(8, width)
    src = (text or "").splitlines()
    i = 0
    while i < len(src):
        raw = src[i].rstrip()
        s = raw.strip()
        if s.startswith("```") or s.startswith("~~~"):
            fence = not fence
            i += 1
            continue
        if fence:
            out.append((raw, curses.A_DIM))
            i += 1
            continue
        if not s:
            out.append(("", 0))
            i += 1
            continue
        if "|" in raw and i + 1 < len(src) and md_is_delim(src[i + 1]):
            rows = [md_cells(raw)]
            aligns = _md_aligns(md_cells(src[i + 1]))
            looks = []                 # (step label, see, spot) per body row
            j = i + 2
            while j < len(src) and "|" in src[j] and src[j].strip() \
                    and not md_is_delim(src[j]):
                cells, see, spot = [], "", None
                for c in md_cells(src[j]):
                    t, s2, sp = parse_look(c)
                    cells.append(t)
                    see, spot = see or s2, spot or sp
                if see or spot:
                    looks.append((cells[0] if cells else "", see, spot))
                rows.append(cells)
                j += 1
            out.extend(md_table(rows, aligns, width))
            for label, see, spot in looks:
                # the annotations of a table step, named after the row
                out.extend(_look_lines(2, (f"step {label}: " if label else "") + see
                                       if see else "", spot, width))
            i = j
            continue
        if _MD_RULE.match(raw):
            out.append(("─" * width, curses.A_DIM))
            i += 1
            continue
        i += 1
        if s.startswith("#"):
            out.append((md_inline(s.lstrip("#").strip()),
                        C.ACC | curses.A_BOLD))
            continue
        quote = 0
        while s.startswith(">"):
            quote += 1
            s = s[1:].lstrip()
        indent = 0 if quote else len(raw) - len(raw.lstrip())
        attr = curses.A_DIM if quote else 0
        marker = ""
        m = _MD_BULLET.match(s) or _MD_NUM.match(s)
        if m:
            marker, s = ("•" if m.group(1) in "-*+" else m.group(1)), m.group(2)
            t = _MD_TASK.match(s)
            if t:                       # "- [x] thing" -> a real check box
                marker, s = ("☑" if t.group(1) in "xX" else "☐"), t.group(2)
            attr = attr or curses.A_BOLD
        prefix = "▏ " * quote + " " * indent + (marker + " " if marker else "")
        body = md_inline(s)
        see, spot = "", None
        if m and marker not in ("•", "☑", "☐"):
            # a numbered step: lift "→ see" and "@{…}" out of the action so
            # they get their own highlighted lines under it
            body, see, spot = parse_look(body)
        wrapped = textwrap.wrap(body, max(4, width - len(prefix))) or [""]
        out.append((prefix + wrapped[0], attr))
        cont = "▏ " * quote + " " * (indent + len(marker) + (1 if marker else 0))
        out.extend((cont + w, curses.A_DIM if quote else 0)
                   for w in wrapped[1:])
        out.extend(_look_lines(len(cont), see, spot, width))
    return out

# ----------------------------------------------------------------- clipboard

def _run(argv, timeout=6):
    return subprocess.run(argv, capture_output=True, timeout=timeout)

def clip_types(run=_run):
    try:
        r = run(["wl-paste", "--list-types"])
        if r.returncode == 0:
            return [t for t in r.stdout.decode("utf-8", "replace").split() if t]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return []

def clip_image(run=_run):
    """(bytes, mime, fingerprint) of the clipboard image, or None. image/png
    preferred; otherwise the first image/* flavour, forwarded as-is."""
    types = clip_types(run)
    imgs = [t for t in types if t.startswith("image/")]
    if not imgs:
        return None
    mime = "image/png" if "image/png" in imgs else imgs[0]
    try:
        r = run(["wl-paste", "--type", mime], timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    return r.stdout, mime, hashlib.sha1(r.stdout).hexdigest()

def copy_text(text, run=subprocess.run):
    """Place a request's complete text on the Wayland clipboard as UTF-8.

    Feed stdin directly: request text is neither a shell command nor an argv
    value, and a failed wl-copy leaves the modal open for another attempt.
    """
    if not text:
        return False
    try:
        result = run(["wl-copy", "--type", "text/plain;charset=utf-8"],
                     input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, timeout=6)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0

class ClipWatch:
    """Event-driven clipboard-change signal: `wl-paste --watch echo x` prints a
    line per clipboard SET, which is the only way two consecutive screenshots
    (both just [image/png]) are distinguishable without hashing the payload
    every poll. Falls back to reporting unsupported, in which case the modal
    polls fingerprints on a slow tick instead."""
    def __init__(self):
        self.proc = None
        self.changed = False       # read-and-clear by the UI loop
        self.ok = False

    def start(self):
        try:
            self.proc = subprocess.Popen(
                ["wl-paste", "--watch", "echo", "x"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            return self
        self.ok = True
        import threading
        def pump():
            for _ in self.proc.stdout:
                self.changed = True
            self.ok = False        # watcher died — the modal shows it
        threading.Thread(target=pump, daemon=True).start()
        return self

    def take(self):
        c, self.changed = self.changed, False
        return c

    def stop(self):
        if self.proc:
            try:
                self.proc.terminate()
            except OSError:
                pass

def decide_attach(fp, baseline_fp, attached_fps):
    """'attach' | 'hint' | 'ignore' for an image now on the clipboard.
    New-since-request → attach. The image that was already there when the
    request opened → hint only (stale clipboard must not auto-ship). Already
    attached → ignore."""
    if fp in attached_fps:
        return "ignore"
    if fp == baseline_fp:
        return "hint"
    return "attach"

# ----------------------------------------------------------------- editor

class MultiLine:
    """Multi-line answer editor: lines + cursor, fed read_key() events.
    Enter is a newline here — sending is ^D, so a pasted checklist reply
    never fires early."""
    def __init__(self):
        self.lines = [""]
        self.cy = self.cx = 0

    def text(self):
        return "\n".join(self.lines)

    def key(self, kind, val):
        ln = self.lines
        cy, cx = self.cy, self.cx
        if kind == "ch":
            if val in ("\n", "\r"):
                ln[cy:cy + 1] = [ln[cy][:cx], ln[cy][cx:]]
                self.cy, self.cx = cy + 1, 0
            elif val in ("\x7f", "\x08"):
                if cx:
                    ln[cy] = ln[cy][:cx - 1] + ln[cy][cx:]; self.cx = cx - 1
                elif cy:
                    self.cx = len(ln[cy - 1])
                    ln[cy - 1] += ln.pop(cy); self.cy = cy - 1
            elif val == "\x01": self.cx = 0
            elif val == "\x05": self.cx = len(ln[cy])
            elif val == "\x0b": ln[cy] = ln[cy][:cx]
            elif val == "\x15": ln[cy] = ln[cy][cx:]; self.cx = 0
            elif val == "\t":
                ln[cy] = ln[cy][:cx] + "  " + ln[cy][cx:]; self.cx = cx + 2
            elif val.isprintable():
                ln[cy] = ln[cy][:cx] + val + ln[cy][cx:]; self.cx = cx + len(val)
            else:
                return False
            return True
        nav, _mod = norm_key(kind, val)
        if nav == "up" and cy: self.cy -= 1
        elif nav == "down" and cy < len(ln) - 1: self.cy += 1
        elif nav == "left":
            if cx: self.cx = cx - 1
            elif cy: self.cy = cy - 1; self.cx = len(ln[cy - 1])
        elif nav == "right":
            if cx < len(ln[cy]): self.cx = cx + 1
            elif cy < len(ln) - 1: self.cy = cy + 1; self.cx = 0
        elif nav == "home": self.cx = 0
        elif nav == "end": self.cx = len(ln[cy])
        elif nav == "del":
            if cx < len(ln[cy]): ln[cy] = ln[cy][:cx] + ln[cy][cx + 1:]
            elif cy < len(ln) - 1: ln[cy] += ln.pop(cy + 1)
        elif nav == "backspace":
            return self.key("ch", "\x7f")
        else:
            return False
        self.cx = min(self.cx, len(self.lines[self.cy]))
        return True

    def draw(self, scr, y0, x0, h, w):
        top = max(0, self.cy - h + 1)
        for i in range(h):
            idx = top + i
            if idx >= len(self.lines):
                break
            line = self.lines[idx]
            off = max(0, self.cx - (w - 1)) if idx == self.cy else 0
            put(scr, y0 + i, x0, line[off:off + w])
        try:
            scr.move(y0 + self.cy - top, x0 + min(self.cx, w - 1))
        except curses.error:
            pass

# ----------------------------------------------------------------- the app

def for_instance(reqs, instance):
    """The requests a panel shows: all of them for a global panel; for an
    instance-scoped one, those whose stamp keys to that instance plus any
    request with no stamp at all (it belongs to every surface)."""
    if not instance:
        return reqs
    return [r for r in reqs
            if instance_id(r.get("instance")) in (None, instance)]

def scan_and_nudge(spool, instance=None):
    """Scan one panel's requests and bridge their desktop arrival nudges.

    Kept module-level so both the idle/list tick and a blocking request modal
    use the identical path. The latter matters when another request lands
    while the operator is answering the first: old MCP servers still get an
    immediate nudge instead of waiting for this modal to close.
    """
    reclaim_answered_orphans(spool)
    if not capability_enabled():
        prune_arrival_nudges(spool)
        return [], 0
    reqs, bad = scan_requests(spool)
    prune_arrival_nudges(spool)
    reqs = for_instance(reqs, instance)
    for request in reqs:
        arrival_nudge(spool, request)
    return reqs, bad

# ---- concurrent requests (2026-09-03, hcop-0p7.3). N requests may be open at
# once, from N instances. The panel lists every one it serves, grouped by
# instance, each answerable on its own and in any order; answers are written
# by REQUEST id (write_answer), so nothing here assumes "the first request".
# These helpers are curses-free so selftest can pin them without a pty.

ENTER_KEYS = (10, 13, getattr(curses, "KEY_ENTER", 343))
PALETTE_FIRST_PAIR = 10          # tuikit owns pairs 1-6; palette gets 10..18
SECONDARY_PAIR = 19              # palette.SECONDARY_XTERM: legible quiet text

# Secondary text attr. A_DIM is half intensity in VTE and fails the 4.5:1
# text bar (hcop-2um), so readable text never draws with it: MUTED becomes
# palette.SECONDARY_XTERM at startup (plain normal text without 256 colours).
# Only pure rules ("─") keep A_DIM.
MUTED = curses.A_NORMAL


def legible(attr):
    """attr with A_DIM swapped for MUTED — md_lines() marks quotes and fenced
    code dim; this is what they are drawn with."""
    return (attr & ~curses.A_DIM) | MUTED if attr & curses.A_DIM else attr

def instance_label(inst):
    """How an instance is named to the operator (lib.js::instanceLabel's
    twin): the session, else the first 8 hex digits of the server uuid."""
    if not isinstance(inst, dict):
        return ""
    if inst.get("session"):
        return str(inst["session"])
    return str(inst.get("server_uuid") or "")[:8]

def auto_open(reqs, backed=()):
    """The one request the panel opens WITHOUT being asked: the ONLY open
    request, unless the operator backed out of it (^B) — then it waits in the
    list until Enter. With two or more open, the list is the screen."""
    if len(reqs) == 1 and reqs[0]["id"] not in set(backed):
        return reqs[0]
    return None

def request_gist(summary):
    """One line naming a request in the list: its first context line and its
    first step ("Context. — do the thing"). Tables, fences and the numbered
    steps themselves are not context."""
    intro, fence = "", False
    for raw in (summary or "").splitlines():
        t = raw.strip()
        if t.startswith("```") or t.startswith("~~~"):
            fence = not fence
            continue
        if fence or not t or "|" in raw or _STEP_LINE.match(raw) or _MD_RULE.match(raw):
            continue
        intro = md_inline(t.lstrip("#").strip())
        break
    steps = step_texts(summary)
    parts = [x for x in (intro, steps[0] if steps else "") if x]
    return " — ".join(parts) or "(no summary)"

def list_items(reqs, numbers):
    """Rows of the request list: `reqs` (oldest first) grouped by instance,
    groups ordered by their palette number (unstamped requests — every
    surface's — last), with a 'group' header row before each group when there
    is more than one. `numbers` maps instance id -> palette number."""
    groups, order = {}, []
    for r in reqs:
        key = instance_id(r.get("instance"))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(r)
    keys = sorted(order, key=lambda k: (k is None, numbers.get(k) or 10 ** 6
                                        if k else 0, order.index(k)))
    items = []
    for k in keys:
        rs = groups[k]
        n = (numbers.get(k) or 0) if k else 0
        if len(keys) > 1:
            items.append({"kind": "group", "key": k, "number": n, "count": len(rs),
                          "label": instance_label(rs[0].get("instance")) if k
                          else "every surface (unstamped)"})
        for r in rs:
            items.append({"kind": "req", "req": r, "key": k, "number": n})
    return items

def request_rows(items):
    return [i for i, it in enumerate(items) if it["kind"] == "req"]

def nearest_request(items, sel):
    """The request row at or after `sel` (else the last one); -1 when the
    list holds no request at all."""
    rows = request_rows(items)
    if not rows:
        return -1
    return next((i for i in rows if i >= sel), rows[-1])

def list_motion(ch, sel, items, page):
    """New selection for a list key (arrows, j/k, PgUp/PgDn, Home/End, g/G)
    or None when `ch` is not one — the list never takes a key the request
    modal or the idle screen uses (T, R, ?, q, ^D, ^B …). Selection lands on
    request rows only; group headers are skipped over."""
    rows = request_rows(items)
    if not rows:
        return None
    if ch in (curses.KEY_DOWN, ord("j")):
        step = 1
    elif ch in (curses.KEY_UP, ord("k")):
        step = -1
    elif ch == curses.KEY_NPAGE:
        step = max(1, page // 2)
    elif ch == curses.KEY_PPAGE:
        step = -max(1, page // 2)
    elif ch in (curses.KEY_HOME, ord("g")):
        return rows[0]
    elif ch in (curses.KEY_END, ord("G")):
        return rows[-1]
    else:
        return None
    cur = nearest_request(items, sel)
    pos = rows.index(cur)
    if cur > sel:                       # sel sat on a header: down = the row
        pos -= 1 if step > 0 else 0     # right below it, up = the row above
        if step > 0:
            step = 1
    return rows[max(0, min(len(rows) - 1, pos + step))]

# A list row names a request's response mode in TEXT (identity never rides on
# colour alone); the default checklist/prose shape needs no tag.
MODE_TAGS = {"choice": "[choice] ", "ack": "[ack] ", "text": "[text] "}

class CopilotApp(TuiApp):
    title = "copilot-tui · human-copilot"

    def __init__(self, instance=None):
        super().__init__()
        self.spool = spool_dir()
        self.instance = instance or None
        if self.instance:
            self.title = f"copilot-tui · human-copilot · {self.instance}"
        self._presence_fd = hold_presence(self.spool, self.instance)   # held until exit
        self._presence_heartbeat_at = time.monotonic()
        self.in_modal = False
        self.answered = 0
        self.last_seen = set()
        self._toggle_signature = None
        self._toggle_snapshot = None
        # the list (hcop-0p7.3): every open request this panel serves
        self.reqs = []               # oldest first, after for_instance()
        self.items = []              # list_items() of the last draw
        self.lv = ListView()
        self.backed = set()          # ids backed out of with ^B: no auto-open
        self.drafts = {}             # id -> {"ed": MultiLine, "images": [...]}
        self._slots = {}             # instance id -> palette number
        self._pal = {}               # palette number -> curses attr
        self._gists = {}             # id -> request_gist()
        self.idle_since = time.time()   # scoped panels only: see _idle_check
        self.idle_exit = False          # left because it was useless, not dismissed

    def run(self):
        """A global panel runs as any house TUI. An instance-scoped panel (the
        sidepanel zellij opened inside the session) also turns SIGHUP — what a
        closed pane delivers — and SIGTERM into an orderly exit, and on ANY
        way out declines the open requests stamped for its instance: the
        surface is gone, the agent must not sit blocked until its timeout."""
        if not self.instance:
            return super().run()
        def hangup(signum, _frame):
            raise SystemExit(128 + signum)
        for sig in (signal.SIGHUP, signal.SIGTERM):
            signal.signal(sig, hangup)
        code = 0
        try:
            super().run()
        except SystemExit as e:
            code = e.code if isinstance(e.code, int) else 0
        except curses.error:            # the pty went away under curses
            code = 1
        finally:
            # The idle path only fires with nothing open for this instance, so
            # there is nothing to decline — and asking would be actively wrong:
            # a request landing in the microseconds between the last scan and
            # here would be answered "the operator dismissed this" by a panel
            # that left because it was empty. It stays open for the overlay.
            declined = [] if self.idle_exit else decline_open(self.spool, self.instance)
            if self.idle_exit:
                sys.stderr.write(f"copilot-tui: pane for {self.instance} idle "
                                 f"{PANE_IDLE_S:g}s with nothing open — closing\n")
            if declined:
                sys.stderr.write(f"copilot-tui: pane for {self.instance} closed — "
                                 f"declined {len(declined)} open request(s)\n")
        return code

    def tick(self):
        self._refresh_presence()
        if self.in_modal:
            return
        # Old MCP server processes do not know the nudge protocol or clean its
        # marker. A current TUI does both without touching live processes.
        reqs, bad = scan_and_nudge(self.spool, self.instance)
        self.bad = bad
        ids = {r["id"] for r in reqs}
        if ids - self.last_seen:
            curses.beep()
        if ids != self.last_seen:
            self.dirty = True            # a row came or went
        self.last_seen = ids
        self.reqs = reqs
        self.backed &= ids
        prune_drafts(self.spool, self.drafts, ids)
        self._gists = {k: v for k, v in self._gists.items() if k in ids}
        self._number_instances(reqs)
        one = auto_open(reqs, self.backed)
        if one:
            self.open_request(one)
        self._idle_check()

    def _refresh_presence(self):
        """Refresh render proof at a bounded rate while this panel paints."""
        now = time.monotonic()
        last = getattr(self, "_presence_heartbeat_at", None)
        if last is None:
            self._presence_heartbeat_at = now
            return
        if now - last < TUI_RUNTIME_REFRESH_S:
            return
        self._presence_heartbeat_at = now
        try:
            record_tui_runtime(self.spool, getattr(self, "instance", None))
        except OSError as e:
            sys.stderr.write(f"copilot-tui: could not refresh render heartbeat: {e}\n")

    def _idle_check(self):
        """A sidepanel is its instance's surface, not furniture. With nothing
        open for it and nobody touching it for PANE_IDLE_S, it closes and the
        pane (--close-on-exit) gives its rows back to the session; the next
        request opens a fresh one, bounded by the per-instance spawn cooldown.
        Only an instance-scoped panel does this — a global copilot-tui is the
        operator's own window and serves every instance.

        The exit is decided on a SECOND scan, not the one this tick already
        did: an arrival in the last tick must not be closed out from under the
        agent. What the two scans cannot close is the flock's own release, and
        `idle_exit` is what makes that residue safe (see run())."""
        if not self.instance or PANE_IDLE_S <= 0:
            return
        if self.reqs or self.in_modal:
            self.idle_since = time.time()
            return
        if time.time() - self.idle_since < PANE_IDLE_S:
            return
        fresh, _bad = scan_requests(self.spool)
        if for_instance(fresh, self.instance):
            self.idle_since = time.time()
            return
        self.idle_exit = True
        self._quit = True

    def _number_instances(self, reqs):
        """Palette numbers for the instances on the list, once per instance
        (palette.colour_for assigns in the spool's registry, so the overlay's
        strip shows the same number for the same session)."""
        for r in reqs:
            key = instance_id(r.get("instance"))
            if key and key not in self._slots:
                try:
                    self._slots[key] = palette.colour_for(r["instance"], self.spool).number
                except Exception:
                    self._slots[key] = 0

    def open_request(self, req):
        """Show one request's checklist; back to the list when it returns.
        The typed draft and attachments survive ^B by request id."""
        rid = req["id"]
        draft = self.drafts.setdefault(rid, {})
        self.in_modal = True
        try:
            status = self.feedback_modal(self.scr, req, queued=len(self.reqs) - 1,
                                         draft=draft)
        finally:
            self.in_modal = False
        if status == "back":
            self.backed.add(rid)
        else:
            self.drafts.pop(rid, None)
        self.dirty = True
        return status

    # -- idle screen -----------------------------------------------------
    def _cached_toggle_state(self):
        signature = []
        for path in (toggle.control_path(), toggle.claude_json_path(),
                     toggle.doctrine_path()):
            try:
                st = os.stat(path)
                signature.append((st.st_mtime_ns, st.st_size))
            except OSError:
                signature.append(None)
        signature = tuple(signature)
        if signature != self._toggle_signature:
            self._toggle_snapshot = toggle.state()
            self._toggle_signature = signature
        return self._toggle_snapshot

    def draw(self, scr, reg):
        h, w = scr.getmaxyx()
        st = self._cached_toggle_state()
        dot = (("● enabled", C.OK) if st.get("enabled", st["registered"])
               else ("○ disabled", C.ATT))
        if self.reqs:
            self._draw_list(scr, reg, dot)
            return
        put(scr, 0, 1, "🧿 human-copilot — operator panel", C.ACC | curses.A_BOLD)
        put(scr, 0, w - len(dot[0]) - 2, dot[0], dot[1] | curses.A_BOLD)
        put(scr, 1, 0, "─" * (w - 1), curses.A_DIM)
        y = 3
        put(scr, y, 3, f"{spinner()} waiting for feedback requests…", curses.A_BOLD)
        y += 2
        put(scr, y, 3, f"spool   {homely(self.spool)}", MUTED); y += 1
        put(scr, y, 3, f"answered this run: {self.answered}", MUTED); y += 1
        if getattr(self, "bad", 0):
            put(scr, y, 3, f"⚠ {self.bad} unreadable request file(s) in the spool "
                           f"— check {homely(self.spool)}/requests", C.WORK); y += 1
        y += 1
        if not st["registered"]:
            put(scr, y, 3, "server is toggled OFF or partial — new Claude/Codex "
                           "sessions can't all call it (T repairs/enables)",
                C.WORK); y += 1
        if not st.get("consistent", st["doctrine"] in ("on", "off")):
            clients = st.get("clients") or {}
            detail = "; ".join(
                f"{name.title()} "
                f"{'registered' if value.get('registered') else 'absent'}"
                f"/{value.get('doctrine', 'unknown')}"
                for name, value in clients.items())
            put(scr, y, 3, f"⚠ partial toggle state — {detail}", C.ATT); y += 1
        y += 1
        if self.instance:
            put(scr, y, 3, f"sidepanel for instance {self.instance} — closing this "
                           f"pane declines its open requests (the agent is told)",
                C.ACC); y += 1
        put(scr, y, 3, "leave this running: a request takes over this screen "
                       "and rings the bell.", MUTED); y += 1
        put(scr, y, 3, "screenshots: just PrintScreen while a request is open "
                       "— the clipboard is watched.", MUTED)
        keybar(scr, reg, self.commands())

    # -- the request list (hcop-0p7.3) -----------------------------------
    def _draw_list(self, scr, reg, dot):
        h, w = scr.getmaxyx()
        n = len(self.reqs)
        put(scr, 0, 1, f"🧿 human-copilot — {n} feedback request{'s' if n != 1 else ''} waiting",
            C.ACC | curses.A_BOLD)
        put(scr, 0, w - len(dot[0]) - 2, dot[0], dot[1] | curses.A_BOLD)
        put(scr, 1, 0, "─" * (w - 1), curses.A_DIM)
        y = 2
        if self.instance:
            put(scr, y, 1, f"sidepanel for instance {self.instance} — closing this pane "
                           f"declines its open requests", C.ACC); y += 1
        put(scr, y, 1, "↑↓ / j k move · ⏎ open the selected request · each is answered "
                       "on its own, in any order", MUTED); y += 1
        if getattr(self, "bad", 0):
            put(scr, y, 1, f"⚠ {self.bad} unreadable request file(s) in the spool", C.WORK); y += 1
        self.items = list_items(self.reqs, self._slots)
        sel = nearest_request(self.items, self.lv.sel)
        if sel >= 0:
            self.lv.sel = sel
        self.lv.render(scr, reg, y, 1, max(1, h - y - 2), w - 2, self.items,
                       self._draw_row, on_activate=self._activate)
        keybar(scr, reg, self.commands())

    def _draw_row(self, scr, y, _idx, item, selected, w):
        if item["kind"] == "group":
            attr = (self._pal.get(item["number"], 0) if item["number"] else C.ACC) | curses.A_BOLD
            tag = f"{item['number']} ■ " if item["number"] else "■ "
            put(scr, y, 1, trunc(f"{tag}{item['label']} · {item['count']} open", w - 2), attr)
            return
        r = item["req"]
        rid = r["id"]
        if rid not in self._gists:
            self._gists[rid] = request_gist(r.get("summary", ""))
        client = r.get("client") if isinstance(r.get("client"), str) else ""
        client = client[:32] or "Your agent"
        left = dur(max(0, r.get("deadline", 0) - time.time()))
        draft = self.drafts.get(rid) or {}
        pen = "✎ " if (draft.get("ed") and draft["ed"].text().strip()) or draft.get("images") else ""
        tag = MODE_TAGS.get(response_of(r)["mode"], "")
        line = f"{'▶' if selected else ' '} {pen}{client} · ⏳ {left} · {tag}{self._gists[rid]}"
        x = 3 if any(it["kind"] == "group" for it in self.items) else 1
        colour = self._pal.get(item["number"], 0) if item["number"] else 0
        put(scr, y, x, trunc(line, w - x - 1),
            (curses.A_REVERSE | curses.A_BOLD) if selected else colour)

    def _activate(self, idx):
        if 0 <= idx < len(self.items) and self.items[idx]["kind"] == "req":
            self.open_request(self.items[idx]["req"])

    def toggle_reg(self):
        st = toggle.state()
        try:
            enabled = st.get("enabled", st["registered"])
            # A failed OFF is intentionally fail-closed. The next toggle
            # retries cleanup; it must not turn the capability back on while
            # registrations/doctrine still disagree with the authority.
            want = False if not st.get("consistent", True) and not enabled \
                else not enabled
            r = toggle.set_enabled(want)
            self._toggle_signature = None
            self.toast(("ON — " if r["registered"] else "OFF — ") + r["note"])
        except ValueError as e:
            self.toast(f"refused: {e}")
        self.dirty = True

    def commands(self):
        return [
            Command("⏎", "open the selected request", self._open_selected,
                    when=lambda: bool(self.reqs), keybar=True),
            Command("T", "toggle server registration", self.toggle_reg, keybar=True),
            Command("R", "rescan spool", lambda: setattr(self, "dirty", True),
                    keybar=True),
            Command("?", "help", lambda: help_modal(self.scr, HELP), keybar=True),
            Command("q", "quit (declines this instance's open requests)" if self.instance
                    else "quit (requests wait in the spool)", self.quit, keybar=True),
        ]

    def handle_key(self, scr, ch):
        self.idle_since = time.time()    # someone is using this panel
        if ch in (ord("T"), ord("t")):
            self.toggle_reg(); return True
        if ch in (ord("R"), ord("r")):
            return True
        if ch == ord("?"):
            help_modal(scr, HELP); return True
        if self.reqs:
            items = self.items or list_items(self.reqs, self._slots)
            new = list_motion(ch, self.lv.sel, items, self.lv.last_h)
            if new is not None:
                self.lv.sel = new
                return True
            if ch in ENTER_KEYS:
                self._open_selected()
                return True
        return False

    def _open_selected(self):
        items = self.items or list_items(self.reqs, self._slots)
        sel = nearest_request(items, self.lv.sel)
        if sel >= 0:
            self.open_request(items[sel]["req"])

    def startup(self, scr):
        self.scr = scr
        self.bad = 0
        try:                             # after tuikit.init_colors(): pairs 10..18
            self._pal = palette.init_curses_pairs(PALETTE_FIRST_PAIR)
        except Exception:
            self._pal = {}
        global MUTED
        try:
            MUTED = palette.init_secondary_pair(SECONDARY_PAIR)
        except Exception:
            MUTED = curses.A_NORMAL

    # -- the request modal ----------------------------------------------
    def feedback_modal(self, scr, req, queued=0, draft=None):
        """One request's checklist and answer editor. Returns how it ended:
        "answered" / "declined" / "withdrawn" / "back" (^B: the draft — editor
        and attachments — lives on in ``draft`` for the next open)."""
        req_path = os.path.join(self.spool, "requests", req["id"] + ".json")
        client = req.get("client") if isinstance(req.get("client"), str) else ""
        client = client[:32] or "Your agent"
        draft = draft if draft is not None else {}
        ed = draft.setdefault("ed", MultiLine())
        # the response contract (hcop-0gj): choice mode renders the options as
        # the answer control and Enter sends the highlighted one; an explicit
        # ack is the only request an empty ^D answers by itself
        resp = response_of(req)
        mode, options = resp["mode"], resp["options"]
        sel = min(max(-1, draft.get("sel", -1)), len(options) - 1)
        sv = ScrollView()
        summary = req.get("summary", "")
        lines_by_width = {}
        images = draft.setdefault("images", [])   # [{mime, b64, size, fp}]
        overlay = [None, 0.0]           # msg, until
        watch = ClipWatch().start()
        base = clip_image()
        baseline_fp = base[2] if base else None
        clip_has_image = bool(base)
        last_poll = 0.0
        next_nudge_scan = time.monotonic() + 0.5
        curses.curs_set(1)

        def show_overlay(msg):
            overlay[0], overlay[1] = msg, time.time() + OVERLAY_S
            curses.beep()

        def attach(data, mime, fp, auto):
            images.append({"mime": mime, "size": len(data), "fp": fp,
                           "b64": base64.b64encode(data).decode()})
            show_overlay(f"📸 screenshot on clipboard — attached (#{len(images)})"
                         + (" · ^X removes" if auto else ""))

        def grab(auto):
            got = clip_image()
            if not got:
                if not auto:
                    show_overlay("clipboard holds no image")
                return
            data, mime, fp = got
            verdict = decide_attach(fp, baseline_fp, {i["fp"] for i in images})
            if verdict == "attach" or (not auto and verdict == "hint"):
                attach(data, mime, fp, auto)
            elif verdict == "hint":
                show_overlay("screenshot on clipboard — ^V attaches it")

        try:
            while True:
                if os.path.exists(os.path.join(
                        self.spool, "answers", req["id"] + ".json")):
                    self.toast("request answered on another Human Copilot surface")
                    return "answered"
                if not os.path.exists(req_path):
                    self.toast("request withdrawn (timed out or cancelled by the agent)")
                    return "withdrawn"
                if request_expired(req):
                    result = responder_adapter.Responder(self.spool).expire(
                        req["id"], req.get("request_revision"))
                    event = ((result or {}).get("event") or {}).get("type")
                    if event == "already-answered":
                        self.toast("request answered on another Human Copilot surface")
                        return "answered"
                    self.toast("request expired — draft was not sent")
                    return "withdrawn"
                # feedback_modal blocks the app's normal tick. Continue
                # scanning here so a second old-server request gets its own
                # desktop nudge while the operator is still answering this
                # one, and so an overlay failure can fail open mid-request.
                now_mono = time.monotonic()
                if now_mono >= next_nudge_scan:
                    contract = req.get("responder")
                    if (isinstance(contract, dict)
                            and contract.get("schema") ==
                                responder_adapter.CONTRACT_SCHEMA
                            and not responder_adapter.Responder(
                                self.spool).local_available(req)):
                        self.toast("another responder owns this request — draft retained")
                        return "back"
                    scan_and_nudge(self.spool, self.instance)
                    next_nudge_scan = now_mono + 0.5
                # clipboard: event-driven when wl-paste --watch works, slow
                # fingerprint poll when it doesn't
                if watch.ok and watch.take():
                    grab(auto=True)
                elif not watch.ok and time.time() - last_poll > 2.0:
                    last_poll = time.time()
                    got = clip_image()
                    if got and decide_attach(got[2], baseline_fp,
                                             {i["fp"] for i in images}) == "attach":
                        attach(got[0], got[1], got[2], auto=True)

                h, w = scr.getmaxyx()
                scr.erase()
                left = max(0, req.get("deadline", 0) - time.time())
                head = f"🧿 {client} feedback request · ⏳ {dur(left)} left"
                if mode == "choice":
                    head += " · pick ONE option"
                elif mode == "ack":
                    head += " · ^D confirms"
                if queued:
                    head += f" · +{queued} more in the list (^B)"
                put(scr, 0, 1, head, C.ACC | curses.A_BOLD)
                put(scr, 1, 0, "─" * (w - 1), curses.A_DIM)
                sum_h = max(5, (h - 9) * 3 // 5)
                if mode == "choice" and options:
                    sum_h = max(3, min(sum_h, h - len(options) - 7))
                summary_w = w - 4
                if summary_w not in lines_by_width:
                    lines_by_width[summary_w] = [(t, legible(a)) for t, a in
                                                 md_lines(summary, summary_w)]
                lines = lines_by_width[summary_w]
                sv.render(scr, Regions(), 2, 2, sum_h, summary_w, lines)
                ey = 2 + sum_h
                if mode == "choice" and options:
                    # the option list IS the answer control (hcop-0gj): one
                    # labelled row per option, the selection marked by ▶ AND
                    # inversion — never by colour alone — with the value's
                    # cost text after it. Enter sends the highlighted one.
                    put(scr, ey, 0, "─" * (w - 1), curses.A_DIM)
                    put(scr, ey, 2, " choose ONE — ↑↓ or 1-9 select · Enter sends · "
                                    "^Y copy request · or type a reply · ^B list · Esc dismiss ",
                        C.ACC)
                    opt_h = max(1, min(len(options), h - ey - 4))
                    opt_top = max(0, min(sel, len(options) - opt_h))
                    for row in range(opt_h):
                        i = opt_top + row
                        o = options[i]
                        label = o.get("label") or o.get("value") or "?"
                        line = f"{'▶' if i == sel else ' '} {i + 1:>2}. {label}"
                        if o.get("hint"):
                            line += f" — {o['hint']}"
                        put(scr, ey + 1 + row, 1, trunc(line, w - 2),
                            (curses.A_REVERSE | curses.A_BOLD) if i == sel else 0)
                    ny = ey + 1 + opt_h
                    put(scr, ny, 1, "note:", MUTED)
                    ed.draw(scr, ny, 7, 1, w - 9)
                else:
                    put(scr, ey, 0, "─" * (w - 1), curses.A_DIM)
                    put(scr, ey, 2, " your answer — ^D send · ^Y copy request · ^T template · ^B back to list · "
                                    "Esc dismiss · ^V attach · ^X remove · PgUp/Dn ",
                        C.ACC)
                att = " · ".join(f"#{i + 1} {img['mime'].split('/')[1]} "
                                 f"{bfmt(img['size'])}" for i, img in enumerate(images))
                foot = ("📎 " + att) if att else \
                    ("clipboard holds an image — ^V attaches it"
                     if clip_has_image and not images else
                     "no screenshots attached — PrintScreen auto-attaches")
                if not watch.ok:
                    foot += " · (no wl-paste --watch: polling)"
                put(scr, h - 1, 1, trunc(foot, w - 2),
                    C.OK if att else MUTED)
                if overlay[0] and time.time() < overlay[1]:
                    msg = " " + overlay[0] + " "
                    ox = max(0, (w - len(msg)) // 2)
                    put(scr, 2, ox, "▁" * len(msg), C.SPC)
                    put(scr, 3, ox, msg, C.SPC | curses.A_REVERSE | curses.A_BOLD)
                    put(scr, 4, ox, "▔" * len(msg), C.SPC)
                # editor last: its scr.move() is what parks the visible cursor
                # (choice mode already drew it — one line under its options)
                if not (mode == "choice" and options):
                    ed.draw(scr, ey + 1, 2, h - ey - 2, w - 4)
                scr.refresh()
                self._refresh_presence()

                kind, val = read_key(scr, wait_ms=TICK_MS)
                if kind is None:
                    continue
                if kind == "esc":
                    if confirm(scr, "Dismiss without answering? The agent is told "
                                    "you declined.", danger=True):
                        result = write_answer(self.spool, req["id"], "", [],
                                              cancelled=True)
                        if result.get("ok"):
                            return "declined"
                        show_overlay(commit_problem(result))
                    continue
                if kind == "ch" and val == "\x19":            # ^Y copy request text
                    if not summary:
                        show_overlay("request has no text to copy")
                    elif copy_text(summary):
                        clip_has_image = False
                        show_overlay("request text copied to clipboard")
                    else:
                        show_overlay("copy failed — check wl-copy and clipboard")
                    continue
                if mode == "choice" and options:
                    # the option list owns the navigation keys in this mode:
                    # ↑↓ and digits move the selection, Enter or ^D sends it,
                    # and anything printable still reaches the note editor
                    def send_choice():
                        o = options[sel] if sel >= 0 else None
                        if o is None and not ed.text().strip():
                            show_overlay("choose an option or type a reply first")
                            return None
                        result = write_answer(
                            self.spool, req["id"], ed.text(), images,
                            choice={"value": o["value"],
                                    "label": o.get("label") or o["value"]} if o else None,
                            response_completion=None if o else {
                                "schema": "human-copilot.response-completion/v1",
                                "schema_version": 1, "mode": "choice",
                                "status": "not_done"})
                        if not result.get("ok"):
                            show_overlay(commit_problem(result))
                            return None
                        self.answered += 1
                        self.toast("answer sent: " + ((o.get("label") or o["value"])
                                                     if o else "reply without a selection"))
                        return "answered"
                    if kind == "ch" and val in ("\n", "\r", "\x04"):
                        outcome = send_choice()
                        if outcome is not None:
                            return outcome
                        continue
                    if kind == "ch" and val.isdigit() and 1 <= int(val) <= len(options):
                        sel = int(val) - 1
                        draft["sel"] = sel
                        continue
                    nav, _m = norm_key(kind, val)
                    if nav in ("up", "down"):
                        sel = ((0 if nav == "down" else len(options) - 1) if sel < 0
                               else (sel + (1 if nav == "down" else -1)) % len(options))
                        draft["sel"] = sel
                        continue
                if kind == "ch" and val == "\x04":            # ^D send
                    verdict = empty_send(mode, ed.text(), images,
                                         looks_like_choices(summary))
                    if verdict != "send":
                        show_overlay(verdict)
                        continue
                    structured = (checklist_answer(req, ed.text())
                                  if mode == "checklist" else None)
                    result = write_answer(self.spool, req["id"], ed.text(), images,
                                          checklist=structured)
                    if not result.get("ok"):
                        show_overlay(commit_problem(result))
                        continue
                    self.answered += 1
                    self.toast("answer sent")
                    return "answered"
                if kind == "ch" and val == "\x02":            # ^B back to the list
                    return "back"                            # draft kept by the caller
                if kind == "ch" and val == "\x16":            # ^V grab clipboard
                    grab(auto=False)
                    continue
                if kind == "ch" and val == "\x14":            # ^T report template
                    tpl = report_template(req.get("summary", ""))
                    if not tpl:
                        show_overlay("no numbered steps to template — just type")
                        continue
                    if ed.text().strip() and not confirm(
                            scr, "Replace what you typed with the template?"):
                        continue
                    # cursor on a fresh last line: type what you saw there,
                    # arrow up to change a step that did not pass
                    ed.lines = tpl.splitlines() + [""]
                    ed.cy, ed.cx = len(ed.lines) - 1, 0
                    show_overlay("template: change any step that did NOT pass, "
                                 "add what you saw, ^D sends")
                    continue
                if kind == "ch" and val == "\x18":            # ^X remove last
                    if images:
                        images.pop()
                        show_overlay(f"attachment removed ({len(images)} left)")
                    continue
                if kind == "mouse":
                    _, _mx, _my, _, bs = val
                    if bs & curses.BUTTON4_PRESSED:
                        sv.scroll(-3, len(lines), sum_h)
                    elif bs & BUTTON5:
                        sv.scroll(+3, len(lines), sum_h)
                    continue
                nav, _m = norm_key(kind, val)
                if nav in ("pgup", "pgdn"):
                    sv.scroll(-sum_h // 2 if nav == "pgup" else sum_h // 2,
                              len(lines), sum_h)
                    continue
                ed.key(kind, val)
        finally:
            watch.stop()
            curses.curs_set(0)
            scr.timeout(TICK_MS)

HELP = [
    "copilot-tui — your agents' eyes and hands, on your keyboard",
    "",
    "  idle: leave it running. a request takes the screen and rings",
    "  the bell. Several open: a list grouped by instance — ↑↓/j/k",
    "  move, Enter opens one, answering or ^B returns.",
    "",
    "  in a request:",
    "    type freely   multi-line; Enter is a newline",
    "    ^D            SEND the answer (with any screenshots)",
    "    choice mode   ↑↓ or 1-9 pick an option, Enter sends it (a",
    "                  typed note rides along); empty send refused",
    "    ack mode      ^D confirms — the one mode an empty send answers",
    "    ^T            mark every step pass — edit the ones that did",
    "                  not, say what you saw, then ^D",
    "    ^Y            copy the complete request text to the clipboard",
    "    Esc           dismiss — the agent is told you declined",
    "    ^B            back to the list; your draft is kept",
    "    PrintScreen   screenshot AUTO-ATTACHES · ^V attach clipboard",
    "    ^X            remove last attachment · PgUp/PgDn scroll",
    "",
    "  reading a step: 👁 = what should change; ⌖ = WHERE.",
    "",
    "  idle keys: T toggle server registration · R rescan · q quit —",
    "  requests queue in the spool while closed, until their timeout.",
    "  press any key to close.",
]

USAGE = """\
copilot-tui — operator panel for the human-copilot MCP lane.

usage: copilot-tui [-h] [--instance ID] [--spool DIR]

Claude and Codex interactive_feedback calls land here; ? inside shows
the full key help.
--instance ID: serve ONE instance (zellij session name or server uuid):
show only its requests, hold only its presence lock; closing that panel
(q, SIGHUP) declines its open requests. Default: every instance
(the global presence lock).
--spool DIR: the spool to watch.
env: HC_SPOOL overrides the spool (default $XDG_RUNTIME_DIR/human-copilot).\
"""

def parse_options(argv):
    """-> {"instance": id or None, "spool": dir or None}; raises SystemExit(2)
    on anything unknown or an unsafe instance id."""
    instance, spool, args = None, None, list(argv)
    while args:
        a = args.pop(0)
        if a == "--instance" and args:
            instance = args.pop(0)
        elif a.startswith("--instance="):
            instance = a.split("=", 1)[1]
        elif a == "--spool" and args:
            spool = args.pop(0)
        elif a.startswith("--spool="):
            spool = a.split("=", 1)[1]
        else:
            sys.stderr.write(f"copilot-tui: unknown argument {a!r}\n{USAGE}\n")
            raise SystemExit(2)
    if instance is not None and instance_id({"session": instance}) != instance:
        sys.stderr.write(f"copilot-tui: --instance {instance!r} is not a plain "
                         f"instance id (letters, digits, . _ @ + -)\n")
        raise SystemExit(2)
    return {"instance": instance or None, "spool": spool or None}

def parse_args(argv):
    """-> instance id or None; raises SystemExit(2) on anything unknown."""
    return parse_options(argv)["instance"]

if __name__ == "__main__":
    if any(a in ("-h", "--help") for a in sys.argv[1:]):
        print(USAGE)
        sys.exit(0)
    opts = parse_options(sys.argv[1:])
    if opts["spool"]:
        os.environ["HC_SPOOL"] = opts["spool"]     # ONE spool resolution: spool_dir()
    sys.exit(CopilotApp(opts["instance"]).run() or 0)
