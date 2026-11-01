#!/usr/bin/env python3
"""drive_tui — copilot-tui in a REAL pty, five passes.

Pass 1: a request is waiting when the panel starts; it takes the screen (a
pipe table included — it must arrive DRAWN, not as raw markdown), the operator
types and ^D sends; the answer file appears, 0600, with the text.

Pass 2: the seamless-screenshot lane, with `wl-paste` STUBBED on PATH (this
box's real clipboard is a Wayland session the test must not depend on): the
stub's --watch fires when the fake clipboard file changes, exactly like the
real one fires on a clipboard SET. Mid-request a "PrintScreen" lands an image;
the test asserts the OVERLAY announced it ("screenshot on clipboard —
attached"), and that the sent answer carries the bytes.

The stub matters beyond convenience: two consecutive screenshots are both just
[image/png], so change detection MUST be event-driven (--watch), not
types-diffing — this drive is what keeps that from being 'simplified' away.

Pass 3 (hcop-0p7.4): a scoped panel closed by SIGHUP declines its request.
Passes 4-5 (hcop-0p7.3): the request LIST — a scoped panel with two own
requests and a foreign one, answered out of order; a global panel with three
instances grouped and numbered, ^B keeping a draft, two same-tick arrivals.
Pass 8: ^Y copies the open request's complete UTF-8 text via wl-copy without
sending the draft; a clipboard failure is reported without changing it.

Run from the repo root: python3 tests/drive_tui.py
"""
import base64
import json
import os
import pty
import select
import signal
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import toggle

def enabled_home(spool):
    """Create the four coherent ON surfaces beside this hermetic spool."""
    home = os.path.join(spool, ".test-home")
    claude = os.path.join(home, ".claude")
    codex = os.path.join(home, ".codex")
    os.makedirs(claude, mode=0o700, exist_ok=True)
    os.makedirs(codex, mode=0o700, exist_ok=True)
    with open(os.path.join(home, ".claude.json"), "w") as f:
        json.dump({"mcpServers": {
            "human-copilot": toggle.default_registration()}}, f)
    with open(os.path.join(codex, "config.toml"), "w") as f:
        f.write(toggle.default_codex_registration())
    doctrine = (toggle.MARK_BEGIN + "\n" + toggle._doctrine_text(True)
                + "\n" + toggle.MARK_END + "\n")
    for path in (os.path.join(claude, "CLAUDE.md"),
                 os.path.join(codex, "AGENTS.md")):
        with open(path, "w") as f:
            f.write(doctrine)
    return home

def start(spool, extra_env, argv=(), spool_via_env=True):
    """copilot-tui in a fresh pty. The spool reaches it through HC_SPOOL, or —
    the sidepanel's way, hcop-0p7.4 — only as a `--spool` argument in ``argv``
    when ``spool_via_env`` is False."""
    m, s = pty.openpty()
    env = {**os.environ, "TERM": "xterm-256color",
           "LINES": "30", "COLUMNS": "110", **extra_env}
    # The operator may intentionally have the live lane OFF.  This is a
    # positive-path TUI drive, so give the child an explicit hermetic ON
    # authority instead of inheriting (or changing) the desktop's control.
    if "HC_CONTROL_FILE" not in extra_env:
        control = os.path.join(spool, ".test-control.json")
        with open(control, "w") as f:
            json.dump({"schema": "human-copilot-control",
                       "schema_version": 1, "enabled": True}, f)
        os.chmod(control, 0o600)
        env["HC_CONTROL_FILE"] = control
    if "HOME" not in extra_env:
        home = enabled_home(spool)
        env["HOME"] = home
        env["CODEX_HOME"] = os.path.join(home, ".codex")
    # A current TUI emits the desktop arrival nudge itself when no healthy
    # overlay is present. Keep this real-pty drive hermetic: record that call
    # instead of displaying test notifications on the operator's desktop.
    notify_bin = os.path.join(spool, ".test-bin")
    os.makedirs(notify_bin, exist_ok=True)
    notify_log = os.path.join(spool, "notifications.log")
    notify_stub = os.path.join(notify_bin, "notify-send")
    with open(notify_stub, "w") as f:
        f.write('#!/bin/sh\nprintf "%s\\n" "$*" >> "$HC_TEST_NOTIFY_LOG"\n')
    os.chmod(notify_stub, 0o755)
    env["PATH"] = notify_bin + ":" + env.get("PATH", "")
    env["HC_TEST_NOTIFY_LOG"] = notify_log
    env.pop("HC_SPOOL", None)
    if spool_via_env:
        env["HC_SPOOL"] = spool
    p = subprocess.Popen(["python3", os.path.join(HERE, "tui.py"), *argv],
                         stdin=s, stdout=s, stderr=subprocess.PIPE,
                         env=env, close_fds=True)
    os.close(s)
    _seen[m] = ""
    return p, m

