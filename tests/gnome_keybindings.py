#!/usr/bin/env python3
"""GNOME keybinding inventory — the "taken chords" side of the overlay check.

The overlay's hotkeys are global grabs (Main.wm.addKeybinding), so a default
that GNOME, mutter, gnome-settings-daemon or another enabled extension already
binds is undefined behaviour: whichever grab wins, the loser silently does
nothing or fires the wrong action. On 2026-09-02 that was `<Super><Alt>s` —
the overlay's attach-snip default AND Ubuntu 26.04's screen-reader toggle
(hcop-9rz). selftest.py imports this module and FAILS when any overlay
default in the schema lands on a taken chord.

Two sources, and the caller always says which one it used:

  live      `gsettings list-recursively` over SCHEMAS, every
            media-keys custom-keybinding child, every system-compiled
            org.gnome.shell.extensions.* schema (Ubuntu ships dash-to-dock,
            ding, tiling-assistant there) and every ENABLED extension that
            carries its own schemas/ dir (read with --schemadir). Our own
            schema is excluded — it is the thing being checked.
  snapshot  tests/gnome-keybindings.snapshot, the same text captured from
            this box, for a run with no gsettings/dconf (e2e can run
            without a session bus). Regenerate after a GNOME upgrade or a
            keybinding change in Settings:

                python3 tests/gnome_keybindings.py > tests/gnome-keybindings.snapshot

Run as a script it prints the live dump (exit 1 with the reason on stderr
when gsettings is unavailable), which is exactly the snapshot format.

Normalisation (`normalize`) is what makes the comparison honest: GNOME writes
the magnifier chords `<Alt><Super>8` and the overlay writes `<Super><Alt>…`;
`<Primary>` and `<Ctrl>` are `<Control>`; a single-character key is
case-folded. Only the normalised forms are compared, the originals are what
the failure message shows.
"""
import ast
import glob
import os
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET

HERE = os.path.dirname(os.path.abspath(__file__))
SNAPSHOT = os.path.join(HERE, "gnome-keybindings.snapshot")
OVERLAY_SCHEMA = os.path.join(
    HERE, "..", "overlay", "copilot-overlay@croze", "schemas",
    "org.gnome.shell.extensions.copilot-overlay.gschema.xml")
OWN_SCHEMA_ID = "org.gnome.shell.extensions.copilot-overlay"

# Non-relocatable schemas whose keys are global grabs.
SCHEMAS = [
    "org.gnome.desktop.wm.keybindings",
    "org.gnome.shell.keybindings",
    "org.gnome.mutter.keybindings",
    "org.gnome.mutter.wayland.keybindings",
    "org.gnome.settings-daemon.plugins.media-keys",
]
MEDIA_KEYS = "org.gnome.settings-daemon.plugins.media-keys"
CUSTOM_SCHEMA = MEDIA_KEYS + ".custom-keybinding"
EXT_DIRS = [
    os.path.expanduser("~/.local/share/gnome-shell/extensions"),
    "/usr/share/gnome-shell/extensions",
]

MOD_ALIASES = {"primary": "Control", "ctrl": "Control", "control": "Control",
               "shift": "Shift", "alt": "Alt", "super": "Super",
               "meta": "Meta", "hyper": "Hyper", "mod1": "Alt", "mod4": "Super"}
# A value that IS a chord: modifier-prefixed, or one of the bare keys GNOME
# binds globally (Print, XF86*, F-keys, keypad). Anything else in an `as`
# key (desktop-file names, dconf paths) is not a binding.
ACCEL_RE = re.compile(r"^(<[A-Za-z0-9]+>)*[A-Za-z0-9_]+$")
BARE_OK_RE = re.compile(r"^(XF86\w+|Print|F\d{1,2}|KP_\w+|Menu|Pause|Scroll_Lock)$")


class Unavailable(Exception):
    """gsettings (or dconf behind it) could not produce the live dump."""


def normalize(accel):
    """'<Alt><Super>S' -> 'Alt+Super+s'; '' -> None; a non-chord -> None."""
    accel = (accel or "").strip()
    if not accel or not ACCEL_RE.match(accel):
        return None
    mods = []
    for m in re.findall(r"<([A-Za-z0-9]+)>", accel):
        mods.append(MOD_ALIASES.get(m.lower(), m))
    key = re.sub(r"<[^>]+>", "", accel)
    if not mods and not BARE_OK_RE.match(key):
        return None
    key = key.lower() if len(key) == 1 else key
    return "+".join(sorted(set(mods)) + [key])


