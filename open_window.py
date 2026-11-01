#!/usr/bin/env python3
"""copilot-open — put a command in a terminal window on the operator's desktop.

    copilot-open hub                # opens `hub` in a new terminal window
    copilot-open --no-hold hub      # ... and let the window close when hub exits
    copilot-open --print hub        # show the argv that would run, spawn nothing

For the agent side of the human-copilot lane: a step that says "run X" is
work the agent could do; this opens X where the operator can look at it,
using the SAME emulator choice as the TUI auto-open ($HC_TERMINAL, else
ptyxis --new-window -x, else x-terminal-emulator).

The window is HELD open after the command exits (2026-09-03, hcop-22h): a
short-lived command — a status, a table, a swatch sheet — otherwise printed
into a window that closed with it, and the operator was asked to look at
something that was no longer there. `--no-hold` is for a program that owns
its own window for as long as it runs. The window is titled after the
program for the same reason: a checklist step can only point at a window the
operator can name.

Exit 2 = usage, 3 = no terminal emulator known (set HC_TERMINAL).
"""
import os
import shlex
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import server  # noqa: E402  (stdlib-only; importing it prints nothing)

# Runs the operator's command, then keeps the window until they dismiss it.
# The command arrives as "$@" — positional arguments, never interpolated into
# this text — so an argument containing spaces, quotes or $ reaches the
# program intact.
HOLD_SCRIPT = (
    '"$@"; rc=$?; '
    'printf \'\\n[copilot-open] finished (exit %d) — press Enter to close\\n\' "$rc"; '
    'read -r _'
)


def hold(argv):
    """``argv`` wrapped so the terminal window survives the command's exit."""
    return ["bash", "-c", HOLD_SCRIPT, "copilot-open"] + list(argv)


def window_title(argv):
    """What the window is called: the program, so a step can name it."""
    return os.path.basename(argv[0]) if argv and argv[0] else None


def main(argv):
    show = False
    keep = True
    while argv and argv[0] in ("--print", "--no-hold"):
        if argv[0] == "--print":
            show = True
        else:
            keep = False
        argv = argv[1:]
    if argv and argv[0] == "--":
        argv = argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        sys.stderr.write(__doc__)
        return 2
    title = window_title(argv)
    inner = hold(argv) if keep else list(argv)
    full = server.terminal_argv(inner, title=title)
    if full is None:
        sys.stderr.write("copilot-open: no terminal emulator known — set "
                         "HC_TERMINAL (e.g. 'ptyxis --new-window -x')\n")
        return 3
    if show:
        print(shlex.join(full))
        return 0
    if server.open_in_terminal(inner, title=title) is None:
        sys.stderr.write("copilot-open: could not spawn %s\n" % shlex.join(full))
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
