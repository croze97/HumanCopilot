#!/usr/bin/env python3
"""human-copilot selftest — run from this directory: python3 selftest.py

Covers, in order:
  1. toggle.py round-trip in a throwaway $HOME/CODEX_HOME (both clients'
     registrations + doctrine, byte-identical stash restore, refusal on
     missing markers leaves both registrations UNTOUCHED)
  2. toggle.py's versioned read-only evidence contract, including typed
     absent/unreadable artifacts and a no-mutation filesystem snapshot
  3. server.py end-to-end as a real subprocess over its real stdio protocol:
     initialize / tools/list / tools/call answered from a stub operator,
     image round-trip, cancel, bad-summary isError
  4. the curses-free logic tui.py exposes: spool scan (corrupt files COUNTED),
     answer-file mode 0600, markdown-lite, clipboard attach decisions, the
     multi-line editor

  5. the overlay's default hotkeys against the GNOME / mutter / media-keys
     / other-extension bindings (live gsettings, or the committed snapshot
     under tests/ — the source is printed), failing on any collision

No zellij, no network, no display needed.
"""
import base64
import fcntl
import functools
import io
import json
import os
import queue
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from unittest import mock
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = 0

def ok(cond, what):
    global PASS
    if not cond:
        print(f"FAIL: {what}")
        sys.exit(1)
    PASS += 1
    print(f"  ok: {what}")

class _FakeProbeResult:
    """Stand-in for subprocess.CompletedProcess in overlay_shell probes."""
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr

def _stub_overlay_shell_runner(argv):
    """Deterministic stand-in for toggle._run_probe: ACTIVE, not globally
    disabled. Every suite spool fixture is unrelated to the live desktop's
    GNOME extension state, so build_status() must never shell out for it."""
    if argv[:1] == ["gsettings"]:
        return _FakeProbeResult(0, "false\n")
    if argv[:1] == ["gdbus"]:
        return _FakeProbeResult(0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <true>, 'state': <1.0>},)\n")
    raise AssertionError(f"unexpected probe argv in tests: {argv}")

def write_control_fixture(path, enabled):
    """Write the same private control contract as toggle.py, for subprocesses."""
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    body = {"schema": "human-copilot-control", "schema_version": 1,
            "enabled": bool(enabled),
            "updated_at": "2026-09-04T00:00:00Z"}
    with open(path, "w") as target:
        json.dump(body, target)
    os.chmod(path, 0o600)

def write_enabled_home_fixture(home):
    """A complete isolated ON client setup for positive-path subprocesses."""
    import toggle
    codex = os.path.join(home, ".codex")
    claude = os.path.join(home, ".claude")
    os.makedirs(codex, mode=0o700, exist_ok=True)
    os.makedirs(claude, mode=0o700, exist_ok=True)
    with open(os.path.join(home, ".claude.json"), "w") as target:
        json.dump({"mcpServers": {
            "human-copilot": toggle.default_registration()}}, target)
    with open(os.path.join(codex, "config.toml"), "w") as target:
        target.write(toggle.default_codex_registration())
    doctrine = (toggle.MARK_BEGIN + "\n" + toggle._doctrine_text(True)
                + "\n" + toggle.MARK_END + "\n")
    for path in (os.path.join(claude, "CLAUDE.md"),
                 os.path.join(codex, "AGENTS.md")):
        with open(path, "w") as target:
            target.write(doctrine)
    for path in (os.path.join(home, ".claude.json"),
                 os.path.join(codex, "config.toml"),
                 os.path.join(claude, "CLAUDE.md"),
                 os.path.join(codex, "AGENTS.md")):
        os.chmod(path, 0o600)

def test_handoff_policy():
    """Pin caller guidance without creating a request or using a live surface."""
    import server as hcserver
    docs = {}
    for name in ("doctrine-on.md", "doctrine-off.md", "docs/SAFETY.md"):
        with open(os.path.join(HERE, name), encoding="utf-8") as source:
            docs[name] = source.read()
    docs["TOOL_DESCRIPTION"] = hcserver.TOOL_DESCRIPTION
    for name, body in docs.items():
        plain = " ".join(body.replace("**", "").split()).lower()
        ok(all(phrase in plain for phrase in (
            "prefer autonomous shell/api, text and browser dom checks",
            "concrete autonomous routes tried and why each failed or is unavailable",
            "operator-only fact", "physical action", "required approval")),
           f"{name}: use autonomous tools; a human call needs concrete necessity")
        ok(all(phrase in plain for phrase in (
            "screenshots and pixel capture are disabled by default",
            "the operator may open a temporary allowance in desktop control",
            "screenctx screen_crop/screen_diff may be used and refuse elsewhere",
            "never open, edit or request an allowance",
            "including screenctx capture_policy.py writes",
            "do not solicit snips", "if pixels are needed",
            "inspect window handles, geometry and sizes",
            "button/control metadata and text through accessibility apis (a11y) and browser dom",
            "no computer-vision navigation",
            "record the observation as unverified and rely on normal app use feedback",
            "existing operator-owned snip functionality is preserved for unsolicited attachments")),
           f"{name}: capture needs operator allowance, unsolicited snips preserved, pixels stay unverified")
        ok(all(phrase in plain for phrase in (
            "never hand off to save tokens, time or automation effort",
            "does not require human visual qa",
            "do not proactively request qa, test checklists, final acceptance or visual sign-off",
            "even after all autonomous work is finished",
            "normal app use and unsolicited issue reports",
            "do not ask the operator to test or report back")),
           f"{name}: no proactive QA or sign-off; normal use supplies feedback")
        ok(all(phrase in plain for phrase in (
            "only for an actual execution blocker", "human fact, action or authorization",
            "continue implementation or operation", "run your own tests",
            "physical or unavailable verification in beads",
            "keep unchecked outcomes unverified", "a test you cannot run is not passed",
            "missing verification alone is not an execution blocker",
            "do not turn awaiting_test into an operator assignment")),
           f"{name}: only execution blockers need humans; cannot-run tests stay unverified")
        ok(all(phrase not in plain for phrase in (
            "disproportionate", "no clean programmatic route",
            "use available automation, browser, screenshot",
            "use your own screenshot tools", "ask for a screenshot",
            "computer vision and gui input tools",
            "instead of guessing from computer vision",
            "or synthesizing gui input", "end your turn to involve",
            "any question you would", "ask them anything",
            "before reading more files", "batch the necessary checks",
            "batch those necessary checks", "batching only those necessary checks",
            "batch only the necessary checks", "final qa pass", "human qa comes last",
            "pending manual qa", "no human pass is required when autonomous")),
           f"{name}: no obsolete cost exception, capture invitation or final-human-QA workflow")
        if name not in ("doctrine-on.md", "doctrine-off.md", "TOOL_DESCRIPTION"):
            continue
        ok(all(phrase in plain for phrase in (
            "cannot complete the needed step yourself and need a human",
            "existing authorization", "do not manufacture a decision or approval gate",
            "i don't know, you decide", "routine technical",
            "verify worker results", "unchecked outcomes unverified",
            "never present it as the operator's verdict")),
           f"{name}: own decisions, exhaust work and report evidence honestly")


def test_doctrine_resync():
    """A guidance-only swap preserves surrounding blocks and never flips clients."""
    from unittest.mock import patch
    import toggle
    with tempfile.TemporaryDirectory(prefix="hc-doctrine-") as fixture:
        paths = [os.path.join(fixture, name) for name in
                 ("CLAUDE.md", "AGENTS.md", "AGENTS.override.md")]
        prefix = "# Concurrent parent guidance\nUse authorized tools.\n\n"
        suffix = "\n<!-- another:block -->\nKeep this byte for byte.\n"
        for on in (False, True):
            before = (prefix + toggle.MARK_BEGIN + "\nold guidance\n"
                      + toggle.MARK_END + suffix)
            for path in paths:
                with open(path, "w") as target:
                    target.write(before)
                os.chmod(path, 0o600)
            with patch.object(toggle, "doctrine_path", return_value=paths[0]), \
                 patch.object(toggle, "codex_doctrine_path", return_value=paths[1]), \
                 patch.object(toggle, "codex_override_path", return_value=paths[2]), \
                 patch.object(toggle, "_client_changes", side_effect=AssertionError("client mutation")), \
                 patch.object(toggle, "set_enabled", side_effect=AssertionError("toggle mutation")):
                toggle._swap_doctrine(on)
                expected = (prefix + toggle.MARK_BEGIN + "\n"
                            + toggle._doctrine_text(on) + "\n"
                            + toggle.MARK_END + suffix)
                for path in paths:
                    with open(path) as source:
                        ok(source.read() == expected
                           and stat.S_IMODE(os.stat(path).st_mode) == 0o600,
                           f"doctrine {'on' if on else 'off'}: {os.path.basename(path)} preserves outside text and mode")
                ok(sorted(os.listdir(fixture)) == sorted(os.path.basename(p) for p in paths),
                   "doctrine-only resync creates no config, control or registration files")


# ------------------------------------------------------------------ 1. toggle

def test_toggle():
    print("toggle.py")
    import toggle
    home = tempfile.mkdtemp(prefix="hc-home-")
    old_home = os.environ.get("HOME")
    old_codex_home = os.environ.get("CODEX_HOME")
    old_spool = os.environ.get("HC_SPOOL")
    old_control = os.environ.pop("HC_CONTROL_FILE", None)
    os.environ["HOME"] = home
    os.environ["CODEX_HOME"] = os.path.join(home, ".codex")
    os.environ["HC_SPOOL"] = os.path.join(home, "runtime-spool")
    try:
        os.makedirs(os.path.join(home, ".claude"))
        os.makedirs(os.path.join(home, ".codex"))
        # `timeout` is in here on purpose: the live registration carries one
        # (the client's 1800s idle abort), and the off/on flip must hand it
        # back untouched rather than flattening the entry to the default
        entry = {"type": "stdio", "command": "/somewhere/custom", "args": ["x"],
                 "env": {"K": "v"}, "timeout": 3600000}
        cfg = {"mcpServers": {"human-copilot": entry, "other": {"type": "stdio"}},
               "unrelated": 42}
        with open(os.path.join(home, ".claude.json"), "w") as f:
            json.dump(cfg, f)
        doc = ("# head\n\nkeep me\n\n" + toggle.MARK_BEGIN
               + "\nold body\n" + toggle.MARK_END + "\n\n# tail\nalso kept\n")
        with open(os.path.join(home, ".claude", "CLAUDE.md"), "w") as f:
            f.write(doc)

        codex_registration = (
            "[mcp_servers.human-copilot]\n"
            'command = "/somewhere/codex-custom"\n'
            'args = ["x"]\n'
            'env = { K = "v" }\n'
            "enabled = true\n"
            "tool_timeout_sec = 7200\n\n")
        codex_tools = (
            "[mcp_servers.human-copilot.tools.interactive_feedback]\n"
            'approval_mode = "approve"\n\n')
        codex_cfg = (
            "model = \"test-model\"\n\n"
            "[mcp_servers.other]\n"
            'command = "/somewhere/else"\n\n'
            + codex_registration + codex_tools
            + "[unrelated]\nanswer = 42\n")
        with open(os.path.join(home, ".codex", "config.toml"), "w") as f:
            f.write(codex_cfg)
        codex_doc = "# codex head\n\nkeep me too\n"
        with open(os.path.join(home, ".codex", "AGENTS.md"), "w") as f:
            f.write(codex_doc)

        # A pre-existing Codex registration without managed instructions is a
        # visible partial state. Enabling repairs it by appending the block.
        st = toggle.state()
        ok(st["schema"] == "human-copilot-status"
           and st["schema_version"] == 1 and st["read_only"] is True
           and set(st["evidence"]) == {
               "overlay", "rollback_tool", "tuikit", "tui_lock", "instances",
               "overlay_runtime", "tui_runtime", "overlay_shell"},
           "state exposes the versioned read-only evidence envelope (v1 four + additive instance/render evidence)")
        ok(not st["registered"] and st["registrations_ready"]
           and not st["enabled"] and not st["consistent"]
           and st["capability"]["state"] == "absent"
           and st["clients"]["codex"]["doctrine"] == "unmarked",
           "absent-control upgrade state derives from registrations plus doctrines and exposes drift")
        r = toggle.set_enabled(True)
        ok(r["registered"] and r["enabled"] and r["consistent"]
           and r["doctrine"] == "on"
           and r["capability"]["state"] == "explicit",
           "enable integrates both clients")
        ok("restart any Codex session" in r["note"]
           and r["client_refresh"]["codex"]["runtime_reload_supported"]
           is False,
           "enable reports the exact existing-Codex restart boundary")
        ok(stat.S_IMODE(os.stat(toggle.control_path()).st_mode) == 0o600,
           "the persistent capability authority is private from its first visible byte")
        codex_body = open(os.path.join(home, ".codex", "AGENTS.md")).read()
        ok(codex_body.startswith(codex_doc) and "DISABLED" not in codex_body
           and toggle.MARK_BEGIN in codex_body,
           "Codex doctrine installed without losing context")
        disabled = codex_registration.replace("enabled = true", "enabled = false") \
            .replace("tool_timeout_sec = 7200", "tool_timeout_sec = 60")
        repaired = tomllib.loads(toggle._enable_codex_registration(disabled))
        ok(repaired["mcp_servers"]["human-copilot"]["enabled"] is True
           and repaired["mcp_servers"]["human-copilot"]["tool_timeout_sec"]
           == 3600
           and repaired["mcp_servers"]["human-copilot"]["tools"]
           ["interactive_feedback"]["approval_mode"] == "approve",
           "enable repairs disabled, short-timeout, or unapproved Codex entry")

        r = toggle.set_enabled(False)
        ok(not r["registered"] and not r["enabled"] and r["consistent"]
           and r["runtime_health"] == "stopped",
           "off revokes the runtime and deregisters both clients")
        cur = json.load(open(os.path.join(home, ".claude.json")))
        ok("human-copilot" not in cur["mcpServers"], "entry removed")
        ok(cur["mcpServers"]["other"] == {"type": "stdio"}
           and cur["unrelated"] == 42, "unrelated config untouched")
        ok(json.load(open(toggle.stash_path())) == entry, "stash holds the entry")
        body = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
        ok("DISABLED" in body and body.startswith("# head")
           and body.rstrip().endswith("also kept"), "doctrine off, context kept")
        codex_path = os.path.join(home, ".codex", "config.toml")
        codex_cur = tomllib.loads(open(codex_path).read())
        ok("human-copilot" not in codex_cur["mcp_servers"]
           and codex_cur["mcp_servers"]["other"]["command"] == "/somewhere/else"
           and codex_cur["unrelated"]["answer"] == 42,
           "Codex entry removed, unrelated TOML untouched")
        ok(open(toggle.codex_stash_path()).read()
           == codex_registration + codex_tools,
           "Codex stash holds the exact root and tool tables")
        codex_body = open(os.path.join(home, ".codex", "AGENTS.md")).read()
        ok("DISABLED" in codex_body and codex_body.startswith(codex_doc),
           "Codex doctrine off, context kept")
        ok(toggle.state()["doctrine"] == "off", "both doctrines read off")

        control_before = open(toggle.control_path()).read()
        toggle.set_enabled(False)                       # idempotent
        ok(toggle.state()["registered"] is False
           and open(toggle.control_path()).read() == control_before,
           "double-off is an idempotent disabled generation")

        r = toggle.set_enabled(True)
        cur = json.load(open(os.path.join(home, ".claude.json")))
        ok(cur["mcpServers"]["human-copilot"] == entry,
           "on restores the stashed entry exactly")
        codex_text = open(codex_path).read()
        codex_cur = tomllib.loads(codex_text)
        ok(codex_cur["mcp_servers"]["human-copilot"]["command"]
           == "/somewhere/codex-custom"
           and codex_cur["mcp_servers"]["human-copilot"]["env"] == {"K": "v"}
           and codex_cur["mcp_servers"]["human-copilot"]["tool_timeout_sec"]
           == 7200 and codex_text.endswith(codex_registration + codex_tools),
           "on restores the exact custom Codex registration")
        ok(toggle.state()["doctrine"] == "on", "both doctrines back on")

        # The control record cannot force an ON over drift introduced outside
        # the toggle transaction.  Existing servers must fail closed and Hub
        # must display the effective OFF verdict until the surfaces are fixed.
        codex_on_doc = open(os.path.join(home, ".codex", "AGENTS.md")).read()
        with open(os.path.join(home, ".codex", "AGENTS.md"), "w") as f:
            f.write(codex_doc)
        drifted_on = toggle.state()
        ok(not drifted_on["enabled"] and not drifted_on["registered"]
           and drifted_on["registrations_ready"]
           and drifted_on["capability"]["requested_enabled"] is True
           and not drifted_on["consistent"],
           "explicit ON fails closed if a client doctrine drifts afterward")
        with open(os.path.join(home, ".codex", "AGENTS.md"), "w") as f:
            f.write(codex_on_doc)
        ok(toggle.state()["enabled"] and toggle.state()["consistent"],
           "restoring all ON surfaces restores the effective capability")

        # With no stash, both clients get the canonical stdio command; Codex
        # also gets an hour client timeout (its default would make the
        # operator lane unusable).
        toggle.set_enabled(False)
        os.unlink(toggle.stash_path())
        os.unlink(toggle.codex_stash_path())
        toggle.set_enabled(True)
        codex_cur = tomllib.loads(open(codex_path).read())
        canonical = codex_cur["mcp_servers"]["human-copilot"]
        ok(canonical["command"] == os.path.join(HERE, "server.py")
           and len(canonical["args"]) == 1
           and canonical["args"][0].startswith(
               toggle.CONNECTION_GENERATION_PREFIX)
           and canonical["tool_timeout_sec"] == 3600
           and canonical["tools"]["interactive_feedback"]["approval_mode"]
           == "approve",
           "canonical Codex registration is usable and carries a reconnect generation")
        first_generation = canonical["args"][0]
        first_codex_text = open(codex_path).read()
        toggle.set_enabled(True)
        ok(open(codex_path).read() == first_codex_text,
           "idempotent canonical ON does not churn its connection generation")
        toggle.set_enabled(False)
        reenabled = toggle.set_enabled(True)
        rotated = tomllib.loads(open(codex_path).read())["mcp_servers"] \
            ["human-copilot"]["args"]
        ok(len(rotated) == 1 and rotated[0] != first_generation
           and rotated[0].startswith(toggle.CONNECTION_GENERATION_PREFIX),
           "a real canonical OFF/ON rotates the Codex connection identity")
        claude_rotated = json.load(open(os.path.join(home, ".claude.json"))) \
            ["mcpServers"]["human-copilot"]["args"]
        ok(len(claude_rotated) == 1
           and claude_rotated[0] == rotated[0],
           "Claude and Codex receive the same reconnect generation")
        generation = rotated[0][len(toggle.CONNECTION_GENERATION_PREFIX):]
        ok(reenabled["client_refresh"]["state"] == "new-sessions-ready"
           and reenabled["client_refresh"]["connection_generation"]
           == generation
           and reenabled["client_refresh"]["codex"]["required_boundary"]
           == "restart-codex-session"
           and "restart any Codex session" in reenabled["note"],
           "status binds the active generation to the required Codex restart")

        # A failure after OFF has published its authority rolls the client
        # files back but deliberately cannot resurrect resident capability.
        before_cfg = open(os.path.join(home, ".claude.json")).read()
        before_doc = open(os.path.join(home, ".claude", "CLAUDE.md")).read()
        before_codex = open(codex_path).read()
        real_atomic = toggle._atomic_write

        def fail_claude_config(path, text, mode=None):
            if path == toggle.claude_json_path():
                raise OSError("stubbed config write failure")
            return real_atomic(path, text, mode)

        toggle._atomic_write = fail_claude_config
        try:
            try:
                toggle.set_enabled(False)
                ok(False, "a client write failure must refuse the transition")
            except ValueError as error:
                ok("remains runtime-disabled" in str(error),
                   "failed OFF diagnoses its fail-closed state")
        finally:
            toggle._atomic_write = real_atomic
        failed = toggle.state()
        ok(not failed["enabled"] and not failed["registered"]
           and failed["registrations_ready"] and not failed["consistent"]
           and failed["capability"]["state"] == "explicit",
           "failed OFF stays revoked and reports registration/doctrine drift")
        ok(open(os.path.join(home, ".claude.json")).read() == before_cfg
           and open(os.path.join(home, ".claude", "CLAUDE.md")).read() == before_doc
           and open(codex_path).read() == before_codex,
           "failed OFF rolls every fallible client/doctrine write back")
        toggle.set_enabled(True)

        # refusal order: no markers -> ValueError AND registration untouched
        with open(os.path.join(home, ".claude", "CLAUDE.md"), "w") as f:
            f.write("# no markers here\n")
        try:
            toggle.set_enabled(False)
            ok(False, "missing markers must refuse")
        except ValueError:
            pass
        cur = json.load(open(os.path.join(home, ".claude.json")))
        ok("human-copilot" in cur["mcpServers"],
           "refusal left Claude registration untouched (doctrine-first order)")
        codex_cur = tomllib.loads(open(codex_path).read())
        ok("human-copilot" in codex_cur["mcp_servers"],
           "refusal left Codex registration untouched (doctrine-first order)")
        refused = toggle.state()
        ok(not refused["enabled"] and not refused["registered"]
           and refused["registrations_ready"] and not refused["consistent"],
           "even a doctrine-preflight refusal leaves runtime authority fail-closed")
    finally:
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex_home
        if old_spool is None:
            os.environ.pop("HC_SPOOL", None)
        else:
            os.environ["HC_SPOOL"] = old_spool
        if old_control is not None:
            os.environ["HC_CONTROL_FILE"] = old_control
        shutil.rmtree(home, ignore_errors=True)

def test_capability_revocation():
    print("toggle/server immediate capability revocation")
    import toggle

    root = tempfile.mkdtemp(prefix="hc-revoke-")
    home = os.path.join(root, "home")
    spool = os.path.join(root, "spool")
    bindir = os.path.join(root, "bin")
    notify_log = os.path.join(root, "notify.log")
    terminal_log = os.path.join(root, "terminal.log")
    os.makedirs(os.path.join(home, ".claude"), mode=0o700)
    os.makedirs(os.path.join(home, ".codex"), mode=0o700)
    os.makedirs(bindir, mode=0o700)
    old = {key: os.environ.get(key) for key in
           ("HOME", "CODEX_HOME", "HC_SPOOL", "HC_CONTROL_FILE")}
    proc = None
    proc2 = None
    try:
        os.environ.update({"HOME": home,
                           "CODEX_HOME": os.path.join(home, ".codex"),
                           "HC_SPOOL": spool})
        os.environ.pop("HC_CONTROL_FILE", None)
        with open(os.path.join(home, ".claude.json"), "w") as target:
            json.dump({"mcpServers": {
                "human-copilot": toggle.default_registration()}}, target)
        on_doc = (toggle.MARK_BEGIN + "\n" + toggle._doctrine_text(True)
                  + "\n" + toggle.MARK_END + "\n")
        for path in (os.path.join(home, ".claude", "CLAUDE.md"),
                     os.path.join(home, ".codex", "AGENTS.md")):
            with open(path, "w") as target:
                target.write(on_doc)
        with open(os.path.join(home, ".codex", "config.toml"), "w") as target:
            target.write(toggle.default_codex_registration())

        # No control.json is an upgrade state, not implicit ON: all four old
        # surfaces must agree. This fixture does, then set_enabled seeds the
        # explicit generation a resident process watches.
        ok(toggle.capability_enabled() is True,
           "absent control derives enabled only from the coherent legacy ON state")
        toggle.set_enabled(True)

        with open(os.path.join(bindir, "notify-send"), "w") as target:
            target.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HC_NOTIFY_LOG\"\n")
        with open(os.path.join(bindir, "fake-term"), "w") as target:
            target.write("#!/bin/sh\nprintf '%s\\n' \"$*\" >> \"$HC_TERMINAL_LOG\"\n")
        for name in ("notify-send", "fake-term"):
            os.chmod(os.path.join(bindir, name), 0o755)
        env = {key: value for key, value in os.environ.items()
               if not key.startswith("ZELLIJ")}
        env.update({"PATH": bindir + ":" + os.environ["PATH"],
                    "HC_TERMINAL": os.path.join(bindir, "fake-term"),
                    "HC_NOTIFY_LOG": notify_log,
                    "HC_TERMINAL_LOG": terminal_log})

        def start_server():
            process = subprocess.Popen(
                [sys.executable, os.path.join(HERE, "server.py")],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, text=True, env=env)
            messages = queue.Queue()
            threading.Thread(
                target=lambda: [messages.put(json.loads(line))
                                for line in process.stdout if line.strip()],
                daemon=True).start()
            return process, messages

        def take_id(messages, wanted, timeout=5):
            end = time.time() + timeout
            seen = []
            while time.time() < end:
                try:
                    message = messages.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                seen.append(message)
                if message.get("id") == wanted:
                    return message, seen
            raise AssertionError(f"no response for id {wanted}; saw {seen}")

        def take_list_changed(messages, timeout=3):
            end = time.time() + timeout
            seen = []
            while time.time() < end:
                try:
                    message = messages.get(timeout=max(0.01, end - time.time()))
                except queue.Empty:
                    break
                seen.append(message)
                if message.get("method") == "notifications/tools/list_changed":
                    return message
            raise AssertionError(f"no tools/list_changed notification; saw {seen}")

        proc, messages = start_server()
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {},
                               "clientInfo": {"name": "revocation-test"}}})
        initialized, _ = take_id(messages, 1)
        ok(initialized["result"]["capabilities"]["tools"]["listChanged"] is True,
           "initialize advertises MCP tools.listChanged support")
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed, _ = take_id(messages, 2)
        ok([tool["name"] for tool in listed["result"]["tools"]]
           == ["interactive_feedback"],
           "a resident ON server exposes interactive_feedback once")

        # Exercise the actual direct CLI boundary while the same server stays
        # connected. Its isolated HOME/spool prevents any live mutation.
        disabled_cli = subprocess.run(
            [sys.executable, os.path.join(HERE, "toggle.py"), "off"],
            env=env, capture_output=True, text=True, timeout=10)
        disabled_state = json.loads(disabled_cli.stdout)
        ok(disabled_cli.returncode == 0 and not disabled_state["enabled"]
           and not disabled_state["registered"],
           "copilot-toggle off publishes one coherent disabled state")
        take_list_changed(messages)
        _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/list"})
        listed_off, _ = take_id(messages, 3)
        ok(listed_off["result"]["tools"] == [],
           "the still-connected server removes the tool from tools/list")
        _rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "must not appear"}}})
        stale, _ = take_id(messages, 4)
        detail = stale["result"].get("structuredContent") or {}
        ok(stale["result"]["isError"] is True
           and detail.get("code") == "human-copilot-disabled"
           and detail.get("enabled") is False,
           "a stale call gets an immediate structured disabled result")
        ok(not os.path.exists(spool)
           and not os.path.exists(notify_log)
           and not os.path.exists(terminal_log),
           "disabled list/call creates no spool, pane, terminal, or arrival notification")

        toggle.set_enabled(True)
        take_list_changed(messages)
        _rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/list"})
        relisted, _ = take_id(messages, 5)
        ok(len(relisted["result"]["tools"]) == 1,
           "re-enable restores exactly one tool on the resident connection")
        toggle.set_enabled(True)
        try:
            duplicate = messages.get(timeout=toggle.CONTROL_SCHEMA_VERSION * 0.35)
        except queue.Empty:
            duplicate = None
        ok(not duplicate or duplicate.get("method") !=
           "notifications/tools/list_changed",
           "idempotent ON emits no duplicate list-changed generation")

        # An already-waiting call is withdrawn on OFF and cannot drift into an
        # expiry result. This request legitimately began while enabled.
        _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "Context.\n1. Look",
                                             "timeout": 30}}})
        request_dir = os.path.join(spool, "requests")
        for _ in range(200):
            if os.path.isdir(request_dir) and any(
                    name.endswith(".json") for name in os.listdir(request_dir)):
                break
            time.sleep(0.01)
        else:
            raise AssertionError("enabled call never reached isolated spool")
        toggle.set_enabled(False)
        revoked, seen = take_id(messages, 6)
        revoked_detail = revoked["result"].get("structuredContent") or {}
        ok(revoked["result"]["isError"] is True
           and revoked_detail.get("reason") == "revoked-while-waiting",
           "OFF withdraws an in-flight wait immediately instead of expiring")
        ok(not [name for name in os.listdir(request_dir)
                if name.endswith(".json") or name.endswith(".arrival-nudge")]
           and not os.listdir(os.path.join(spool, "answers")),
           "revocation cleans request, answer, and arrival sidecars")
        proc.stdin.close()
        proc.wait(timeout=5)
        proc = None

        # Exact launch-time upgrade edge from the live incident: OFF happened
        # before control.json existed. Config registrations and doctrines are
        # already OFF; a fresh server must derive OFF rather than default ON.
        os.unlink(toggle.control_path())
        ok(toggle.capability_enabled() is False
           and toggle.state()["capability"]["state"] == "absent",
           "absent control plus effective legacy OFF derives fail-closed")
        before_notify = (open(notify_log).read() if os.path.exists(notify_log)
                         else "")
        proc2, messages2 = start_server()
        _rpc(proc2, {"jsonrpc": "2.0", "id": 10, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18",
                                "clientInfo": {"name": "upgrade-test"}}})
        take_id(messages2, 10)
        _rpc(proc2, {"jsonrpc": "2.0", "id": 11, "method": "tools/list"})
        absent_list, _ = take_id(messages2, 11)
        _rpc(proc2, {"jsonrpc": "2.0", "id": 12, "method": "tools/call",
                     "params": {"name": "interactive_feedback",
                                "arguments": {"summary": "must stay hidden"}}})
        absent_call, _ = take_id(messages2, 12)
        after_notify = (open(notify_log).read() if os.path.exists(notify_log)
                        else "")
        ok(absent_list["result"]["tools"] == []
           and absent_call["result"]["structuredContent"]["code"]
           == "human-copilot-disabled" and before_notify == after_notify,
           "fresh server honors pre-control OFF with no operator notification")
        proc2.stdin.close()
        proc2.wait(timeout=5)
        proc2 = None

        # PID delivery is exact-path and same-owner, never a substring pgrep.
        fake_proc = os.path.join(root, "proc")
        os.makedirs(fake_proc)
        wanted = os.path.realpath(os.path.join(HERE, "server.py"))
        for pid, argv in ((101, [sys.executable, "-u", wanted]),
                          (102, ["/bin/bash", "-c", f"echo {wanted}"]),
                          (103, [sys.executable, "/elsewhere/server.py"]),
                          (104, ["/usr/bin/vim", wanted]),
                          (105, [sys.executable, "-c", "print('x')", wanted]),
                          (106, [sys.executable, "/elsewhere/tool.py", wanted])):
            base = os.path.join(fake_proc, str(pid))
            os.makedirs(base)
            with open(os.path.join(base, "cmdline"), "wb") as target:
                target.write(b"\0".join(os.fsencode(arg) for arg in argv) + b"\0")
            os.symlink(HERE, os.path.join(base, "cwd"))
        delivered = []
        wake = toggle._wake_resident_servers(
            fake_proc, kill=lambda pid, sig: delivered.append((pid, sig)))
        ok(wake["matched"] == wake["signalled"] == 1
           and delivered == [(101, toggle.CONTROL_SIGNAL)],
           "server wake validates the exact Python script operand, never an editor, later argument, or substring")

        # A file appearing after the first directory snapshot is caught by a
        # later bounded purge pass; unrelated names remain untouched.
        purge_spool = os.path.join(root, "purge")
        for dirname in (purge_spool, os.path.join(purge_spool, "requests"),
                        os.path.join(purge_spool, "answers")):
            os.makedirs(dirname, mode=0o700, exist_ok=True)
            os.chmod(dirname, 0o700)
        first = os.path.join(purge_spool, "requests", "aaaaaaaaaaaa.json")
        late = os.path.join(purge_spool, "requests", "bbbbbbbbbbbb.json")
        answer = os.path.join(purge_spool, "answers", "aaaaaaaaaaaa.json")
        keep = os.path.join(purge_spool, "requests", "operator-notes.json")
        for path in (first, answer, keep):
            with open(path, "w") as target:
                target.write("{}")
        real_scandir = toggle.os.scandir
        injected = [False]

        def racing_scandir(path):
            entries = list(real_scandir(path))
            if path == os.path.join(purge_spool, "requests") and not injected[0]:
                injected[0] = True
                with open(late, "w") as target:
                    target.write("{}")
            return iter(entries)

        toggle.os.scandir = racing_scandir
        try:
            purge = toggle._purge_pending(purge_spool)
        finally:
            toggle.os.scandir = real_scandir
        ok(purge["state"] == "purged" and purge["removed"] == 3
           and purge["remaining"] == 0 and os.path.exists(keep)
           and not os.path.exists(first) and not os.path.exists(late)
           and not os.path.exists(answer),
           "bounded purge catches a publication race and preserves unrelated files")
    finally:
        for process in (proc, proc2):
            if process is not None:
                try:
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    process.kill()
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(root, ignore_errors=True)