# The pty stream is NOT the screen: curses paints the whole window once and
# then repaints only what changed (the countdown digit). Text that arrived in
# an earlier read is therefore gone from the stream but still ON the screen,
# so every reader appends to one buffer per pty and checks the whole of it.
# `fresh=True` is the opposite question — "did this appear BECAUSE of what I
# just pressed" — and looks only at what arrives after the call begins.
_seen = {}

def _read_into(m, sec):
    """Append up to ``sec`` seconds of pty output to this pty's buffer."""
    out = b""
    end = time.time() + sec
    while time.time() < end:
        r, _, _ = select.select([m], [], [], 0.2)
        if r:
            try:
                out += os.read(m, 65536)
            except OSError:
                break
    text = out.decode("utf-8", "replace")
    _seen[m] = _seen.get(m, "") + text
    return text

def drain(m, sec):
    """Read whatever the pty says for ``sec`` seconds. Only for flushing echo
    nobody asserts on — anything a check depends on goes through ``expect``,
    which waits for the text instead of hoping it arrived in time."""
    return _read_into(m, sec)

# A fixed drain is a bet on how fast the box is: at load ~10 this driver
# failed twice and passed on a re-run (hcop-8y7), and a red run that teaches
# "run it again" defends nothing. Every check now waits for the text it needs,
# up to a deadline generous enough that reaching it means something is really
# wrong — and says what it wanted and what the screen showed instead.
DEADLINE = float(os.environ.get("HC_DRIVE_DEADLINE", "25"))
SETTLE = 0.4          # after the wanted text, before asserting text is ABSENT

def expect(m, *needles, absent=(), fresh=False, timeout=DEADLINE, what=""):
    """Wait until every needle has appeared on this pty; return what was seen.

    Fails naming the needle still missing and the tail of the screen, so a
    real failure says what it wanted instead of "re-run it". ``fresh`` limits
    the search to output arriving from now on — use it when the point of the
    check is that a keypress produced a NEW screen, since text already painted
    would otherwise satisfy it for free. ``absent`` strings are checked once
    the needles are in and the stream has gone quiet."""
    if fresh:
        _seen[m] = ""
    end = time.time() + timeout
    while True:
        buf = _seen.get(m, "")
        missing = [n for n in needles if n not in buf]
        if not missing:
            break
        if time.time() >= end:
            raise AssertionError(
                "%stimed out after %.0fs waiting for %r\nseen (tail):\n%s"
                % (what + ": " if what else "", timeout, missing[0], buf[-900:]))
        _read_into(m, 0.2)
    if absent:
        _read_into(m, SETTLE)
        buf = _seen.get(m, "")
        for a in absent:
            assert a not in buf, (
                "%s%r should NOT be on screen\nseen (tail):\n%s"
                % (what + ": " if what else "", a, buf[-900:]))
    return _seen.get(m, "")

def wait_file(path, timeout=DEADLINE, what=""):
    """Wait for a spool file to appear; fail naming it and its directory."""
    end = time.time() + timeout
    while time.time() < end:
        if os.path.exists(path):
            return path
        time.sleep(0.05)
    d = os.path.dirname(path)
    raise AssertionError(
        "%s%s never appeared (%s holds %r)"
        % (what + ": " if what else "", path,
           d, sorted(os.listdir(d)) if os.path.isdir(d) else "no such dir"))

def add_request(spool, rid, summary, instance=None, created=None):
    """A request file as server.py writes it; ``instance`` is the hcop-0p7.1
    stamp (omitted = a pre-stamp request that belongs to every surface)."""
    body = {"id": rid, "created": created or time.time(),
            "deadline": time.time() + 300, "summary": summary, "client": "Codex"}
    if instance is not None:
        body["instance"] = instance
    with open(os.path.join(spool, "requests", rid + ".json"), "w") as f:
        json.dump(body, f)

def spool_with_request(tmp, rid, summary, instance=None):
    spool = os.path.join(tmp, "spool")
    for d in ("requests", "answers"):
        os.makedirs(os.path.join(spool, d), exist_ok=True)
    add_request(spool, rid, summary, instance)
    return spool

