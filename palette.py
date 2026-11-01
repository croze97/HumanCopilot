#!/usr/bin/env python3
"""palette.py — the per-instance colour palette, ONE source of truth.

    python3 palette.py --swatches     every entry as a numbered swatch line
    python3 palette.py --check        contrast + distinguishability report, rc 1 on a miss
    python3 palette.py --emit-js      the same table as an ES module (stdout)
    python3 palette.py --registry     the live assignment registry as JSON

Nine (or more) hues for the multi-instance lane (hcop-0p7): every copilot
SESSION gets one for its lifetime, and the TUI (curses), the overlay (CSS)
and the spotlight frame all draw the same colour from this table. tuikit.py
only knows the eight basic curses colours and must stay byte-identical to
zellij-hub's copy, so the palette lives here, not there. Stdlib only.

Every entry is an xterm-256 index whose sRGB is EXACT by construction — all
of them sit in the 6x6x6 cube (levels 0/95/135/175/215/255) or the grey
ramp — so `hex` is what a 256-colour terminal is asked to show, not an
approximation of it. `fg` is the text colour to draw ON a swatch of that
colour (a numbered chip); one dark ink pairs with all of them.

Thresholds (WCAG 2.x relative-luminance contrast, computed below):
  * every entry as TEXT on the overlay background and on the terminal
    background: >= 4.5:1 (normal-text bar; the stricter one, so the same
    number also covers the 3:1 large-text / UI-component bar for the
    spotlight frame and the instance strip);
  * the chip ink `fg` on every entry: >= 4.5:1;
  * mutual distinguishability: CIE76 delta-E between any two entries
    >= 30 (a floor against near-duplicates, not a claim of CVD safety —
    nine hues cannot all be told apart by every operator, which is why the
    NUMBER next to each swatch is load-bearing, never decoration).

Assignment (the registry) is per SESSION: the key is `instance.session`,
falling back to `instance.server_uuid` when there is no zellij session.
It lives in the spool (tmpfs, gone at logout) as `palette.json` under the
flock `palette.lock`, written 0600 via create-exclusive tmp + rename — a
visible file is a complete file, like every other spool file. A HOLDER is
one server process; it proves it is alive by keeping a shared flock on
`palette-holders/<holder>.lock` for its whole life (`hold()`; the tui.lock
idea: a lock cannot go stale and cannot match the wrong thing). A session
is reclaimed when none of its holders holds its lock any more. A lookup
that does not hold (`colour_for`, what a surface does for a server that
never called hold) is PROVISIONAL: it keeps its slot while slots are free
and is the first thing evicted (oldest first) when a new session needs one.
Two live sessions never share a colour; past len(PALETTE) live sessions the
table wraps and the result says so (`wrapped`, `shares_with`, `note`) —
the slot NUMBER is then what tells two of them apart.
"""
import fcntl
import json
import math
import os
import sys
import time

HERE = os.path.dirname(os.path.realpath(__file__))

# ---------------------------------------------------------------- the table
#
# Overlay chrome is `background-color: rgba(16,20,28,0.92)` (extension.js);
# over a dark desktop that composites to #10141c, the darker of the two
# backgrounds. Ptyxis here runs its stock GNOME palette in the dark
# interface style, whose background is #1e1e1e — the lighter one, and the
# one every ratio is tighter against.
OVERLAY_BG = "#10141c"
TERMINAL_BG = "#1e1e1e"
CHIP_INK = "#1a1a1a"          # the ink extension.js already uses on the spot label

TEXT_THRESHOLD = 4.5          # colour as text on a background, and ink on a chip
UI_THRESHOLD = 3.0            # colour as a frame / bar (large UI component)
DISTINCT_THRESHOLD = 30.0     # CIE76 delta-E between any two entries

# Secondary text (hints, notes, quotes, fenced code, the footer) in the TUI.
# It used to be curses A_DIM, which Ptyxis/VTE draws at half intensity: the
# stock #d0d0d0-ish foreground drops to about 3:1 on #1e1e1e and reads as
# grey mush (hcop-2um). This exact grey-ramp entry is quieter than the body
# text but still clears the text bar on both backgrounds; --check proves it.
SECONDARY_XTERM = 250         # #bcbcbc