# --------------------------------------------------------- 2. status contract

def test_symlink_here():
    """~/bin/copilot-toggle, copilot-tui and copilot-open are symlinks into
    this tree. HERE must resolve the link target: abspath() left the probe
    looking for overlay/ and tuikit.py beside the SYMLINK, so a `state` run
    through ~/bin reported the overlay absent and the lane not-installed
    while the same probe run from the tree said installed-current."""
    print("HERE resolves through a symlink")
    import importlib.util
    temporary = tempfile.mkdtemp(prefix="hc-symlink-")
    old_env = {key: os.environ.get(key) for key in (
        "HOME", "CODEX_HOME", "HC_CONTROL_FILE", "HC_SPOOL",
        "XDG_RUNTIME_DIR", "PATH")}
    try:
        for name in ("toggle", "server", "tui", "palette"):
            link = os.path.join(temporary, name + "-link.py")
            os.symlink(os.path.join(HERE, name + ".py"), link)
            spec = importlib.util.spec_from_file_location(
                "hc_symlink_" + name, link)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            ok(module.HERE == os.path.realpath(HERE),
               f"{name}.HERE resolves the symlink to the tree")
        import toggle
        home = os.path.join(temporary, "home")
        runtime = os.path.join(temporary, "runtime")
        spool = os.path.join(temporary, "spool")
        control = os.path.join(temporary, "control", "control.json")
        commands = os.path.join(temporary, "commands")
        probe_log = os.path.join(temporary, "probe.log")
        os.makedirs(home, mode=0o700)
        os.makedirs(runtime, mode=0o700)
        os.makedirs(os.path.join(spool, "requests"), mode=0o700)
        os.makedirs(os.path.join(spool, "answers"), mode=0o700)
        os.makedirs(commands, mode=0o700)
        for name, output in (("gsettings", "false\n"),
                             ("gdbus", "({'uuid': <'copilot-overlay@croze'>, 'enabled': <true>, 'state': <1.0>},)\n")):
            command = os.path.join(commands, name)
            with open(command, "w") as stub:
                stub.write("#!/bin/sh\n")
                stub.write("printf '%s\\n' %s >> %s\n" %
                           (name, shlex.quote(name), shlex.quote(probe_log)))
                stub.write("printf %s\n" % shlex.quote(output))
            os.chmod(command, 0o700)
        installed_parent = os.path.join(
            home, ".local", "share", "gnome-shell", "extensions")
        os.makedirs(installed_parent, mode=0o700)
        os.symlink(os.path.join(HERE, "overlay", toggle.OVERLAY_UUID),
                   os.path.join(installed_parent, toggle.OVERLAY_UUID))
        write_control_fixture(control, True)
        os.environ.update({"HOME": home, "CODEX_HOME": os.path.join(home, ".codex"),
                           "HC_CONTROL_FILE": control, "HC_SPOOL": spool,
                           "XDG_RUNTIME_DIR": runtime,
                           "PATH": commands + os.pathsep + (old_env["PATH"] or "")})
        link = os.path.join(temporary, "copilot-toggle")
        os.symlink(os.path.join(HERE, "toggle.py"), link)
        out = subprocess.run([sys.executable, link, "state"], cwd=temporary,
                             capture_output=True, text=True, timeout=60)
        ok(out.returncode == 0, "state runs through a symlink")
        status = json.loads(out.stdout)
        ok(status["evidence"]["overlay"]["state"] == "current"
           and status["deployment_status"] == "installed-current",
           "symlinked state proves the source overlay and installed deployment are current")
        ok(status["evidence"]["overlay_shell"]["state"] == "current"
           and status["evidence"]["overlay_shell"]["enabled"] is True
           and status["evidence"]["overlay_shell"]["user_extensions_disabled"] is False,
           "child state uses the fixture GNOME command evidence")
        with open(probe_log) as source:
            probes = source.read().splitlines()
        ok(probes == ["gsettings", "gdbus"],
           "child state invokes both fixture GNOME commands")
        direct = toggle.build_status()
        ok(status["evidence"]["overlay"]["state"]
           == direct["evidence"]["overlay"]["state"]
           and status["evidence"]["tuikit"]["state"]
           == direct["evidence"]["tuikit"]["state"]
           and status["deployment_status"] == direct["deployment_status"],
           "symlinked state preserves direct in-tree probe equivalence")
    finally:
        for key, value in old_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(temporary, ignore_errors=True)


def test_status_contract():
    print("toggle.py read-only status contract")
    import toggle

    temporary = tempfile.mkdtemp(prefix="hc-status-")
    try:
        root = os.path.join(temporary, "human-copilot")
        source_overlay = os.path.join(root, "overlay", toggle.OVERLAY_UUID)
        home = os.path.join(temporary, "home")
        installed_overlay = os.path.join(
            home, ".local", "share", "gnome-shell", "extensions",
            toggle.OVERLAY_UUID)
        rollback = os.path.join(
            home, ".local", "share", "uv", "tools",
            "mcp-feedback-enhanced")
        canonical = os.path.join(temporary, "zellij-hub", "tuikit.py")
        spool = os.path.join(temporary, "runtime", "human-copilot")

        def write(path, body):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            mode = "wb" if isinstance(body, bytes) else "w"
            with open(path, mode) as target:
                target.write(body)

        manifest = {
            "uuid": toggle.OVERLAY_UUID,
            "name": "fixture",
            "description": "status-fixture-secret",
            "version": 1,
            "shell-version": ["50"],
        }
        overlay_bodies = {
            "extension.js": "extension fixture\n",
            "lib.js": "library fixture\n",
            "metadata.json": json.dumps(manifest),
            "schemas/org.gnome.shell.extensions.copilot-overlay.gschema.xml":
                "<schemalist/>\n",
            "schemas/gschemas.compiled": b"compiled\x00fixture",
        }
        for relative, body in overlay_bodies.items():
            write(os.path.join(source_overlay, *relative.split("/")), body)
        os.makedirs(os.path.dirname(installed_overlay), exist_ok=True)
        os.symlink(source_overlay, installed_overlay)

        local_tuikit = os.path.join(root, "tuikit.py")
        write(local_tuikit, "same vendored bytes\n")
        write(canonical, "same vendored bytes\n")
        dist = os.path.join(
            rollback, "lib", "python3.13", "site-packages",
            "mcp_feedback_enhanced-2.6.2.dist-info")
        write(os.path.join(rollback, "uv-receipt.toml"),
              "fixture_token = 'rollback-fixture-secret'\n")
        write(os.path.join(rollback, "bin", "mcp-feedback-enhanced"),
              "#!/bin/sh\n")
        write(os.path.join(rollback, "bin", "interactive-feedback-mcp"),
              "#!/bin/sh\n")
        write(os.path.join(dist, "METADATA"),
              "Name: mcp-feedback-enhanced\nVersion: 2.6.2\n")
        write(os.path.join(dist, "RECORD"), "fixture,sha256=abc,1\n")
        write(os.path.join(spool, "tui.lock"), b"")
        write(os.path.join(spool, "overlay-runtime.json"), json.dumps({
            "schema": "human-copilot-overlay-runtime", "schema_version": 1,
            "state": "healthy",
            "observed": datetime(2026, 9, 2, 12, tzinfo=timezone.utc).timestamp(),
            "pending_requests": 0, "rendered_cards": 0,
            "answer_filter_version": 1,
            "window_binding_version": 1,
        }))
        write(os.path.join(spool, "tui-runtime.json"), json.dumps({
            "schema": "human-copilot-tui-runtime", "schema_version": 1,
            "state": "healthy",
            "observed": datetime(2026, 9, 2, 12, tzinfo=timezone.utc).timestamp(),
            "instance": None,
            "pid": os.getpid(),
        }))
        # hcop-0p7.6: the colour registry as palette.py writes it — one
        # leased holder whose lock is HELD here, one whose server died (lock
        # file present, unheld), one whose lock file is gone, one provisional
        # (a surface's lookup, never a server) and a wrapped slot.
        write(os.path.join(spool, "instances", "hc-A", "tui.lock"), b"")
        write(os.path.join(spool, "instances", "hc-A", "delivery.json"),
              json.dumps({"schema": "human-copilot-delivery", "schema_version": 1,
                          "observed": 1.0, "state": "degraded",
                          "surface": "global-fallback",
                          "reason": "pane-spawn-failed"}))
        write(os.path.join(spool, "palette-holders", "u-alive.lock"), b"")
        write(os.path.join(spool, "palette-holders", "u-dead.lock"), b"")
        write(os.path.join(spool, "palette.json"), json.dumps({
            "version": 1, "sessions": {
                "hc-A": {"slot": 1, "since": 1.0,
                         "holders": {"u-alive": {"since": 1.0, "leased": True}}},
                "hc-B": {"slot": 2, "since": 2.0,
                         "holders": {"u-dead": {"since": 2.0, "leased": True}}},
                "hc-C": {"slot": 10, "since": 3.0,
                         "holders": {"u-gone": {"since": 3.0, "leased": True}}},
                "hc-P": {"slot": 3, "since": 4.0,
                         "holders": {"u-prov": {"since": 4.0, "leased": False}}},
            }}))
        held_fds = []
        for rel in (("palette-holders", "u-alive.lock"), ("instances", "hc-A", "tui.lock")):
            fd = os.open(os.path.join(spool, *rel), os.O_RDWR)
            fcntl.flock(fd, fcntl.LOCK_SH)
            held_fds.append(fd)
        # A renderer can be alive enough to hold its process lock and still
        # throw before constructing a single card. This is the exact outage
        # hcop-ea1 must expose as unhealthy, never "healthy" by lock alone.
        render_spool = os.path.join(temporary, "render-failure-spool")
        write(os.path.join(render_spool, "tui.lock"), b"")
        write(os.path.join(render_spool, "requests", "missed.json"), "{}")
        write(os.path.join(render_spool, "overlay-runtime.json"), json.dumps({
            "schema": "human-copilot-overlay-runtime", "schema_version": 1,
            "state": "error",
            "observed": datetime(2026, 9, 2, 12, tzinfo=timezone.utc).timestamp(),
            "pending_requests": 1, "rendered_cards": 0,
            "error": {"name": "TypeError", "message": "fixture"},
        }))
        render_fd = os.open(os.path.join(render_spool, "tui.lock"), os.O_RDWR)
        fcntl.flock(render_fd, fcntl.LOCK_SH)
        held_fds.append(render_fd)
        # hcop-m8n: complete answers are not actionable prompts. Status keeps
        # the v1 total while classifying a live caller, a conclusively dead
        # caller and legacy ambiguity separately.
        import server as hcserver
        queue_spool = os.path.join(temporary, "answered-queue-spool")
        live_owner = hcserver.caller_process_identity()
        dead_owner = {**live_owner, "boot_id": "definitely-an-older-boot"}
        for rid, owner in (("live", live_owner), ("dead", dead_owner),
                           ("legacy", None), ("pending", live_owner)):
            req = {"id": rid, "summary": "fixture request"}
            if owner is not None:
                req["caller_process"] = owner
            write(os.path.join(queue_spool, "requests", rid + ".json"),
                  json.dumps(req))
            if rid != "pending":
                write(os.path.join(queue_spool, "answers", rid + ".json"),
                      json.dumps({"text": "done", "cancelled": False,
                                  "images": []}))
        answered_only_spool = os.path.join(temporary, "answered-only-spool")
        write(os.path.join(answered_only_spool, "requests", "legacy.json"),
              json.dumps({"id": "legacy", "summary": "already answered"}))
        write(os.path.join(answered_only_spool, "answers", "legacy.json"),
              json.dumps({"text": "done", "cancelled": False, "images": []}))
        corrupt_spool = os.path.join(temporary, "corrupt-spool")
        write(os.path.join(corrupt_spool, "palette.json"), "{not json")

        bad_installed = os.path.join(temporary, "bad-installed")
        bad_rollback = os.path.join(temporary, "bad-rollback")
        bad_canonical = os.path.join(temporary, "bad-canonical")
        bad_spool = os.path.join(temporary, "bad-spool")
        write(bad_installed, "not a directory")
        write(bad_rollback, "not a directory")
        os.makedirs(bad_canonical)
        os.makedirs(os.path.join(bad_spool, "tui.lock"))
        os.makedirs(os.path.join(bad_spool, "palette.json"))

        def snapshot(path):
            seen = {}
            for current, directories, files in os.walk(path):
                for name in directories + files:
                    item = os.path.join(current, name)
                    relative = os.path.relpath(item, path)
                    info = os.lstat(item)
                    if os.path.islink(item):
                        value = ("link", info.st_mode, os.readlink(item))
                    elif os.path.isfile(item):
                        with open(item, "rb") as source:
                            value = ("file", info.st_mode, source.read())
                    else:
                        value = ("directory", info.st_mode)
                    seen[relative] = value
            return seen

        before = snapshot(temporary)
        observed = datetime(2026, 9, 2, 12, tzinfo=timezone.utc)
        status = toggle.build_status(
            root=root, home=home, installed_overlay=installed_overlay,
            rollback_tool=rollback, canonical_tuikit=canonical, spool=spool,
            observed_at=observed)
        ok(status["schema"] == "human-copilot-status"
           and status["schema_version"] == 1
           and status["read_only"] is True
           and status["source"] == "human-copilot:toggle.py state"
           and status["observed_at"] == "2026-09-02T12:00:00Z"
           and set(status["prerequisites"]) == {
               "overlay", "rollback_tool", "tuikit", "tui_lock"},
           "contract identity, timestamp and prerequisites are stable")
        evidence = status["evidence"]
        ok(status["artifact"]["type"] == "gnome-shell-extension"
           and status["artifact"]["version"] == 1
           and status["artifact"]["digest"] ==
           evidence["overlay"]["source_digest"]
           and status["deployed_digest"] ==
           evidence["overlay"]["installed_digest"],
           "generic artifact fields expose the source and installed digests")
        ok(set(evidence) >= {"overlay", "rollback_tool", "tuikit", "tui_lock"}
           and all(item["digest"].startswith("sha256:")
                   for key, item in evidence.items() if key != "overlay_shell"),
           "the four v1 evidence items and the additive instances item all carry SHA-256 digests "
           "(overlay_shell is a live command probe, not a file artifact, and carries none)")
        ok(evidence["overlay"]["state"] == "current"
           and evidence["overlay"]["digests_match"] is True
           and evidence["overlay"]["install_type"] == "symlink"
           and evidence["overlay"]["install_target"] == "source"
           and evidence["overlay"]["version"] == 1,
           "overlay source, installed link, digest and version agree")
        ok(evidence["rollback_tool"]["state"] == "present"
           and evidence["rollback_tool"]["version"] == "2.6.2",
           "parked uv rollback package is versioned and digested")
        ok(evidence["tuikit"]["state"] == "current"
           and evidence["tuikit"]["digests_match"] is True,
           "vendored and canonical tuikit digests agree")
        ok(evidence["tui_lock"]["state"] == "present"
           and evidence["tui_lock"]["holder_state"] == "unheld"
           and evidence["tui_lock"]["surface_state"] == "inactive",
           "tui.lock presence and flock state are observed without creation")
        runtime = evidence["overlay_runtime"]
        ok(runtime["state"] == "current" and runtime["runtime_state"] == "healthy"
           and runtime["fresh"] is True and runtime["spooled_requests"] == 0
           and runtime["answer_filter_version"] == 1
           and runtime["window_binding_version"] == 1,
           "a fresh completed layout identifies the answer filter and window binding loaded by the live shell")
        tui_runtime = evidence["tui_runtime"]
        ok(tui_runtime["state"] == "current"
           and tui_runtime["runtime_state"] == "healthy"
           and tui_runtime["process_alive"] is True
           and tui_runtime["lease_held"] is False
           and tui_runtime["prerequisites_met"] is False,
           "terminal render evidence also requires its live global lease")
        # hcop-0p7.6: evidence.instances — the registry read as recorded
        inst = evidence["instances"]
        rows = {r["id"]: r for r in inst["sessions"]}
        ok(inst["state"] == "present" and inst["palette_registry_state"] == "present"
           and inst["count"] == 4 and inst["live_count"] == 1
           and [r["id"] for r in inst["sessions"]] == ["hc-A", "hc-B", "hc-C", "hc-P"],
           "evidence.instances counts every registered session, live ones separately")
        ok(rows["hc-A"]["number"] == 1 and rows["hc-A"]["colour"] == {
               "name": "gold", "hex": "#ffd700", "xterm": 220}
           and rows["hc-A"]["lock_held"] is True and rows["hc-A"]["presence_held"] is True
           and rows["hc-A"]["holders"][0]["lock_state"] == "held",
           "a held lease reads lock_held with the slot's number, palette colour and its own presence lock")
        ok(rows["hc-B"]["lock_held"] is False and rows["hc-B"]["holders"][0]["lock_state"] == "unheld"
           and rows["hc-C"]["lock_held"] is False and rows["hc-C"]["holders"][0]["lock_state"] == "absent"
           and rows["hc-B"]["presence_held"] is False and rows["hc-B"]["presence_state"] == "absent",
           "a dead server (unheld or absent lease lock) reads lock_held false, never unknown")
        ok(rows["hc-C"]["wrapped"] is True and rows["hc-C"]["colour"]["name"] == "gold"
           and rows["hc-P"]["lock_held"] is False
           and rows["hc-P"]["holders"][0]["lock_state"] == "provisional",
           "a wrapped slot names the entry it shares; a provisional session is never live")
        lock_rows = {r["id"]: r for r in evidence["tui_lock"]["instances"]}
        ok(lock_rows["hc-A"]["delivery"]["attempt_state"] == "degraded"
           and lock_rows["hc-A"]["delivery"]["reason"] == "pane-spawn-failed",
           "status exposes the last failed pane delivery and its global fallback")
        crashed = toggle.build_status(
            root=root, home=home, installed_overlay=installed_overlay,
            rollback_tool=rollback, canonical_tuikit=canonical,
            spool=render_spool, observed_at=observed)
        crash_ev = crashed["evidence"]["overlay_runtime"]
        ok(crashed["runtime_health"] == "unhealthy"
           and crash_ev["state"] == "error"
           and crash_ev["error_name"] == "TypeError"
           and crash_ev["spooled_requests"] == 1
           and crash_ev["actionable_requests"] == 1,
           "a live render exception plus a pending request is unhealthy even while tui.lock is held")
        queue_status = toggle.build_status(
            root=root, home=home, installed_overlay=installed_overlay,
            rollback_tool=rollback, canonical_tuikit=canonical,
            spool=queue_spool, observed_at=observed)
        queue_ev = queue_status["evidence"]["overlay_runtime"]
        ok(queue_status["runtime_health"] == "unhealthy"
           and queue_ev["spooled_requests"] == 4
           and queue_ev["actionable_requests"] == 1
           and queue_ev["answered_requests"] == 3
           and queue_ev["answered_live"] == 1
           and queue_ev["answered_orphans"] == 1
           and queue_ev["answered_ambiguous"] == 1,
           "status separates actionable work from live, orphaned and ambiguous completed answers")
        answered_only = toggle.build_status(
            root=root, home=home, installed_overlay=installed_overlay,
            rollback_tool=rollback, canonical_tuikit=canonical,
            spool=answered_only_spool, observed_at=observed)
        ok(answered_only["runtime_health"] != "unhealthy"
           and answered_only["evidence"]["overlay_runtime"]["actionable_requests"] == 0,
           "an answered orphan never manufactures a live render outage")
        ok(status["prerequisites"]["tui_lock"]["palette_registry"] == "palette.json"
           and status["prerequisites"]["tui_lock"]["palette_holder_lock"] == "palette-holders/<uuid>.lock",
           "prerequisites.tui_lock names the registry and holder lock (additive keys, set of prerequisites unchanged)")
        corrupt = toggle.build_status(
            root=root, home=home, installed_overlay=installed_overlay,
            rollback_tool=rollback, canonical_tuikit=canonical,
            spool=corrupt_spool, observed_at=observed)["evidence"]["instances"]
        ok(corrupt["state"] == "unreadable" and corrupt["reason"] == "corrupt-registry"
           and corrupt["count"] == 0 and corrupt["sessions"] == [],
           "a corrupt registry is a typed unreadable observation, not a crash and not repaired")
        encoded = json.dumps(status)
        ok("status-fixture-secret" not in encoded
           and "rollback-fixture-secret" not in encoded,
           "status emits digests, never artifact contents or secrets")

        missing = toggle.build_status(
            root=root, home=home,
            installed_overlay=os.path.join(temporary, "missing-installed"),
            rollback_tool=os.path.join(temporary, "missing-rollback"),
            canonical_tuikit=os.path.join(temporary, "missing-tuikit"),
            spool=os.path.join(temporary, "missing-spool"),
            observed_at=observed)
        ok(missing["evidence"]["overlay"]["state"] == "absent"
           and missing["evidence"]["rollback_tool"]["state"] == "absent"
           and missing["evidence"]["tuikit"]["state"] == "absent"
           and missing["evidence"]["tui_lock"]["state"] == "absent"
           and missing["evidence"]["instances"]["state"] == "absent"
           and missing["evidence"]["overlay_runtime"]["state"] == "absent"
           and missing["evidence"]["tui_runtime"]["state"] == "absent"
           and missing["evidence"]["instances"]["count"] == 0,
           "missing artifacts are successful typed absent observations")

        unreadable = toggle.build_status(
            root=root, home=home, installed_overlay=bad_installed,
            rollback_tool=bad_rollback, canonical_tuikit=bad_canonical,
            spool=bad_spool, observed_at=observed)
        ok(unreadable["evidence"]["overlay"]["state"] == "unreadable"
           and unreadable["evidence"]["rollback_tool"]["state"] == "unreadable"
           and unreadable["evidence"]["tuikit"]["state"] == "unreadable"
           and unreadable["evidence"]["tui_lock"]["state"] == "unreadable"
           and unreadable["evidence"]["instances"]["state"] == "unreadable",
           "wrong-type artifacts are successful typed unreadable observations")
        for fd in held_fds:
            os.close(fd)
        ok(snapshot(temporary) == before,
           "all evidence paths are byte-for-byte non-mutating (the registry and holders dir included)")

        real_manifest = json.load(open(os.path.join(
            HERE, "overlay", toggle.OVERLAY_UUID, "metadata.json")))
        ok(real_manifest.get("version") == 1,
           "the real GNOME extension manifest carries version 1")
    finally:
        shutil.rmtree(temporary, ignore_errors=True)

def test_overlay_shell_evidence():
    """hcop-1o0: evidence.overlay_shell names why GNOME never rendered the
    overlay (disable-user-extensions=true left it INITIALIZED on 2026-09-09),
    entirely from a stubbed runner — never live gsettings/D-Bus."""
    print("toggle.py overlay_shell evidence")
    import toggle

    def runner_for(gsettings_rc, gsettings_out, ext_rc, ext_out):
        def runner(argv):
            if argv[:1] == ["gsettings"]:
                return _FakeProbeResult(gsettings_rc, gsettings_out)
            if argv[:1] == ["gdbus"]:
                return _FakeProbeResult(ext_rc, ext_out)
            raise AssertionError(f"unexpected probe argv: {argv}")
        return runner

    # (a) globally disabled
    ev = toggle._overlay_shell_evidence(runner=runner_for(
        0, "true\n", 0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <false>, 'state': <2.0>},)\n"))
    ok(ev["state"] == "disabled-globally"
       and ev["user_extensions_disabled"] is True
       and ev["prerequisites_met"] is False
       and "disable-user-extensions" in ev["reason"],
       "org.gnome.shell disable-user-extensions=true is named as the cause")

    # (b) not globally disabled but left INITIALIZED (the 2026-09-09 incident)
    ev = toggle._overlay_shell_evidence(runner=runner_for(
        0, "false\n", 0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <true>, 'state': <6.0>},)\n"))
    ok(ev["state"] == "inactive" and ev["shell_state"] == "INITIALIZED"
       and ev["user_extensions_disabled"] is False
       and ev["prerequisites_met"] is False
       and "copilot-overlay@croze" in ev["reason"]
       and "INITIALIZED" in ev["reason"],
       "an extension stuck INITIALIZED (never ACTIVE) is named, not just absent")

    # (c) ACTIVE and not globally disabled
    ev = toggle._overlay_shell_evidence(runner=runner_for(
        0, "false\n", 0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <true>, 'state': <1.0>},)\n"))
    ok(ev["state"] == "current" and ev["prerequisites_met"] is True
       and ev["reason"] is None,
       "ACTIVE plus enabled user extensions reads current with prerequisites met")

    # (c2) the shell does not know the extension: GetExtensionInfo returns ({},)
    ev = toggle._overlay_shell_evidence(runner=runner_for(0, "false\n", 0, "({},)\n"))
    ok(ev["state"] == "unknown" and ev["shell_state"] is None
       and "not installed" in ev["reason"],
       "an extension unknown to gnome-shell is a typed unknown with a reason")

    # (d) probe binary missing entirely -> typed unknown, never a crash
    def raising_runner(argv):
        raise FileNotFoundError(argv[0])
    ev = toggle._overlay_shell_evidence(runner=raising_runner)
    ok(ev["state"] == "unknown" and ev["enabled"] is None
       and ev["shell_state"] is None and ev["user_extensions_disabled"] is None
       and ev["reason"],
       "a missing gsettings/gdbus binary is a typed unknown, not an exception")

    # (e) runtime_health_reason only fires when it can name the cause
    ok(toggle._runtime_health_reason(
           "unhealthy", {"state": "disabled-globally", "reason": "x"}) == "x"
       and toggle._runtime_health_reason(
           "unhealthy", {"state": "inactive", "reason": "y"}) == "y"
       and toggle._runtime_health_reason(
           "unhealthy", {"state": "current", "reason": None}) is None
       and toggle._runtime_health_reason(
           "healthy", {"state": "disabled-globally", "reason": "x"}) is None,
       "runtime_health_reason names the shell cause only for an unhealthy/"
       "stopped surface the shell can explain, null otherwise")

    # build_status() threads the injected runner end to end and sets the
    # additive top-level key without disturbing v1 fields. A live render
    # error plus a pending request is the same unhealthy fixture shape used
    # in test_status_contract, so runtime_health lands on "unhealthy" here.
    unhealthy_spool = tempfile.mkdtemp(prefix="hc-overlay-shell-")
    with open(os.path.join(unhealthy_spool, "tui.lock"), "wb") as f:
        pass
    os.makedirs(os.path.join(unhealthy_spool, "requests"))
    with open(os.path.join(unhealthy_spool, "requests", "missed.json"), "w") as f:
        f.write("{}")
    with open(os.path.join(unhealthy_spool, "overlay-runtime.json"), "w") as f:
        json.dump({"schema": "human-copilot-overlay-runtime", "schema_version": 1,
                   "state": "error",
                   "observed": datetime.now(timezone.utc).timestamp(),
                   "pending_requests": 1, "rendered_cards": 0,
                   "error": {"name": "TypeError", "message": "fixture"}}, f)
    status = toggle.build_status(
        spool=unhealthy_spool,
        overlay_shell_runner=runner_for(
            0, "true\n", 0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <false>, 'state': <2.0>},)\n"))
    ok(status["evidence"]["overlay_shell"]["state"] == "disabled-globally"
       and status["runtime_health"] == "unhealthy"
       and status["runtime_health_reason"]
       == status["evidence"]["overlay_shell"]["reason"],
       "build_status plumbs the stubbed runner and sets runtime_health_reason "
       "when the shell explains an unhealthy surface")

    status = toggle.build_status(
        spool=tempfile.mkdtemp(prefix="hc-overlay-shell-"),
        overlay_shell_runner=runner_for(
            0, "false\n", 0, "({'uuid': <'copilot-overlay@croze'>, 'enabled': <true>, 'state': <1.0>},)\n"))
    ok(status["evidence"]["overlay_shell"]["state"] == "current"
       and status["runtime_health_reason"] is None,
       "runtime_health_reason stays null when the shell reports the "
       "extension current")