def pass_basic():
    tmp = tempfile.mkdtemp(prefix="hc-drive1-")
    spool = spool_with_request(
        tmp, "r1",
        "# Check the thing\n\n"
        "| # | do | report |\n|---|---|---|\n| 1 | open it | pass/fail |\n\n"
        "2. report **pass/fail** @{Firefox · top-right} → toast says Saved")
    p, m = start(spool, {})
    try:
        out = expect(m, "Codex feedback request", "Check the thing", "┌",
                     what="pass 1: the waiting request takes the screen")
        sys.path.insert(0, HERE)
        import server as hcserver
        notify_log = os.path.join(spool, "notifications.log")
        wait_file(hcserver.arrival_nudge_path(spool, "r1"), what="pass 1: TUI records arrival")
        time.sleep(0.5)                  # several scans must not duplicate it
        notes = open(notify_log).read().splitlines() if os.path.exists(notify_log) else []
        marker = hcserver.arrival_nudge_path(spool, "r1")
        assert notes == [], \
            "TUI did not emit exactly one useful arrival nudge: %r" % notes
        assert os.path.exists(marker) and os.stat(marker).st_mode & 0o777 == 0o600, \
            "TUI's per-request nudge marker is missing or not 0600"
        # The request modal blocks the app's normal tick. A second request
        # from an old resident server must still be nudged while the first is
        # open, not after the operator eventually backs out or answers.
        add_request(spool, "r1b", "Second prompt while the first is open")
        marker2 = hcserver.arrival_nudge_path(spool, "r1b")
        end = time.time() + DEADLINE
        while time.time() < end and not os.path.exists(marker2):
            time.sleep(0.05)
        assert os.path.exists(marker2), \
            "TUI stopped discovering arrivals while its first request modal was open"
        time.sleep(0.5)
        notes = open(notify_log).read().splitlines() if os.path.exists(notify_log) else []
        assert notes == [], \
            "modal-time request did not get exactly one immediate nudge: %r" % notes
        assert hcserver.tui_alive(spool), \
            "running panel must hold the presence lock (auto-open depends on it)"
        # a table must reach the real screen as a drawn table, not raw pipes
        out += expect(m, "│ open it", what="pass 1: the table is drawn, not raw pipes")
        assert "|---|" not in out, "delimiter row leaked to the operator"
        assert "**pass/fail**" not in out, "inline markers not stripped"
        # the eye-pointers arrive as their own lines, never as raw grammar
        out += expect(m, "👁 toast says Saved", "⌖ Firefox · top-right",
                      what="pass 1: the eye-pointers are drawn as their own lines")
        assert "@{" not in out and "→ toast" not in out, "raw annotation leaked"
        # ^T drops in the per-step template, one 'pass' per step
        os.write(m, b"\x14")
        expect(m, "1. pass", "2. pass", "change any step that did NOT pass",
               fresh=True, what="pass 1: ^T inserts the per-step template")
        for _ in range(40):
            os.write(m, b"\x7f")               # backspace the template away again
        drain(m, 0.5)
        os.write(m, b"it works")
        expect(m, "it works", fresh=True, what="pass 1: typed text is echoed")
        os.write(m, b"\x04")
        path = wait_file(os.path.join(spool, "answers", "r1.json"),
                         what="pass 1: ^D sends")
        ans = json.load(open(path))
        assert ans["text"] == "it works" and ans["cancelled"] is False
        assert os.stat(path).st_mode & 0o777 == 0o600, "answer not 0600"
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: TUI nudged each request once, including one arriving mid-modal; answer sent, 0600")