# number = the ordinal the operator sees (1-based); index = position.
# basic = the 8-colour curses fallback when the terminal has no 256 colours.
_TABLE = (
    # number, name,     xterm, basic
    (1, "gold",   220, "yellow"),
    (2, "sky",     75, "blue"),
    (3, "coral",  209, "red"),
    (4, "green",   77, "green"),
    (5, "pink",   207, "magenta"),
    (6, "cyan",    44, "cyan"),
    (7, "rose",   204, "red"),
    (8, "violet", 141, "magenta"),
    (9, "silver", 252, "white"),
)


def xterm_rgb(index):
    """sRGB of an xterm-256 index in the cube or the grey ramp (16..255)."""
    if 16 <= index < 232:
        i = index - 16
        steps = (0, 95, 135, 175, 215, 255)
        return (steps[i // 36], steps[(i // 6) % 6], steps[i % 6])
    if 232 <= index <= 255:
        g = 8 + 10 * (index - 232)
        return (g, g, g)
    raise ValueError(f"xterm index {index} is not in the exact cube/ramp range")


def rgb_hex(rgb):
    return "#%02x%02x%02x" % tuple(rgb)


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def _entry(number, name, xterm, basic):
    rgb = xterm_rgb(xterm)
    return {"index": number - 1, "number": number, "name": name,
            "xterm": xterm, "hex": rgb_hex(rgb), "rgb": rgb,
            "fg": CHIP_INK, "basic": basic}


PALETTE = tuple(_entry(*row) for row in _TABLE)


def entry_for(slot):
    """The entry a 1-based slot draws; past the table it wraps."""
    return PALETTE[(slot - 1) % len(PALETTE)]


# ---------------------------------------------------------------- the maths

def _linear(v):
    v /= 255.0
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def relative_luminance(rgb):
    r, g, b = (_linear(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast_ratio(a, b):
    """WCAG contrast between two sRGB triples (or hex strings), >= 1."""
    if isinstance(a, str):
        a = hex_rgb(a)
    if isinstance(b, str):
        b = hex_rgb(b)
    la, lb = relative_luminance(a), relative_luminance(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def cielab(rgb):
    r, g, b = (_linear(v) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def delta_e(a, b):
    """CIE76 colour difference between two sRGB triples."""
    return math.dist(cielab(a), cielab(b))


def check_palette(palette=PALETTE):
    """Every threshold above, computed. Returns a list of problem strings —
    empty means the table is fit for both surfaces."""
    problems = []
    if len(palette) < 9:
        problems.append(f"only {len(palette)} entries, need >= 9")
    seen = {}
    for e in palette:
        if e["hex"] in seen:
            problems.append(f"{e['number']} {e['name']} repeats {seen[e['hex']]}'s hex")
        seen[e["hex"]] = e["name"]
        if rgb_hex(xterm_rgb(e["xterm"])) != e["hex"]:
            problems.append(f"{e['number']} {e['name']}: xterm {e['xterm']} is not {e['hex']}")
        for label, bg in (("overlay", OVERLAY_BG), ("terminal", TERMINAL_BG)):
            r = contrast_ratio(e["rgb"], bg)
            if r < TEXT_THRESHOLD:
                problems.append(f"{e['number']} {e['name']} {e['hex']} on {label} "
                                f"{bg}: {r:.2f}:1 < {TEXT_THRESHOLD}:1")
        r = contrast_ratio(e["rgb"], e["fg"])
        if r < TEXT_THRESHOLD:
            problems.append(f"{e['number']} {e['name']}: ink {e['fg']} on {e['hex']} "
                            f"{r:.2f}:1 < {TEXT_THRESHOLD}:1")
    sec = xterm_rgb(SECONDARY_XTERM)
    for label, bg in (("overlay", OVERLAY_BG), ("terminal", TERMINAL_BG)):
        r = contrast_ratio(sec, bg)
        if r < TEXT_THRESHOLD:
            problems.append(f"secondary text {rgb_hex(sec)} on {label} {bg}: "
                            f"{r:.2f}:1 < {TEXT_THRESHOLD}:1")
    for i, a in enumerate(palette):
        for b in palette[i + 1:]:
            d = delta_e(a["rgb"], b["rgb"])
            if d < DISTINCT_THRESHOLD:
                problems.append(f"{a['number']} {a['name']} vs {b['number']} {b['name']}: "
                                f"delta-E {d:.1f} < {DISTINCT_THRESHOLD}")
    return problems


def report(out=sys.stdout):
    out.write(f"backgrounds: overlay {OVERLAY_BG}  terminal {TERMINAL_BG}  "
              f"ink {CHIP_INK}\n")
    out.write(f"thresholds: text {TEXT_THRESHOLD}:1  ui {UI_THRESHOLD}:1  "
              f"delta-E {DISTINCT_THRESHOLD}\n")
    out.write(" n  name    xterm  hex      overlay  terminal  ink\n")
    for e in PALETTE:
        out.write(f"{e['number']:2d}  {e['name']:<7} {e['xterm']:5d}  {e['hex']}  "
                  f"{contrast_ratio(e['rgb'], OVERLAY_BG):6.2f}  "
                  f"{contrast_ratio(e['rgb'], TERMINAL_BG):8.2f}  "
                  f"{contrast_ratio(e['rgb'], e['fg']):5.2f}\n")
    sec = xterm_rgb(SECONDARY_XTERM)
    out.write(f" -  second  {SECONDARY_XTERM:5d}  {rgb_hex(sec)}  "
              f"{contrast_ratio(sec, OVERLAY_BG):6.2f}  "
              f"{contrast_ratio(sec, TERMINAL_BG):8.2f}      -\n")
    dmin = min(delta_e(a["rgb"], b["rgb"])
               for i, a in enumerate(PALETTE) for b in PALETTE[i + 1:])
    out.write(f"smallest pairwise delta-E: {dmin:.1f}\n")
    problems = check_palette()
    for p in problems:
        out.write(f"FAIL: {p}\n")
    return not problems


# ---------------------------------------------------------------- swatches

def swatch_lines():
    """One line per entry: a 256-colour block, a truecolor block of the same
    hex, the number and name as text next to them, and the hex. The two
    blocks are the measurement: under an emulator that honours both, they
    are identical; under one that re-renders the 256 index (zellij does)
    the difference is the thing to record."""
    lines = []
    ink = hex_rgb(CHIP_INK)
    for e in PALETTE:
        r, g, b = e["rgb"]
        chip256 = (f"\x1b[48;5;{e['xterm']}m\x1b[38;2;{ink[0]};{ink[1]};{ink[2]}m"
                   f" {e['number']} {e['name']:<6} \x1b[0m")
        chip24 = (f"\x1b[48;2;{r};{g};{b}m\x1b[38;2;{ink[0]};{ink[1]};{ink[2]}m"
                  f" {e['number']} {e['name']:<6} \x1b[0m")
        text = f"\x1b[38;5;{e['xterm']}m{e['number']}. {e['name']}\x1b[0m"
        pad = " " * (8 - len(e["name"]))
        lines.append(f"{e['number']:2d} {e['name']:<7} {chip256} {chip24} "
                     f"{text}{pad} {e['hex']}  xterm {e['xterm']}")
    return lines


# ---------------------------------------------------------------- emit-js

JS_PATH = os.path.join(HERE, "overlay", "copilot-overlay@croze", "palette.js")
REGENERATE = "python3 palette.py --emit-js > overlay/copilot-overlay@croze/palette.js"


def emit_js():
    """The same table as an ES module for the overlay. Deterministic text:
    selftest compares the committed file to this byte for byte."""
    out = [
        "// palette.js — GENERATED from palette.py, do not edit:",
        f"//     {REGENERATE}",
        "// One source of truth for the per-instance colours on every surface.",
        f"export const OVERLAY_BG = '{OVERLAY_BG}';",
        f"export const TERMINAL_BG = '{TERMINAL_BG}';",
        f"export const TEXT_THRESHOLD = {TEXT_THRESHOLD};",
        f"export const UI_THRESHOLD = {UI_THRESHOLD};",
        "export const PALETTE = Object.freeze([",
    ]
    for e in PALETTE:
        out.append(f"    Object.freeze({{number: {e['number']}, name: '{e['name']}', "
                   f"hex: '{e['hex']}', fg: '{e['fg']}', xterm: {e['xterm']}}}),")
    out += [
        "]);",
        "",
        "// The entry a 1-based slot draws; past the table it wraps (the slot",
        "// number is then what tells two instances apart).",
        "export function entryFor(slot) {",
        "    return PALETTE[(slot - 1) % PALETTE.length];",
        "}",
        "",
    ]
    return "\n".join(out)


def js_in_sync():
    """None when the committed palette.js equals emit_js(), else a message."""
    try:
        with open(JS_PATH) as f:
            current = f.read()
    except OSError as e:
        return f"{JS_PATH} unreadable ({e}) — regenerate: {REGENERATE}"
    if current != emit_js():
        return f"palette.js is STALE against palette.py — regenerate: {REGENERATE}"
    return None


# ---------------------------------------------------------------- registry

REGISTRY = "palette.json"
LOCK = "palette.lock"
HOLDERS = "palette-holders"


def default_spool():
    from server import spool_dir            # ONE spool resolution, not two
    return spool_dir()


def session_key(instance):
    """The registry key: the zellij session, else the server's uuid."""
    if isinstance(instance, str):
        return instance
    return instance.get("session") or instance.get("server_uuid")


def _write_private(path, data):
    tmp = f"{path}.tmp-{os.getpid()}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, sort_keys=True)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    os.replace(tmp, path)


def _load(spool):
    try:
        with open(os.path.join(spool, REGISTRY)) as f:
            reg = json.load(f)
        if isinstance(reg, dict) and isinstance(reg.get("sessions"), dict):
            return reg
    except (OSError, ValueError):
        pass
    return {"version": 1, "sessions": {}}


class _Locked:
    """Exclusive flock on palette.lock around one read-modify-write."""
    def __init__(self, spool):
        self.spool = spool

    def __enter__(self):
        self.fd = os.open(os.path.join(self.spool, LOCK),
                          os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)


def _holder_path(spool, holder):
    d = os.path.join(spool, HOLDERS)
    os.makedirs(d, mode=0o700, exist_ok=True)
    return os.path.join(d, f"{holder}.lock")


def _lock_held(path):
    """True when some process holds a flock on `path` (the tui_alive probe)."""
    try:
        fd = os.open(path, os.O_RDWR)
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


def _reclaim(reg, spool):
    """Drop holders whose lease lock is no longer held, then sessions with
    no holder left. Returns the keys that were released."""
    gone = []
    for key, rec in list(reg["sessions"].items()):
        for holder, h in list(rec["holders"].items()):
            if h.get("leased"):
                path = os.path.join(spool, HOLDERS, f"{holder}.lock")
                if not _lock_held(path):
                    del rec["holders"][holder]
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        if not rec["holders"]:
            del reg["sessions"][key]
            gone.append(key)
    return gone


def _is_provisional(rec):
    return not any(h.get("leased") for h in rec["holders"].values())


def _free_slot(reg):
    """Lowest free slot in 1..N; evict the oldest provisional session
    first when the table is full; else the next slot past N (wrapped)."""
    n = len(PALETTE)
    used = {rec["slot"] for rec in reg["sessions"].values()}
    for s in range(1, n + 1):
        if s not in used:
            return s, None
    prov = sorted((rec["since"], key) for key, rec in reg["sessions"].items()
                  if _is_provisional(rec))
    if prov:
        _, key = prov[0]
        slot = reg["sessions"].pop(key)["slot"]
        return slot, key
    s = n + 1
    while s in used:
        s += 1
    return s, None


class Assignment:
    """What a caller gets back. `entry` is the palette row; `number` is the
    slot number the operator sees next to the colour; `wrapped` says the
    table ran out and `shares_with` names the live sessions drawing the same
    entry; `note` is a sentence saying so, else None."""
    __slots__ = ("key", "slot", "number", "entry", "wrapped", "shares_with",
                 "evicted", "released", "note", "lease")

    def __init__(self, key, slot, shares_with=(), evicted=None, released=()):
        self.key = key
        self.slot = slot
        self.number = slot
        self.entry = entry_for(slot)
        self.wrapped = slot > len(PALETTE)
        self.shares_with = list(shares_with)
        self.evicted = evicted
        self.released = list(released)
        self.lease = None
        if self.wrapped:
            self.note = (f"palette wrapped: slot {slot} draws {self.entry['name']} "
                         f"({self.entry['hex']}) like "
                         + ", ".join(f"{k}" for k in self.shares_with)
                         + " — the number is what tells them apart")
        else:
            self.note = None

    def as_dict(self):
        return {"key": self.key, "slot": self.slot, "number": self.number,
                "entry": {k: v for k, v in self.entry.items() if k != "rgb"},
                "wrapped": self.wrapped, "shares_with": self.shares_with,
                "evicted": self.evicted, "released": self.released,
                "note": self.note}

    def release(self):
        if self.lease is not None:
            self.lease.release()

    def __repr__(self):
        return (f"Assignment({self.key!r}, slot={self.slot}, "
                f"{self.entry['name']} {self.entry['hex']}"
                + (", WRAPPED" if self.wrapped else "") + ")")


class Lease:
    """A held shared flock on the holder's lock file: alive while open."""
    def __init__(self, spool, key, holder):
        self.spool, self.key, self.holder = spool, key, holder
        self.fd = os.open(_holder_path(spool, holder),
                          os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_SH)

    def release(self):
        if self.fd is None:
            return
        try:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
        finally:
            os.close(self.fd)
            self.fd = None
        release(self.key, self.holder, self.spool)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.release()


def assign(instance, holder=None, spool=None, leased=False):
    """The session's colour, allocating one if it has none. `holder` names
    the server process (its uuid); `leased=True` records that the holder
    keeps a lock (use `hold()` — it takes the lock first)."""
    spool = spool or default_spool()
    key = session_key(instance)
    if not key:
        raise ValueError("instance has neither session nor server_uuid")
    holder = holder or (instance.get("server_uuid") if isinstance(instance, dict)
                        else None) or key
    with _Locked(spool):
        reg = _load(spool)
        released = _reclaim(reg, spool)
        evicted = None
        rec = reg["sessions"].get(key)
        if rec is None:
            slot, evicted = _free_slot(reg)
            rec = {"slot": slot, "since": time.time(), "holders": {}}
            reg["sessions"][key] = rec
        # Leased holders are authoritative: a session is either provisional
        # (only lookups know it) or leased (a server proves it is alive), never
        # a mix — otherwise a surface's lookup would keep a dead server's
        # session registered forever.
        if leased:
            rec["holders"] = {k: v for k, v in rec["holders"].items() if v.get("leased")}
            rec["holders"][holder] = {"since": time.time(), "leased": True}
        elif not any(v.get("leased") for v in rec["holders"].values()):
            rec["holders"].setdefault(holder, {"since": time.time(), "leased": False})
        shares = [k for k, r in reg["sessions"].items()
                  if k != key and entry_for(r["slot"]) is entry_for(rec["slot"])]
        _write_private(os.path.join(spool, REGISTRY), reg)
    return Assignment(key, rec["slot"], shares, evicted, released)


def hold(instance, holder=None, spool=None):
    """assign() for a SERVER: takes the holder's shared lock first, so the
    session stays assigned exactly as long as this process keeps the lease
    (`.release()`, or the process ending). Returns the Assignment with
    `.lease` set."""
    spool = spool or default_spool()
    key = session_key(instance)
    holder = holder or (instance.get("server_uuid") if isinstance(instance, dict)
                        else None) or key
    lease = Lease(spool, key, holder)
    try:
        a = assign(instance, holder, spool, leased=True)
    except BaseException:
        if lease.fd is not None:
            os.close(lease.fd)
            lease.fd = None
        raise
    a.lease = lease
    return a


def colour_for(instance, spool=None):
    """What a SURFACE asks: the colour for a request's instance stamp —
    the registered one, else a provisional assignment (see module doc)."""
    return assign(instance, spool=spool)


def lookup(instance, spool=None):
    """The registered Assignment, or None. Never allocates, but does
    reclaim: a session whose holders are all dead reads as None."""
    spool = spool or default_spool()
    key = session_key(instance)
    with _Locked(spool):
        reg = _load(spool)
        if _reclaim(reg, spool):
            _write_private(os.path.join(spool, REGISTRY), reg)
        rec = reg["sessions"].get(key)
        if rec is None:
            return None
        shares = [k for k, r in reg["sessions"].items()
                  if k != key and entry_for(r["slot"]) is entry_for(rec["slot"])]
    return Assignment(key, rec["slot"], shares)


def release(instance, holder=None, spool=None):
    """Drop one holder from a session (and the session when it was the
    last). Returns True when the session was reclaimed by this call."""
    spool = spool or default_spool()
    key = session_key(instance)
    holder = holder or (instance.get("server_uuid") if isinstance(instance, dict)
                        else None) or key
    with _Locked(spool):
        reg = _load(spool)
        rec = reg["sessions"].get(key)
        if rec is None:
            return False
        rec["holders"].pop(holder, None)
        gone = not rec["holders"]
        if gone:
            del reg["sessions"][key]
        _reclaim(reg, spool)
        _write_private(os.path.join(spool, REGISTRY), reg)
    return gone


def live_sessions(spool=None):
    """{key: Assignment} for every registered session, after a reclaim."""
    spool = spool or default_spool()
    with _Locked(spool):
        reg = _load(spool)
        if _reclaim(reg, spool):
            _write_private(os.path.join(spool, REGISTRY), reg)
        out = {}
        for key, rec in reg["sessions"].items():
            shares = [k for k, r in reg["sessions"].items()
                      if k != key and entry_for(r["slot"]) is entry_for(rec["slot"])]
            out[key] = Assignment(key, rec["slot"], shares)
    return out


# ---------------------------------------------------------------- curses

def init_curses_pairs(first_pair):
    """Init one colour pair per entry at first_pair + index (fg on the
    default background) and return {number: attr}. Uses the exact xterm
    index when the terminal has 256 colours, else the entry's basic colour.
    Call after tuikit.init_colors(); pairs 1-6 are tuikit's."""
    import curses
    attrs = {}
    have256 = curses.has_colors() and curses.COLORS >= 256
    for e in PALETTE:
        pair = first_pair + e["index"]
        fg = e["xterm"] if have256 else getattr(curses, "COLOR_" + e["basic"].upper())
        try:
            curses.init_pair(pair, fg, -1)
            attrs[e["number"]] = curses.color_pair(pair)
        except curses.error:
            attrs[e["number"]] = 0
    return attrs


def init_secondary_pair(pair):
    """The attr for secondary TUI text: SECONDARY_XTERM on the default
    background when the terminal has 256 colours, else plain normal text.
    Never A_DIM — half-intensity text is what failed the contrast bar."""
    import curses
    if not (curses.has_colors() and curses.COLORS >= 256):
        return curses.A_NORMAL
    try:
        curses.init_pair(pair, SECONDARY_XTERM, -1)
        return curses.color_pair(pair)
    except curses.error:
        return curses.A_NORMAL


# ---------------------------------------------------------------- cli

def main(argv):
    if argv == ["--swatches"]:
        print("human-copilot instance palette — left chip: 256-colour index, "
              "right chip: exact hex (truecolor); same colour = renders true")
        for line in swatch_lines():
            print(line)
        return 0
    if argv == ["--check"]:
        return 0 if report() else 1
    if argv == ["--emit-js"]:
        sys.stdout.write(emit_js())
        return 0
    if argv == ["--registry"]:
        print(json.dumps({k: a.as_dict() for k, a in live_sessions().items()},
                         indent=1, sort_keys=True))
        return 0
    sys.stderr.write(__doc__.split("\n\n")[0] + "\n")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