# ------------------------------------------------------------------ 3. server

def _rpc(proc, msg):
    proc.stdin.write(json.dumps(msg) + "\n")
    proc.stdin.flush()

def _read(proc, want_id, deadline_s=15):
    end = time.time() + deadline_s
    while time.time() < end:
        line = proc.stdout.readline()
        if not line:
            break
        m = json.loads(line)
        if m.get("id") == want_id:
            return m
    raise AssertionError(f"no response for id {want_id}")

def _isolated_legacy_server_test(test):
    """Give standalone legacy-routing checks the same private setup as the suite."""
    @functools.wraps(test)
    def isolated(*args, **kwargs):
        import toggle
        with tempfile.TemporaryDirectory(prefix="hc-standalone-control-") as root:
            control = os.path.join(root, "control.json")
            home = os.path.join(root, "home")
            surface = os.path.join(root, "surface.json")
            write_control_fixture(control, True)
            write_enabled_home_fixture(home)
            with open(surface, "w") as target:
                json.dump({"mode": "auto"}, target)
            with mock.patch.dict(os.environ, {
                    "HC_CONTROL_FILE": control,
                    "HC_SURFACE_POLICY_FILE": surface,
                    "HOME": home,
                    "CODEX_HOME": os.path.join(home, ".codex"),
            }), mock.patch.object(toggle, "_run_probe", _stub_overlay_shell_runner):
                return test(*args, **kwargs)
    return isolated


@_isolated_legacy_server_test
def test_server():
    print("server.py (subprocess, real protocol)")
    import server as hcserver
    # Codex's MCP launcher may omit XDG_RUNTIME_DIR even though the desktop
    # responder uses /run/user/$UID. Pin that exact live failure: both must
    # still resolve the same spool instead of silently splitting the lane.
    runtime = tempfile.mkdtemp(prefix="hc-runtime-")
    os.chmod(runtime, 0o700)
    old_runtime = os.environ.pop("XDG_RUNTIME_DIR", None)
    old_spool = os.environ.pop("HC_SPOOL", None)
    old_uid_runtime = hcserver._uid_runtime_dir
    hcserver._uid_runtime_dir = lambda: runtime
    try:
        resolved = hcserver.spool_dir()
        ok(resolved == os.path.join(runtime, "human-copilot"),
           "missing XDG_RUNTIME_DIR still converges on the user runtime spool")
        os.chmod(runtime, 0o755)
        fallback_home = tempfile.mkdtemp(prefix="hc-fallback-home-")
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = fallback_home
        try:
            fallback = hcserver.spool_dir()
            ok(fallback == os.path.join(fallback_home, ".local", "state",
                                        "human-copilot", "spool"),
               "an unsafe guessed runtime directory is rejected")
        finally:
            if old_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = old_home
            shutil.rmtree(fallback_home, ignore_errors=True)
    finally:
        hcserver._uid_runtime_dir = old_uid_runtime
        if old_runtime is not None:
            os.environ["XDG_RUNTIME_DIR"] = old_runtime
        if old_spool is not None:
            os.environ["HC_SPOOL"] = old_spool
        shutil.rmtree(runtime, ignore_errors=True)

    # A global tui.lock can be either the desktop overlay or a terminal TUI.
    # Only the overlay's fresh successful render record suppresses the
    # desktop arrival notification; every ambiguous/error state fails open.
    import toggle
    import tui
    render_probe = tempfile.mkdtemp(prefix="hc-render-probe-")
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(render_probe, sub))
    render_path = os.path.join(render_probe, hcserver.OVERLAY_RUNTIME_FILE)
    render_fd = tui.hold_presence(render_probe)
    now = 2_000_000_000.0

    def write_render(state="healthy", observed=now, **extra):
        with open(render_path, "w") as f:
            json.dump({"schema": "human-copilot-overlay-runtime",
                       "schema_version": 1, "state": state,
                       "observed": observed, "pending_requests": 1,
                       "rendered_cards": 1, **extra}, f)

    try:
        ok(not hcserver.overlay_rendering(render_probe, now),
           "a global TUI lock without an overlay heartbeat cannot suppress the arrival nudge")

        # The server and every TUI share one O_EXCL marker. This is the
        # durable bridge for requests written by MCP server processes that
        # were already resident before arrival nudges moved into the TUI.
        bridge_req = {"id": "old-server-request", "summary": "Choose the safe path",
                      "client": "Old Codex", "created": now,
                      "deadline": now + 60}
        bridge_path = os.path.join(render_probe, "requests",
                                   bridge_req["id"] + ".json")
        hcserver._write_private(bridge_path, bridge_req)
        saved_notify = hcserver.notify_operator
        bridge_notes, bridge_states = [], []
        hcserver.notify_operator = lambda message, client=None: \
            bridge_notes.append((message, client))
        try:
            racers = [threading.Thread(target=lambda fn=fn:
                       bridge_states.append(fn(render_probe, bridge_req, now)))
                       for fn in ([hcserver.arrival_nudge, tui.arrival_nudge] * 4)]
            for racer in racers:
                racer.start()
            for racer in racers:
                racer.join()
        finally:
            hcserver.notify_operator = saved_notify
        marker = hcserver.arrival_nudge_path(render_probe, bridge_req["id"])
        reqs, bad = tui.scan_requests(render_probe)
        ok(bridge_states.count("notified") == 1
           and bridge_states.count("already") == 7
           and bridge_notes == [("Human Copilot request waiting — focus it, or "
                                 "open one: tui → c.  Choose the safe path",
                                 "Old Codex")],
           "server and multiple TUIs race to exactly one desktop nudge for an old-server request")
        ok(os.stat(marker).st_mode & 0o777 == 0o600
           and [r["id"] for r in reqs] == [bridge_req["id"]] and bad == 0,
           "the atomic arrival marker is 0600 and never appears as a request")
        os.unlink(bridge_path)
        tui.prune_arrival_nudges(render_probe)
        ok(not os.path.exists(marker),
           "a TUI cleans an old server's orphan arrival marker with the request lifecycle")

        saved_delivery = (hcserver.spool_dir, hcserver.instance_stamp,
                          hcserver.hold_palette, hcserver.unattached_refusal,
                          hcserver.notify_operator, hcserver.POLL_S)
        nudges = []
        hcserver.spool_dir = lambda: render_probe
        hcserver.instance_stamp = lambda: {
            "session": None, "pane": None, "server_uuid": "n" * 32,
            "attached": None}
        hcserver.hold_palette = lambda _inst, _spool: None
        hcserver.unattached_refusal = lambda _inst: None
        hcserver.notify_operator = lambda message, client=None: nudges.append(message)
        hcserver.POLL_S = 0.01

        def direct_request(summary, rpc_id):
            outcome = []
            thread = threading.Thread(target=lambda: outcome.append(
                hcserver.run_feedback(summary, 2, rpc_id)))
            thread.start()
            req_id = None
            for _ in range(200):
                names = [name for name in os.listdir(os.path.join(
                    render_probe, "requests")) if name.endswith(".json")]
                if names:
                    req_id = names[0][:-5]
                    break
                time.sleep(0.005)
            if req_id:
                hcserver._write_private(os.path.join(
                    render_probe, "answers", req_id + ".json"),
                    {"text": "seen", "cancelled": False, "images": []})
            thread.join(timeout=2)
            return outcome

        try:
            outcome = direct_request("global TUI is behind windows", 901)
            ok(outcome and len(nudges) == 1
               and "global TUI is behind windows" in nudges[0],
               "a waiting request behind only a global TUI gets one immediate desktop nudge")
            nudges.clear()
            write_render(observed=time.time())
            outcome = direct_request("healthy overlay owns the nudge", 902)
            ok(outcome and not nudges,
               "a request with fresh healthy overlay evidence gets no duplicate notification")
        finally:
            (hcserver.spool_dir, hcserver.instance_stamp,
             hcserver.hold_palette, hcserver.unattached_refusal,
             hcserver.notify_operator, hcserver.POLL_S) = saved_delivery

        write_render()
        ok(hcserver.overlay_rendering(render_probe, now)
           and hcserver.OVERLAY_RUNTIME_FRESH_S == toggle.OVERLAY_RUNTIME_FRESH_S,
           "a held overlay lease plus its fresh healthy heartbeat suppresses duplicate notification")
        write_render(state="error")
        ok(not hcserver.overlay_rendering(render_probe, now),
           "an explicit render error fails open to the arrival nudge")
        write_render(observed=now - hcserver.OVERLAY_RUNTIME_FRESH_S - 0.1)
        ok(not hcserver.overlay_rendering(render_probe, now),
           "a stale healthy heartbeat fails open to the arrival nudge")
        with open(render_path, "w") as f:
            f.write("{not json")
        ok(not hcserver.overlay_rendering(render_probe, now),
           "a malformed heartbeat fails open to the arrival nudge")
        write_render()
        os.close(render_fd)
        render_fd = None
        ok(not hcserver.overlay_rendering(render_probe, now),
           "a fresh heartbeat without its overlay lease cannot suppress the arrival nudge")
    finally:
        if render_fd is not None:
            os.close(render_fd)
        shutil.rmtree(render_probe, ignore_errors=True)

    spool = tempfile.mkdtemp(prefix="hc-spool-")
    # Stub the desktop out: notify-send must not fire for real during tests,
    # and HC_TERMINAL records auto-open attempts instead of opening anything.
    # With no healthy overlay, every request gets one arrival notification
    # even when a terminal can open; a terminal may be behind other windows.
    bindir = os.path.join(spool, "bin"); os.makedirs(bindir)
    rec = os.path.join(spool, "spawns.log")
    notes = os.path.join(spool, "notify.log")
    with open(os.path.join(bindir, "fake-term"), "w") as f:
        f.write(f'#!/bin/bash\necho "$@" >> "{rec}"\n')
    with open(os.path.join(bindir, "notify-send"), "w") as f:
        f.write(f'#!/bin/bash\necho "$@" >> "{notes}"\n')
    for n in ("fake-term", "notify-send"):
        os.chmod(os.path.join(bindir, n), 0o755)
    # This selftest may itself run inside an ATTACHED zellij session; a server
    # inheriting that would open a REAL copilot pane in it (hcop-0p7.4). This
    # section is about the non-zellij path: strip the stamp's inputs.
    env = {k: v for k, v in os.environ.items() if not k.startswith("ZELLIJ")}
    env.update({"HC_SPOOL": spool,
                "HC_TERMINAL": os.path.join(bindir, "fake-term"),
                "PATH": bindir + ":" + os.environ["PATH"]})
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env)
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {},
                               "clientInfo": {"name": "codex-mcp-client"}}})
        r = _read(proc, 1)
        ok(r["result"]["protocolVersion"] == "2025-06-18"
           and r["result"]["serverInfo"]["name"] == "human-copilot",
           "initialize echoes protocol, names itself")
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        tools = _read(proc, 2)["result"]["tools"]
        ok([t["name"] for t in tools] == ["interactive_feedback"]
           and "summary" in tools[0]["inputSchema"]["required"],
           "tools/list exposes interactive_feedback")
        ok("Use shell and API tools first" in tools[0]["description"]
           and "routine technical" in tools[0]["description"]
           and "end your turn to involve" not in tools[0]["description"],
           "tools/list teaches focused human assistance, not universal questions (hcop-u03)")
        schema = tools[0]["inputSchema"]
        summary_contract = schema["properties"]["summary"]["description"]
        ok(all(phrase in summary_contract for phrase in (
            "actual execution blocker", "human fact, action or authorization",
            "continue implementation or operation",
            "Do not proactively request QA, test checklists, final acceptance or visual sign-off",
            "Screenshots and pixel capture are disabled by default",
            "screenctx screen_crop/screen_diff may be used only inside",
            "an operator-open temporary allowance in Desktop Control",
            "Never open, edit or request an allowance",
            "including screenctx capture_policy.py writes", "or solicit snips",
            "concrete autonomous routes tried", "why each failed or is unavailable",
            "operator-only fact", "physical action", "required approval",
            "existing authorization")),
           "tools/list summary contract requires an execution blocker and forbids proactive QA")
        ok(schema["required"] == ["summary"]
           and set(schema["properties"]) == {"summary", "mode", "options", "timeout"},
           "handoff guidance preserves existing MCP arguments")

        # stub operator: wait for the request file, answer with text + image
        png = b"\x89PNG\r\n\x1a\nfakebytes"
        def operator():
            rdir = os.path.join(spool, "requests")
            for _ in range(200):
                reqs = ([f for f in os.listdir(rdir) if f.endswith(".json")]
                        if os.path.isdir(rdir) else [])
                if reqs:
                    rid = reqs[0][:-5]
                    body = json.load(open(os.path.join(rdir, reqs[0])))
                    assert "1. run `thing` in a terminal" in body["summary"]
                    assert body["client"] == "Codex"
                    tmp = os.path.join(spool, "answers", rid + ".json.t")
                    with open(tmp, "w") as f:
                        json.dump({"text": "step 1 passed", "cancelled": False,
                                   "images": [{"mime": "image/png",
                                               "b64": base64.b64encode(png).decode()},
                                              {"mime": "image/png", "b64": "!!notb64"}]},
                                  f)
                    os.replace(tmp, os.path.join(spool, "answers", rid + ".json"))
                    return
                time.sleep(0.05)
        t = threading.Thread(target=operator); t.start()
        _rpc(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "1. run `thing` in a terminal, then click it",
                                             "timeout": 60}}})
        r = _read(proc, 3); t.join()
        c = r["result"]["content"]
        ok(r["result"]["isError"] is False and c[0]["type"] == "text"
           and c[0]["text"] == "step 1 passed", "operator text comes back")
        imgs = [b for b in c if b["type"] == "image"]
        ok(len(imgs) == 1 and base64.b64decode(imgs[0]["data"]) == png
           and imgs[0]["mimeType"] == "image/png", "image round-trips")
        ok(not any("brevity" in b.get("text", "") for b in c),
           "a one-line checklist earns no brevity note")
        ok(any("clarity" in b.get("text", "") for b in c),
           "a bare step list (no context line) earns the clarity note over the wire")
        ok(any("handoff: step 1" in b.get("text", "") for b in c),
           "a step that hands the operator a command earns the handoff note over the wire")
        ok(any(b["type"] == "text" and "unreadable" in b["text"] for b in c),
           "junk b64 is dropped LOUDLY, not forwarded")
        ok(not os.listdir(os.path.join(spool, "requests"))
           and not os.listdir(os.path.join(spool, "answers")),
           "spool is clean after the call")
        time.sleep(0.3)
        spawns = open(rec).read().splitlines() if os.path.exists(rec) else []
        ok(len(spawns) == 1 and "tui.py" in spawns[0],
           "no TUI watching -> auto-open fired exactly once")
        notified = open(notes).read().splitlines() if os.path.exists(notes) else []
        ok(not notified, "desktop arrival popups are disabled")

        # cancel: request goes up, client cancels, spool empties
        _rpc(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "wait for me"}}})
        for _ in range(100):
            if os.listdir(os.path.join(spool, "requests")):
                break
            time.sleep(0.05)
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": 4}})
        r = _read(proc, 4)
        ok("cancelled" in r["result"]["content"][0]["text"]
           and "structuredContent" not in r["result"],
           "cancel stops the wait and answers without an authorization receipt")
        ok(not os.listdir(os.path.join(spool, "requests")),
           "cancelled request leaves the spool")

        _rpc(proc, {"jsonrpc": "2.0", "id": 5, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "   "}}})
        r = _read(proc, 5)
        ok(r["result"]["isError"] is True, "empty summary is an isError, not a hang")
        ok("@{Window" in tools[0]["description"] and "FAILED" in tools[0]["description"],
           "tool description teaches the locator grammar and the three step states")

        _rpc(proc, {"jsonrpc": "2.0", "id": 6, "method": "no/such"})
        ok(_read(proc, 6).get("error", {}).get("code") == -32601,
           "unknown method errors instead of silence")
        spawns = open(rec).read().splitlines() if os.path.exists(rec) else []
        ok(len(spawns) == 1,
           "spawn cooldown held — later calls opened no second window")
        notified = open(notes).read().splitlines() if os.path.exists(notes) else []
        ok(not notified, "no desktop popup is raised while the overlay is unavailable")
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait(timeout=5)

    # No surface and no terminal still nudges, as before.
    notes2 = os.path.join(spool, "notify2.log")
    with open(os.path.join(bindir, "notify-send"), "w") as f:
        f.write(f'#!/bin/bash\necho "$@" >> "{notes2}"\n')
    os.chmod(os.path.join(bindir, "notify-send"), 0o755)
    blind = {**env, "HC_SPOOL": spool, "PATH": bindir}   # no emulator, no ZELLIJ_*
    blind.pop("HC_TERMINAL", None)
    # clear every cooldown: the stamp is per instance (spool/instances/<id>/),
    # and this selftest may itself run inside the zellij session the first
    # server inherited, which would make the blind server the same instance
    shutil.rmtree(os.path.join(spool, "instances"), ignore_errors=True)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=blind)
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18",
                               "capabilities": {}, "clientInfo": {"name": "t"}}})
        _read(proc, 1)
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                    "params": {"name": "interactive_feedback",
                               "arguments": {"summary": "nobody is watching"}}})
        for _ in range(100):
            if os.path.exists(notes2):
                break
            time.sleep(0.05)
        said = open(notes2).read() if os.path.exists(notes2) else ""
        time.sleep(0.5)
        said = open(notes2).read() if os.path.exists(notes2) else ""
        ok(said == "", "no surface AND no terminal -> no desktop popup (disabled)")
        ok(not said.splitlines(), "...and none per poll either")
        _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                    "params": {"requestId": 2}})
        _read(proc, 2)
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait(timeout=5)
        shutil.rmtree(spool, ignore_errors=True)

    # The request remains a complete visible spool file for its whole wait,
    # then expiry reports UNVERIFIED and removes it. Keep this direct and fast
    # by shrinking only the in-process poll interval.
    expiry_spool = tempfile.mkdtemp(prefix="hc-expiry-")
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(expiry_spool, sub))
    saved = (hcserver.spool_dir, hcserver.instance_stamp, hcserver.hold_palette,
             hcserver.tui_alive, hcserver.overlay_rendering,
             hcserver.notify_operator, hcserver.POLL_S)
    outcome = []
    try:
        hcserver.spool_dir = lambda: expiry_spool
        hcserver.instance_stamp = lambda: {
            "session": None, "pane": None, "server_uuid": "e" * 32,
            "attached": None}
        hcserver.hold_palette = lambda _inst, _spool: None
        hcserver.tui_alive = lambda _spool, _iid=None: True
        hcserver.overlay_rendering = lambda _spool: True
        hcserver.notify_operator = lambda _message, client=None: None
        hcserver.POLL_S = 0.01
        thread = threading.Thread(target=lambda: outcome.append(
            hcserver.run_feedback("Context.\n1. inspect", 0.18, 999)))
        thread.start()
        request_body = None
        for _ in range(100):
            names = [name for name in os.listdir(os.path.join(
                expiry_spool, "requests")) if name.endswith(".json")]
            if names:
                request_body = json.load(open(os.path.join(
                    expiry_spool, "requests", names[0])))
                break
            time.sleep(0.005)
        ok(request_body is not None
           and request_body["deadline"] > request_body["created"]
           and request_body["timeout"] == 0.18,
           "a waiting request is complete and carries its full lifetime before expiry")
        ok(hcserver.caller_process_state(request_body.get("caller_process")) == "alive",
           "a published request carries PID + boot + start identity for conservative orphan cleanup")
        thread.join(timeout=3)
        ok(not thread.is_alive()
           and "No operator response within 0.18s" in outcome[0][0]["text"]
           and getattr(outcome[0], "structured", None) is None
           and not os.listdir(os.path.join(expiry_spool, "requests")),
           "expiry reports the pass unverified, emits no receipt, and cleans only after its deadline")
    finally:
        (hcserver.spool_dir, hcserver.instance_stamp, hcserver.hold_palette,
         hcserver.tui_alive, hcserver.overlay_rendering,
         hcserver.notify_operator, hcserver.POLL_S) = saved
        shutil.rmtree(expiry_spool, ignore_errors=True)

# ------------------------------------------------------- answered orphan lifecycle

def test_answered_orphan_lifecycle():
    print("answered request orphan lifecycle")
    import server as hcserver
    import tui

    root = tempfile.mkdtemp(prefix="hc-orphan-")
    spool = os.path.join(root, "spool")
    proc = os.path.join(root, "proc")
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(spool, sub))
    os.makedirs(os.path.join(proc, "sys", "kernel", "random"))
    with open(os.path.join(proc, "sys", "kernel", "random", "boot_id"), "w") as f:
        f.write("boot-a\n")

    pid = 4242
    os.makedirs(os.path.join(proc, str(pid)))
    # After the final ')' fields[0] is state and fields[19] is starttime.
    proc_fields = ["S"] + ["0"] * 18 + ["99"]
    with open(os.path.join(proc, str(pid), "stat"), "w") as f:
        f.write(f"{pid} (worker ) with spaces) " + " ".join(proc_fields) + "\n")
    live_owner = hcserver.caller_process_identity(pid=pid, proc_root=proc)
    ok(live_owner is not None
       and live_owner["start_ticks"] == "99"
       and hcserver.caller_process_state(live_owner, proc_root=proc) == "alive",
       "caller identity uses boot id and /proc start ticks, including a tricky process name")
    ok(hcserver.caller_process_state(
           {**live_owner, "start_ticks": "98"}, proc_root=proc) == "dead"
       and hcserver.caller_process_state(
           {**live_owner, "boot_id": "older-boot"}, proc_root=proc) == "dead"
       and hcserver.caller_process_state({}, proc_root=proc) == "unknown",
       "PID reuse, reboot and ambiguous legacy ownership have distinct conservative states")

    def pair(rid, owner, answer_time):
        request = {"id": rid, "summary": "Context.\n1. inspect",
                   "created": answer_time - 10, "deadline": answer_time + 60}
        if owner is not None:
            request["caller_process"] = owner
        hcserver._write_private(os.path.join(spool, "requests", rid + ".json"), request)
        hcserver._write_private(os.path.join(spool, "answers", rid + ".json"),
                                {"text": "operator answer", "cancelled": False,
                                 "images": []})
        os.utime(os.path.join(spool, "answers", rid + ".json"),
                 (answer_time, answer_time))

    try:
        pair("aaaaaaaaaaaa", live_owner, 900)                    # live caller
        pair("bbbbbbbbbbbb", {**live_owner, "start_ticks": "98"}, 900)
        pair("cccccccccccc", None, 900)                          # legacy ambiguity
        pair("dddddddddddd", {**live_owner, "start_ticks": "98"}, 990)
        hcserver._write_private(
            os.path.join(spool, "requests", "eeeeeeeeeeee.json"),
            {"id": "eeeeeeeeeeee", "summary": "still actionable",
             "created": 900, "deadline": 1200,
             "caller_process": live_owner})
        hcserver._claim_arrival_nudge(spool, "bbbbbbbbbbbb")

        result = hcserver.reclaim_answered_orphans(
            spool, now=1000, grace_s=30, proc_root=proc)
        ok(result == {"state": "complete", "pairs": 4, "reclaimed": 1,
                      "live": 1, "ambiguous": 1, "young": 1, "errors": []},
           "GC reclaims only an old conclusively dead answered caller")
        ok(not os.path.exists(os.path.join(spool, "requests", "bbbbbbbbbbbb.json"))
           and not os.path.exists(os.path.join(spool, "answers", "bbbbbbbbbbbb.json"))
           and not os.path.exists(hcserver.arrival_nudge_path(spool, "bbbbbbbbbbbb")),
           "orphan cleanup removes request, answer and its arrival marker together")
        ok(all(os.path.exists(os.path.join(spool, side, rid + ".json"))
               for rid in ("aaaaaaaaaaaa", "cccccccccccc", "dddddddddddd")
               for side in ("requests", "answers")),
           "live, legacy-ambiguous and within-grace answer pairs are preserved")
        saved_tui_time = tui.time.time
        tui.time.time = lambda: 1000
        try:
            reqs, bad = tui.scan_requests(spool)
        finally:
            tui.time.time = saved_tui_time
        ok([r["id"] for r in reqs] == ["eeeeeeeeeeee"] and bad == 0,
           "the TUI exposes only actionable requests and never resurrects completed answers")
        again = hcserver.reclaim_answered_orphans(
            spool, now=1000, grace_s=30, proc_root=proc)
        ok(again["reclaimed"] == 0 and again["pairs"] == 3,
           "orphan cleanup is idempotent under repeated surface scans")
        later = hcserver.reclaim_answered_orphans(
            spool, now=1100, grace_s=30, proc_root=proc)
        ok(later["reclaimed"] == 1
           and not os.path.exists(os.path.join(spool, "requests", "dddddddddddd.json")),
           "a late answer is retained for its grace, then reclaimed after the dead caller stays dead")
        ok(os.stat(os.path.join(spool, hcserver.ORPHAN_GC_LOCK)).st_mode & 0o777 == 0o600,
           "the cross-surface orphan-GC arbitration lock is private")
    finally:
        shutil.rmtree(root, ignore_errors=True)

# ------------------------------------------------------------------ 3. tui logic