def pass_clipboard():
    tmp = tempfile.mkdtemp(prefix="hc-drive2-")
    spool = spool_with_request(tmp, "r2", "1. screenshot the window")
    clip = os.path.join(tmp, "clipboard.png")
    bindir = os.path.join(tmp, "bin"); os.makedirs(bindir)
    stub = os.path.join(bindir, "wl-paste")
    with open(stub, "w") as f:
        f.write(f'''#!/bin/bash
CLIP="{clip}"
case "$1" in
  --list-types) [ -f "$CLIP" ] && echo image/png ;;
  --type) cat "$CLIP" ;;
  --watch) shift; last=""
    while :; do
      cur=$(stat -c %Y "$CLIP" 2>/dev/null || echo none)
      if [ "$cur" != "$last" ] && [ "$cur" != none ]; then last="$cur"; "$@"; fi
      sleep 0.2
    done ;;
esac
''')
    os.chmod(stub, 0o755)
    p, m = start(spool, {"PATH": bindir + ":" + os.environ["PATH"]})
    try:
        expect(m, "PrintScreen auto-attaches",
               what="pass 2: the clipboard hint line")
        png = b"\x89PNG\r\n\x1a\n" + os.urandom(64)
        with open(clip, "wb") as f:
            f.write(png)                       # "PrintScreen"
        expect(m, "screenshot on clipboard", "attached (#1)", fresh=True,
               what="pass 2: the overlay announces the auto-attach")
        os.write(m, b"saw it\x04")
        ans = json.load(open(wait_file(os.path.join(spool, "answers", "r2.json"),
                                       what="pass 2: ^D sends with the image")))
        assert ans["text"] == "saw it"
        assert len(ans["images"]) == 1 \
            and base64.b64decode(ans["images"][0]["b64"]) == png
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: overlay announced the screenshot; bytes shipped in the answer")

def pass_copy():
    """A real pty keypress writes request text through wl-copy's stdin."""
    tmp = tempfile.mkdtemp(prefix="hc-drive-copy-")
    summary = "# Copy this\n\nThe agent showed café and `code`.\n2. Keep every line"
    spool = spool_with_request(tmp, "copy", summary)
    clip = os.path.join(tmp, "clipboard.txt")
    fail = os.path.join(tmp, "copy-fails")
    bindir = os.path.join(tmp, "bin")
    os.makedirs(bindir)
    with open(os.path.join(bindir, "wl-copy"), "w") as f:
        f.write('#!/bin/sh\n'
                '[ "$1" = --type ] && [ "$2" = "text/plain;charset=utf-8" ] || exit 2\n'
                '[ ! -e "$HC_TEST_COPY_FAIL" ] || exit 3\n'
                'cat > "$HC_TEST_CLIPBOARD"\n')
    with open(os.path.join(bindir, "wl-paste"), "w") as f:
        f.write('#!/bin/sh\n'
                'case "$1" in --list-types) exit 0 ;; --watch) sleep 60 ;; esac\n')
    for name in ("wl-copy", "wl-paste"):
        os.chmod(os.path.join(bindir, name), 0o755)
    p, m = start(spool, {"PATH": bindir + ":" + os.environ["PATH"],
                         "HC_TEST_CLIPBOARD": clip, "HC_TEST_COPY_FAIL": fail})
    try:
        expect(m, "Copy this", "café and code", "^Y copy request",
               what="copy: the open request and its action are visible")
        os.write(m, b"draft stays\x19")
        expect(m, "request text copied to clipboard", fresh=True,
               what="copy: ^Y confirms the clipboard write")
        assert open(clip, encoding="utf-8").read() == summary, \
            "wl-copy did not receive the complete request text"
        assert not os.path.exists(os.path.join(spool, "answers", "copy.json")), \
            "copy must not answer the request"
        with open(fail, "w"):
            pass
        os.write(m, b"\x19")
        expect(m, "copy failed", fresh=True,
               what="copy: a failed wl-copy is visible to the operator")
        assert open(clip, encoding="utf-8").read() == summary, \
            "failed copy changed the previous clipboard text"
        os.write(m, b"\x04")
        answer = wait_file(os.path.join(spool, "answers", "copy.json"),
                           what="copy: the draft remains sendable")
        assert json.load(open(answer))["text"] == "draft stays", \
            "copy changed the answer draft"
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: ^Y copied complete UTF-8 request text; failure was visible; draft survived")

