# Human Copilot

A local MCP server that lets an AI coding agent request a human fact, decision,
or action when it cannot continue autonomously. Answer from a GNOME Shell
overlay or an explicitly opened terminal panel. Supports checklists, text,
choices, acknowledgements, optional clipboard images, and multiple agent sessions.

**Status:** Complete for the local desktop workflow. No feature roadmap;
maintenance is occasional. Linux with Python 3.11+ is required. The overlay is
supported on GNOME Shell 50; other Shell versions are unverified. The Python
runtime uses only the standard library. Zellij is optional.

## Install

Install your distribution's Python 3 (including curses), GLib schema tools,
GNOME extension tools, and GJS. `wl-clipboard` is optional for clipboard images.
Install Claude Code and/or Codex separately if you want their configuration
managed by the toggle command.

```sh
git clone --branch v1.0.0 https://github.com/croze97/HumanCopilot.git
cd HumanCopilot
python3 install.py
export PATH="$HOME/bin:$PATH"
```

Keep this checkout in place: commands and the GNOME extension link to it.
The installer adds a managed **disabled** guidance block to
`~/.claude/CLAUDE.md`, preserving surrounding text, and initializes an empty Claude configuration only
when none exists. It refuses unrelated files
at its command/extension destinations. It does not enable the MCP lane or alter
your desktop's enabled-extension list.

Log out and back in so GNOME discovers the extension, then enable it explicitly:

```sh
gnome-extensions enable copilot-overlay@croze
copilot-toggle on
```

The toggle manages the `human-copilot` registration in `~/.claude.json` and
`${CODEX_HOME:-$HOME/.codex}/config.toml`, and managed agent guidance blocks.
Review [the bundled guidance](docs/SAFETY.md) before enabling. Its conservative
policy mentions optional Desktop Control, screenctx and Beads integrations;
those tools are not included or required for this MCP server. Adapt both
`doctrine-on.md` and `doctrine-off.md` to your environment if needed.
Restart agent sessions after registration changes; existing sessions may retain
old tool descriptions. `copilot-toggle state` reports the connection boundary.

## Use

Ask your MCP client to call `human-copilot.interactive_feedback` when a human
fact or action is actually needed. For example, a text request can carry:

```json
{"summary":"Choose the deployment window needed to continue.","mode":"text"}
```

The request appears in the overlay. Enter your response and submit it; the
calling agent receives the answer under the request ID. Checklist entries keep
individual done/failed/not-done results. Dismissal and timeout do not count as
approval. The overlay's glossary lists shortcuts; `Super+Alt+n` opens a note,
`Super+Alt+Return` submits, and `Super+Alt+BackSpace` declines.

`copilot-tui` explicitly opens the terminal answering surface. `copilot-toggle
off` disables the lane and makes stale calls refuse without prompting.
`python3 server.py` starts the JSON-RPC stdio server directly for another MCP
client; do not use its stdout for logs. Inspect its `tools/list` response for
the complete request schema.

No network listener is started. Requests and optional attachments stay in the
local runtime spool; the connected AI client can forward returned content to
its provider. Attach only information you intend to share. Files use private
permissions. Authenticated receipts protect local request bindings, not against
an attacker controlling the same Unix account. The clipboard attachment feature
does not grant an agent permission to capture the desktop.

## Verify

From the checkout:

```sh
python3 selftest.py
python3 -m unittest discover -s tests -p 'test_*.py'
python3 tests/drive_tui.py
gjs -m tests/drive_overlay.js
```

These exercise isolated protocol/configuration fixtures and interface logic.
They do not establish physical pointer behavior or compatibility with an
unlisted GNOME version. The fallback keybinding inventory is a synthetic
fixture; check shortcut conflicts on your own desktop before assigning them.

## Update or uninstall

Disable the lane before changing versions. Check out a new release and rerun
`python3 install.py`; log out/in to load updated GNOME extension code.

```sh
copilot-toggle off
gnome-extensions disable copilot-overlay@croze
python3 install.py --uninstall
```

Uninstall removes only this checkout's links. Private state under
`~/.local/state/human-copilot`, client configuration and managed disabled
guidance blocks remain. Optional attachments in the runtime spool normally
expire at logout. Remove retained state only when you no longer need recovery
or signed receipts.

Report reproducible bugs through GitHub issues; response times are not promised.
Use GitHub private vulnerability reporting for sensitive security reports rather
than posting credentials, prompt contents or screenshots publicly.

## License

See `LICENSE` for the source license and `NOTICE.md` for included components.