def test_tui_logic():
    print("tui.py logic (curses-free)")
    import tui
    spool = tempfile.mkdtemp(prefix="hc-spool2-")
    for d in ("requests", "answers"):
        os.makedirs(os.path.join(spool, d))
    try:
        with open(os.path.join(spool, "requests", "aaa.json"), "w") as f:
            json.dump({"id": "aaa", "summary": "s", "created": 2,
                       "deadline": time.time() + 60}, f)
        with open(os.path.join(spool, "requests", "bbb.json"), "w") as f:
            json.dump({"id": "bbb", "summary": "s", "created": 1,
                       "deadline": time.time() + 60}, f)
        with open(os.path.join(spool, "requests", "expired.json"), "w") as f:
            json.dump({"id": "expired", "summary": "s", "created": 0,
                       "deadline": time.time() - 1}, f)
        with open(os.path.join(spool, "requests", "junk.json"), "w") as f:
            f.write("{nope")
        reqs, bad = tui.scan_requests(spool)
        ok([r["id"] for r in reqs] == ["bbb", "aaa"] and bad == 1,
           "scan sorts oldest-first, hides expired requests, and COUNTS the corrupt file")
        ok(not os.path.exists(os.path.join(
            spool, "requests", "expired.json")),
           "the TUI scan retires the expired request instead of leaving a stale spool card")

        tui.write_answer(spool, "bbb", "hi", [{"mime": "image/png", "b64": "QQ=="}])
        p = os.path.join(spool, "answers", "bbb.json")
        ok(os.stat(p).st_mode & 0o777 == 0o600, "answer file is 0600")
        ok(json.load(open(p))["images"][0]["b64"] == "QQ==", "answer carries images")

        lines = tui.md_lines("# Head\n\n1. do the thing and watch it very "
                             "carefully for a long long time\n```\ncode\n```", 30)
        ok(lines[0][0] == "Head" and any(l[0].startswith("   ") for l in lines[2:4]),
           "md: heading stripped, list wraps with hanging indent")
        ok(("code", tui.curses.A_DIM) in lines, "md: fenced code kept dim, fences dropped")

        ok(tui.md_inline("see [wiki](http://x/y) and `code`, keep __init__ "
                         "and *.log *.tmp")
           == "see wiki (http://x/y) and code, keep __init__ and *.log *.tmp",
           "md: links flattened, code stripped, dunders and globs untouched")
        ok(tui.md_inline("**bold** *em* ~~gone~~ \\*literal\\*")
           == "bold em gone *literal*", "md: emphasis dropped, escapes resolved")

        ok(tui.md_cells(r"| a | b\|c | |") == ["a", "b|c", ""],
           "md: table cells, outer pipes optional, escaped pipe kept")
        ok(tui.md_is_delim("|---|:--:|--:|") and tui.md_is_delim(" --- | --- ")
           and not tui.md_is_delim("---") and not tui.md_is_delim("| a | b |"),
           "md: a delimiter row needs pipes — a lone --- is a rule, not a table")

        tbl = ("| step | what to observe | report |\n"
               "|------|:---------------:|-------:|\n"
               "| 1 | the window opens at 4K | pass/fail |\n"
               "| 2 | no blur on DP-2 | pass and the scale |\n")
        wide = tui.md_lines(tbl, 70)
        ok(wide[0][0].startswith("┌") and wide[-1][0].endswith("┘"),
           "md: a table is boxed, not printed as raw pipes")
        ok(all(len(l[0]) <= 70 for l in wide), "md: table fits the given width")
        ok(len({len(l[0]) for l in wide}) == 1, "md: every table line is one width")
        ok(any("pass/fail" in l[0] for l in wide) and
           not any(l[0].strip().startswith("|---") for l in wide),
           "md: cells survive, the delimiter row does not")
        ok(wide[1][1] == tui.curses.A_BOLD, "md: the header row is bold")
        narrow = tui.md_lines(tbl, 26)
        ok(not any("┌" in l[0] for l in narrow) and
           any(l[0].startswith("what to observe: the") for l in narrow),
           "md: too narrow for columns -> header: value records, nothing lost")

        rich = tui.md_lines("> caution\n\n---\n\n- [x] done\n- [ ] todo\n"
                            "* plain bullet", 40)
        ok(("▏ caution", tui.curses.A_DIM) in rich, "md: block quotes are marked")
        ok(("─" * 40, tui.curses.A_DIM) in rich, "md: --- is a horizontal rule")
        ok(any(l[0] == "☑ done" for l in rich) and
           any(l[0] == "☐ todo" for l in rich) and
           any(l[0] == "• plain bullet" for l in rich),
           "md: task boxes checked/unchecked, bullets normalised")

        ok(tui.md_lines("| a | b |\nnot a table", 40)[0][0] == "| a | b |",
           "md: a pipe line with no delimiter row stays verbatim prose")

        # a bullet list indented under a numbered step: the TUI has real
        # layout, so the nesting is drawn where the eye expects it and the
        # continuation paragraph after it stays inside the step. (The overlay
        # half of this break is pinned by drive_overlay.js.)
        nest = [l[0] for l in tui.md_lines(
            "4. Replace the existing two with:\n"
            "   - `adrian.ns.cloudflare.com`\n"
            "   - `aiden.ns.cloudflare.com`\n\n"
            "   and remove `wns2061.hostgator.com`. Save.\n"
            "5. Wait for propagation.", 64)]
        ok(nest == ["4. Replace the existing two with:",
                    "   \u2022 adrian.ns.cloudflare.com",
                    "   \u2022 aiden.ns.cloudflare.com",
                    "",
                    "   and remove wns2061.hostgator.com. Save.",
                    "5. Wait for propagation."],
           "md: a sublist under a step renders nested, and so does what follows it")
        deep = [l[0] for l in tui.md_lines(
            "1. outer\n   - level one\n     - level two\n   - back out", 40)]
        ok(deep == ["1. outer", "   \u2022 level one", "     \u2022 level two",
                    "   \u2022 back out"],
           "md: two-level nesting keeps each level at its own indent")

        import server as hcserver
        ok(hcserver.brevity_note("1. a\n2. b") is None, "brevity: short checklist passes")
        ok("8 steps" in hcserver.brevity_note("\n".join(f"{i}. s" for i in range(1, 9))),
           "brevity: too many steps -> the nudge names the count")
        ok("41 words" in hcserver.brevity_note("1. " + "w " * 40),
           "brevity: a wordy step -> the nudge names the words")
        ok("no numbered steps" in hcserver.brevity_note("w " * 130),
           "brevity: long prose without steps -> asks for numbered steps")
        ok(hcserver.count_steps("1. a\n```\n2. no\n```\n| # | d |\n|---|---|\n| 3 | t |\n   4. nested")
           == 2, "brevity counts steps the way the overlay walks them")
        ok("<=12" not in (hcserver.brevity_note("1. " + "w " * 40) or "")
           and "<=12" not in hcserver.TOOL_DESCRIPTION,
           "brevity: the 12-word rule is gone — clarity first (hcop-e6s)")
        # The strict autonomy boundary supersedes the earlier cost exception.
        # Keep the existing routing and reporting contract checks below.
        _doc = {}
        for _name in ("doctrine-on.md", "doctrine-off.md"):
            with open(os.path.join(HERE, _name), encoding="utf-8") as _f:
                _doc[_name] = _f.read()
        # hcop-tlc: with the lane enabled, a necessary operator question rides
        # Human Copilot rather than the native Codex input tools; with it off,
        # the direct/native channel stays supported. Routing only — never an
        # approval bypass or an always-question rule.
        _on = " ".join(_doc["doctrine-on.md"].replace("**", "").split()).lower()
        _off = " ".join(_doc["doctrine-off.md"].split()).lower()
        ok("interactive_feedback" in _on and "request_user_input" in _on
           and "queued follow-up inputs" in _on and "no approval bypass" in _on,
           "doctrine-on: necessary questions prefer Copilot over native Codex inputs (hcop-tlc)")
        ok("do not repeatedly call" in _on and "direct/native" in _on,
           "doctrine-on: an unavailable lane is stated once, then direct fallback (hcop-tlc)")
        ok("request_user_input" in _off and "direct/native" in _off
           and "without invoking copilot" in _off,
           "doctrine-off: necessary questions keep the direct/native channel (hcop-tlc)")
        ok("request_user_input" in hcserver.TOOL_DESCRIPTION
           and "no approval bypass" in hcserver.TOOL_DESCRIPTION
           and "do not call it repeatedly" in hcserver.TOOL_DESCRIPTION,
           "TOOL_DESCRIPTION: routing preference and honest unavailability fallback (hcop-tlc)")
        # hcop-0gj / hcop-453 / hcop-5sy: structured choices, unique-window
        # targets and the every-request-a-card surface — pinned in the
        # doctrine bodies and TOOL_DESCRIPTION the same way
        ok("mode: \"choice\"" in _doc["doctrine-on.md"]
           and "exactly ONE" in _doc["doctrine-on.md"]
           and "gets its own card" in _doc["doctrine-on.md"],
           "doctrine-on: structured choices, unique-window locators, one card per request")
        ok("mode:'choice'" in hcserver.TOOL_DESCRIPTION
           and "EXACTLY ONE" in hcserver.TOOL_DESCRIPTION,
           "TOOL_DESCRIPTION: choices and unique locators for a client that loads no doctrine")
        ok("explicit options with their stakes" in
           " ".join(_doc["doctrine-off.md"].split()),
           "doctrine-off: decisions stay structured with the lane off")
        ok(hcserver.brevity_note("Context. " * 30 + "\n1. a\n2. b") is None,
           "brevity: a context line never counts against the steps")
        ok("words above the steps" in hcserver.brevity_note("c " * 90 + "\n1. a"),
           "brevity: an essay above the steps is nudged separately")
        ok("no context line" in hcserver.clarity_note("1. a\n2. b"),
           "clarity: numbered steps with nothing above them earn the nudge")
        ok(hcserver.clarity_note("Hub keeps the last row on screen now.\n\n1. Press G -> row shown") is None,
           "clarity: one line of context above step 1 is enough")
        ok("no context line" in hcserver.clarity_note("| # | do | see |\n|---|---|---|\n| 1 | x | y |"),
           "clarity: a table header is furniture, not context")
        ok("no context line" in hcserver.clarity_note("```\nlog\n```\n1. a"),
           "clarity: a fenced log above step 1 is not context")
        ok(hcserver.clarity_note("just a question, no steps") is None,
           "clarity: prose without steps has nothing to nudge")
        # handoff (hcop-rhr): the four real offenders fire, GUI steps do not
        for i, real in enumerate([
                "Check.\n1. Quit and relaunch `hub`, press G -> row shown",
                "Check.\n1. Run `hub` in a wide terminal (>=180 cols) -> header",
                "Check.\n1. In a terminal run hub -> the project list appears",
                "Check.\n1. Press G -> x\n2. Quit hub, run `hub` again -> y"], 1):
            want = "step 2" if i == 4 else "step 1"
            ok(want in (hcserver.handoff_note(real) or ""),
               f"handoff: real offender {i} is named by step number")
        ok(hcserver.handoff_note(
            "Check.\n1. Click Save @{Firefox · top-right} -> toast\n2. Press G -> row\n"
            "3. Drag the window wider -> more columns\n4. Close this attach window, "
            "click [attach] again -> new window\n5. Leave it in front for 5 s, then tick\n"
            "6. Start the game and press A -> title screen\n"
            "7. Click the Run button @{IDE · top} -> output pane fills") is None,
           "handoff: look/click/press/drag steps are never flagged")
        ok(hcserver.handoff_note("Check.\n```\n1. run `x`\n```\n1. Press G -> row") is None,
           "handoff: a run line inside a fence is not a step")
        ok(hcserver.handoff_note("run `hub` and tell me") is None,
           "handoff: prose without numbered steps is not walked")
        os.environ["HC_TERMINAL"] = "fake-term -e"
        try:
            wrapped = ["env", "-u", "NO_COLOR", "-u", "NODE_DISABLE_COLORS",
                       "--", "hub", "--wide"]
            ok(hcserver.terminal_argv(["hub", "--wide"])
               == ["fake-term", "-e"] + wrapped,
               "copilot-open: HC_TERMINAL is used verbatim, safe command appended")
        finally:
            os.environ.pop("HC_TERMINAL", None)
        old_which = hcserver.shutil.which
        try:
            hcserver.shutil.which = lambda name: (
                "/usr/bin/ptyxis" if name == "ptyxis" else None)
            ptyxis_argv = hcserver.terminal_argv(["hub"], title="Human Copilot")
            ok(ptyxis_argv[:5] == ["ptyxis", "--standalone", "-T",
                                      "Human Copilot", "-x"]
               and ptyxis_argv[-1].endswith("-- hub"),
               "default Ptyxis fallback is standalone, so GApplication cannot drop its command")
        finally:
            hcserver.shutil.which = old_which
        saved_env = {key: os.environ.get(key) for key in
                     ("HC_TERMINAL", "NO_COLOR", "NODE_DISABLE_COLORS",
                      "HC_TEST_KEEP")}
        spawned = {}
        old_popen = hcserver.subprocess.Popen
        try:
            os.environ.pop("NO_COLOR", None)
            os.environ.pop("NODE_DISABLE_COLORS", None)
            os.environ["HC_TEST_KEEP"] = "kept"
            clean_env = hcserver._terminal_env()
            ok("NO_COLOR" not in clean_env
               and "NODE_DISABLE_COLORS" not in clean_env
               and clean_env["HC_TEST_KEEP"] == "kept",
               "copilot-open: a normal environment stays intact")
            os.environ.update({"HC_TERMINAL": "fake-term -e",
                               "NO_COLOR": "1",
                               "NODE_DISABLE_COLORS": "1",
                               "HC_TEST_KEEP": "kept"})

            def capture_popen(argv, **kwargs):
                spawned.update(argv=argv, kwargs=kwargs)
                return object()

            hcserver.subprocess.Popen = capture_popen
            opened = hcserver.open_in_terminal(["hub", "--wide"])
            child_env = spawned["kwargs"]["env"]
            ok(opened == ["fake-term", "-e"] + wrapped
               and "NO_COLOR" not in child_env
               and "NODE_DISABLE_COLORS" not in child_env
               and child_env["HC_TEST_KEEP"] == "kept",
               "copilot-open: child drops color suppression and keeps unrelated env")
            ok(os.environ["NO_COLOR"] == "1"
               and os.environ["NODE_DISABLE_COLORS"] == "1",
               "copilot-open: sanitizing the child does not mutate its parent")
        finally:
            hcserver.subprocess.Popen = old_popen
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        sys.path.insert(0, HERE)
        import open_window  # noqa: F401  (pinned just below)
        out = subprocess.run([sys.executable, os.path.join(HERE, "open_window.py"),
                              "--print", "--no-hold", "hub", "--wide"],
                             env={**os.environ, "HC_TERMINAL": "fake-term -e"},
                             capture_output=True, text=True)
        ok(out.returncode == 0 and out.stdout.strip()
           == "fake-term -e env -u NO_COLOR -u NODE_DISABLE_COLORS -- hub --wide",
           "copilot-open --print shows the argv and spawns nothing")

        # hcop-22h: the window must outlive a command that exits at once — the
        # operator was sent to look at a window that had already closed.
        out = subprocess.run([sys.executable, os.path.join(HERE, "open_window.py"),
                              "--print", "hub", "--wide"],
                             env={**os.environ, "HC_TERMINAL": "fake-term -e"},
                             capture_output=True, text=True)
        held = out.stdout.strip()
        ok(out.returncode == 0 and " bash -c " in held
           and "press Enter to close" in held and held.endswith(" copilot-open hub --wide"),
           "copilot-open holds the window open by default, command last as positionals")
        ok(sys.modules["open_window"].hold(["hub"])[:2] == ["bash", "-c"]
           and sys.modules["open_window"].window_title(["/usr/bin/hub", "-x"]) == "hub",
           "copilot-open: the hold wrapper is bash -c and the window is titled after the program")
        quoted = subprocess.run(
            sys.modules["open_window"].hold(
                [sys.executable, "-c", "import sys;print('|'.join(sys.argv[1:]))",
                 "two words", "it's", "$HOME"]),
            input="", capture_output=True, text=True)
        ok("two words|it's|$HOME" in quoted.stdout
           and "finished (exit 0)" in quoted.stdout,
           "copilot-open: an argument with a space, a quote and a $ survives the wrapper")
        rc7 = subprocess.run(sys.modules["open_window"].hold(["bash", "-c", "exit 7"]),
                             input="", capture_output=True, text=True)
        ok("finished (exit 7)" in rc7.stdout,
           "copilot-open: the banner reports the command's exit code")
        os.environ["HC_TERMINAL"] = "fake-term -e"
        try:
            ok(hcserver.terminal_argv(["hub"], title="hub")
               == hcserver.terminal_argv(["hub"]),
               "copilot-open: a title changes nothing for $HC_TERMINAL "
               "(the operator's own command is used verbatim)")
        finally:
            os.environ.pop("HC_TERMINAL", None)
        ok(hcserver.terminal_argv.__defaults__ == (None,),
           "copilot-open: terminal_argv's title is optional, so the TUI auto-open is untouched")
        out = subprocess.run([sys.executable, os.path.join(HERE, "open_window.py"), "--print", "hub"],
                             env={"PATH": "/nonexistent"}, capture_output=True, text=True)
        ok(out.returncode == 3 and "HC_TERMINAL" in out.stderr,
           "copilot-open with no emulator refuses loudly and names the remedy")

        # pointing the eyes: the Python twin of lib.js::parseLook
        act, see, spot = tui.parse_look('Click Save @{Firefox · top-right} → toast says "Saved"')
        ok(act == "Click Save" and see == 'toast says "Saved"'
           and spot == {"window": "Firefox", "region": "top-right", "rect": None},
           "parse_look lifts the observation and the locator out of a step")
        ok(tui.parse_look("plain") == ("plain", "", None), "parse_look leaves a plain step alone")
        ok(tui.parse_spot("Firefox top right")["region"] == "top-right"
           and tui.parse_spot("upper left")["region"] == "top-left"
           and tui.parse_spot("bottom") == {"window": "", "region": "bottom", "rect": None}
           and tui.parse_spot("z 70%,0,30,10")["rect"] == [0.7, 0.0, 0.3, 0.1]
           and tui.parse_spot("") is None, "parse_spot: regions, synonyms, rects")
        ok(tui.spot_label(tui.parse_spot("Firefox · top-right")) == "Firefox · top-right"
           and tui.spot_label(tui.parse_spot("full")) == "whole screen", "spot_label reads back")
        look = [l for l in tui.md_lines(
            "1. Click Save @{Firefox · top-right} → toast says Saved\n2. plain", 60)]
        ok(look[0][0] == "1. Click Save" and look[1][0] == "   👁 toast says Saved"
           and look[2][0].startswith("   ⌖ Firefox · top-right") and look[3][0] == "2. plain",
           "md: a step's → and @{} become highlighted 👁 / ⌖ lines under it")
        ok(look[1][1] & tui.curses.A_BOLD, "md: the 👁 line is emphasised")
        ok(not any("@{" in l[0] or "→" in l[0] for l in look),
           "md: the raw annotations never reach the operator")
        tl = tui.md_lines("| # | do | see |\n|---|---|---|\n| 1 | click @{top} | toast → says Saved |", 60)
        ok(any(l[0] == "  👁 step 1: says Saved" for l in tl)
           and any(l[0].startswith("  ⌖ top") for l in tl)
           and not any("@{" in l[0] for l in tl),
           "md: table-cell annotations are drawn after the table, named by row")
        ok(tui.step_texts("x\n1. a → b\n2. c\n```\n3. no\n```\n| # | do |\n|---|---|\n| 3 | t |")
           == ["a", "c", "t"], "step_texts counts like the overlay, minus annotations")
        ok(tui.report_template("1. a\n2. b") == "1. pass\n2. pass\n"
           and tui.report_template("prose only") == "",
           "^T template: one 'pass' per step, nothing when there are no steps")

        # Idle redraws include the authority plus the legacy config/doctrine
        # generation markers. Each repaint stats them, but only a changed
        # signature may call the comparatively expensive aggregate state().
        cache_app = object.__new__(tui.CopilotApp)
        cache_app._toggle_signature = None
        cache_app._toggle_snapshot = None
        claude_json = tui.toggle.claude_json_path()
        claude_doc = tui.toggle.doctrine_path()
        control_file = tui.toggle.control_path()
        signatures = {control_file: (5, 50), claude_json: (10, 100),
                      claude_doc: (20, 200)}
        stat_calls = []
        state_calls = []

        class FakeStat:
            def __init__(self, values):
                self.st_mtime_ns, self.st_size = values

        def fake_stat(path):
            stat_calls.append(path)
            return FakeStat(signatures[path])

        def fake_toggle_state():
            state_calls.append(True)
            on = len(state_calls) > 1
            return {"registered": on, "doctrine": "on" if on else "off"}

        old_stat = tui.os.stat
        old_toggle_state = tui.toggle.state
        try:
            tui.os.stat = fake_stat
            tui.toggle.state = fake_toggle_state
            first_state = cache_app._cached_toggle_state()
            same_state = cache_app._cached_toggle_state()
            signatures[claude_json] = (11, 100)  # the toggle's atomic replace
            flipped_state = cache_app._cached_toggle_state()
        finally:
            tui.os.stat = old_stat
            tui.toggle.state = old_toggle_state
        ok(stat_calls == [control_file, claude_json, claude_doc] * 3
           and len(state_calls) == 2 and first_state is same_state
           and not first_state["registered"] and flipped_state["registered"],
           "idle toggle cache includes control generation and refreshes after a flip")

        # Exercise the real modal loop through two typed characters and one
        # resize. The immutable summary should render once at each width, not
        # once per keypress or timer repaint.
        cache_req = {"id": "cache", "summary": "1. keep typing",
                     "created": time.time(), "deadline": time.time() + 60}
        with open(os.path.join(spool, "requests", "cache.json"), "w") as f:
            json.dump(cache_req, f)

        class FakeScreen:
            widths = (80, 80, 96)

            def __init__(self):
                self.frame = 0

            def getmaxyx(self):
                return 30, self.widths[min(self.frame, len(self.widths) - 1)]

            def erase(self): pass
            def addstr(self, *_args): pass
            def move(self, *_args): pass
            def timeout(self, _ms): pass

            def refresh(self):
                self.frame += 1

        class FakeWatch:
            ok = False

            def start(self): return self
            def take(self): return False
            def stop(self): pass

        old_md_lines = tui.md_lines
        old_read_key = tui.read_key
        old_clip_watch = tui.ClipWatch
        old_clip_image = tui.clip_image
        old_curs_set = tui.curses.curs_set
        md_calls = []
        keys = iter((("ch", "a"), ("ch", "b"), ("ch", "\x04")))

        def counted_md_lines(text, width):
            md_calls.append(width)
            return old_md_lines(text, width)

        try:
            tui.md_lines = counted_md_lines
            tui.read_key = lambda _scr, wait_ms=None: next(keys)
            tui.ClipWatch = FakeWatch
            tui.clip_image = lambda: None
            tui.curses.curs_set = lambda _visible: None
            app = object.__new__(tui.CopilotApp)
            app.spool = spool
            app.answered = 0
            app.toast = lambda _message: None
            app.feedback_modal(FakeScreen(), cache_req)
        finally:
            tui.md_lines = old_md_lines
            tui.read_key = old_read_key
            tui.ClipWatch = old_clip_watch
            tui.clip_image = old_clip_image
            tui.curses.curs_set = old_curs_set
        cached_answer = json.load(open(os.path.join(spool, "answers", "cache.json")))
        ok(md_calls == [76, 92] and cached_answer["text"] == "ab",
           "feedback modal renders markdown once per width while typing")

        expired_modal = {"id": "expired-modal", "summary": "type a reply",
                         "created": time.time() - 10,
                         "deadline": time.time() - 1}
        with open(os.path.join(spool, "requests", "expired-modal.json"), "w") as f:
            json.dump(expired_modal, f)
        expired_notices = []
        app.toast = expired_notices.append
        old_read_key = tui.read_key
        old_clip_watch = tui.ClipWatch
        old_clip_image = tui.clip_image
        old_curs_set = tui.curses.curs_set
        try:
            tui.read_key = lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("an expired modal must close before reading input"))
            tui.ClipWatch = FakeWatch
            tui.clip_image = lambda: None
            tui.curses.curs_set = lambda _visible: None
            expired_status = app.feedback_modal(FakeScreen(), expired_modal)
        finally:
            tui.read_key = old_read_key
            tui.ClipWatch = old_clip_watch
            tui.clip_image = old_clip_image
            tui.curses.curs_set = old_curs_set
        ok(expired_status == "withdrawn"
           and "expired" in expired_notices[-1]
           and not os.path.exists(os.path.join(
               spool, "requests", "expired-modal.json")),
           "a modal that expires while a reply is being edited closes and discards the draft visibly")

        fd = tui.hold_presence(spool)
        runtime_path = hcserver.tui_runtime_path(spool)
        runtime_mode = stat.S_IMODE(os.stat(runtime_path).st_mode)
        runtime_body = json.load(open(runtime_path))
        ok(hcserver.tui_alive(spool) is True
           and runtime_mode == 0o600
           and runtime_body["schema"] == "human-copilot-tui-runtime",
           "a real TUI lease plus its private fresh paint heartbeat reads alive")
        runtime_body["pid"] = 2_147_483_647
        with open(runtime_path, "w") as f:
            json.dump(runtime_body, f)
        ok(hcserver.tui_alive(spool) is False,
           "a fresh heartbeat from a dead TUI process cannot borrow another surface's lock")
        hcserver.record_tui_runtime(spool, now=time.time() - 30)
        ok(hcserver.tui_alive(spool) is False,
           "a held lock with a stale TUI heartbeat cannot suppress fallback")
        hcserver.record_tui_runtime(spool)
        os.close(fd)
        ok(hcserver.tui_alive(spool) is False, "released lock reads not-alive")

        ok(tui.decide_attach("f1", None, set()) == "attach", "new image attaches")
        ok(tui.decide_attach("f0", "f0", set()) == "hint",
           "pre-existing clipboard image only hints")
        ok(tui.decide_attach("f1", "f0", {"f1"}) == "ignore",
           "already-attached image is ignored")

        ed = tui.MultiLine()
        for c in "ab":
            ed.key("ch", c)
        ed.key("ch", "\n")
        for c in "cd":
            ed.key("ch", c)
        ok(ed.text() == "ab\ncd", "editor: enter is a newline")
        ed.key("ch", "\x7f"); ed.key("ch", "\x7f"); ed.key("ch", "\x7f")
        ok(ed.text() == "ab", "editor: backspace joins across lines")
    finally:
        shutil.rmtree(spool, ignore_errors=True)

def test_overlay_spotlight_safety():
    """Pin the GNOME 50 failure that leaked giant, undeletable actors, plus
    the hcop-5sy/hcop-453/hcop-0gj source contracts of the card surface."""
    print("overlay spotlight safety (source contract)")
    path = os.path.join(
        HERE, "overlay", "copilot-overlay@croze", "extension.js")
    with open(path) as f:
        source = f.read()
    ok(source.count("    _label(text, style) {") == 1
       and source.count("this._label(") >= 10,
       "the card renderer defines the wrapping label helper used by every card row")
    label_helper = source.split("    _label(text, style) {", 1)[1].split(
        "    _cardKey(v, plan) {", 1)[0]
    ok("set_line_wrap_mode(2" in label_helper,
       "the wrapping label helper falls back to a mid-word break (Pango "
       "WORD_CHAR) so an unbreakable long token (e.g. a Wine Z:\\ path) "
       "wraps fully visible instead of overflowing the card (hcop-7os)")
    scan_guard = source.split("    _scan() {", 1)[1].split(
        "    _scanOnce() {", 1)[0]
    ok("this._scanOnce();" in scan_guard
       and "this._writeRuntimeStatus('error', e);" in scan_guard
       and "this._dropPresence();" in scan_guard,
       "a card-construction exception records render failure and releases presence for fallback")
    ok("answer_filter_version: 1" in source,
       "the live overlay heartbeat identifies the answered-request filter version it loaded")
    ok("window_binding_version: 1" in source
       and "windowBindings: new WindowBindings()" in source
       and "windows, monitors, primary, v.windowBindings" in source
       and "v.windowBindings.resolve(spot.window, records)" in source,
       "frame and canonical geometry share per-request window bindings with runtime version evidence")
    enable_prefix = source.split("    enable() {", 1)[1].split(
        "    disable() {", 1)[0]
    ok("this._holdPresence();" not in enable_prefix
       and "if (this._writeRuntimeStatus('healthy'))" in scan_guard,
       "overlay presence begins only after a successful initial layout and heartbeat")
    draw = source.split("    _drawSpot(owner, rect, step, entry = null, number = 0, pulse = true) {", 1)[1].split(
        "    _clearSpot(id) {", 1)[0]
    enable = source.split("    enable() {", 1)[1].split(
        "    disable() {", 1)[0]
    record = draw.index("this._spots.set(owner, frame);")
    attach = draw.index("Main.layoutManager.addTopChrome(a);")
    ok("addTopChrome(a," not in draw,
       "GNOME 50 addTopChrome receives no rejected options object")
    ok(record < attach and "catch (err)" in draw and "this._clearSpot(owner);" in draw,
       "spot actors are recorded (by owning request) before attach and destroyed on failure")
    ok("spec.bars.map(" in draw and "box-shadow" not in draw,
       "spotlight is edge bars (lib.js::spotFrameSpec), never a potentially filled frame")
    ok("this._sweepOrphanSpots();" in enable,
       "extension enable sweeps actors leaked by an older load")
    # hcop-0p7.5: colour follows the instance; hcop-5sy: EVERY resolved
    # targeted request frames its own target at the same time
    sweep = source.split("    _sweepOrphanSpots() {", 1)[1].split("\n    }", 1)[0]
    ok("isSpotActor(a.name" in sweep and "f5c211" not in sweep and "SPOT_COLOR" not in sweep,
       "the orphan sweep matches by NAME through lib.js::isSpotActor, not by a colour in this file")
    ok("spotFrameSpec(rect, step, entry, number)" in draw and "SPOT_COLOR" not in draw,
       "_drawSpot takes its colour from the instance's palette entry, never a fixed colour")
    ok(source.count("this._drawSpot(") == 1
       and "this._drawSpot(id, plan.frame, plan.cur,\n                               plan.entry, plan.number, this._req === v);" in source,
       "one layout pass frames EVERY resolved targeted request — activity only picks which frame pulses (hcop-5sy)")
    drop = source.split("    _dropView(id) {", 1)[1].split("    // -- layout", 1)[0]
    ok("card.box.destroy();" in drop and "this._clearSpot(id);" in drop,
       "a withdrawal/answer/error takes exactly its OWN card and frame, never another's")
    ok("if (this._grab) {" in source
       and "editingFile" in source and "editingAnswered" in source
       and "editingExpired" in source
       and "this._remoteLease(editing.req)" in source
       and "editing.note = this._entry.get_text();" in source
       and "this._dropNoteGrab();" in source,
       "the note entry freezes ordinary rebuilds but still retires expired, answered, or leased requests")
    ok("this._expireRequest(req);" in source
       and "op: 'expire'" in source
       and "!requestExpired(r)" in source,
       "the overlay filters expired cards and asks the transaction authority to tombstone them")
    ok("name === 'submit-answer' && this._noteReq" in source
       and "symbol === Clutter.KEY_KP_Enter" in source
       and "Super+Alt+Return sends" in source,
       "the note editor accepts the advertised submit chord without requiring a separate save step")
    make_view = source.split("    _makeView(req) {", 1)[1].split(
        "    _takeCard(v) {", 1)[0]
    ok("Array.isArray(req.responder.steps)" in make_view
       and "const steps = stamped ? stamped.map" in make_view
       and "text: typeof spec.instruction === 'string' ? spec.instruction" in make_view,
       "overlay consumes server-stamped canonical step ids and display descriptors")
    # hcop-453: the target is resolved per request; no corner fallback exists
    ok("v.windowBindings.resolve(spot.window, records)" in source
       and "targetExpired(v.unresolvedSince, now)" in source
       and "targetErrorAnswer(res, spot.window)" in source,
       "an unresolved target runs the grace clock and errors to the engine (hcop-453)")
    ok("def _place" not in source and "corners" not in source
       and "placeNearTarget(res.window.frame" in source
       and "stackCards(work, sizes, occupied)" in source,
       "targeted cards anchor beside their window; untargeted ones use the bounded grid — no corner fallback")
    ok("const lastPlaced = rects.reduce((acc, r, i) => r ? i : acc, -1);" in source
       and "plan.dropped = dropped && i === lastPlaced ? dropped : 0;" in source,
       "stack rects stay index-aligned (null = dropped) and the overflow count rides the LAST card that fit")
    ok("[...completed].reverse().find(s => s.spot)?.spot || null" in source
       and "if (!v || v.commitPending)" in source,
       "a completed targeted checklist retains its bound target until one guarded answer commit")
    # hcop-ttc/hcop-qb8: choices remain keyboard-only readable text, while
    # the request, options and typed answer use the enlarged surface type.
    choice_rows = source.split("if (isChoice) {", 1)[1].split("} else {", 1)[0]
    ok("row.connect('button-press-event', (_actor, event) => {" in choice_rows
       and "return Clutter.EVENT_STOP;" in choice_rows
       and "this._submitView(v)" not in choice_rows
       and "this._copyRow(v," in choice_rows,
       "choice rows allow copying without selecting an option or submitting")
    ok("font-size: 12.5pt" in source and "font-size: 11.5pt" in source,
       "overlay request, option, and answer typography uses the larger readable scale")
    # hcop-0gj: an empty send is ack-only
    ok("emptySubmitNote(v.mode, looksLikeChoices(v.req.summary))" in source
       and "choiceAnswer(v.options, v.sel, v.note, images)" in source,
       "submit refuses empty sends outside ack mode and uses the tested choice reply contract")
    ok("checklistAnswer(v.req, steps)" in source
       and "answer.checklist = checklist;" in source,
       "checklist submit pairs its prose with the request-bound structured verdict")
    ok(source.count("parseResponse(req)") >= 1
       and "modeHint(v.mode, v.steps, accels, v.sel, v.options.length)" in source,
       "every card renders by its response mode with a mode-aware NOW line")
    ok("GLib.build_filenamev(\n            [this.path, '..', '..', 'responder.py'])" in source
       and "canonicalize_filename" not in source,
       "the responder helper path follows the installed extension symlink before traversing to the workspace")
    link_root = tempfile.mkdtemp(prefix="hc-overlay-link-")
    try:
        project = os.path.join(link_root, "project")
        extension_target = os.path.join(project, "overlay", "copilot-overlay@croze")
        install = os.path.join(link_root, "home", ".local", "share",
                               "gnome-shell", "extensions")
        os.makedirs(extension_target)
        os.makedirs(install)
        helper = os.path.join(project, "responder.py")
        with open(helper, "w") as f:
            f.write("helper")
        installed = os.path.join(install, "copilot-overlay@croze")
        os.symlink(extension_target, installed)
        traversed = os.path.join(installed, "..", "..", "responder.py")
        ok(open(traversed).read() == "helper" and os.path.samefile(traversed, helper),
           "the uncanonicalized ../../ helper path resolves through the installed symlink in kernel order")
    finally:
        shutil.rmtree(link_root, ignore_errors=True)
    ok("GLib.compute_checksum_for_string(" in source
       and "JSON.stringify({request_id: id, lifecycle, answer: obj})" in source,
       "overlay retry ids bind the exact answer snapshot instead of reusing an id after edits")
    ok("const row = this._copyRow(v," in source
       and "choiceDisplayText(o, i, sel)" in source
       and "const content = this._label(display, style);" in source
       and "row.set_reactive(true);" in source
       and "row.set_x_expand(true);" in source,
       "choice rows keep complete wrapped text beside their copy control")
    ok("new St.ScrollView({" in source
       and "vscrollbar_policy: St.PolicyType.AUTOMATIC" in source
       and "`max-height: ${CHOICE_VIEW_H}px;`" in source
       and "choiceNeedsScroll(v.options)" in source,
       "tall choice sets have a bounded scroll viewport and an explicit affordance")
    # hcop-m8n: a complete answer is no longer an actionable card, including
    # after a shell restart; only conclusive PID+boot+start death can reclaim.
    ok("const answerFiles = this._answerFiles();" in source
       and "rawReqs.filter(r => !answerFiles.has(r.id) &&" in source
       and "!requestExpired(r)" in source
       and "this._answered.add(id);" in source,
       "the overlay never resurrects or misreports a request already answered on any surface")
    orphan = source.split("    _reclaimAnsweredOrphans(reqs, answers) {", 1)[1].split(
        "\n    _answerPath(id) {", 1)[0]
    ok("this._callerState(req.caller_process) !== 'dead'" in orphan
       and "now - answer.modified < ORPHAN_GRACE_S" in orphan
       and orphan.index("const requestPath") < orphan.index("'answers', req.id + '.json'"),
       "overlay orphan cleanup requires conclusive dead ownership plus grace and hides the request first")