def pass_scoped_close():
    """Pass 3 (hcop-0p7.4): the sidepanel — copilot-tui scoped to ONE
    instance, told its spool only by `--spool` (a zellij pane inherits the
    zellij server's env, never the agent's) — holds its instance's lock and
    not the global one; when the operator closes the pane, zellij hangs the
    pty up (SIGHUP), and the panel DECLINES its instance's open request with
    the exact answer JSON the Esc path writes, so the agent is told. An
    unstamped request in the same spool belongs to every surface and is left
    alone."""
    tmp = tempfile.mkdtemp(prefix="hc-drive3-")
    stamp = {"session": "hc-A", "pane": "0", "server_uuid": "a" * 32, "attached": True}
    spool = spool_with_request(tmp, "r3", "Context line.\n1. look at the thing", stamp)
    add_request(spool, "r4", "1. for everyone", created=time.time() + 1)
    p, m = start(spool, {}, argv=["--instance", "hc-A", "--spool", spool],
                 spool_via_env=False)
    try:
        expect(m, "feedback request", "look at the thing",
               what="pass 3: the scoped panel picks up its request via --spool")
        sys.path.insert(0, HERE)
        import server as hcserver
        assert hcserver.instance_alive(spool, "hc-A"), "scoped panel must hold ITS instance lock"
        assert not hcserver.tui_alive(spool), "scoped panel must NOT hold the global lock"
        p.send_signal(signal.SIGHUP)              # the operator closed the pane
        p.wait(timeout=6)
        err = p.stderr.read().decode("utf-8", "replace")
        path = wait_file(os.path.join(spool, "answers", "r3.json"),
                         what="pass 3: closing the pane declines its request; stderr:\n" + err)
        ans = json.load(open(path))
        assert ans == {"text": "", "cancelled": True, "images": []}, \
            "declined answer differs from the Esc path's: %r" % ans
        assert os.stat(path).st_mode & 0o777 == 0o600, "declined answer not 0600"
        assert not os.path.exists(os.path.join(spool, "answers", "r4.json")), \
            "an UNSTAMPED request (every surface's) must not be declined by one pane closing"
        assert "declined 1 open request" in err, "the panel did not say what it declined:\n" + err
    finally:
        if p.poll() is None:
            p.terminate(); p.wait(timeout=5)
    print("  ok: scoped panel via --spool held its lock only; SIGHUP declined its request, left the shared one")

def _stamp(session, uuid_char):
    return {"session": session, "pane": "0", "server_uuid": uuid_char * 32, "attached": True}

def pass_scoped_list():
    """Pass 4 (hcop-0p7.3): the sidepanel with TWO open requests of its own
    instance and one of a foreign instance in the same spool. The scoped
    panel lists its own two (the foreign one is not its business), the
    operator answers the SECOND one first, and each answer lands under its
    own request id. Once one request is left it takes the screen by itself,
    as a single request always did."""
    tmp = tempfile.mkdtemp(prefix="hc-drive4-")
    t = time.time()
    spool = spool_with_request(tmp, "a1", "Alpha context.\n1. first of A", _stamp("hc-A", "a"))
    add_request(spool, "a2", "Second context.\n1. second of A", _stamp("hc-A", "a"), created=t + 1)
    add_request(spool, "b1", "Beta context.\n1. only of B", _stamp("hc-B", "b"), created=t + 2)
    p, m = start(spool, {}, argv=["--instance", "hc-A", "--spool", spool], spool_via_env=False)
    try:
        expect(m, "2 feedback requests waiting", "first of A", "second of A",
               absent=("only of B",),
               what="pass 4: the scoped panel lists its two and hides the foreign one")
        sys.path.insert(0, HERE)
        import server as hcserver
        assert all(os.path.exists(hcserver.arrival_nudge_path(spool, rid))
                   for rid in ("a1", "a2")), \
            "scoped TUI did not nudge each request it serves"
        assert not os.path.exists(hcserver.arrival_nudge_path(spool, "b1")), \
            "scoped TUI nudged a foreign instance's request"
        notes_path = os.path.join(spool, "notifications.log")
        notes = open(notes_path).read().splitlines() if os.path.exists(notes_path) else []
        assert notes == [], \
            "scoped TUI should emit one nudge for each own request: %r" % notes
        os.write(m, b"j")                          # down to the second one
        drain(m, 0.4)
        os.write(m, b"\r")                         # open it
        expect(m, "second of A", "feedback request ·", "+1 more in the list",
               fresh=True, what="pass 4: Enter opens the selected (second) request")
        _seen[m] = ""       # capture the repaint caused by the completed answer
        os.write(m, b"two done\x04")
        a2 = wait_file(os.path.join(spool, "answers", "a2.json"),
                       what="pass 4: the second request is answered first")
        assert json.load(open(a2))["text"] == "two done", "a2's answer carries the wrong text"
        for other in ("a1", "b1"):
            assert not os.path.exists(os.path.join(spool, "answers", other + ".json")), \
                f"answering a2 must not touch {other}"
        # Deliberately leave the request file behind: a completed answer is
        # enough to make it non-actionable even if its server dies before
        # normal request cleanup (hcop-m8n).
        # curses updates only changed cells: "Second" -> "Alpha" while the
        # unchanged " context." suffix stays on screen.
        expect(m, "Alpha", "first of A",
               what="pass 4: the one request left takes the screen by itself")
        os.write(m, b"one done\x04")
        a1 = wait_file(os.path.join(spool, "answers", "a1.json"),
                       what="pass 4: the remaining request is answered")
        assert json.load(open(a1))["text"] == "one done", "a1's answer carries the wrong text"
        assert not os.path.exists(os.path.join(spool, "answers", "b1.json")), "the foreign request was answered"
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: scoped TUI nudged only its two; hid the foreign one; answered out of order")