def _run(argv):
    try:
        p = subprocess.run(argv, capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unavailable(f"{' '.join(argv)}: {e}")
    if p.returncode != 0:
        raise Unavailable(f"{' '.join(argv)} -> rc {p.returncode}: "
                          f"{p.stderr.strip()[:200]}")
    return p.stdout


def _gsettings(*args, schemadir=None):
    argv = ["gsettings"]
    if schemadir:
        argv += ["--schemadir", schemadir]
    return _run(argv + list(args))


def parse_string_array(value, source="gsettings value"):
    """Parse the string-array syntax printed by ``gsettings get``.

    GVariant has to annotate an empty array, so a legitimate empty ``as`` is
    printed as ``@as []`` rather than the Python-literal-shaped ``[]`` used by
    populated values.  Keep using ``literal_eval`` for the actual list and
    fail into the inventory's named snapshot path on any other shape.
    """
    raw = (value or "").strip() or "[]"
    if raw.startswith("@as "):
        raw = raw[4:].lstrip()
    try:
        parsed = ast.literal_eval(raw)
    except (ValueError, SyntaxError) as e:
        raise Unavailable(f"{source}: malformed string array: {raw[:120]!r}") from e
    if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
        raise Unavailable(f"{source}: expected a string array")
    return parsed


def _extension_schemas():
    """(schema_id, schemadir|None) for every extension we must not collide with."""
    out = []
    for sid in _gsettings("list-schemas").split():
        if sid.startswith("org.gnome.shell.extensions.") and sid != OWN_SCHEMA_ID:
            out.append((sid, None))
    enabled = parse_string_array(
        _gsettings("get", "org.gnome.shell", "enabled-extensions"),
        "org.gnome.shell enabled-extensions")
    for uuid in enabled:
        for base in EXT_DIRS:
            sdir = os.path.join(base, uuid, "schemas")
            if not os.path.isdir(sdir):
                continue
            for xmlf in glob.glob(os.path.join(sdir, "*.gschema.xml")):
                for schema in ET.parse(xmlf).getroot().iter("schema"):
                    sid = schema.get("id")
                    if sid and sid != OWN_SCHEMA_ID:
                        out.append((sid, sdir))
    return out


def dump_live():
    """The live inventory, in `gsettings list-recursively` line format."""
    if not shutil.which("gsettings"):
        raise Unavailable("no `gsettings` on PATH")
    lines = []
    for sid in SCHEMAS:
        lines.append(_gsettings("list-recursively", sid).rstrip("\n"))
    customs = parse_string_array(
        _gsettings("get", MEDIA_KEYS, "custom-keybindings"),
        f"{MEDIA_KEYS} custom-keybindings")
    for path in customs:
        lines.append(_gsettings("list-recursively",
                                f"{CUSTOM_SCHEMA}:{path}").rstrip("\n"))
    for sid, sdir in _extension_schemas():
        lines.append(_gsettings("list-recursively", sid,
                                schemadir=sdir).rstrip("\n"))
    return "\n".join(l for l in lines if l) + "\n"


def parse_dump(text):
    """dump text -> {normalized chord: [(schema, key, original), ...]}"""
    taken = {}
    for line in text.splitlines():
        if not line.strip() or line.startswith("#"):
            continue
        parts = line.split(" ", 2)
        if len(parts) != 3:
            continue
        schema, key, value = parts
        if value.startswith("@"):        # typed empty: @as []
            continue
        try:
            val = ast.literal_eval(value)
        except (ValueError, SyntaxError):
            continue
        accels = val if isinstance(val, list) else [val]
        for a in accels:
            if not isinstance(a, str):
                continue
            norm = normalize(a)
            if norm:
                taken.setdefault(norm, []).append((schema, key, a))
    return taken


def overlay_defaults(path=OVERLAY_SCHEMA):
    """{key name: [default accel, ...]} for every `as` key in our schema."""
    out = {}
    for key in ET.parse(path).getroot().iter("key"):
        if key.get("type") != "as":
            continue
        default = key.find("default")
        out[key.get("name")] = ast.literal_eval((default.text or "").strip())
    return out


def load_inventory():
    """(taken, source_line). Live first; the committed snapshot as fallback.

    Never returns an empty inventory silently: with neither source the caller
    gets an Unavailable and must fail.
    """
    try:
        text = dump_live()
        source = "live gsettings"
    except Unavailable as e:
        if not os.path.exists(SNAPSHOT):
            raise Unavailable(f"{e}; and no snapshot at {SNAPSHOT}")
        with open(SNAPSHOT) as f:
            text = f.read()
        stamp = next((l for l in text.splitlines() if l.startswith("# generated")),
                     "# generated ?")
        source = f"SNAPSHOT {os.path.relpath(SNAPSHOT, os.getcwd())} " \
                 f"({stamp[2:]}) because {e}"
    taken = parse_dump(text)
    if not taken:
        raise Unavailable(f"{source} produced zero bindings")
    return taken, source


def collisions(defaults, taken):
    """[(overlay key, overlay accel, [(schema, key, original)...]), ...]"""
    out = []
    for name, accels in defaults.items():
        for a in accels:
            hit = taken.get(normalize(a))
            if hit:
                out.append((name, a, hit))
    return out


if __name__ == "__main__":
    import datetime
    try:
        text = dump_live()
    except Unavailable as e:
        print(f"gnome_keybindings: {e}", file=sys.stderr)
        sys.exit(1)
    host = os.uname().nodename
    today = datetime.date.today().isoformat()
    print("# gnome-keybindings snapshot — `gsettings list-recursively` over the "
          "global-grab schemas, media-keys custom children and extension schemas")
    print(f"# generated {today} on {host} by: "
          f"python3 tests/gnome_keybindings.py > tests/gnome-keybindings.snapshot")
    print("# selftest.py uses this ONLY when live gsettings is unavailable, and says so.")
    sys.stdout.write(text)