# ------------------------------------------- 6. instance stamp + attached check
# hcop-0p7.1: every request carries instance {session, pane, server_uuid,
# attached}; the verdict comes from a stubbed `zellij` first on PATH; the
# default unattached behaviour is today's path and HC_REQUIRE_ATTACHED=1 is
# the opt-in refusal. server.py is driven as a real subprocess each time.

_ZELLIJ_STUB = r'''#!/bin/bash
# records argv and where stdin points (the protocol pipe must never be
# inherited), then prints list-clients' real shape with $HC_STUB_CLIENTS rows.
# new-pane (hcop-0p7.4) prints the created pane id the way 0.44.3 does, or
# fails when HC_STUB_NEWPANE_FAIL is set; show-floating-panes / focus-pane-id
# succeed silently. No pane, window or session is ever really opened.
echo "$* stdin=$(readlink /proc/$$/fd/0)" >> "$HC_STUB_LOG"
case " $* " in
  *" new-pane "*)
    [ -n "$HC_STUB_NEWPANE_FAIL" ] && { echo "stub: no pane for you" >&2; exit 1; }
    echo "terminal_7"; exit 0 ;;
  *" show-floating-panes "*|*" focus-pane-id "*) exit 0 ;;
esac
echo "CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND"
for i in $(seq 1 "${HC_STUB_CLIENTS:-0}"); do
  echo "$i         terminal_0     node codex -C /x 1. the whole agent prompt rides here"
done
'''

def _instance_call(case_env, expect_request=True, calls=1, *, spawn_timeout=5):
    """One server process, one tools/call. Returns (result, request_body,
    zellij_log_lines, spawn_lines, server_uuid_of_second_call)."""
    spool = tempfile.mkdtemp(prefix="hc-inst-")
    bindir = os.path.join(spool, "bin"); os.makedirs(bindir)
    rec = os.path.join(spool, "spawns.log")
    zlog = os.path.join(spool, "zellij.log")
    with open(os.path.join(bindir, "fake-term"), "w") as f:
        f.write('#!/bin/bash\n'
                'if [ -n "$HC_STUB_TERM_PIDFILE" ]; then echo $$ > "$HC_STUB_TERM_PIDFILE"; fi\n'
                'if [ -n "$HC_STUB_TERM_DELAY" ]; then sleep "$HC_STUB_TERM_DELAY"; fi\n'
                '[ "$HC_STUB_TERM_SKIP_LOG" = 1 ] && exit 0\n'
                f'echo "$@" >> "{rec}"\n')
    with open(os.path.join(bindir, "notify-send"), "w") as f:
        f.write('#!/bin/bash\nexit 0\n')
    with open(os.path.join(bindir, "zellij"), "w") as f:
        f.write(_ZELLIJ_STUB)
    for n in ("fake-term", "notify-send", "zellij"):
        os.chmod(os.path.join(bindir, n), 0o755)
    # the selftest itself may run inside zellij: start from a clean slate
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZELLIJ") and k != "HC_REQUIRE_ATTACHED"}
    env.update({"HC_SPOOL": spool, "HC_TERMINAL": os.path.join(bindir, "fake-term"),
                "PATH": bindir + ":" + os.environ["PATH"], "HC_STUB_LOG": zlog})
    env.update(case_env)
    proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env)
    bodies = []
    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "claude-code"}}})
        _read(proc, 1)
        rdir = os.path.join(spool, "requests")
        def operator():
            for _ in range(200):
                reqs = ([f for f in os.listdir(rdir) if f.endswith(".json")]
                        if os.path.isdir(rdir) else [])
                if reqs:
                    rid = reqs[0][:-5]
                    bodies.append(json.load(open(os.path.join(rdir, reqs[0]))))
                    tmp = os.path.join(spool, "answers", rid + ".json.t")
                    with open(tmp, "w") as f:
                        json.dump({"text": "✔ 1.", "cancelled": False, "images": []}, f)
                    os.replace(tmp, os.path.join(spool, "answers", rid + ".json"))
                    return
                time.sleep(0.05)
        # a refused call answers synchronously with nothing spooled, so it
        # gets no operator thread (one polling for 10 s would only find the
        # absence the leftover check already proves)
        calls = calls if expect_request else 1
        result = None
        for k in range(calls):
            t = threading.Thread(target=operator) if expect_request else None
            if t:
                t.start()
            _rpc(proc, {"jsonrpc": "2.0", "id": 10 + k, "method": "tools/call",
                        "params": {"name": "interactive_feedback",
                                   "arguments": {"summary": "Context line.\n1. Look at it",
                                                 "timeout": 60}}})
            r = _read(proc, 10 + k)
            if t:
                t.join(timeout=12)
            if k == 0:
                result = r["result"]
        # Popen returning and the MCP reply do not prove the detached shell
        # has written its launch log. Wait for evidence only when this case
        # expects a desktop terminal, with a deadline for a missing launch.
        wants_terminal = any(not body.get("instance", {}).get("attached")
                             for body in bodies)
        until = time.monotonic() + spawn_timeout
        while wants_terminal and time.monotonic() < until:
            if os.path.exists(rec) and os.path.getsize(rec):
                break
            time.sleep(0.01)
        zl = open(zlog).read().splitlines() if os.path.exists(zlog) else []
        sp = open(rec).read().splitlines() if os.path.exists(rec) else []
        leftover = os.listdir(rdir) if os.path.isdir(rdir) else []
        return result, bodies, zl, sp, leftover
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait(timeout=5)
        # The terminal is deliberately detached by the production launcher.
        # On a failed/timeout case stop only this uniquely named fixture's
        # process group before removing files a delayed stub might still use.
        stub = os.fsencode(os.path.join(bindir, "fake-term"))
        for name in os.listdir("/proc"):
            if not name.isdigit():
                continue
            try:
                with open(f"/proc/{name}/cmdline", "rb") as source:
                    argv = source.read().split(b"\0")
                pid = int(name)
                if stub in argv and os.getpgid(pid) == pid:
                    os.killpg(pid, 9)
                    until = time.monotonic() + 1
                    while time.monotonic() < until:
                        with open(f"/proc/{pid}/stat") as source:
                            if source.read().rsplit(")", 1)[1].split()[0] == "Z":
                                break
                        time.sleep(0.01)
            except (OSError, ProcessLookupError):
                pass
        shutil.rmtree(spool, ignore_errors=True)

@_isolated_legacy_server_test
def test_instance_stamp():
    print("instance stamp + attached check (server.py subprocess, stubbed zellij)")
    import server as hcserver
    ok(hcserver.REQUIRE_ATTACHED_DEFAULT is False,
       "the named default is today's path: unattached calls are NOT refused (operator decision hcop-0p7.9)")
    ok(len(hcserver.SERVER_UUID) == 32 and all(c in "0123456789abcdef" for c in hcserver.SERVER_UUID),
       "SERVER_UUID is one hex uuid4 per process")

    # (a) under zellij, the stub lists 1 client
    zenv = {"ZELLIJ_SESSION_NAME": "hc-test-session", "ZELLIJ_PANE_ID": "0",
            "HC_STUB_CLIENTS": "1"}
    res, bodies, zl, sp, left = _instance_call(zenv, calls=2)   # two calls: uuid stability
    inst = bodies[0].get("instance")
    ok(isinstance(inst, dict) and set(inst) == {"session", "pane", "server_uuid", "attached"},
       "request JSON carries instance {session, pane, server_uuid, attached}")
    ok(inst["session"] == "hc-test-session" and inst["pane"] == "0",
       "stamp names the zellij session and pane the server inherited")
    ok(len(inst["server_uuid"]) == 32 and bodies[1]["instance"]["server_uuid"] == inst["server_uuid"],
       "server_uuid is minted once per server process (same across two calls)")
    ok(inst["attached"] is True, "stub lists 1 client -> attached: true")
    ok(len(zl) >= 1 and zl[0].startswith("--session hc-test-session action list-clients"),
       "the verdict came from `zellij --session <S> action list-clients`")
    ok(all(ln.endswith("stdin=/dev/null") for ln in zl),
       "the probe never inherits the protocol stdin")
    ok(res["isError"] is False and res["content"][0]["text"] == "✔ 1.",
       "an attached call answers normally over the wire")
    uuid_a = inst["server_uuid"]

    # (b) under zellij, the stub lists 0 clients: today's path, unchanged
    res, bodies, zl, sp, left = _instance_call({**zenv, "HC_STUB_CLIENTS": "0"})
    ok(bodies[0]["instance"]["attached"] is False, "stub lists 0 clients -> attached: false")
    ok(bodies[0]["instance"]["session"] == "hc-test-session",
       "a detached session is still named in the stamp")
    ok(bodies[0]["instance"]["server_uuid"] != uuid_a,
       "a second server process carries a different server_uuid")
    ok(res["isError"] is False and res["content"][0]["text"] == "✔ 1." and not left,
       "DEFAULT unattached: the request is still written and answered normally")
    ok(len(sp) == 1 and "tui.py" in sp[0],
       "DEFAULT unattached: the terminal auto-open still fires (today's path)")

    # (c) not under zellij at all
    res, bodies, zl, sp, left = _instance_call({"HC_STUB_CLIENTS": "1"})
    inst = bodies[0]["instance"]
    ok(inst["session"] is None and inst["pane"] is None,
       "no ZELLIJ_* -> session and pane are null")
    ok(inst["attached"] is None, "no ZELLIJ_* -> attached: null (not under zellij)")
    ok(zl == [], "no ZELLIJ_* -> zellij is never probed")
    ok(res["isError"] is False and res["content"][0]["text"] == "✔ 1.",
       "a plain-terminal session answers normally (today's path)")

    # (d) the opt-in: HC_REQUIRE_ATTACHED=1 with 0 clients refuses over the wire
    res, bodies, zl, sp, left = _instance_call(
        {**zenv, "HC_STUB_CLIENTS": "0", "HC_REQUIRE_ATTACHED": "1"}, expect_request=False)
    text = res["content"][0]["text"]
    ok(res["isError"] is False and "refused" in text and "no attached client" in text
       and "hc-test-session" in text and "do not open another" in text.lower(),
       "HC_REQUIRE_ATTACHED=1 + 0 clients -> the refusal text comes back over the wire")
    ok(bodies == [] and not left and sp == [],
       "a refused call spools no request and opens no terminal")
    # ...and the opt-in never refuses an ATTACHED session
    res, bodies, zl, sp, left = _instance_call(
        {**zenv, "HC_STUB_CLIENTS": "1", "HC_REQUIRE_ATTACHED": "1"})
    ok(res["content"][0]["text"] == "✔ 1." and bodies[0]["instance"]["attached"] is True,
       "HC_REQUIRE_ATTACHED=1 + 1 client -> proceeds normally")
    # ...and refuses a non-zellij session too, naming that case
    res, bodies, zl, sp, left = _instance_call(
        {"HC_REQUIRE_ATTACHED": "1"}, expect_request=False)
    ok("refused" in res["content"][0]["text"] and "not running under zellij" in res["content"][0]["text"],
       "HC_REQUIRE_ATTACHED=1 outside zellij -> refusal names the non-zellij case")

    # the probe's edges, in-process: parsing, hang, failure, PATH
    rows = hcserver._client_rows
    ok(rows("CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n2         terminal_0     node codex -C /x\n") and
       rows("CLIENT_ID ZELLIJ_PANE_ID RUNNING_COMMAND\n") == [],
       "list-clients rows are counted by a leading client id under the header")
    ok(rows("Session 'x' not found. The following sessions are active:\n\x1b[32;1mzhub\x1b[m [Created 1m ago]\n") is None,
       "the exit-0 'not found' prose (measured on 0.44.3) yields no verdict, not a client")
    ok(hcserver.zellij_attached(None) is None, "zellij_attached(None) -> None")
    tmp = tempfile.mkdtemp(prefix="hc-probe-")
    old_path, old_to = os.environ.get("PATH"), hcserver.ATTACH_PROBE_TIMEOUT_S
    try:
        os.environ["PATH"] = tmp                                # no zellij at all
        ok(hcserver.zellij_attached("s") is False, "no zellij on PATH -> attached: false")
        with open(os.path.join(tmp, "zellij"), "w") as f:
            f.write("#!/bin/bash\nexit 1\n")
        os.chmod(os.path.join(tmp, "zellij"), 0o755)
        ok(hcserver.zellij_attached("s") is False, "zellij exiting non-zero -> attached: false")
        with open(os.path.join(tmp, "zellij"), "w") as f:
            f.write("#!/bin/bash\nsleep 30\n")
        hcserver.ATTACH_PROBE_TIMEOUT_S = 0.5
        t0 = time.time()
        verdict = hcserver.zellij_attached("s")
        ok(verdict is False and time.time() - t0 < 5,
           "a hanging zellij is cut at the probe timeout and reads as not attached")
    finally:
        os.environ["PATH"] = old_path
        hcserver.ATTACH_PROBE_TIMEOUT_S = old_to
        shutil.rmtree(tmp, ignore_errors=True)
    old_req = os.environ.pop("HC_REQUIRE_ATTACHED", None)
    try:
        ok(hcserver.require_attached() is hcserver.REQUIRE_ATTACHED_DEFAULT,
           "HC_REQUIRE_ATTACHED unset -> the named default")
        os.environ["HC_REQUIRE_ATTACHED"] = "0"
        ok(hcserver.require_attached() is False, "HC_REQUIRE_ATTACHED=0 -> today's path")
        os.environ["HC_REQUIRE_ATTACHED"] = "yes"
        ok(hcserver.require_attached() is True, "HC_REQUIRE_ATTACHED=yes -> refusal armed")
    finally:
        if old_req is None:
            os.environ.pop("HC_REQUIRE_ATTACHED", None)
        else:
            os.environ["HC_REQUIRE_ATTACHED"] = old_req

# ------------------------------------------------ 5. overlay hotkeys vs GNOME

def test_overlay_hotkeys_unclaimed():
    """Every overlay default chord must be one GNOME does not already grab.

    hcop-9rz: attach-snip shipped as <Super><Alt>s — Ubuntu 26.04's
    screen-reader toggle; whichever grab won, the loser did nothing or
    toggled Orca. The inventory (tests/gnome_keybindings.py) is the live
    gsettings dump of the wm/shell/mutter/media-keys schemas, the media-keys
    custom children and every other extension's schema; with no
    gsettings/dconf it falls back to tests/gnome-keybindings.snapshot. The
    source is PRINTED and asserted — a run that could not measure must never
    look like one that did. Chords are compared normalised (modifier order,
    <Primary>/<Ctrl> → Control, single-key case).
    """
    print("overlay hotkeys vs GNOME bindings")
    sys.path.insert(0, os.path.join(HERE, "tests"))
    import gnome_keybindings as gk
    ok(gk.normalize("<Alt><Super>8") == gk.normalize("<Super><Alt>8")
       and gk.normalize("<Primary>A") == gk.normalize("<Ctrl>a")
       == gk.normalize("<Control>a") and gk.normalize("") is None
       and gk.normalize("firefox.desktop") is None,
       "chord normalisation: modifier order, Primary/Ctrl, key case, non-chords")
    ok(gk.parse_string_array("@as []") == []
       and gk.parse_string_array("['one', 'two']") == ["one", "two"]
       and gk.parse_string_array("@as ['typed']") == ["typed"],
       "gsettings string arrays accept typed-empty and preserve populated values")

    def malformed_string_array(value):
        try:
            gk.parse_string_array(value, "test inventory")
        except gk.Unavailable as error:
            return "test inventory" in str(error)
        return False

    ok(malformed_string_array("@as nope")
       and malformed_string_array("{'not': 'an array'}")
       and malformed_string_array("[1, 2]"),
       "malformed gsettings inventory becomes a named unavailable source")
    defaults = gk.overlay_defaults()
    ok(len(defaults) >= 8 and all(defaults.values()),
       f"overlay schema declares {len(defaults)} bound hotkeys")
    flat = [gk.normalize(a) for accels in defaults.values() for a in accels]
    ok(None not in flat and len(flat) == len(set(flat)),
       "overlay defaults are distinct, parseable chords")
    try:
        taken, source = gk.load_inventory()
    except gk.Unavailable as e:
        taken, source = {}, None
        ok(False, f"no keybinding inventory at all: {e}")
    print(f"  source: {source} — {len(taken)} taken chords")
    ok(source.startswith("live gsettings") or source.startswith("SNAPSHOT"),
       "inventory source is named (live gsettings or the committed snapshot)")
    hits = gk.collisions(defaults, taken)
    detail = "".join(
        f"\n        overlay {name} = {a} COLLIDES with "
        + ", ".join(f"{sc} {k} ({o})" for sc, k, o in hit)
        for name, a, hit in hits)
    ok(not hits, "no overlay default chord is bound by GNOME, mutter, "
       "gnome-settings-daemon or another extension" + detail)
    # The inventory must actually SEE the chord that caused hcop-9rz (written
    # <Alt><Super>s by GNOME) or the check above is vacuous on this box.
    hit = gk.collisions({"attach-snip": ["<Super><Alt>s"]}, taken)
    ok(hit and any(k == "screenreader" for _, k, _ in hit[0][2]),
       "inventory sees media-keys screenreader as <Super><Alt>s (order-normalised)")
    # The bus-less path: gsettings unavailable → the snapshot, and it says so.
    real = gk.dump_live
    gk.dump_live = lambda: (_ for _ in ()).throw(gk.Unavailable("stubbed: no dconf"))
    try:
        snap_taken, snap_source = gk.load_inventory()
    finally:
        gk.dump_live = real
    ok(snap_source.startswith("SNAPSHOT") and "no dconf" in snap_source
       and not gk.collisions(defaults, snap_taken)
       and gk.collisions({"x": ["<Super><Alt>s"]}, snap_taken),
       "without gsettings the committed snapshot is used, named, and still catches <Super><Alt>s")
    if source.startswith("live") and set(snap_taken) != set(taken):
        print("  NOTE: tests/gnome-keybindings.snapshot has drifted from live "
              "gsettings — regenerate: python3 tests/gnome_keybindings.py "
              "> tests/gnome-keybindings.snapshot")

# ------------------------------------ 7. presence + cooldown keyed per instance
# hcop-0p7.7: spool/instances/<id>/{tui.lock,spawn.stamp}, <id> = the zellij
# session when set, else the server uuid (a non-zellij server is its own
# instance). A request is served when the GLOBAL spool/tui.lock is held (a
# surface for every instance: copilot-tui with no --instance, the overlay) OR
# its own instance lock is. The storm guard opens ONE window per instance per
# cooldown and never tells another instance "a window was opened" about a
# window that was not its own. Every stage runs inside one 15 s cooldown
# window. The stages are independent (presence is per SPOOL), so each gets its
# own spool and they run concurrently; verdicts are collected and asserted from
# the main thread in a fixed order — each server call costs the server's 0.3 s
# answer poll and the e2e perf budget for this whole file is 6 s.