def pass_global_list():
    """Pass 5 (hcop-0p7.3): the global panel with THREE instances' requests:
    grouped and numbered, the middle one opened by j+Enter, a draft kept
    across ^B, answered under its own id; then two requests landing in the
    same tick both appear, and the last one is answered next."""
    tmp = tempfile.mkdtemp(prefix="hc-drive5-")
    t = time.time()
    spool = spool_with_request(tmp, "a1", "1. alpha step", _stamp("hc-A", "a"))
    add_request(spool, "b1", "1. beta step", _stamp("hc-B", "b"), created=t + 1)
    add_request(spool, "c1", "1. gamma step", _stamp("hc-C", "c"), created=t + 2)
    p, m = start(spool, {})
    try:
        expect(m, "3 feedback requests waiting",
               "alpha step", "beta step", "gamma step",
               "1 ■ hc-A · 1 open", "2 ■ hc-B · 1 open", "3 ■ hc-C · 1 open",
               what="pass 5: three instances grouped, numbered and counted")
        os.write(m, b"j")                          # the middle one
        drain(m, 0.4)
        os.write(m, b"\r")
        expect(m, "beta step", "+2 more in the list", fresh=True,
               what="pass 5: the middle request opens")
        os.write(m, b"half typed")
        expect(m, "half typed", fresh=True, what="pass 5: the draft is echoed")
        os.write(m, b"\x02")                       # ^B: back to the list, draft kept
        expect(m, "3 feedback requests waiting", "✎", fresh=True,
               what="pass 5: ^B returns to the list and marks the kept draft")
        assert not os.listdir(os.path.join(spool, "answers")), "^B wrote an answer"
        os.write(m, b"\r")                         # selection stayed on it: reopen
        expect(m, "half typed", fresh=True,
               what="pass 5: the draft survives ^B")
        os.write(m, b" and sent\x04")
        b1 = wait_file(os.path.join(spool, "answers", "b1.json"),
                       what="pass 5: the middle request is answered")
        assert json.load(open(b1))["text"] == "half typed and sent", \
            "b1's answer carries the wrong text"
        assert sorted(os.listdir(os.path.join(spool, "answers"))) == ["b1.json"], "another request was answered"
        # Keep b1's request beside its answer. The list must not resurrect it.
        # two requests land in the same tick
        add_request(spool, "d1", "1. delta step", _stamp("hc-D", "d"), created=t + 3)
        add_request(spool, "e1", "1. epsilon step", _stamp("hc-E", "e"), created=t + 4)
        # (curses repaints only what changed: the header's digit, not its
        # words — the two new ROWS are the proof both arrivals appeared)
        expect(m, "4 ■ hc-D · 1 open", "delta step",
               "5 ■ hc-E · 1 open", "epsilon step", fresh=True,
               what="pass 5: two same-tick arrivals both appear")
        os.write(m, b"G")                          # the last one
        drain(m, 0.4)
        os.write(m, b"\r")
        expect(m, "epsilon step", "+3 more in the list", fresh=True,
               what="pass 5: G then Enter opens the last request")
        os.write(m, b"eps\x04")
        e1 = wait_file(os.path.join(spool, "answers", "e1.json"),
                       what="pass 5: the last request is answered")
        assert json.load(open(e1))["text"] == "eps", "e1's answer carries the wrong text"
        assert sorted(os.listdir(os.path.join(spool, "answers"))) == ["b1.json", "e1.json"], \
            "an answer landed under the wrong id"
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: global list grouped three instances; middle answered after ^B kept its draft; same-tick pair both listed")

def pass_idle_selfclose():
    """Pass 6 (hcop-emd): a sidepanel with nothing open closes ITSELF, so the
    pane stops parking over the operator's terminal for the life of the
    session. It must decline nothing on the way out (the SIGHUP path in pass 3
    still does), a panel with work must never idle out, and a GLOBAL panel
    must never self-close at all — it is the operator's own window."""
    # (a) empty scoped panel: it leaves on its own and says why
    tmp = tempfile.mkdtemp(prefix="hc-drive6-")
    spool = os.path.join(tmp, "spool")
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(spool, sub), exist_ok=True)
    p, m = start(spool, {"HC_PANE_IDLE_S": "4"},
                 argv=["--instance", "hc-A", "--spool", spool], spool_via_env=False)
    try:
        expect(m, "waiting for feedback requests",
               what="pass 6: the empty sidepanel came up")
        p.wait(timeout=DEADLINE)
        err = p.stderr.read().decode("utf-8", "replace")
        assert "idle" in err and "closing" in err, \
            "the panel left without saying it was an idle close:\n" + err
        assert os.listdir(os.path.join(spool, "answers")) == [], \
            "an idle close must decline NOTHING — it only fires with nothing open"
        assert "declined" not in err, "the idle path declined something:\n" + err
    finally:
        if p.poll() is None:
            p.terminate(); p.wait(timeout=5)

    # (b) a panel with its instance's work never idles out (its OWN spool: an
    # open request in the empty one would take over pass (c)'s screen)
    stamp = {"session": "hc-A", "pane": "0", "server_uuid": "a" * 32, "attached": True}
    busy = spool_with_request(tempfile.mkdtemp(prefix="hc-drive6b-"), "r6",
                              "Context line.\n1. still here", stamp)
    p, m = start(busy, {"HC_PANE_IDLE_S": "1"},
                 argv=["--instance", "hc-A", "--spool", busy], spool_via_env=False)
    try:
        expect(m, "still here", what="pass 6: the busy sidepanel shows its request")
        deadline = time.time() + 4
        while time.time() < deadline:
            assert p.poll() is None, "a panel with an open request idled out"
            time.sleep(0.2)
    finally:
        p.terminate(); p.wait(timeout=5)

    # (c) a GLOBAL panel is the operator's window: it stays, empty or not
    p, m = start(spool, {"HC_PANE_IDLE_S": "1"})
    try:
        expect(m, "waiting for feedback requests", what="pass 6: the global panel came up")
        deadline = time.time() + 4
        while time.time() < deadline:
            assert p.poll() is None, "a GLOBAL panel self-closed; only scoped panels may"
            time.sleep(0.2)
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: empty sidepanel closed itself declining nothing; a busy one and a global one stayed")

def pass_choice_objection():
    """A typed objection with no navigation never selects the first option."""
    import responder
    tmp = tempfile.mkdtemp(prefix="hc-objection-")
    spool = spool_with_request(tmp, "objection", "Credentials need attention.")
    path = os.path.join(spool, "requests", "objection.json")
    with open(path) as stream:
        request = json.load(stream)
    request["response"] = {"mode": "choice", "options": [
        {"value": "recheck", "label": "Fixed, recheck"},
        {"value": "pause", "label": "Leave blocked"}]}
    responder.stamp_request_contract(request)
    responder.refresh_request_revision(request)
    with open(path, "w") as stream:
        json.dump(request, stream)
    p, m = start(spool, {})
    try:
        expect(m, "1. Fixed, recheck", absent=("▶  1.",),
               what="choice starts with no implicit selection")
        os.write(m, b"\r")
        expect(m, "choose an option or type a reply first", fresh=True)
        answer_path = os.path.join(spool, "answers", "objection.json")
        assert not os.path.exists(answer_path)
        os.write(m, b"I do not know how to fix that\r")
        with open(wait_file(answer_path)) as stream:
            answer = json.load(stream)
        assert "choice" not in answer, answer
        assert answer["text"] == "I do not know how to fix that", answer
        assert answer["response_completion"]["status"] == "not_done", answer
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: typed objection preserved without invented first choice")