class _Server:
    """One server.py subprocess on a spool whose terminal, notify-send and
    zellij (1 attached client) are stubbed on PATH. Calls on one spool are
    serialized by the caller, so the stub operator answers THIS request."""
    def __init__(self, spool, case_env):
        bindir = os.path.join(spool, "bin")
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("ZELLIJ") and k != "HC_REQUIRE_ATTACHED"}
        env.update({"HC_SPOOL": spool,
                    "HC_CONTROL_FILE": os.path.join(spool, "test-control.json"),
                    "HOME": os.path.join(spool, "test-home"),
                    "CODEX_HOME": os.path.join(spool, "test-home", ".codex"),
                    "HC_TERMINAL": os.path.join(bindir, "fake-term"),
                    "PATH": bindir + ":" + os.environ["PATH"],
                    "HC_STUB_LOG": os.path.join(spool, "zellij.log"),
                    "HC_STUB_CLIENTS": "1"})
        env.update(case_env)
        self.spool, self.n = spool, 10
        self.proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                                     stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.DEVNULL, text=True, env=env)
        _rpc(self.proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                         "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                                    "clientInfo": {"name": "claude-code"}}})
        _read(self.proc, 1)

    def call(self, before_answer=None):
        """One tools/call, answered by a stub operator. Returns the request body."""
        self.n += 1
        rdir = os.path.join(self.spool, "requests")
        seen = []
        def operator():
            for _ in range(200):
                reqs = ([f for f in os.listdir(rdir) if f.endswith(".json")]
                        if os.path.isdir(rdir) else [])
                if reqs:
                    rid = reqs[0][:-5]
                    seen.append(json.load(open(os.path.join(rdir, reqs[0]))))
                    if before_answer is not None:
                        before_answer()
                    tmp = os.path.join(self.spool, "answers", rid + ".json.t")
                    with open(tmp, "w") as f:
                        json.dump({"text": "✔ 1.", "cancelled": False, "images": []}, f)
                    os.replace(tmp, os.path.join(self.spool, "answers", rid + ".json"))
                    return
                time.sleep(0.02)
        t = threading.Thread(target=operator); t.start()
        _rpc(self.proc, {"jsonrpc": "2.0", "id": self.n, "method": "tools/call",
                         "params": {"name": "interactive_feedback",
                                    "arguments": {"summary": "Context line.\n1. Look at it",
                                                  "timeout": 60}}})
        _read(self.proc, self.n)
        t.join(timeout=12)
        return seen[0] if seen else {}

    def stop(self):
        self.proc.stdin.close(); self.proc.terminate(); self.proc.wait(timeout=5)

_ZA = {"ZELLIJ_SESSION_NAME": "hc-A", "ZELLIJ_PANE_ID": "0"}
_ZB = {"ZELLIJ_SESSION_NAME": "hc-B", "ZELLIJ_PANE_ID": "0"}

class _PresenceStage:
    """A throwaway spool with the three stubs, its own servers, and a
    verdict list [(cond, what)] emitted by the main thread afterwards."""
    def __init__(self):
        self.spool = tempfile.mkdtemp(prefix="hc-pres-")
        self.control = os.path.join(self.spool, "test-control.json")
        write_control_fixture(self.control, True)
        write_enabled_home_fixture(os.path.join(self.spool, "test-home"))
        bindir = os.path.join(self.spool, "bin"); os.makedirs(bindir)
        self.rec = os.path.join(self.spool, "spawns.log")
        with open(os.path.join(bindir, "fake-term"), "w") as f:
            f.write(f'#!/bin/bash\necho "$@" >> "{self.rec}"\n')
        with open(os.path.join(bindir, "notify-send"), "w") as f:
            f.write('#!/bin/bash\nexit 0\n')
        with open(os.path.join(bindir, "zellij"), "w") as f:
            f.write(_ZELLIJ_STUB)
        for n in ("fake-term", "notify-send", "zellij"):
            os.chmod(os.path.join(bindir, n), 0o755)
        self.servers, self.checks, self.error = [], [], None

    def server(self, case_env):
        s = _Server(self.spool, case_env)
        self.servers.append(s)
        return s

    def terminals(self):
        """Desktop terminal windows opened (the fake-term stub's lines)."""
        return open(self.rec).read().splitlines() if os.path.exists(self.rec) else []

    def zlog(self):
        """Every zellij invocation the stub saw, one line each."""
        z = os.path.join(self.spool, "zellij.log")
        return open(z).read().splitlines() if os.path.exists(z) else []

    def spawns(self):
        """Every auto-open on this spool: terminal windows AND copilot panes
        (hcop-0p7.4: an attached instance opens a pane inside its session, a
        non-zellij or detached one a terminal window). The guards under test —
        presence, cooldown, one per instance — apply to both kinds alike."""
        return self.terminals() + [ln for ln in self.zlog() if " action new-pane " in ln]

    def clear_stamps(self):              # end a cooldown without waiting 15 s
        for root, _dirs, files in os.walk(self.spool):
            for f in files:
                if f == "spawn.stamp":
                    os.unlink(os.path.join(root, f))

    def check(self, cond, what):
        self.checks.append((bool(cond), what))

    def run(self, body):
        try:
            body(self)
        except BaseException as e:       # surfaces as a failed check, never a hang
            self.error = e
        finally:
            for s in self.servers:
                s.stop()

    def emit(self):
        if self.error is not None:
            ok(False, f"stage raised: {self.error!r}")
        for cond, what in self.checks:
            ok(cond, what)

def _stage_both_spawn(st):
    """Two instances inside one cooldown window both open; the storm guard
    stays within one instance."""
    A, B = st.server(_ZA), st.server(_ZB)
    A.call(); B.call()
    st.check(len(st.spawns()) == 2,
             "two instances inside one cooldown window: BOTH auto-open (B is never told A's window was its own)")
    good = True
    for iid in ("hc-A", "hc-B"):
        d = os.path.join(st.spool, "instances", iid)
        stamp = os.path.join(d, "spawn.stamp")
        good = good and stat.S_IMODE(os.stat(d).st_mode) == 0o700 \
            and os.path.getsize(stamp) > 0 \
            and stat.S_IMODE(os.stat(stamp).st_mode) == 0o600
    st.check(good, "instances/<id>/ is 0700 and holds a written 0600 spawn.stamp per instance")
    st.check(not os.path.exists(os.path.join(st.spool, "spawn.stamp")),
             "instance calls write no global spawn.stamp")
    A2 = st.server(_ZA)                 # same session, new uuid = same instance
    A2.call()
    st.check(len(st.spawns()) == 2,
             "storm guard within one instance: a second server in the SAME session opens nothing inside the cooldown")

def _stage_instance_lock(st):
    """A surface holding B's lock serves B only."""
    import server as hcserver
    import tui
    A, B = st.server(_ZA), st.server(_ZB)
    fd_b = tui.hold_presence(st.spool, "hc-B")
    try:
        st.check(hcserver.tui_alive(st.spool, "hc-B") is True
                 and hcserver.tui_alive(st.spool, "hc-A") is False
                 and hcserver.tui_alive(st.spool) is False,
                 "an instance lock reads alive for its own instance only, never as the global lock")
        A.call()
        st.check(len(st.spawns()) == 1,
                 "a surface holding instance B's lock does NOT suppress A's auto-open")
        B.call()
        st.check(len(st.spawns()) == 1, "...and does suppress B's own")
    finally:
        os.close(fd_b)

def _stage_global_lock(st):
    """The global lock is presence for every instance's TERMINAL auto-open.
    It does not stand in for an ATTACHED session's pane (hcop-0p7.4 / the
    hcop-0p7.8 decision: the overlay and the sidepanel are both meant to be
    up), so an attached instance still gets its pane while a detached zellij
    instance and a non-zellij one open nothing."""
    import server as hcserver
    import tui
    fd_g = tui.hold_presence(st.spool)
    try:
        st.check(hcserver.tui_alive(st.spool, "hc-A") and hcserver.tui_alive(st.spool, "hc-B")
                 and hcserver.tui_alive(st.spool, "never-seen") and hcserver.tui_alive(st.spool),
                 "the global lock reads alive for every instance, seen before or not")
        A = st.server({**_ZA, "HC_STUB_CLIENTS": "0"})   # A: zellij, detached
        C = st.server({})                                # C: not under zellij
        A.call(); C.call()
        st.check(len(st.spawns()) == 0,
                 "the global lock suppresses the terminal auto-open for every instance, zellij or not")
        B = st.server(_ZB)                               # B: zellij, ATTACHED
        B.call()
        st.check(len(st.terminals()) == 0 and len(st.spawns()) == 1
                 and st.spawns()[0].startswith("--session hc-B action new-pane"),
                 "…but an ATTACHED session still gets its own pane: the global surface does not stand in for it")
    finally:
        os.close(fd_g)

def _stage_non_zellij(st):
    """A non-zellij server is its own instance, keyed by uuid."""
    import server as hcserver
    C, D = st.server({}), st.server({})
    cb, db = C.call(), D.call()
    cid, did = hcserver.instance_id(cb["instance"]), hcserver.instance_id(db["instance"])
    st.check(cid == cb["instance"]["server_uuid"] and did != cid and len(st.spawns()) == 2
             and os.path.getsize(os.path.join(st.spool, "instances", cid, "spawn.stamp")) > 0
             and os.path.getsize(os.path.join(st.spool, "instances", did, "spawn.stamp")) > 0,
             "two non-zellij servers are two instances, keyed by uuid: both auto-open")
    st.cid = cid

def _stage_renderer_drops_presence(st):
    """A request already waiting when the overlay crashes gets a fallback."""
    import tui
    fd = tui.hold_presence(st.spool)
    released = False
    try:
        server = st.server({})

        def after_request_arrives():
            nonlocal released
            # Let run_feedback observe the initially held global lease first;
            # this is a mid-wait renderer failure, not the ordinary no-surface
            # startup path.
            time.sleep(0.6)
            st.check(st.terminals() == [],
                     "a held renderer lease initially suppresses terminal fallback")
            os.close(fd)
            released = True
            end = time.time() + 6
            while time.time() < end and not st.terminals():
                time.sleep(0.05)
            st.check(len(st.terminals()) == 1,
                     "when that renderer drops presence, the waiting request opens one terminal before expiry")

        server.call(before_answer=after_request_arrives)
    finally:
        if not released:
            os.close(fd)

@_isolated_legacy_server_test
def test_instance_presence():
    print("presence + cooldown per instance (server.py processes on 4 concurrent spools, stubbed terminal)")
    import server as hcserver
    import toggle
    import tui
    ok(hcserver.instance_id({"session": "hc-A", "server_uuid": "u" * 32}) == "hc-A",
       "instance id = the zellij session when set")
    ok(hcserver.instance_id({"session": None, "server_uuid": "u" * 32}) == "u" * 32,
       "...else the server uuid (a non-zellij server is its own instance)")
    ok(hcserver.instance_id({}) is None and hcserver.instance_id(None) is None,
       "no stamp -> None (a pre-stamp request belongs to every surface)")
    odd = hcserver.instance_id({"session": "../x/y z"})
    ok(odd and "/" not in odd and " " not in odd and not odd.startswith("."),
       "a hostile session name becomes one plain path segment")

    stages = [(_PresenceStage(), body) for body in
              (_stage_both_spawn, _stage_instance_lock, _stage_global_lock,
               _stage_non_zellij, _stage_renderer_drops_presence)]
    threads = [threading.Thread(target=st.run, args=(body,)) for st, body in stages]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        for st, _body in stages:
            st.emit()

        # toggle state on the instance-lock stage's spool (it has hc-A and hc-B):
        # additive per-instance evidence, v1 fields intact, nothing created
        lock_stage, nz_stage = stages[1][0], stages[3][0]
        shutil.copytree(os.path.join(nz_stage.spool, "instances", nz_stage.cid),
                        os.path.join(lock_stage.spool, "instances", nz_stage.cid))
        fd_b = tui.hold_presence(lock_stage.spool, "hc-B")
        inst_dir = os.path.join(lock_stage.spool, "instances")
        before = sorted(os.path.join(r, f) for r, _d, fs in os.walk(inst_dir) for f in fs)
        ev = toggle.build_status(spool=lock_stage.spool)["evidence"]["tui_lock"]
        os.close(fd_b)
        v1 = {"state", "digest", "size_bytes", "location_source", "holder_state",
              "surface_state", "prerequisites_met"}
        rows = {r["id"]: r for r in ev["instances"]}
        ok(v1 <= set(ev) and ev["surface_state"] == "inactive",
           "toggle state keeps every v1 tui_lock field and its meaning (global lock unheld -> inactive)")
        ok(ev["instances_state"] == "present"
           and rows["hc-B"]["held"] is True and rows["hc-B"]["holder_state"] == "held"
           and rows["hc-A"]["held"] is False and nz_stage.cid in rows
           and ev["instances_held"] == ["hc-B"],
           "toggle state lists each instance lock with its held flag (additive fields)")
        after = sorted(os.path.join(r, f) for r, _d, fs in os.walk(inst_dir) for f in fs)
        ok(before == after, "reading the per-instance evidence creates nothing")
    finally:
        for st, _body in stages:
            shutil.rmtree(st.spool, ignore_errors=True)

    # the TUI side: --instance scopes the panel to one instance
    reqs = [{"id": "1", "instance": {"session": "hc-A", "server_uuid": "x" * 32}},
            {"id": "2", "instance": {"session": None, "server_uuid": "y" * 32}},
            {"id": "3"}]
    ok([r["id"] for r in tui.for_instance(reqs, "hc-A")] == ["1", "3"]
       and [r["id"] for r in tui.for_instance(reqs, "y" * 32)] == ["2", "3"]
       and tui.for_instance(reqs, None) == reqs,
       "an instance-scoped panel shows its own requests plus unstamped ones; the default shows all")
    ok(tui.parse_args(["--instance", "hc-A"]) == "hc-A"
       and tui.parse_args(["--instance=hc-A"]) == "hc-A"
       and tui.parse_args([]) is None,
       "copilot-tui --instance <id> parses; no argument = the global panel")
    quiet, sys.stderr = sys.stderr, io.StringIO()
    try:
        codes = []
        for bad in (["--instance", "a/b"], ["--bogus"]):
            try:
                tui.parse_args(bad); codes.append(None)
            except SystemExit as e:
                codes.append(e.code)
    finally:
        sys.stderr = quiet
    ok(codes == [2, 2], "an unsafe instance id or an unknown argument exits 2")
    ok("--instance" in tui.USAGE and "[--instance ID]" in tui.USAGE,
       "copilot-tui --help names --instance")


# ------------------------ 9. sidepanel in the requesting zellij session
# hcop-0p7.4: an ATTACHED request opens `copilot-tui --instance <id> --spool
# <spool>` as a floating pane INSIDE its zellij session — arguments, never
# environment (the pane inherits the zellij SERVER's env, frozen at session
# creation). A second call inside the cooldown opens nothing; a call while
# the instance's own lock is held FOCUSES the recorded pane; an unattached
# request keeps today's terminal path (hcop-0p7.9); zellij refusing the pane
# falls back to that path. The pane is TILED since hcop-goq — it splits the
# session so the terminal re-wraps, rather than covering it. Stubbed zellij (no pane is ever opened), the
# section-7 concurrent-stage pattern (perf budget 6 s for the whole file).

def _stage_pane_open(st):
    import tui
    A = st.server(_ZA)                       # attached: the stub lists 1 client
    A.call()
    panes = [ln for ln in st.zlog() if " action new-pane " in ln]
    st.check(len(panes) == 1 and panes[0].startswith(
                 "--session hc-A action new-pane --direction down --close-on-exit --name copilot · hc-A -- "),
             "an attached request opens a TILED, named, close-on-exit pane inside ITS session")
    st.check(any(ln.startswith("--session hc-A action focus-previous-pane")
                     for ln in st.zlog()),
             "opening the pane immediately restores the operator's previously focused pane")
    st.check(panes and "tui.py --instance hc-A --spool " + st.spool + " stdin=/dev/null" in panes[0],
             "the pane runs copilot-tui with the instance id AND the spool as ARGUMENTS, stdin kept off the protocol")
    st.check(st.terminals() == [], "…and opens no desktop terminal window")
    pane_file = os.path.join(st.spool, "instances", "hc-A", "pane.json")
    try:
        recorded = json.load(open(pane_file))
        mode = stat.S_IMODE(os.stat(pane_file).st_mode)
    except (OSError, ValueError):
        recorded, mode = {}, None
    st.check(recorded.get("pane") == "terminal_7" and recorded.get("session") == "hc-A"
             and mode == 0o600,
             "the pane id zellij printed is recorded 0600 in the instance dir")
    A.call()                                 # cooldown: the pane's TUI is still starting
    st.check(len([ln for ln in st.zlog() if " action new-pane " in ln]) == 1
             and not any(" focus-pane-id " in ln for ln in st.zlog()),
             "a second call inside the cooldown (pane not yet locked) opens nothing and focuses nothing")
    fd = tui.hold_presence(st.spool, "hc-A")   # the pane's TUI is up: its lock is held
    try:
        st.clear_stamps()
        A.call()
        zl = st.zlog()
        st.check(len([ln for ln in zl if " action new-pane " in ln]) == 1
                 and not any(" focus-pane-id " in ln for ln in zl)
                 and not any(" show-floating-panes " in ln for ln in zl),
                 "with the instance's lock held, a call preserves focus instead of opening or focusing a pane")
        st.check(json.load(open(pane_file)).get("floating") is False
                 and any(" action new-pane --direction down " in ln for ln in st.zlog()),
                 "…and the pane it opened was TILED: --direction down, recorded floating:false (hcop-goq)")
        st.check(st.terminals() == [], "…still no terminal window")
    finally:
        os.close(fd)

def _stage_pane_unattached(st):
    A = st.server({**_ZA, "HC_STUB_CLIENTS": "0"})
    A.call()
    st.check(not any(" action new-pane " in ln for ln in st.zlog())
             and len(st.terminals()) == 1 and "tui.py" in st.terminals()[0],
             "an UNATTACHED zellij request takes today's path: a terminal window, no pane (hcop-0p7.9)")

def _stage_pane_fails(st):
    import tui
    A = st.server({**_ZA, "HC_STUB_NEWPANE_FAIL": "1"})
    A.call()
    # open_in_terminal intentionally does not wait for the terminal process;
    # give this asynchronous test stub time to append its one-line receipt.
    deadline = time.time() + 2
    while time.time() < deadline and not st.terminals():
        time.sleep(0.02)
    st.check(any(" action new-pane " in ln for ln in st.zlog())
             and len(st.terminals()) == 1 and "tui.py" in st.terminals()[0],
             "zellij refusing the pane falls back to today's terminal window")
    st.check(not os.path.exists(os.path.join(st.spool, "instances", "hc-A", "pane.json")),
             "…and records no pane id")
    fd = tui.hold_presence(st.spool)         # a global surface (overlay) is up
    try:
        st.clear_stamps()
        A.call()
        st.check(len(st.terminals()) == 1,
                 "…but opens no terminal when a global surface already shows the request")
    finally:
        os.close(fd)

def _stage_palette_lease(st):
    """hcop-0p7.5: a server HOLDS its session's palette slot from its first
    call until it dies; the registry the overlay (registrySlots) and the TUI
    (colour_for) read is authoritative for live sessions and reclaimed when
    the process exits. Two servers, one under zellij and one not."""
    import palette as P
    A = st.server(_ZA)                       # session hc-A (attached: pane, stubbed)
    N = st.server({})                        # no ZELLIJ_* at all: keyed by its uuid
    body_a = A.call()
    body_n = N.call()
    reg = json.load(open(os.path.join(st.spool, P.REGISTRY)))
    rec_a = reg["sessions"].get("hc-A")
    uuid_n = body_n.get("instance", {}).get("server_uuid")
    rec_n = reg["sessions"].get(uuid_n or "?")
    st.check(rec_a and rec_a["holders"].get(body_a["instance"]["server_uuid"], {}).get("leased") is True
             and rec_n and rec_n["holders"].get(uuid_n, {}).get("leased") is True,
             "after its first call each server holds a LEASED slot for its session key in spool/palette.json")
    st.check(rec_a and rec_n and rec_a["slot"] != rec_n["slot"]
             and stat.S_IMODE(os.stat(os.path.join(st.spool, P.REGISTRY)).st_mode) == 0o600,
             "two live servers hold two different slots; the registry is 0600")
    live = P.live_sessions(spool=st.spool)
    st.check(set(live) == {"hc-A", uuid_n} and P.lookup("hc-A", spool=st.spool).slot == rec_a["slot"],
             "live_sessions (what the TUI numbers from) names exactly the two live servers")
    st.check(set(json.load(open(os.path.join(st.spool, P.REGISTRY)))["sessions"]) >= {"hc-A"}
             and body_a["instance"]["session"] == "hc-A"
             and set(body_a["instance"]) == {"session", "pane", "server_uuid", "attached"},
             "the request JSON did not grow a colour field: surfaces read the registry")
    A.call()                                 # a second call leases nothing new
    reg2 = json.load(open(os.path.join(st.spool, P.REGISTRY)))
    st.check(reg2["sessions"]["hc-A"]["slot"] == rec_a["slot"]
             and list(reg2["sessions"]["hc-A"]["holders"]) == [body_a["instance"]["server_uuid"]],
             "a second call from the same server keeps the one lease and the same slot")
    import toggle as T                       # hcop-0p7.6: the status contract reads the leases
    ev = T.build_status(spool=st.spool)["evidence"]["instances"]
    rows = {r["id"]: r for r in ev["sessions"]}
    st.check(ev["count"] == 2 and ev["live_count"] == 2
             and rows["hc-A"]["number"] == rec_a["slot"] and rows["hc-A"]["lock_held"] is True
             and rows[uuid_n]["number"] == rec_n["slot"] and rows[uuid_n]["lock_held"] is True
             and rows["hc-A"]["colour"]["hex"] == P.entry_for(rec_a["slot"])["hex"]
             and rows["hc-A"]["presence_state"] == "present",
             "toggle state lists both live servers with their slot numbers, palette colours and held leases")
    N.stop()                                 # the non-zellij server exits -> reclaimed
    reg_before = open(os.path.join(st.spool, P.REGISTRY), "rb").read()
    ev = T.build_status(spool=st.spool)["evidence"]["instances"]
    rows = {r["id"]: r for r in ev["sessions"]}
    st.check(ev["count"] == 2 and ev["live_count"] == 1
             and rows[uuid_n]["lock_held"] is False and rows["hc-A"]["lock_held"] is True
             and open(os.path.join(st.spool, P.REGISTRY), "rb").read() == reg_before,
             "after a server exits the status reads its lease as not held and leaves the registry unreclaimed (never repairs)")
    st.check(P.lookup(uuid_n, spool=st.spool) is None and P.lookup("hc-A", spool=st.spool) is not None,
             "a server that exited is reclaimed at the next lookup; the live one keeps its slot")
    A.stop()
    st.check(P.live_sessions(spool=st.spool) == {},
             "when the last server exits the registry is empty — the slot went with the process")

@_isolated_legacy_server_test
def test_sidepanel():
    print("sidepanel in the requesting zellij session (server.py processes on 4 concurrent spools, stubbed zellij)")
    import server as hcserver
    import tui
    stages = [(_PresenceStage(), body) for body in
              (_stage_pane_open, _stage_pane_unattached, _stage_pane_fails, _stage_palette_lease)]
    threads = [threading.Thread(target=st.run, args=(body,)) for st, body in stages]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    try:
        for st, _body in stages:
            st.emit()
    finally:
        for st, _body in stages:
            shutil.rmtree(st.spool, ignore_errors=True)

    # in-process: the argv, the binary override, the fallback's voice
    tmp = tempfile.mkdtemp(prefix="hc-pane-")
    old_z = os.environ.pop("HC_ZELLIJ", None)
    try:
        stub = os.path.join(tmp, "zj")
        with open(stub, "w") as f:
            f.write("#!/bin/bash\necho nope >&2\nexit 3\n")
        os.chmod(stub, 0o755)
        os.environ["HC_ZELLIJ"] = stub
        argv = hcserver.pane_argv("S", "S", "/sp")
        ok(argv[0] == stub and argv[1:10] == ["--session", "S", "action", "new-pane",
                                              "--direction", "down",
                                              "--close-on-exit", "--name", "copilot · S"]
           and argv[10] == "--" and argv[11].endswith("tui.py")
           and argv[12:] == ["--instance", "S", "--spool", "/sp"],
           "pane_argv: HC_ZELLIJ names the binary; instance and spool ride as arguments after --")
        ok(hcserver.pane_layout() == (["--direction", "down"], False),
           "the copilot pane is TILED by default — it re-flows the session instead of covering it")
        for value, expect in (("right", (["--direction", "right"], False)),
                              ("FLOATING", (["--floating"], True)),
                              ("sideways", (["--direction", "down"], False)),
                              ("", (["--direction", "down"], False))):
            os.environ["HC_PANE_DIRECTION"] = value
            ok(hcserver.pane_layout() == expect,
               f"HC_PANE_DIRECTION={value!r} -> {expect[0]}"
               + (" (today's covering pane, restored)" if expect[1] else ""))
        os.environ["HC_PANE_DIRECTION"] = "floating"
        ok(hcserver.pane_argv("S", "S", "/sp")[5] == "--floating",
           "…and the floating escape hatch reaches the argv, so the old behaviour is one variable away")
        os.environ.pop("HC_PANE_DIRECTION", None)
        quiet, sys.stderr = sys.stderr, io.StringIO()
        try:
            r = hcserver.open_pane_in_session("S", "S", tmp)
            said = sys.stderr.getvalue()
        finally:
            sys.stderr = quiet
        ok(r is None and "exited 3" in said and "nope" in said and "falling back" in said,
           "a failing new-pane returns None and says why, and that it falls back, on stderr")
        ok(hcserver.focus_pane_in_session("S", "S", tmp) is False,
           "focus with no recorded pane id is a quiet False, never a zellij call")
        # what the record says decides whether the floating layer is revealed
        log = os.path.join(tmp, "focus.log")
        with open(stub, "w") as f:
            f.write(f'#!/bin/bash\necho "$*" >> {shlex.quote(log)}\nexit 0\n')
        os.chmod(stub, 0o755)
        idir = os.path.join(tmp, "instances", "S")
        os.makedirs(idir, exist_ok=True)
        for record, want_reveal, label in (
                ({"pane": "terminal_7", "floating": False}, False, "a TILED pane is focused by id alone"),
                ({"pane": "terminal_7", "floating": True}, True, "a FLOATING pane is revealed first"),
                ({"pane": "terminal_7"}, True, "a record written before hcop-goq reads as floating")):
            with open(os.path.join(idir, "pane.json"), "w") as f:
                json.dump(record, f)
            open(log, "w").close()
            hcserver.focus_pane_in_session("S", "S", tmp)
            said = open(log).read()
            ok(("show-floating-panes" in said) is want_reveal and "focus-pane-id terminal_7" in said,
               f"focus: {label}")
        os.environ["HC_ZELLIJ"] = os.path.join(tmp, "missing")
        ok(hcserver.zellij_bin() is None and hcserver.pane_argv("S", "S", "/sp") is None,
           "no zellij binary -> no argv (the caller falls back)")
    finally:
        if old_z is None:
            os.environ.pop("HC_ZELLIJ", None)
        else:
            os.environ["HC_ZELLIJ"] = old_z
        shutil.rmtree(tmp, ignore_errors=True)

    # the TUI side: --spool as an argument, and what a closing pane declines
    ok(tui.parse_options(["--instance", "hc-A", "--spool", "/x"]) == {"instance": "hc-A", "spool": "/x"}
       and tui.parse_options(["--spool=/y"]) == {"instance": None, "spool": "/y"}
       and tui.parse_args(["--instance", "hc-A", "--spool", "/x"]) == "hc-A",
       "copilot-tui --spool DIR parses (both spellings); parse_args still yields the instance")
    ok("[--spool DIR]" in tui.USAGE, "copilot-tui --help names --spool")
    reqs = [{"id": "1", "instance": {"session": "hc-A", "server_uuid": "x" * 32}},
            {"id": "2", "instance": {"session": "hc-B", "server_uuid": "y" * 32}},
            {"id": "3"},
            {"id": "4", "instance": {"session": "hc-A", "server_uuid": "z" * 32}}]
    ok(tui.decline_targets(reqs, "hc-A") == ["1", "4"],
       "a closing hc-A pane declines exactly hc-A's requests — not another instance's, not an unstamped one")
    ok(tui.decline_targets(reqs, "hc-A", answered_ids={"1"}) == ["4"],
       "…and never one that already has an answer")
    ok(tui.decline_targets(reqs, None) == [],
       "a GLOBAL panel closing declines nothing (its requests wait in the spool, as always)")
    spool = tempfile.mkdtemp(prefix="hc-decl-")
    try:
        for d in ("requests", "answers"):
            os.makedirs(os.path.join(spool, d))
        for r in reqs:
            with open(os.path.join(spool, "requests", r["id"] + ".json"), "w") as f:
                json.dump({**r, "summary": "1. x", "created": int(r["id"])}, f)
        tui.write_answer(spool, "1", "done", [])          # already answered
        done = tui.decline_open(spool, "hc-A")
        a4 = json.load(open(os.path.join(spool, "answers", "4.json")))
        ok(done == ["4"] and a4 == {"text": "", "cancelled": True, "images": []}
           and stat.S_IMODE(os.stat(os.path.join(spool, "answers", "4.json")).st_mode) == 0o600
           and sorted(os.listdir(os.path.join(spool, "answers"))) == ["1.json", "4.json"],
           "decline_open writes the Esc path's exact declined answer, 0600, only for the open ones")
    finally:
        shutil.rmtree(spool, ignore_errors=True)


# ------------------------------------------------ 8. per-instance palette
#
# palette.py is the ONE source of truth for the instance colours (hcop-0p7.2):
# the contrast and distinguishability maths are computed here, not trusted;
# the per-session registry in the spool is driven with two holders, a lease
# that dies, and a full table; and the generated overlay/palette.js must equal
# what palette.py emits today.

def test_palette():
    print("per-instance palette")
    import palette as P
    ok(len(P.PALETTE) >= 9 and len({e["hex"] for e in P.PALETTE}) == len(P.PALETTE),
       f"{len(P.PALETTE)} entries, all distinct hex")
    ok(all(P.rgb_hex(P.xterm_rgb(e["xterm"])) == e["hex"] for e in P.PALETTE),
       "every xterm-256 index is EXACTLY its hex (cube/ramp, no approximation)")
    # The maths itself, against the WCAG worked values.
    ok(abs(P.contrast_ratio("#ffffff", "#000000") - 21.0) < 0.01
       and abs(P.contrast_ratio("#767676", "#ffffff") - 4.54) < 0.01,
       "contrast_ratio: white/black 21:1, #767676/white 4.54:1")
    worst = min(min(P.contrast_ratio(e["rgb"], P.OVERLAY_BG),
                    P.contrast_ratio(e["rgb"], P.TERMINAL_BG)) for e in P.PALETTE)
    ok(worst >= P.TEXT_THRESHOLD >= P.UI_THRESHOLD,
       f"every entry >= {P.TEXT_THRESHOLD}:1 on overlay {P.OVERLAY_BG} AND terminal "
       f"{P.TERMINAL_BG} (worst {worst:.2f}:1)")
    ok(all(P.contrast_ratio(e["rgb"], e["fg"]) >= P.TEXT_THRESHOLD for e in P.PALETTE),
       f"the chip ink {P.CHIP_INK} reads on every entry at >= {P.TEXT_THRESHOLD}:1")
    dmin = min(P.delta_e(a["rgb"], b["rgb"])
               for i, a in enumerate(P.PALETTE) for b in P.PALETTE[i + 1:])
    ok(dmin >= P.DISTINCT_THRESHOLD,
       f"pairwise CIE76 delta-E >= {P.DISTINCT_THRESHOLD} (smallest {dmin:.1f})")
    ok(P.check_palette() == [], "check_palette() finds no problem in the shipped table")
    # ...and it DOES fail when an entry is too dark: a darkened copy of the
    # table must produce the entry's own message.
    dark = list(P.PALETTE)
    dark[1] = dict(dark[1], rgb=(30, 60, 95), hex="#1e3c5f", xterm=23)
    probs = P.check_palette(tuple(dark))
    ok(any("2 sky" in m and "terminal" in m and "<" in m for m in probs),
       "a darkened entry fails the contrast check with its own message: "
       + (probs[0] if probs else "(none)"))
    ok(all(f"{e['number']} {e['name']}" in line
           for e, line in zip(P.PALETTE, P.swatch_lines())),
       "every swatch line carries its number and name as text (load-bearing)")
    # the overlay's copy is generated; stale = fail with the regenerate command
    sync = P.js_in_sync()
    ok(sync is None,
       "overlay/copilot-overlay@croze/palette.js is in sync with palette.py"
       + (f" — {sync}" if sync else ""))
    ok(all(e["hex"] in P.emit_js() for e in P.PALETTE)
       and "export function entryFor" in P.emit_js(),
       "emit_js carries every hex and the wrap helper")
    real_js = P.JS_PATH
    P.JS_PATH = os.path.join(tempfile.mkdtemp(), "palette.js")
    try:
        with open(P.JS_PATH, "w") as f:
            f.write(P.emit_js().replace("#ffd700", "#ffd701"))
        msg = P.js_in_sync()
    finally:
        P.JS_PATH = real_js
    ok(msg and "STALE" in msg and "--emit-js" in msg,
       "a stale palette.js fails naming the regenerate command")

    # -- the registry, in a throwaway spool
    spool = tempfile.mkdtemp()
    inst = lambda s, u: {"session": s, "pane": "0", "server_uuid": u, "attached": True}
    a = P.hold(inst("S1", "u1"), spool=spool)
    b = P.hold(inst("S2", "u2"), spool=spool)
    ok(a.entry is not b.entry and a.slot != b.slot,
       f"two live sessions get different colours ({a.entry['name']}, {b.entry['name']})")
    again = P.colour_for(inst("S1", "u1-second-server"), spool=spool)
    ok(again.slot == a.slot and again.entry is a.entry
       and P.lookup("S1", spool=spool).slot == a.slot,
       "the same session gets the same colour on every call, even from a second server")
    ok(P.session_key({"session": None, "server_uuid": "abc"}) == "abc"
       and P.session_key(inst("S9", "x")) == "S9",
       "key = session, else server_uuid")
    st = os.stat(os.path.join(spool, P.REGISTRY))
    ok(st.st_mode & 0o777 == 0o600 and not any(n.startswith(P.REGISTRY + ".tmp")
                                             for n in os.listdir(spool)),
       "registry file is 0600 and no tmp file is left behind")
    # a holder that DIES (kill -9, no cleanup) is reclaimed by the lock probe
    code = ("import palette,time,sys; a=palette.hold({'session':'SUB','server_uuid':'usub'},"
            f" spool={spool!r}); print(a.slot, flush=True); time.sleep(60)")
    sub = subprocess.Popen([sys.executable, "-c", code], cwd=HERE,
                           stdout=subprocess.PIPE, text=True)
    sub_slot = int(sub.stdout.readline())
    alive = P.lookup("SUB", spool=spool)
    sub.kill(); sub.wait()
    ok(alive is not None and alive.slot == sub_slot and P.lookup("SUB", spool=spool) is None,
       "a session whose server died (SIGKILL) is reclaimed at the next lookup")
    # explicit release
    b.release()
    ok(P.lookup("S2", spool=spool) is None and "S2" not in P.live_sessions(spool=spool),
       "release() reclaims the session when its last holder lets go")
    # fill the table: every slot distinct, then the tenth wraps and SAYS so
    holds = [P.hold(inst(f"S{i}", f"u{i}"), spool=spool) for i in range(2, 10)]
    slots = sorted(x.slot for x in P.live_sessions(spool=spool).values())
    ok(slots == list(range(1, len(P.PALETTE) + 1)),
       f"{len(P.PALETTE)} live sessions occupy every slot exactly once")
    tenth = P.hold(inst("S10", "u10"), spool=spool)
    ok(tenth.wrapped and tenth.slot == len(P.PALETTE) + 1 and tenth.entry is a.entry
       and tenth.shares_with == ["S1"] and tenth.note and "S1" in tenth.note
       and "number" in tenth.note,
       "the tenth live session wraps to slot 10 and the result names who it shares with")
    ok(not a.wrapped and a.note is None,
       "an unwrapped assignment carries no note")
    # a provisional (never held) session yields its slot before anyone wraps
    tenth.release()
    holds[0].release()                       # S2 gone -> slot 2 free
    prov = P.colour_for(inst("P1", "up1"), spool=spool)
    ok(prov.slot == 2 and P._is_provisional(P._load(spool)["sessions"]["P1"]),
       "a surface lookup for a never-held server is provisional, in the free slot")
    newcomer = P.hold(inst("S11", "u11"), spool=spool)
    ok(newcomer.slot == 2 and newcomer.evicted == "P1" and not newcomer.wrapped
       and P.lookup("P1", spool=spool) is None,
       "with the table full, a new held session evicts the oldest provisional one first")
    for h in holds[1:] + [a, newcomer]:
        h.release()
    ok(P.live_sessions(spool=spool) == {}, "releasing every holder empties the registry")
    ok(P.emit_js() == P.emit_js(), "emit_js is deterministic")
    # the CLI surface the operator measurement uses
    r = subprocess.run([sys.executable, "palette.py", "--swatches"], cwd=HERE,
                       capture_output=True, text=True, env={**os.environ, "HC_SPOOL": spool})
    ok(r.returncode == 0 and all(f"{e['number']} {e['name']}" in r.stdout for e in P.PALETTE)
       and all(e["hex"] in r.stdout for e in P.PALETTE)
       and f"\x1b[48;5;{P.PALETTE[0]['xterm']}m" in r.stdout
       and "\x1b[48;2;255;215;0m" in r.stdout,
       "--swatches prints every entry as a 256-colour AND truecolor chip with number, name, hex")
    r = subprocess.run([sys.executable, "palette.py", "--check"], cwd=HERE,
                       capture_output=True, text=True)
    ok(r.returncode == 0 and "smallest pairwise delta-E" in r.stdout, "--check passes and reports")
    r = subprocess.run([sys.executable, "open_window.py", "--print", "--no-hold",
                        "python3",
                        os.path.join(HERE, "palette.py"), "--swatches"], cwd=HERE,
                       capture_output=True, text=True,
                       env={**os.environ, "HC_TERMINAL": "ptyxis --new-window -x"})
    ok(r.returncode == 0 and r.stdout.strip().endswith("palette.py --swatches")
       and r.stdout.startswith("ptyxis --new-window -x"),
       "copilot-open --print composes the operator's swatch window without spawning it")
    shutil.rmtree(spool, ignore_errors=True)


# -------------------------------- 10. concurrent requests: the TUI list (hcop-0p7.3)
# The panel lists every open request it serves, grouped by instance, each
# answerable on its own; these are the curses-free halves (tests/drive_tui.py
# passes 4-5 drive the real pty). Pure logic only — the e2e perf budget for
# this file is 6 s and section 3 already spends most of it.

def test_concurrent_lists():
    print("concurrent requests: the TUI list (curses-free)")
    import curses
    import tui
    A = {"session": "hc-A", "pane": "0", "server_uuid": "a" * 32, "attached": True}
    B = {"session": None, "pane": None, "server_uuid": "b" * 32, "attached": None}
    r1 = {"id": "1", "created": 1, "summary": "Ctx.\n\n1. first", "instance": A}
    r2 = {"id": "2", "created": 2, "summary": "| # | do |\n|---|---|\n| 1 | tbl |", "instance": B}
    r3 = {"id": "3", "created": 3, "summary": "1. loose"}                 # unstamped
    r4 = {"id": "4", "created": 4, "summary": "```\n1. code\n```\n# Head\n2. real", "instance": A}
    ok(tui.auto_open([r1]) is r1 and tui.auto_open([r1], backed={"1"}) is None
       and tui.auto_open([r1, r2]) is None and tui.auto_open([]) is None,
       "auto_open: the only open request takes the screen unless backed out of; two or more -> the list")
    ok(tui.request_gist(r1["summary"]) == "Ctx. — first"
       and tui.request_gist(r2["summary"]) == "tbl"
       and tui.request_gist(r4["summary"]) == "Head — real"
       and tui.request_gist("") == "(no summary)",
       "request_gist: first context line — first step; tables and fences are not context")
    nums = {"hc-A": 2, "b" * 32: 1}
    items = tui.list_items([r1, r2, r3, r4], nums)
    shape = [(it["kind"], it["req"]["id"] if it["kind"] == "req" else it["key"]) for it in items]
    ok(shape == [("group", "b" * 32), ("req", "2"), ("group", "hc-A"), ("req", "1"),
                 ("req", "4"), ("group", None), ("req", "3")],
       "list_items: grouped by instance, groups by palette number, unstamped last, oldest first inside")
    ok(items[0]["label"] == "b" * 8 and items[2]["label"] == "hc-A" and items[2]["count"] == 2
       and items[0]["number"] == 1 and items[2]["number"] == 2 and items[5]["number"] == 0
       and items[5]["label"].startswith("every surface"),
       "group rows carry label (session, else 8 hex of the uuid), palette number and count")
    ok(all(it["kind"] == "req" for it in tui.list_items([r1, r4], nums)),
       "a single instance draws no group header")
    ok(tui.request_rows(items) == [1, 3, 4, 6], "request_rows are the answerable rows")
    ok(tui.nearest_request(items, 0) == 1 and tui.nearest_request(items, 3) == 3
       and tui.nearest_request(items, 5) == 6 and tui.nearest_request(items, 99) == 6
       and tui.nearest_request([items[0]], 0) == -1,
       "nearest_request lands on a request row, clamps, -1 when there is none")
    ok(tui.list_motion(ord("j"), 1, items, 10) == 3 and tui.list_motion(curses.KEY_DOWN, 3, items, 10) == 4
       and tui.list_motion(ord("k"), 4, items, 10) == 3 and tui.list_motion(curses.KEY_UP, 1, items, 10) == 1
       and tui.list_motion(ord("j"), 6, items, 10) == 6,
       "list_motion: j/k and arrows walk request rows only, clamped at both ends")
    ok(tui.list_motion(ord("G"), 1, items, 10) == 6 and tui.list_motion(curses.KEY_END, 1, items, 10) == 6
       and tui.list_motion(ord("g"), 6, items, 10) == 1 and tui.list_motion(curses.KEY_HOME, 6, items, 10) == 1
       and tui.list_motion(curses.KEY_NPAGE, 1, items, 4) == 4 and tui.list_motion(curses.KEY_PPAGE, 6, items, 4) == 3,
       "list_motion: g/G, Home/End, half-page jumps")
    ok(tui.list_motion(curses.KEY_DOWN, 0, items, 10) == 1 and tui.list_motion(curses.KEY_UP, 2, items, 10) == 1
       and tui.list_motion(curses.KEY_DOWN, 2, items, 10) == 3,
       "from a header row (a click): down = the request under it, up = the one above")
    ok(all(tui.list_motion(ch, 1, items, 10) is None
           for ch in (ord("T"), ord("t"), ord("R"), ord("?"), ord("q"), 4, 2, 21, 13, 10, 9, 16)),
       "list keys never shadow T/R/?/q, ^D, ^B, ^U, Enter, Tab or ^P")
    ok(tui.list_motion(ord("j"), 0, [items[0]], 10) is None, "no request rows -> no motion")
    ok(tui.instance_label(A) == "hc-A" and tui.instance_label(B) == "b" * 8
       and tui.instance_label(None) == "" and tui.instance_label({}) == "",
       "instance_label: session, else 8 hex digits, else nothing")
    ok(set(tui.ENTER_KEYS) >= {10, 13} and tui.PALETTE_FIRST_PAIR > 6,
       "Enter is both newline codes; palette pairs start past tuikit's 1-6")

    # ^B through the REAL modal loop, no pty: the draft rides in the dict the
    # caller keeps by request id, and the next open resumes it.
    spool = tempfile.mkdtemp(prefix="hc-spool-list-")
    for d in ("requests", "answers"):
        os.makedirs(os.path.join(spool, d))
    try:
        req = {"id": "back", "summary": "1. type then leave", "created": time.time(),
               "deadline": time.time() + 60}
        with open(os.path.join(spool, "requests", "back.json"), "w") as f:
            json.dump(req, f)

        class FakeScreen:
            def getmaxyx(self): return 30, 80
            def erase(self): pass
            def addstr(self, *_a): pass
            def move(self, *_a): pass
            def timeout(self, _ms): pass
            def refresh(self): pass

        class FakeWatch:
            ok = False
            def start(self): return self
            def take(self): return False
            def stop(self): pass

        keep = (tui.read_key, tui.ClipWatch, tui.clip_image, tui.curses.curs_set)
        app = object.__new__(tui.CopilotApp)
        app.spool, app.answered, app.toast = spool, 0, (lambda _m: None)
        draft = {}
        try:
            tui.ClipWatch, tui.clip_image, tui.curses.curs_set = FakeWatch, (lambda: None), (lambda _v: None)
            keys = iter((("ch", "x"), ("ch", "\x02")))
            tui.read_key = lambda _scr, wait_ms=None: next(keys)
            first = app.feedback_modal(FakeScreen(), req, queued=2, draft=draft)
            none_yet = not os.listdir(os.path.join(spool, "answers"))
            back_text, back_images = draft["ed"].text(), list(draft["images"])
            keys = iter((("ch", "y"), ("ch", "\x04")))
            second = app.feedback_modal(FakeScreen(), req, draft=draft)
        finally:
            tui.read_key, tui.ClipWatch, tui.clip_image, tui.curses.curs_set = keep
        ok(first == "back" and back_text == "x" and back_images == [] and none_yet,
           "^B returns 'back' with the draft kept and no answer written")
        ans = json.load(open(os.path.join(spool, "answers", "back.json")))
        ok(second == "answered" and ans["text"] == "xy" and app.answered == 1,
           "reopening with the same draft resumes the text; ^D answers under the request id")
    finally:
        shutil.rmtree(spool, ignore_errors=True)

    # A mobile lease arriving while the modal is open hides the request but is
    # not a withdrawal. The modal yields promptly, and the exact editor object
    # remains available when that bounded lease is released.
    import responder as hcresponder
    lease_spool = tempfile.mkdtemp(prefix="hc-spool-lease-draft-")
    for d in ("requests", "answers", "targets"):
        os.makedirs(os.path.join(lease_spool, d))
    try:
        lease_req = {
            "id": "leased-draft", "summary": "Keep this typed answer",
            "created": time.time(), "deadline": time.time() + 60,
            "timeout": 60, "client": "Codex",
            "instance": {"session": None, "pane": None,
                         "server_uuid": "d" * 32, "attached": None},
            "response": {"mode": "text", "options": []},
        }
        hcresponder.stamp_request_contract(lease_req)
        hcresponder.refresh_request_revision(lease_req)
        with open(os.path.join(lease_spool, "requests", "leased-draft.json"), "w") as f:
            json.dump(lease_req, f)
        target = {
            "schema": hcresponder.TARGET_SCHEMA, "schema_version": 1,
            "request_id": lease_req["id"],
            "request_revision": lease_req["request_revision"],
            "generation": 1, "state": "untargeted", "targets": [],
        }
        with open(os.path.join(lease_spool, "targets", "leased-draft.json"), "w") as f:
            json.dump(target, f)
        adapter = hcresponder.Responder(
            lease_spool, token_factory=lambda: "draft-lease-token")
        leased = adapter.acquire("phone", 20)
        token = leased["event"]["request"]["lease"]["token"]
        draft = {"ed": tui.MultiLine(), "images": []}
        for char in "same draft":
            draft["ed"].key("ch", char)
        drafts = {lease_req["id"]: draft}
        notices = []
        app = object.__new__(tui.CopilotApp)
        app.spool, app.answered, app.toast = lease_spool, 0, notices.append
        keep = (tui.read_key, tui.ClipWatch, tui.clip_image,
                tui.curses.curs_set, tui.time.monotonic)
        ticks = iter((0.0, 1.0, 2.0))
        try:
            tui.read_key = lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("leased modal should yield before reading input"))
            tui.ClipWatch, tui.clip_image = FakeWatch, (lambda: None)
            tui.curses.curs_set = lambda _v: None
            tui.time.monotonic = lambda: next(ticks)
            status = app.feedback_modal(FakeScreen(), lease_req, draft=draft)
        finally:
            (tui.read_key, tui.ClipWatch, tui.clip_image,
             tui.curses.curs_set, tui.time.monotonic) = keep
        hidden, _ = tui.scan_requests(lease_spool)
        tui.prune_drafts(lease_spool, drafts, {row["id"] for row in hidden})
        released = adapter.release(
            lease_req["id"], lease_req["request_revision"], token)
        visible, _ = tui.scan_requests(lease_spool)
        ok(status == "back" and not hidden and released["ok"]
           and [row["id"] for row in visible] == [lease_req["id"]]
           and drafts[lease_req["id"]]["ed"].text() == "same draft"
           and any("draft retained" in message for message in notices),
           "a lease acquired mid-modal hides promptly and release restores the exact TUI draft")
    finally:
        shutil.rmtree(lease_spool, ignore_errors=True)