def pass_modes():
    """Pass 7 (hcop-0gj): the response contract on a real screen. A CHOICE
    request renders its options as selectable rows (digit 2 selects, Enter
    sends, and the answer carries the exact value); an ACK request confirms
    with a bare ^D and no dialog; a choice-like LEGACY prose request refuses
    an empty ^D — it never silently becomes an acknowledgement — and a typed
    answer goes out as prose."""
    # (a) choice: options drawn, 2 selects, Enter sends the exact value
    tmp = tempfile.mkdtemp(prefix="hc-drive7-")
    spool = spool_with_request(tmp, "r7", "Branch protection needs a paid plan.")
    with open(os.path.join(spool, "requests", "r7.json")) as f:
        body = json.load(f)
    body["response"] = {"mode": "choice", "options": [
        {"value": "free", "label": "Stay free", "hint": "manual rules"},
        {"value": "pro", "label": "Upgrade to Pro", "hint": "$4/mo"},
        {"value": "later", "label": "Decide later", "hint": None}]}
    with open(os.path.join(spool, "requests", "r7.json"), "w") as f:
        json.dump(body, f)
    p, m = start(spool, {})
    try:
        expect(m, "pick ONE option", "1. Stay free — manual rules",
               "2. Upgrade to Pro — $4/mo", "3. Decide later",
               what="pass 7: the options render as labelled rows with their costs")
        os.write(m, b"2")                          # select option two
        expect(m, "▶  2. Upgrade to Pro", fresh=True,
               what="pass 7: digit 2 moves the selection (marked ▶ and inverted)")
        # type the note WITHOUT ^D: ^D submits in this mode. The note line
        # renders one positioned run per keystroke (the editor redraws
        # incrementally), so the render assertion rides the option rows:
        # an arrow press repaints them contiguously, proving keys still move
        # the selection after text entry.
        os.write(m, b"a paid plan then")
        drain(m, 1.0)
        os.write(m, b"\x1b[B")                     # ↓ one option
        expect(m, "▶  3. Decide later", fresh=True,
               what="pass 7: arrows move the selection after typing the note")
        os.write(m, b"\x1b[A")                     # ↑ back to option two
        expect(m, "▶  2. Upgrade to Pro", fresh=True,
               what="pass 7: the selection returns to option two")
        os.write(m, b"\r")                         # Enter sends choice + note
        ans = json.load(open(wait_file(os.path.join(spool, "answers", "r7.json"),
                                       what="pass 7: Enter sends the selected option + note")))
        assert ans["choice"] == {"value": "pro", "label": "Upgrade to Pro"}, \
            f"the wrong option went out: {ans}"
        assert ans["text"] == "a paid plan then" and ans["cancelled"] is False
    finally:
        p.terminate(); p.wait(timeout=5)

    # (b) ack: a bare ^D confirms — no dialog, empty text IS the answer
    tmp = tempfile.mkdtemp(prefix="hc-drive7b-")
    spool = spool_with_request(tmp, "r8", "Confirm the flip.")
    with open(os.path.join(spool, "requests", "r8.json")) as f:
        body = json.load(f)
    body["response"] = {"mode": "ack", "options": []}
    with open(os.path.join(spool, "requests", "r8.json"), "w") as f:
        json.dump(body, f)
    p, m = start(spool, {})
    try:
        expect(m, "^D confirms", what="pass 7: the ack request names its mode")
        os.write(m, b"\x04")
        ans = json.load(open(wait_file(os.path.join(spool, "answers", "r8.json"),
                                       what="pass 7: ^D confirms an ack")))
        assert ans == {"text": "", "cancelled": False, "images": []}, \
            f"an ack must confirm empty, untouched: {ans}"
    finally:
        p.terminate(); p.wait(timeout=5)

    # (c) choice-like LEGACY prose: empty ^D refused, typed answer goes out
    tmp = tempfile.mkdtemp(prefix="hc-drive7c-")
    spool = spool_with_request(
        tmp, "r9", "Upgrade GitHub to Pro? ✔ yes · ✘ stay free, rules stay manual")
    p, m = start(spool, {})
    try:
        expect(m, "✔ yes", "✘ stay free", what="pass 7: the prose is shown as sent")
        os.write(m, b"\x04")
        expect(m, "reads as a decision", fresh=True,
               what="pass 7: an empty ^D on choice-like prose is refused")
        time.sleep(0.3)
        assert not os.listdir(os.path.join(spool, "answers")), \
            "the refused ^D must not have written an answer"
        os.write(m, b"stay free for now\x04")
        ans = json.load(open(wait_file(os.path.join(spool, "answers", "r9.json"),
                                       what="pass 7: the typed answer goes out")))
        assert ans["text"] == "stay free for now" and "choice" not in ans, \
            f"a prose answer must stay prose: {ans}"
    finally:
        p.terminate(); p.wait(timeout=5)
    print("  ok: choice options selectable and sent by value; ack confirmed empty; choice-like prose never an empty ack")

if __name__ == "__main__":
    print("drive_tui (real pty)")
    pass_basic()
    pass_clipboard()
    pass_scoped_close()
    pass_scoped_list()
    pass_global_list()
    pass_idle_selfclose()
    pass_modes()
    pass_choice_objection()
    pass_copy()
    print("all good")