def test_idle_selfclose():
    """hcop-emd: a sidepanel with nothing open closes itself; a global panel
    and a panel with work never do, and neither declines anything."""
    print("the sidepanel closes itself when it is useless")
    import tui
    spool = tempfile.mkdtemp(prefix="hc-idle-")
    for sub in ("requests", "answers", "instances"):
        os.makedirs(os.path.join(spool, sub), exist_ok=True)

    def write_req(rid, instance):
        body = {"id": rid, "summary": "Context.\n1. Look", "created": time.time(),
                "deadline": time.time() + 600, "timeout": 600, "client": "test"}
        if instance is not None:
            body["instance"] = instance
        with open(os.path.join(spool, "requests", rid + ".json"), "w") as f:
            json.dump(body, f)

    def panel(instance, idle=0.0, since=None):
        app = object.__new__(tui.CopilotApp)
        app.spool, app.instance = spool, instance
        app.reqs, app.in_modal = [], False
        app.idle_since = time.time() - (0 if since is None else since)
        app.idle_exit, app._quit = False, False
        return app

    try:
        keep = tui.PANE_IDLE_S
        tui.PANE_IDLE_S = 5.0
        a = panel("hc-A", since=99)
        a._idle_check()
        ok(a._quit and a.idle_exit,
           "a scoped panel past the idle window with nothing open sets the idle exit")

        a = panel("hc-A", since=1)
        a._idle_check()
        ok(not a._quit and not a.idle_exit,
           "…inside the window it stays: the next request focuses this pane, not a new one")

        a = panel("hc-A", since=99)
        a.reqs = [{"id": "r1"}]
        a._idle_check()
        ok(not a._quit and a.idle_since > time.time() - 1,
           "a panel with an open request never idles out, however long it waits")

        a = panel("hc-A", since=99)
        a.in_modal = True
        a._idle_check()
        ok(not a._quit, "…nor while the operator is inside a request")

        g = panel(None, since=99)
        g._idle_check()
        ok(not g._quit and not g.idle_exit,
           "a GLOBAL panel never self-closes: it is the operator's window and serves every instance")

        tui.PANE_IDLE_S = 0
        a = panel("hc-A", since=99)
        a._idle_check()
        ok(not a._quit, "HC_PANE_IDLE_S=0 disables self-closing entirely")
        tui.PANE_IDLE_S = 5.0

        # the second scan: an arrival since this tick's scan cancels the exit
        write_req("late", {"session": "hc-A", "pane": None,
                           "server_uuid": "u", "attached": True})
        a = panel("hc-A", since=99)
        ok(a.reqs == [], "(the panel's own list is still empty — the tick that saw it has passed)")
        a._idle_check()
        ok(not a._quit and not a.idle_exit,
           "a request that arrived since the last tick CANCELS the exit — the second scan is what sees it")

        # foreign work is not this instance's; an UNSTAMPED request is
        os.unlink(os.path.join(spool, "requests", "late.json"))
        write_req("foreign", {"session": "hc-B", "pane": None,
                              "server_uuid": "u", "attached": True})
        a = panel("hc-A", since=99)
        a._idle_check()
        ok(a._quit and a.idle_exit,
           "another instance's request does not keep this pane alive")
        write_req("everyones", None)
        a = panel("hc-A", since=99)
        a._idle_check()
        ok(not a._quit,
           "an UNSTAMPED request does: a scoped panel shows those too, so it is not useless")
        ok(sorted(os.listdir(os.path.join(spool, "answers"))) == [],
           "…and nothing was declined on the way: _idle_check writes no answer")
    finally:
        tui.PANE_IDLE_S = keep
        shutil.rmtree(spool, ignore_errors=True)

# ----------------------------- 11. authenticated checklist receipts (hcop-d1f)

def test_checklist_receipts():
    print("authenticated structured checklist receipts")
    import server as hcserver

    key = bytes(range(hcserver.RECEIPT_KEY_BYTES))

    def request():
        body = {
            "id": "receipt-request", "summary": "Context.\n1. Look\n2. Click",
            "created": 1_700_000_000.0, "deadline": 1_700_000_600.0,
            "timeout": 600, "client": "Codex",
            "instance": {"session": "hc-A", "pane": "7",
                         "server_uuid": "a" * 32, "attached": True},
            "response": {"mode": None, "options": []},
            "caller_process": {"schema": hcserver.CALLER_PROCESS_SCHEMA,
                               "schema_version": 1, "pid": 42,
                               "boot_id": "boot", "start_ticks": "9"},
        }
        ok(hcserver.stamp_checklist_contract(body, generation="generation-7"),
           "a legacy numbered request receives the additive checklist identity")
        return body

    req = request()
    ids = [row["id"] for row in req["checklist"]["steps"]]

    def answer(rows):
        return {"text": "operator prose stays here", "cancelled": False,
                "images": [], "checklist": {
                    "schema": hcserver.CHECKLIST_ANSWER_SCHEMA,
                    "schema_version": 1, "request_id": req["id"],
                    "request_digest": req["request_digest"], "steps": rows}}

    done_rows = [{"id": ids[0], "verdict": "done", "note": "looks right"},
                 {"id": ids[1], "verdict": "done", "note": ""}]
    receipt = hcserver.build_checklist_receipt(
        req, answer(done_rows), answered_at=1_700_000_010.0, signing_key=key)
    ok(receipt["schema"] == "human-copilot.checklist-verdict/v1"
       and receipt["schema_version"] == 1
       and receipt["request"] == {
           "id": req["id"], "digest": req["request_digest"],
           "requested_at": "2023-11-14T22:13:20.000000Z",
           "deadline_at": "2023-11-14T22:23:20.000000Z"}
       and receipt["answered_at"] == "2023-11-14T22:13:30.000000Z",
       "v1 receipt binds immutable request id/digest and all three timestamps")
    ok(receipt["provider"] == {"name": "human-copilot",
                               "instance": "a" * 32,
                               "generation": "generation-7"}
       and receipt["steps"] == done_rows
       and receipt["outcome"] == "passed" and receipt["authorizing"] is True,
       "provider instance/generation and ordered done verdicts yield the sole authorizing outcome")
    auth = receipt["authentication"]
    ok(auth["algorithm"] == "hmac-sha256"
       and auth["key_id"].startswith("sha256:")
       and auth["digest"].startswith("sha256:")
       and auth["signature"].startswith("hmac-sha256:"),
       "receipt carries named HMAC algorithm, key id, payload digest and signature")

    expected = dict(expected_request_id=req["id"],
                    expected_request_digest=req["request_digest"],
                    expected_provider_instance="a" * 32,
                    expected_provider_generation="generation-7",
                    expected_step_ids=ids, signing_key=key)
    verified = hcserver.verify_checklist_receipt(receipt, **expected)
    ok(verified["valid"] and verified["authenticated"]
       and verified["authorizing"] and verified["reason"] is None,
       "consumer verifies signature, bindings, semantics and authorization")
    unbound = hcserver.verify_checklist_receipt(receipt, signing_key=key)
    ok(not unbound["valid"] and unbound["authenticated"]
       and not unbound["authorizing"]
       and unbound["reason"] == "missing-expected-binding",
       "verification without every expected request/provider/step binding cannot authorize")

    def clone(value):
        return json.loads(json.dumps(value))

    tampered = clone(receipt)
    tampered["steps"][0]["note"] = "forged"
    extra = clone(receipt)
    extra["authorization"] = {"passed": True}
    auth_extra = clone(receipt)
    auth_extra["authentication"]["authorization"] = "passed"
    flipped = clone(receipt)
    flipped["authorizing"] = False
    for changed, label in ((tampered, "step/note tamper"),
                           (extra, "extra authorization field"),
                           (auth_extra, "extra authentication metadata"),
                           (flipped, "authorizing-bit tamper")):
        verdict = hcserver.verify_checklist_receipt(changed, **expected)
        ok(not verdict["valid"] and not verdict["authorizing"]
           and verdict["reason"] == "tampered",
           f"canonical signature rejects {label}")

    for override, reason in (
            ({"expected_request_id": "another"}, "wrong-request"),
            ({"expected_request_digest": "sha256:another"},
             "wrong-request-digest"),
            ({"expected_provider_instance": "b" * 32},
             "wrong-provider-instance"),
            ({"expected_provider_generation": "generation-8"},
             "wrong-provider-generation")):
        args = {**expected, **override}
        verdict = hcserver.verify_checklist_receipt(receipt, **args)
        ok(not verdict["valid"] and verdict["authenticated"]
           and not verdict["authorizing"] and verdict["reason"] == reason,
           f"authenticated receipt is rejected on {reason}")

    seen = set()
    first = hcserver.verify_checklist_receipt(receipt, replay_cache=seen,
                                              **expected)
    replay = hcserver.verify_checklist_receipt(receipt, replay_cache=seen,
                                               **expected)
    ok(first["authorizing"] and not replay["valid"]
       and replay["reason"] == "replay" and not replay["authorizing"],
       "a caller-owned consumed-digest set rejects exact replay")

    def rejected(raw, code):
        try:
            hcserver.build_checklist_receipt(req, raw,
                                             answered_at=1_700_000_010.0,
                                             signing_key=key)
        except hcserver.ChecklistReceiptError as error:
            return error.code == code
        return False

    missing = answer(done_rows[:1])
    duplicate = answer([done_rows[0], done_rows[0]])
    unknown = answer([done_rows[0], {"id": "step-999", "verdict": "done",
                                     "note": ""}])
    wrong_request = answer(done_rows)
    wrong_request["checklist"]["request_id"] = "another"
    ok(rejected(missing, "missing-step")
       and rejected(duplicate, "duplicate-step")
       and rejected(unknown, "unknown-step"),
       "answer validation deterministically refuses missing, duplicate and unknown steps")
    ok(rejected(wrong_request, "wrong-request")
       and rejected({"text": "all passed", "cancelled": False, "images": []},
                    "text-only-output")
       and rejected({"text": "", "cancelled": True, "images": []},
                    "dismissed"),
       "wrong-request, text-only and dismissed answers cannot produce a receipt")

    incomplete_rows = [done_rows[0], {"id": ids[1], "verdict": "not-done",
                                      "note": "operator stopped"}]
    incomplete = hcserver.build_checklist_receipt(
        req, answer(incomplete_rows), answered_at=1_700_000_010.0,
        signing_key=key)
    inc_v = hcserver.verify_checklist_receipt(incomplete, **expected)
    failed_rows = [done_rows[0], {"id": ids[1], "verdict": "failed",
                                  "note": "button missing"}]
    failed = hcserver.build_checklist_receipt(
        req, answer(failed_rows), answered_at=1_700_000_010.0,
        signing_key=key)
    fail_v = hcserver.verify_checklist_receipt(failed, **expected)
    ok(incomplete["outcome"] == "incomplete" and not incomplete["authorizing"]
       and inc_v["valid"] and not inc_v["authorizing"]
       and failed["outcome"] == "failed" and not failed["authorizing"]
       and fail_v["valid"] and not fail_v["authorizing"],
       "valid NOT DONE and FAILED receipts authenticate but never authorize")

    contradictory = {key_: value for key_, value in incomplete.items()
                     if key_ != "authentication"}
    contradictory["outcome"] = "passed"
    contradictory["authorizing"] = True
    contradictory = hcserver._sign_receipt_body(contradictory, key)
    contradiction = hcserver.verify_checklist_receipt(contradictory, **expected)
    ok(not contradiction["valid"] and contradiction["authenticated"]
       and contradiction["reason"] == "inconsistent-outcome"
       and not contradiction["authorizing"],
       "verifier recomputes outcome/authorization; a signed NOT DONE cannot claim passed")

    ok(hcserver.verify_checklist_result(
           {"isError": False, "content": [{"type": "text", "text": "passed"}]},
           **expected)["reason"] == "missing-structured-content"
       and hcserver.verify_checklist_result(
           {"isError": True, "content": [{"type": "text",
                                            "text": "Transport closed"}]},
           **expected)["reason"] == "tool-error",
       "text-only and transport-error MCP results fail closed at the consumer boundary")

    key_root = tempfile.mkdtemp(prefix="hc-receipt-key-")
    key_path = os.path.join(key_root, "state", "receipt-key")
    try:
        first_key = hcserver.receipt_signing_key(key_path)
        second_key = hcserver.receipt_signing_key(key_path)
        lock_path = os.path.join(os.path.dirname(key_path),
                                 hcserver.RECEIPT_KEY_LOCK_FILE)
        ok(first_key == second_key and len(first_key) == 32
           and stat.S_IMODE(os.stat(os.path.dirname(key_path)).st_mode) == 0o700
           and stat.S_IMODE(os.stat(key_path).st_mode) == 0o600
           and stat.S_IMODE(os.stat(lock_path).st_mode) == 0o600,
           "persistent signing key is stable; parent is 0700 and key+lock are 0600")
    finally:
        shutil.rmtree(key_root, ignore_errors=True)

    absent_root = tempfile.mkdtemp(prefix="hc-receipt-verify-")
    absent_key = os.path.join(absent_root, "missing-state", "receipt-key")
    old_key_path = os.environ.get(hcserver.RECEIPT_KEY_ENV)
    os.environ[hcserver.RECEIPT_KEY_ENV] = absent_key
    try:
        unavailable = hcserver.verify_checklist_receipt(
            receipt, **{key_: value for key_, value in expected.items()
                        if key_ != "signing_key"})
        ok(unavailable["reason"] == "verification-unavailable"
           and not os.path.exists(os.path.dirname(absent_key)),
           "verification with a missing key fails closed without creating state")
    finally:
        if old_key_path is None:
            os.environ.pop(hcserver.RECEIPT_KEY_ENV, None)
        else:
            os.environ[hcserver.RECEIPT_KEY_ENV] = old_key_path
        shutil.rmtree(absent_root, ignore_errors=True)

    ok(hcserver.provider_generation([
           hcserver.toggle.CONNECTION_GENERATION_PREFIX + "g-1"]) == "g-1"
       and hcserver.provider_generation([]) == "unversioned"
       and hcserver.provider_generation([
           hcserver.toggle.CONNECTION_GENERATION_PREFIX + "a",
           hcserver.toggle.CONNECTION_GENERATION_PREFIX + "b"])
           == "unversioned",
       "provider generation accepts exactly one registration marker and names legacy ambiguity")

# --------------------------------- 12. response modes + choices (hcop-0gj,
# hcop-453): a decision reaches the operator as REAL options, an explicit ack
# is the only empty answer, and an unresolved target is an ERROR to the engine.

def test_response_contract():
    print("response contract (checklist/text/choice/ack + target error)")
    import server as hcserver

    opts2 = [{"value": "pro", "label": "Upgrade to Pro", "hint": "$4/mo"},
             {"value": "free", "label": "Stay free"}]
    good = [
        ({}, {"mode": None, "options": []}),              # the LEGACY shape
        ({"mode": "ack"}, {"mode": "ack", "options": []}),
        ({"mode": "text"}, {"mode": "text", "options": []}),
        ({"mode": "checklist"}, {"mode": "checklist", "options": []}),
        ({"mode": "choice", "options": opts2},
         {"mode": "choice", "options": [
             {"value": "pro", "label": "Upgrade to Pro", "hint": "$4/mo"},
             {"value": "free", "label": "Stay free", "hint": None}]}),
        # options without a mode ARE a choice — the intent is unambiguous
        ({"options": [{"value": "a", "label": "A"}, {"value": "b", "label": "B"}]},
         {"mode": "choice", "options": [{"value": "a", "label": "A", "hint": None},
                                        {"value": "b", "label": "B", "hint": None}]}),
    ]
    for args, want in good:
        got, err = hcserver.response_contract(args)
        ok(got == want and err is None, f"contract accepts {args}")

    bad = [
        ({"mode": "bogus"}, "must be one of"),
        ({"mode": "choice"}, "needs `options`"),
        ({"mode": "choice", "options": [{"value": "a", "label": "A"}]},
         "at least two options"),
        ({"mode": "ack", "options": opts2}, "cannot be combined"),
        ({"mode": "text", "options": opts2}, "cannot be combined"),
        ({"mode": "checklist", "options": opts2}, "cannot be combined"),
        ({"mode": "choice", "options": [
            {"value": "a", "label": "A"}, {"value": "a", "label": "B"}]},
         "duplicate value"),
        ({"mode": "choice", "options": [
            {"value": "", "label": "A"}, {"value": "b", "label": "B"}]},
         "non-empty string `value`"),
        ({"mode": "choice", "options": [
            {"value": "a"}, {"value": "b", "label": "B"}]},
         "non-empty string `label`"),
        ({"mode": "choice", "options": [
            {"value": "a", "label": "A", "hint": "  "},
            {"value": "b", "label": "B"}]},
         "`hint`"),
        ({"mode": "choice", "options": opts2 * 7}, "1-12"),
        ({"mode": "choice", "options": "pro"}, "must be a list"),
        ({"mode": "choice", "options": ["pro", "free"]}, "must be an object"),
    ]
    for args, frag in bad:
        got, err = hcserver.response_contract(args)
        ok(got is None and err is not None and frag in err,
           f"contract refuses {str(args)[:64]}… naming why ({frag})")

    # decision syntax in prose is detected structurally, never guessed
    yes = ["Upgrade GitHub to Pro? ✔ yes · ✘ stay free, branch protection stays manual",
           "a) stay free\nb) pay for protection",
           "(a) keep the rule\n(b) drop the rule",
           "proceed with the flip? yes/no"]
    no = ["1. click save\n2. reload the page",
          "plain prose asking what the weather was like yesterday",
          "A. single heading line only",
          "note: rm *.log and *_drafts to clean up"]
    for s in yes:
        ok(hcserver.looks_like_choices(s), f"choice-like prose detected: {s[:44]}…")
    for s in no:
        ok(not hcserver.looks_like_choices(s), f"non-choice prose left alone: {s[:44]}…")
    ok(hcserver.choices_note(yes[0], None) is not None
       and hcserver.choices_note(yes[0], {"mode": "choice", "options": []}) is None,
       "the choices nudge rides for choice-like prose, never when a mode was sent")
    ok(hcserver.choices_note(no[0], None) is None,
       "an ordinary checklist earns no choices nudge")

    ok("mode:'choice'" in hcserver.TOOL_DESCRIPTION
       and "options [{value, label, hint?}]" in hcserver.TOOL_DESCRIPTION
       and "EXACTLY ONE" in hcserver.TOOL_DESCRIPTION,
       "TOOL_DESCRIPTION teaches structured choices and unique-window locators")
    spool = tempfile.mkdtemp(prefix="hc-contract-")
    bindir = os.path.join(spool, "bin"); os.makedirs(bindir)
    with open(os.path.join(bindir, "fake-term"), "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    with open(os.path.join(bindir, "notify-send"), "w") as f:
        f.write("#!/bin/bash\nexit 0\n")
    for n in ("fake-term", "notify-send"):
        os.chmod(os.path.join(bindir, n), 0o755)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith("ZELLIJ") and k != "HC_REQUIRE_ATTACHED"}
    env.update({"HC_SPOOL": spool,
                "HC_TERMINAL": os.path.join(bindir, "fake-term"),
                "HC_RECEIPT_KEY_FILE": os.path.join(spool, "state",
                                                     "receipt-key"),
                "PATH": bindir + ":" + os.environ["PATH"]})
    generation = "isolated-codex-generation"
    proc = subprocess.Popen([
        sys.executable, os.path.join(HERE, "server.py"),
        hcserver.toggle.CONNECTION_GENERATION_PREFIX + generation],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, env=env)

    def one_call(rpc_id, args, answer=None):
        """One tools/call; ``answer`` (when given) is what the operator
        writes. Returns (result, request body or None)."""
        captured = {}

        def op():
            rdir = os.path.join(spool, "requests")
            for _ in range(200):
                names = ([f for f in os.listdir(rdir) if f.endswith(".json")]
                         if os.path.isdir(rdir) else [])
                if names:
                    rid = names[0][:-5]
                    with open(os.path.join(rdir, names[0])) as f:
                        captured["body"] = json.load(f)
                    reply = (answer(captured["body"])
                             if callable(answer) else answer)
                    tmp = os.path.join(spool, "answers", rid + ".json.t")
                    with open(tmp, "w") as f:
                        json.dump(reply, f)
                    os.replace(tmp, os.path.join(spool, "answers", rid + ".json"))
                    return
                time.sleep(0.05)
        t = threading.Thread(target=op) if answer is not None else None
        if t:
            t.start()
        _rpc(proc, {"jsonrpc": "2.0", "id": rpc_id, "method": "tools/call",
                    "params": {"name": "interactive_feedback", "arguments": args}})
        r = _read(proc, rpc_id)
        if t:
            t.join()
        return r, captured.get("body")

    try:
        _rpc(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize",
                    "params": {"protocolVersion": "2025-06-18", "capabilities": {},
                               "clientInfo": {"name": "codex-mcp-client"}}})
        _read(proc, 1)
        _rpc(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        schema = _read(proc, 2)["result"]["tools"][0]["inputSchema"]["properties"]
        ok(schema.get("mode", {}).get("enum") == list(hcserver.RESPONSE_MODES),
           "inputSchema declares the four modes as an enum")
        ok(schema.get("options", {}).get("items", {}).get("required") == ["value", "label"],
           "inputSchema requires value+label on every option")

        # a structured choice round-trips: request carries the contract, the
        # answer comes back as the exact selected value — never an empty ack
        r, body = one_call(10, {
            "summary": "Flipper branch protection needs a paid plan.\n"
                       "1. read the banner @{hub · full}",
            "mode": "choice", "options": opts2, "timeout": 60},
            {"text": "", "cancelled": False, "images": [],
             "choice": {"value": "pro", "label": "Upgrade to Pro"}})
        ok(r["result"]["isError"] is False
           and body is not None
           and body["response"] == {"mode": "choice", "options": [
               {"value": "pro", "label": "Upgrade to Pro", "hint": "$4/mo"},
               {"value": "free", "label": "Stay free", "hint": None}]},
           "the request file carries the validated mode/options contract")
        c = r["result"]["content"]
        ok(c[0]["type"] == "text"
           and c[0]["text"] == '[selected: "Upgrade to Pro" (value: "pro")]',
           "a choice answer returns the exact value AND label, never an empty ack")
        ok(not any("copilot choices:" in b.get("text", "") for b in c),
           "a structured request earns no choices nudge")

        # malformed contracts are refused at the boundary, nothing spooled
        for k, args, frag in (
                (11, {"summary": "x", "mode": "bogus"}, "must be one of"),
                (12, {"summary": "x", "mode": "choice"}, "needs `options`"),
                (13, {"summary": "x", "mode": "choice", "options": [
                    {"value": "a", "label": "A"}, {"value": "a", "label": "B"}]},
                 "duplicate value"),
                (14, {"summary": "x", "mode": "ack", "options": opts2},
                 "cannot be combined"),
                (15, {"summary": "The GPS Track test is ready.", "mode": "checklist"},
                 "needs at least one numbered action"),
                (16, {"summary": "Example only:\n```\n1. not an action\n```",
                      "mode": "checklist"}, "needs at least one numbered action")):
            r, body = one_call(k, args)
            ok(r["result"]["isError"] is True and frag in r["result"]["content"][0]["text"]
               and body is None
               and not os.listdir(os.path.join(spool, "requests")),
               f"malformed contract refused over the wire ({frag}), nothing spooled")

        # legacy call: response mode None, answer untouched, no choices nudge
        r, body = one_call(20, {"summary": "Context.\n1. look", "timeout": 60},
                           {"text": "✔ 1. done", "cancelled": False, "images": []})
        ok(body["response"] == {"mode": None, "options": []}
           and r["result"]["isError"] is False
           and r["result"]["content"][0]["text"] == "✔ 1. done"
           and "structuredContent" not in r["result"],
           "a legacy text-only answer is untouched but cannot masquerade as a receipt")

        # A current surface answering a LEGACY numbered request supplies the
        # additive answer contract. A real stdio process initialised as Codex
        # returns the v1 structured object without changing text/image blocks.
        png = base64.b64encode(b"receipt-image").decode()

        def current_surface(req):
            return {"text": "operator report", "cancelled": False,
                    "images": [{"mime": "image/png", "b64": png}],
                    "checklist": {
                        "schema": hcserver.CHECKLIST_ANSWER_SCHEMA,
                        "schema_version": 1, "request_id": req["id"],
                        "request_digest": req["request_digest"],
                        "steps": [{"id": row["id"], "verdict": "done",
                                   "note": "seen" if i == 0 else ""}
                                  for i, row in enumerate(
                                      req["checklist"]["steps"])]}}

        r, body = one_call(22, {
            "summary": "Legacy caller context.\n1. look\n2. click",
            "timeout": 60}, current_surface)
        result = r["result"]
        receipt = result.get("structuredContent")
        image_blocks = [block for block in result["content"]
                        if block.get("type") == "image"]
        signing_key = hcserver.receipt_signing_key(
            os.path.join(spool, "state", "receipt-key"))
        verified = hcserver.verify_checklist_result(
            result, expected_request_id=body["id"],
            expected_request_digest=body["request_digest"],
            expected_provider_instance=body["instance"]["server_uuid"],
            expected_provider_generation=generation,
            expected_step_ids=[row["id"] for row in body["checklist"]["steps"]],
            signing_key=signing_key)
        ok(result["isError"] is False
           and result["content"][0] == {"type": "text", "text": "operator report"}
           and len(image_blocks) == 1 and image_blocks[0]["data"] == png,
           "structured receipt is additive: legacy prose and image content are byte-preserved")
        ok(receipt["schema"] == "human-copilot.checklist-verdict/v1"
           and body["response"] == {"mode": None, "options": []}
           and body["provider"]["generation"] == generation
           and verified["valid"] and verified["authorizing"],
           "a real Codex-compatible stdio session receives an authenticated v1 object for a legacy numbered call")

        def malformed_surface(req):
            value = current_surface(req)
            value["text"] = "still return this"
            value["checklist"]["steps"] = value["checklist"]["steps"][:1]
            return value

        r, _b = one_call(23, {
            "summary": "Context.\n1. look\n2. click", "timeout": 60},
            malformed_surface)
        ok(r["result"]["content"][0]["text"] == "still return this"
           and "structuredContent" not in r["result"],
           "malformed surface cardinality preserves prose but yields no receipt")

        r, _b = one_call(24, {"summary": "Context.\n1. look", "timeout": 60},
                          {"text": "", "cancelled": True, "images": []})
        ok("dismissed" in r["result"]["content"][0]["text"]
           and "structuredContent" not in r["result"],
           "operator dismissal keeps its legacy result and emits no receipt")

        # choice-like prose answered in prose: the nudge rides back
        r, _b = one_call(21, {"summary": "Upgrade GitHub to Pro? ✔ yes · ✘ stay free",
                              "timeout": 60},
                         {"text": "yes please", "cancelled": False, "images": []})
        ok(any("copilot choices:" in b.get("text", "") for b in r["result"]["content"]),
           "a decision sent as prose earns the resend-as-choice nudge over the wire")

        # an unresolved target is an ENGINE-facing error, not a pass
        for k, err_body, frag in (
                (30, {"text": "", "cancelled": False, "images": [],
                      "error": {"reason": "no-match", "query": "Firefx"}},
                 "no window matching 'Firefx'"),
                (31, {"text": "", "cancelled": False, "images": [],
                      "error": {"reason": "ambiguous", "query": "hub", "matches": 3}},
                 "matched 3 windows")):
            r, _b = one_call(k, {"summary": "Context.\n1. look @{hub · full}",
                                 "timeout": 60}, err_body)
            c = r["result"]["content"]
            ok(r["result"]["isError"] is True and frag in c[0]["text"]
               and "Review or correct" in c[0]["text"]
               and "never shown to the operator" in c[0]["text"]
               and "structuredContent" not in r["result"],
               f"target error reaches the engine as isError ({frag})")
        ok(not os.listdir(os.path.join(spool, "requests"))
           and not os.listdir(os.path.join(spool, "answers")),
           "the spool is clean after every contract call")
    finally:
        proc.stdin.close(); proc.terminate(); proc.wait(timeout=5)
        shutil.rmtree(spool, ignore_errors=True)

    # -- the TUI half of the contract (curses-free): mode derivation, the
    # empty-send rule, and the answer write carrying the chosen option
    import tui
    ok(tui.response_of({"summary": "1. a step"}) ==
       {"mode": "checklist", "options": []},
       "a legacy request with steps reads as checklist")
    ok(tui.response_of({"summary": "just prose?"})["mode"] == "prose",
       "a legacy summary with no steps reads as prose — never an implicit ack")
    ok(tui.response_of({"summary": "q", "response": {"mode": "ack", "options": []}})
       == {"mode": "ack", "options": []},
       "an explicit ack is what the engine sent, not a derived shape")
    ok(tui.response_of({"summary": "q", "response": {"mode": "choice", "options": [
        {"value": "a", "label": "A"}, {"value": "b", "label": "B"}]}})
       ["options"][-1]["value"] == "b",
       "choice options ride through to the surface in order")
    ok(tui.response_of({"summary": "q", "response": {"mode": "choice",
                                                     "options": "junk"}})["options"] == [],
       "a malformed options payload renders as no options, never a crash")
    ok(tui.empty_send("ack", "", []) == "send",
       "an explicit ack sends empty — the ONE mode that may")
    ok("not an answer" in tui.empty_send("checklist", "", [])
       and "not an answer" in tui.empty_send("prose", "", []),
       "checklist and legacy prose REFUSE an empty send — no empty confirmation")
    ok(tui.empty_send("text", "", []).startswith("type your answer"),
       "text mode refuses an empty send")
    ok(tui.empty_send("choice", "", []).startswith("choose an option"),
       "choice mode refuses an empty send — never an empty ack")
    ok(tui.empty_send("prose", "", [], True).startswith("this reads as a decision"),
       "choice-like prose cannot silently become an empty ack")
    ok(tui.empty_send("prose", "typed", [], False) == "send"
       and tui.empty_send("ack", "", [{"mime": "image/png", "b64": "x"}]) == "send",
       "typed text or an attachment always sends")
    ok(tui.MODE_TAGS["choice"] == "[choice] " and "checklist" not in tui.MODE_TAGS,
       "the request list tags choice/ack/text in words, not colour")

    tui_req = {
        "id": "tui-verdict", "summary": "Context.\n1. look\n2. click\n3. judge",
        "created": 1.0, "deadline": 61.0, "timeout": 60, "client": "Codex",
        "instance": {"session": None, "pane": None,
                     "server_uuid": "c" * 32, "attached": None},
        "response": {"mode": None, "options": []},
    }
    hcserver.stamp_checklist_contract(tui_req, generation="tui-generation")
    structured = tui.checklist_answer(
        tui_req, "1. pass\n2. FAILED: button missing\n3. NOT DONE\n   note: ran out of time")
    ok([row["verdict"] for row in structured["steps"]] ==
       ["done", "failed", "not-done"]
       and structured["steps"][1]["note"] == "button missing"
       and structured["steps"][2]["note"] == "ran out of time"
       and structured["request_digest"] == tui_req["request_digest"],
       "TUI strict report maps pass/FAILED/NOT DONE and notes to ordered server ids")
    ok(tui.checklist_answer(tui_req, tui.report_template(tui_req["summary"]))
       is not None,
       "the TUI's existing ^T pass template is valid structured evidence")
    ok(tui.checklist_answer(tui_req, "everything looked great") is None
       and tui.checklist_answer({"id": "old"}, "1. pass") is None,
       "free prose and old requests stay prose-only; the surface invents no receipt binding")

    spool2 = tempfile.mkdtemp(prefix="hc-choice-")
    for d in ("requests", "answers"):
        os.makedirs(os.path.join(spool2, d))
    try:
        # A surface attached to a resident pre-4jq server still sees requests
        # with no responder contract. They remain local-only and commit through
        # the shared no-clobber lock without changing their legacy answer shape.
        for request_id in ("c1", "c2", "c3"):
            with open(os.path.join(spool2, "requests", request_id + ".json"), "w") as f:
                json.dump({"id": request_id, "summary": "legacy request",
                           "created": time.time(), "deadline": time.time() + 60}, f)
            os.chmod(os.path.join(spool2, "requests", request_id + ".json"), 0o600)
        result = tui.write_answer(
            spool2, "c1", "", [],
            choice={"value": "pro", "label": "Upgrade to Pro"})
        path = os.path.join(spool2, "answers", "c1.json")
        body = json.load(open(path))
        ok(result["ok"] and result["event"].get("legacy") is True
           and body == {"text": "", "cancelled": False, "images": [],
                    "choice": {"value": "pro", "label": "Upgrade to Pro"}},
           "a pre-adapter choice answer takes the locked legacy path with its exact shape")
        ok(os.stat(path).st_mode & 0o777 == 0o600, "the choice answer is 0600")
        tui.write_answer(spool2, "c2", "", [], cancelled=True)
        ok(json.load(open(os.path.join(spool2, "answers", "c2.json"))) ==
           {"text": "", "cancelled": True, "images": []},
           "the declined answer keeps its exact legacy shape (no choice key)")
        tui.write_answer(spool2, "c3", "report", [], checklist=structured)
        c3 = json.load(open(os.path.join(spool2, "answers", "c3.json")))
        ok(c3["text"] == "report" and c3["checklist"] == structured,
           "TUI answer file adds the machine verdict without changing human text")
    finally:
        shutil.rmtree(spool2, ignore_errors=True)

@_isolated_legacy_server_test
def test_instance_launch_wait():
    print("terminal launch evidence is bounded and survives a slow child")
    env = {"ZELLIJ_SESSION_NAME": "hc-test-session", "ZELLIJ_PANE_ID": "0",
           "HC_STUB_CLIENTS": "0"}
    result, _bodies, _zl, sp, _left = _instance_call(
        {**env, "HC_STUB_TERM_DELAY": "0.5"})
    ok(not result["isError"] and len(sp) == 1,
       "delayed terminal logging is observed after the MCP answer")
    start = time.monotonic()
    result, _bodies, _zl, sp, _left = _instance_call(
        {**env, "HC_STUB_TERM_SKIP_LOG": "1"}, spawn_timeout=0.1)
    ok(not result["isError"] and not sp and time.monotonic() - start < 5,
       "missing launch evidence returns a failing observation within its deadline")
    with tempfile.TemporaryDirectory(prefix="hc-stub-pid-") as root:
        pidfile = os.path.join(root, "pid")
        _instance_call({**env, "HC_STUB_TERM_DELAY": "30",
                        "HC_STUB_TERM_PIDFILE": pidfile}, spawn_timeout=0.1)
        with open(pidfile) as source:
            pid = int(source.read())
        try:
            with open(f"/proc/{pid}/stat") as source:
                stopped = source.read().rsplit(")", 1)[1].split()[0] == "Z"
        except FileNotFoundError:
            stopped = True
        ok(stopped, "timed-out detached stub cannot write after fixture removal")

def test_capability_poll_cache():
    print("idle capability cache invalidates every authority input")
    import server, toggle
    from unittest import mock
    with tempfile.TemporaryDirectory(prefix="hc-poll-cache-") as root, mock.patch.dict(
            os.environ, {"HOME": root, "CODEX_HOME": os.path.join(root, ".codex"),
                         "HC_CONTROL_FILE": os.path.join(root, "control.json")}):
        on_doc = toggle.MARK_BEGIN + "\nENABLED\n" + toggle.MARK_END
        values = {
            toggle.control_path(): json.dumps({"schema": toggle.CONTROL_SCHEMA,
                "schema_version": toggle.CONTROL_SCHEMA_VERSION, "enabled": True}),
            toggle.claude_json_path(): json.dumps({"mcpServers": {
                "human-copilot": toggle.default_registration()}}),
            toggle.codex_config_path(): toggle.default_codex_registration(),
            toggle.doctrine_path(): on_doc,
            toggle.codex_doctrine_path(): on_doc,
        }
        def write(path, text):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w") as target:
                target.write(text)
        for path, value in values.items():
            write(path, value)
        cache = server.CapabilityPollCache()
        with mock.patch.object(server, "capability_enabled", wraps=server.capability_enabled) as read:
            ok(all(cache.enabled() for _ in range(20)) and read.call_count == 1,
               "twenty unchanged polls parse authority only once")
        for path, value in values.items():
            write(path, "malformed")
            ok(cache.enabled() is False, f"cache revokes malformed {os.path.basename(path)}")
            replacement = path + ".new"
            write(replacement, value)
            os.replace(replacement, path)
            ok(cache.enabled() is True, f"cache detects replacement of {os.path.basename(path)}")
            os.unlink(path)
            ok(cache.enabled() == server.capability_enabled(),
               f"cache detects removal of {os.path.basename(path)}")
            write(path, value)
            ok(cache.enabled() is True, f"cache detects recreation of {os.path.basename(path)}")
        override = toggle.codex_override_path()
        write(override, toggle.MARK_BEGIN + "\nDISABLED\n" + toggle.MARK_END)
        ok(cache.enabled() is False, "new nonempty override invalidates the fallback doctrine")
        write(override, "")
        ok(cache.enabled() is True, "empty override restores fallback doctrine")
        write(override, on_doc)
        ok(cache.enabled() is True, "valid override becomes active")
        write(override, "malformed")
        ok(cache.enabled() is False, "malformed override revokes cached ON")
        os.unlink(override)
        ok(cache.enabled() is True, "override removal restores fallback doctrine")
        path = toggle.doctrine_path()
        before = os.stat(path)
        write(path, values[path].replace("ENABLED", "DISABLE"))
        os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
        with mock.patch.object(server, "capability_enabled", wraps=server.capability_enabled) as read:
            cache.enabled()
            ok(read.call_count == 1, "same-size rewrite with restored mtime invalidates via ctime")
        with mock.patch.object(cache, "signature", return_value=None), mock.patch.object(
                server, "capability_enabled", return_value=False) as read:
            ok(not cache.enabled() and not cache.enabled() and read.call_count == 2,
               "stat failures never reuse a cached verdict")
        cache = server.CapabilityPollCache()
        with mock.patch.object(server, "capability_enabled", side_effect=[False, True]) as read:
            ok(not cache.enabled() and cache.enabled() and read.call_count == 2,
               "transient denied reads can recover without a metadata change")
        cache = server.CapabilityPollCache()
        with mock.patch.object(cache, "signature", side_effect=[(1,), (2,), (2,), (2,)]), \
                mock.patch.object(server, "capability_enabled", return_value=True) as read:
            ok(cache.enabled() and cache.enabled() and read.call_count == 2,
               "authority changed during a read is not cached")
        with mock.patch.object(server, "capability_enabled", return_value=False) as read:
            with server.capability_guard() as enabled:
                ok(enabled is False and read.call_count == 1,
                   "request publication always performs the full authority read")

def test_publish_request():
    print("publish_request: the public non-blocking publication (hcop-da5)")
    import responder as hcresponder
    import server as hcserver

    # run_feedback (exercised below via `waited`) goes through
    # capability_guard(), which reads the real ambient control/registration
    # state. Standalone (outside the full suite's suite-wide ON fixture) that
    # ambient state need not be ON, so this test establishes and tears down
    # its own complete isolated ON fixture instead of depending on the caller
    # (hcop-7a6).
    control_root = tempfile.mkdtemp(prefix="hc-publish-control-")
    control_path = os.path.join(control_root, "control.json")
    fixture_home = os.path.join(control_root, "home")
    write_control_fixture(control_path, True)
    write_enabled_home_fixture(fixture_home)
    old_control = os.environ.get("HC_CONTROL_FILE")
    old_home = os.environ.get("HOME")
    old_codex_home = os.environ.get("CODEX_HOME")
    os.environ["HC_CONTROL_FILE"] = control_path
    os.environ["HOME"] = fixture_home
    os.environ["CODEX_HOME"] = os.path.join(fixture_home, ".codex")

    checklist = ("Context for the operator.\n"
                 "1. Inspect @{Firefox · top-right} → badge is green\n"
                 "2. Confirm the value")
    choice_opts = [{"value": "pro", "label": "Upgrade to Pro", "hint": "$4/mo"},
                   {"value": "free", "label": "Stay free"}]
    cases = [("checklist", checklist, {"mode": "checklist"}),
             ("choice", "Pick the release route.",
              {"mode": "choice", "options": choice_opts}),
             ("legacy", checklist, None)]

    spool = tempfile.mkdtemp(prefix="hc-publish-")
    for sub in ("requests", "answers"):
        os.makedirs(os.path.join(spool, sub), mode=0o700)
    saved = (hcserver.spool_dir, hcserver.instance_stamp, hcserver.hold_palette,
             hcserver.unattached_refusal, hcserver.maybe_spawn_tui,
             hcserver.arrival_nudge, hcserver.POLL_S)
    hcserver.spool_dir = lambda: spool
    hcserver.instance_stamp = lambda: {"session": "hc-A", "pane": "7",
                                       "server_uuid": "p" * 32, "attached": True}
    hcserver.hold_palette = lambda _inst, _spool: None
    hcserver.unattached_refusal = lambda _inst: None
    hcserver.maybe_spawn_tui = lambda *a, **k: True
    hcserver.arrival_nudge = lambda *a, **k: "already"
    hcserver.POLL_S = 0.01

    def waited(summary, response, rpc_id):
        """One run_feedback publication, read before its answer ends the wait."""
        outcome, spooled = [], []
        thread = threading.Thread(target=lambda: outcome.append(
            hcserver.run_feedback(summary, 120, rpc_id, response)))
        thread.start()
        for _ in range(400):
            names = [n for n in os.listdir(os.path.join(spool, "requests"))
                     if n.endswith(".json")]
            if names:
                with open(os.path.join(spool, "requests", names[0])) as f:
                    spooled.append(json.load(f))
                hcserver._write_private(
                    os.path.join(spool, "answers", names[0]),
                    {"text": "seen", "cancelled": False, "images": []})
                break
            time.sleep(0.005)
        thread.join(timeout=10)
        return spooled[0] if spooled else None

    try:
        for index, (name, summary, args) in enumerate(cases):
            contract, err = hcserver.response_contract(args or {})
            ok(err is None, f"{name}: fixture arguments are themselves valid")
            spooled = waited(summary, contract, 7100 + index)
            ok(spooled is not None, f"{name}: run_feedback published a request")
            req_id = hcserver.publish_request(summary, 120, args,
                                              client="graphene-copilot")
            path = os.path.join(spool, "requests", req_id + ".json")
            with open(path) as f:
                published = json.load(f)
            # Ownership is the ONE deliberate difference: publish_request does
            # not wait, so nothing survives to consume the answer and stamping
            # a dead caller would hand the pair to the orphan reclaimer.
            ok(set(spooled) - {"caller_process"} == set(published)
               and "caller_process" not in published,
               f"{name}: same field set as run_feedback, minus caller ownership")
            varies = {"id", "created", "deadline", "client",
                      "request_revision", "request_digest"}
            ok(all(spooled[key] == published[key]
                   for key in set(published) - varies),
               f"{name}: every stamped field matches run_feedback's ({name})")
            ok(published["timeout"] == 120
               and published["deadline"] - published["created"] == 120
               and published["client"] == "graphene-copilot",
               f"{name}: the publisher names itself and owns its own deadline")
            ok(published["request_revision"] == hcresponder.request_revision(published)
               and published["request_revision"] != spooled["request_revision"],
               f"{name}: the immutable revision covers this request's own bytes")
            if name == "choice":
                ok(published.get("checklist") is None
                   and "request_digest" not in published,
                   "choice: no checklist stamp and no request digest")
            else:
                ok(published["request_digest"]
                   == hcserver.checklist_request_digest(published)
                   and [row["id"] for row in published["checklist"]["steps"]]
                   == [row["id"] for row in published["responder"]["steps"]],
                   f"{name}: the checklist digest and step ids are the server's")
            ok(os.stat(path).st_mode & 0o777 == 0o600,
               f"{name}: the published request is 0600 like every spool write")
            os.unlink(path)

        before = sorted(os.listdir(os.path.join(spool, "requests")))
        refusals = [
            (("", 120, None, "surface"),
             "interactive_feedback needs a non-empty `summary`"),
            ((None, 120, None, "surface"),
             "interactive_feedback needs a non-empty `summary`"),
            (("   \n", 120, None, "surface"),
             "interactive_feedback needs a non-empty `summary`"),
            ((checklist, 120, {"mode": "bogus"}, "surface"), "must be one of"),
            ((checklist, 120, {"mode": "choice"}, "surface"), "needs `options`"),
            ((checklist, 120, {"mode": "choice", "options": choice_opts[:1]},
              "surface"), "at least two options"),
            ((checklist, 120, {"mode": "ack", "options": choice_opts},
              "surface"), "cannot be combined"),
            (("Plain prose with no numbered action.", 120,
              {"mode": "checklist"}, "surface"), "at least one numbered action"),
            ((checklist, 120, "checklist", "surface"),
             "`response` must be an object"),
            ((checklist, 120, None, ""), "`client` must be a short non-empty"),
            ((checklist, 120, None, None), "`client` must be a short non-empty"),
        ]
        for (summary, timeout, args, client), fragment in refusals:
            try:
                hcserver.publish_request(summary, timeout, args, client=client)
                raised = None
            except hcserver.PublishRefused as refusal:
                raised = refusal.text
            ok(raised is not None and fragment in raised,
               f"publish_request refuses {fragment!r} exactly as tools/call does")
        ok(sorted(os.listdir(os.path.join(spool, "requests"))) == before,
           "a refused publication shows the operator nothing")
        # The same gate the MCP lane uses, so the two can never drift apart.
        for (summary, timeout, args, _client), fragment in refusals[:8]:
            try:
                hcserver.feedback_arguments(summary, timeout, args)
                shared = None
            except hcserver.PublishRefused as refusal:
                shared = refusal.text
            ok(shared is not None and fragment in shared,
               f"the shared tools/call gate refuses {fragment!r} identically")
        clamped = hcserver.feedback_arguments(checklist, "not a number", {})[1]
        ok(clamped == hcserver.DEFAULT_TIMEOUT_S
           and hcserver.feedback_arguments(checklist, 5, {})[1] == hcserver.MIN_TIMEOUT_S
           and hcserver.feedback_arguments(checklist, 10 ** 9, {})[1] == hcserver.MAX_TIMEOUT_S,
           "an unusable timeout still clamps instead of refusing the call")
    finally:
        (hcserver.spool_dir, hcserver.instance_stamp, hcserver.hold_palette,
         hcserver.unattached_refusal, hcserver.maybe_spawn_tui,
         hcserver.arrival_nudge, hcserver.POLL_S) = saved
        shutil.rmtree(spool, ignore_errors=True)
        if old_control is None:
            os.environ.pop("HC_CONTROL_FILE", None)
        else:
            os.environ["HC_CONTROL_FILE"] = old_control
        if old_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_home
        if old_codex_home is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_codex_home
        shutil.rmtree(control_root, ignore_errors=True)

def test_malformed_protocol():
    print("malformed MCP messages preserve the connection")
    with tempfile.TemporaryDirectory(prefix="hc-protocol-") as root:
        env = dict(os.environ, HC_SPOOL=os.path.join(root, "spool"))
        proc = subprocess.Popen([sys.executable, os.path.join(HERE, "server.py")],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, env=env)
        try:
            for value in ([], None, 4, "text", {},
                          {"jsonrpc": "2.0", "id": [], "method": "ping"},
                          {"jsonrpc": "2.0", "id": True, "method": "ping"}):
                _rpc(proc, value)
                ok(_read(proc, None)["error"]["code"] == -32600,
                   f"invalid envelope rejected: {value!r}")
            for method, params in (("initialize", [1]), ("ping", None),
                                   ("tools/call", "bad"),
                                   ("tools/call", {"name": "interactive_feedback", "arguments": []}),
                                   ("tools/call", {"name": "interactive_feedback", "arguments": None})):
                _rpc(proc, {"jsonrpc": "2.0", "id": 41, "method": method,
                            "params": params})
                ok(_read(proc, 41)["error"]["code"] == -32602,
                   f"invalid {method} params receive an error")
            for params in ([1], {"requestId": []}, {"requestId": {}},
                           {"requestId": True}, {}):
                _rpc(proc, {"jsonrpc": "2.0", "method": "notifications/cancelled",
                            "params": params})
            _rpc(proc, {"jsonrpc": "2.0", "method": "tools/call", "params": {
                "name": "interactive_feedback", "arguments": {"summary": "Do not show this"}}})
            _rpc(proc, {"jsonrpc": "2.0", "id": 99, "method": "ping"})
            ok(_read(proc, 99)["result"] == {} and proc.poll() is None,
               "bad cancellations and ownerless calls cannot kill the next ping")
            ok(not os.path.exists(env["HC_SPOOL"]),
               "malformed input creates no spool or operator request")
        finally:
            proc.stdin.close(); proc.terminate(); proc.wait(timeout=5)
            proc.stdout.close(); proc.stderr.close()

if __name__ == "__main__":
    # Every build_status() call in this suite exercises fixture spools that
    # have nothing to do with the live desktop's GNOME extension state,
    # so the overlay_shell probe is stubbed for the whole run (never live
    # gsettings/gdbus/D-Bus). test_overlay_shell_evidence below
    # exercises the parser directly with its own per-case stubs.
    import toggle as _toggle_for_stub
    _toggle_for_stub._run_probe = _stub_overlay_shell_runner
    test_handoff_policy()
    test_doctrine_resync()
    test_toggle()
    test_capability_revocation()
    # Every remaining server harness is explicitly ON and isolated from the
    # operator's live control state. Individual tests still isolate spools.
    control_root = tempfile.mkdtemp(prefix="hc-suite-control-")
    suite_control = os.path.join(control_root, "control.json")
    suite_home = os.path.join(control_root, "home")
    write_control_fixture(suite_control, True)
    write_enabled_home_fixture(suite_home)
    old_suite_control = os.environ.get("HC_CONTROL_FILE")
    old_suite_home = os.environ.get("HOME")
    old_suite_codex = os.environ.get("CODEX_HOME")
    # Legacy routing tests explicitly opt in; production defaults to overlay.
    old_surface_policy = os.environ.get("HC_SURFACE_POLICY_FILE")
    suite_surface_policy = os.path.join(control_root, "surface.json")
    with open(suite_surface_policy, "w") as surface_file:
        json.dump({"mode": "auto"}, surface_file)
    os.environ["HC_SURFACE_POLICY_FILE"] = suite_surface_policy
    os.environ["HC_CONTROL_FILE"] = suite_control
    os.environ["HOME"] = suite_home
    os.environ["CODEX_HOME"] = os.path.join(suite_home, ".codex")
    try:
        test_status_contract()
        test_overlay_shell_evidence()
        test_symlink_here()
        test_server()
        test_malformed_protocol()
        test_capability_poll_cache()
        test_answered_orphan_lifecycle()
        test_tui_logic()
        test_overlay_spotlight_safety()
        # gsettings reads the real desktop's dconf independently of the MCP
        # fixture; with a brand-new HOME it prints typed empty variants such
        # as ``@as []`` instead of the operator's actual key inventory.
        if old_suite_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_suite_home
        try:
            test_overlay_hotkeys_unclaimed()
        finally:
            os.environ["HOME"] = suite_home
        test_instance_stamp()
        test_instance_launch_wait()
        test_instance_presence()
        test_sidepanel()
        test_palette()
        test_concurrent_lists()
        test_idle_selfclose()
        test_checklist_receipts()
        test_response_contract()
        test_publish_request()
    finally:
        if old_suite_control is None:
            os.environ.pop("HC_CONTROL_FILE", None)
        else:
            os.environ["HC_CONTROL_FILE"] = old_suite_control
        if old_suite_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = old_suite_home
        if old_suite_codex is None:
            os.environ.pop("CODEX_HOME", None)
        else:
            os.environ["CODEX_HOME"] = old_suite_codex
        if old_surface_policy is None:
            os.environ.pop("HC_SURFACE_POLICY_FILE", None)
        else:
            os.environ["HC_SURFACE_POLICY_FILE"] = old_surface_policy
        shutil.rmtree(control_root, ignore_errors=True)
    print(f"\nall good — {PASS} checks passed")
