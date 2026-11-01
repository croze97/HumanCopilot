#!/usr/bin/env python3
"""tuikit — shared stdlib-curses toolkit for the house TUIs.

One file, no dependencies, vendored per repo (like the shared hooks copy:
edit the canonical copy in zellij-hub/, then `cp` to the sibling repos — each
app must stay self-contained so a broken save in one repo can't take down the
other three).

What lives here (and what every app therefore stops duplicating):
  - put()            addstr that never throws on short screens / the last cell
  - init_colors()    named color attrs, NO_COLOR + mono fallback
  - Regions          click / double-click / wheel targets, rebuilt per draw
  - read_key()       logical-key reader: curses codes, Alt-chords, raw CSI
                     (Ctrl/Alt+arrows, bracketed paste) — ported from the
                     zellij-hub composer, see its comments for the whole story
  - LineEdit         single-line editor w/ readline motions + history
  - ListView         selectable scrolling list (vim keys, wheel, click,
                     double-click activate, type-ahead filter hook)
  - ScrollView       tail-following text pane (logs, feeds, screens)
  - modals           confirm / menu / help / scrollbox
  - Palette          Ctrl+P command palette with substring filter
  - keybar()         context-sensitive footer hints
  - Toast mixin      transient bottom-right notices
  - Poller           background HTTP poll thread with force-refresh event
  - TuiApp           the main loop: dirty-flag redraws, focus zones, resize,
                     mouse dispatch (draw-then-dispatch so regions match the
                     screen), palette/help wiring

House interaction rules every app inherits by using this:
  - everything reachable by keyboard is also clickable, and vice versa
  - j/k + arrows move, g/G home/end, ^d/^u half-page, wheel scrolls the pane
    under the cursor, Tab cycles focus zones, / filters lists, ? help,
    Ctrl+P palette, q quits (never the only way out: Esc backs out of modals)
  - colors are semantic: green ok/idle, yellow busy/warn, red attention/down,
    cyan accent/selection, magenta special — identical across all four apps
"""
import curses
import os
import re
import sys
import threading
import time

__version__ = "1.0"

# ----------------------------------------------------------------- fmt helpers

def trunc(s, w):
    """Flatten newlines and clip to w columns with an ellipsis."""
    s = (s or "").replace("\n", " ")
    if w <= 0:
        return ""
    return s if len(s) <= w else (s[: max(0, w - 1)] + "…")

def ago(t):
    s = max(0, time.time() - (t or 0))
    if s < 60: return f"{int(s)}s"
    if s < 3600: return f"{int(s / 60)}m"
    if s < 86400: return f"{int(s / 3600)}h"
    return f"{int(s / 86400)}d"

def dur(s):
    """Compact duration: 43s, 2m14s, 1h05m."""
    s = max(0, int(s or 0))
    if s < 60: return f"{s}s"
    if s < 3600: return f"{s // 60}m{s % 60:02d}s"
    return f"{s // 3600}h{(s % 3600) // 60:02d}m"

def kfmt(n):
    n = n or 0
    return f"{n / 1000:.1f}k".replace(".0k", "k") if n >= 1000 else str(n)

def bfmt(b):
    b = b or 0
    if b >= 1e9: return f"{b / 1e9:.1f}GB"
    if b >= 1e6: return f"{b / 1e6:.0f}MB"
    if b >= 1e3: return f"{b / 1e3:.0f}KB"
    return f"{b}B"

def homely(p):
    return (p or "").replace(os.path.expanduser("~"), "~")

SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

def spinner():
    return SPINNER[int(time.time() * 5) % len(SPINNER)]

# ----------------------------------------------------------------- colors

class C:
    """Named attributes, filled by init_colors(). Semantic, not decorative:
    the same name means the same thing in every app."""
    OK = WORK = ATT = ACC = BTN = SPC = DIM = 0

def init_colors():
    curses.start_color()
    curses.use_default_colors()
    C.DIM = curses.A_DIM
    if os.environ.get("NO_COLOR") or not curses.has_colors():
        # mono: selection/reverse still works, semantics degrade to bold/dim
        C.OK = 0; C.WORK = curses.A_BOLD; C.ATT = curses.A_BOLD
        C.ACC = curses.A_BOLD; C.BTN = curses.A_REVERSE; C.SPC = curses.A_BOLD
        return
    curses.init_pair(1, curses.COLOR_GREEN, -1)
    curses.init_pair(2, curses.COLOR_YELLOW, -1)
    curses.init_pair(3, curses.COLOR_RED, -1)
    curses.init_pair(4, curses.COLOR_CYAN, -1)
    curses.init_pair(5, curses.COLOR_BLACK, curses.COLOR_CYAN)
    curses.init_pair(6, curses.COLOR_MAGENTA, -1)
    C.OK, C.WORK, C.ATT = (curses.color_pair(i) for i in (1, 2, 3))
    C.ACC = curses.color_pair(4)
    C.BTN = curses.color_pair(5)
    C.SPC = curses.color_pair(6)

# ----------------------------------------------------------------- drawing

# Control characters arrive from the wild: a transcript whose first "prompt" was
# a pasted PNG, an escape sequence in a container log. A NUL makes addstr raise
# ValueError — which is NOT curses.error, so it used to take the whole app down —
# and the rest render as two-column ^X that quietly break every width sum. One
# flat 1:1 table, so len(s) stays the column count.
_SAFE = str.maketrans({**{c: "\u00b7" for c in range(32)}, 9: " ", 127: "\u00b7"})

def put(scr, y, x, s, attr=0, maxw=None):
    """addstr that never throws on the bottom-right cell / short screens."""
    h, w = scr.getmaxyx()
    if y < 0 or y >= h or x >= w:
        return
    if maxw is not None:
        s = trunc(s, maxw)
    s = (s or "").translate(_SAFE)[: max(0, w - x)]
    if not s:
        return
    try:
        scr.addstr(y, x, s, attr)
    except (curses.error, ValueError):
        pass

def vline(scr, x, y0, y1, attr=0):
    for y in range(y0, y1 + 1):
        put(scr, y, x, "│", attr or curses.A_DIM)

# ----------------------------------------------------------------- mouse

BUTTON5 = getattr(curses, "BUTTON5_PRESSED", 1 << 21)

class Regions:
    """Click + wheel targets, rebuilt on every draw. Later registrations win
    (overlays draw after the base screen)."""
    def __init__(self):
        self.click = []            # (y, x1, x2, fn)
        self.dbl = []              # (y, x1, x2, fn) — double-click targets
        self.wheel = []            # (y1, y2, x1, x2, fn(delta))

    def add(self, y, x1, x2, fn):
        self.click.append((y, x1, x2, fn))

    def add_dbl(self, y, x1, x2, fn):
        self.dbl.append((y, x1, x2, fn))

    def add_wheel(self, y1, y2, x1, x2, fn):
        self.wheel.append((y1, y2, x1, x2, fn))

    def on_click(self, y, x):
        for ry, x1, x2, fn in reversed(self.click):
            if ry == y and x1 <= x <= x2:
                fn(); return True
        return False

    def on_dbl(self, y, x):
        for ry, x1, x2, fn in reversed(self.dbl):
            if ry == y and x1 <= x <= x2:
                fn(); return True
        return self.on_click(y, x)     # dbl on a plain target = a click

    def on_wheel(self, y, x, delta):
        for y1, y2, x1, x2, fn in reversed(self.wheel):
            if y1 <= y <= y2 and x1 <= x <= x2:
                fn(delta); return True
        return False

def enable_mouse():
    curses.mousemask(curses.ALL_MOUSE_EVENTS | curses.REPORT_MOUSE_POSITION)
    curses.mouseinterval(120)

# ----------------------------------------------------------------- key reading
#
# Navigation keys arrive in three shapes and all three are real (verified in
# zellij-hub's composer, where the comments live): a curses KEY_* int, an
# extended terminfo int (kRIT5 etc — numbers vary per terminfo, match by
# keyname), or a raw CSI body when get_wch() got no keypad translation
# (bracketed paste markers never have a terminfo entry at all). Everything
# normalises to (name, mod) — mod is the xterm digit: 1 none 2 shift 3 alt
# 5 ctrl.

ESC_PEEK_MS = 40          # ncurses' own escdelay is 30ms; stay just above it

_KNAME_RE = re.compile(r"^k(UP|DN|RIT|LFT|HOM|END|DC)(\d)$")
_KNAME_TO_NAME = {"UP": "up", "DN": "down", "RIT": "right", "LFT": "left",
                  "HOM": "home", "END": "end", "DC": "del"}
_CODE_TO_NAME = {}          # filled in init_keys() — curses.KEY_* aren't consts
_CSI_RE = re.compile(r"^(?:(\d+)(?:;(\d+))?)?([A-Za-z~])$")
_CSI_FINAL = {"A": "up", "B": "down", "C": "right", "D": "left",
              "H": "home", "F": "end"}
_CSI_TILDE = {"2": "ins", "3": "del", "5": "pgup", "6": "pgdn",
              "1": "home", "4": "end"}
_NAME_FINAL = {"up": "A", "down": "B", "right": "C", "left": "D",
               "home": "H", "end": "F"}
_NAME_TILDE = {"ins": "2", "del": "3", "pgup": "5", "pgdn": "6"}

def init_keys():
    _CODE_TO_NAME.update({
        curses.KEY_UP: "up", curses.KEY_DOWN: "down",
        curses.KEY_LEFT: "left", curses.KEY_RIGHT: "right",
        curses.KEY_HOME: "home", curses.KEY_END: "end",
        curses.KEY_DC: "del", curses.KEY_IC: "ins",
        curses.KEY_PPAGE: "pgup", curses.KEY_NPAGE: "pgdn",
        curses.KEY_BACKSPACE: "backspace", curses.KEY_ENTER: "enter",
        curses.KEY_BTAB: "btab",
    })

def key_name(code):
    try:
        return curses.keyname(code).decode()
    except Exception:
        return ""

def norm_key(kind, val):
    """(name, mod) for a navigation key, else (None, 1)."""
    if kind == "code":
        if val in _CODE_TO_NAME:
            return _CODE_TO_NAME[val], 1
        m = _KNAME_RE.match(key_name(val))
        if m:
            return _KNAME_TO_NAME[m.group(1)], int(m.group(2))
        return None, 1
    if kind == "csi":
        m = _CSI_RE.match(val)
        if not m:
            return None, 1
        num, mod, fin = m.group(1), m.group(2), m.group(3)
        mod = int(mod) if mod else 1
        if fin == "~":
            return _CSI_TILDE.get(num or ""), mod
        if fin == "Z":
            return "btab", 1
        return _CSI_FINAL.get(fin), mod
    return None, 1

def name_bytes(name, mod=1):
    """Put a normalised key back on the wire as xterm would have sent it."""
    if name == "btab":
        return b"\x1b[Z"
    if name == "enter":
        return b"\r"
    if name == "backspace":
        return b"\x7f"
    if name in _NAME_FINAL:
        return (f"\x1b[1;{mod}{_NAME_FINAL[name]}" if mod > 1
                else f"\x1b[{_NAME_FINAL[name]}").encode()
    if name in _NAME_TILDE:
        n = _NAME_TILDE[name]
        return (f"\x1b[{n};{mod}~" if mod > 1 else f"\x1b[{n}~").encode()
    return None

def read_key(scr, wait_ms=None):
    """Read one logical key; wait_ms None = block, 0 = poll, N = wait N ms.
    Returns (kind, value):
        ("ch", str)    a character (printable or control)
        ("code", int)  a curses KEY_* constant
        ("alt", str)   Meta/Alt + character
        ("csi", str)   a raw CSI sequence body, e.g. "1;5D" or "200~"
        ("mouse", ev)  a curses.getmouse() tuple, already drained
        ("esc", None)  a bare Escape
        (None, None)   nothing available (non-blocking only)

    KEY_MOUSE is drained here (getmouse) — otherwise every editor loop that
    ignores mouse codes leaves a stale event in ncurses' queue."""
    scr.timeout(-1 if wait_ms is None else wait_ms)
    try:
        ch = scr.get_wch()
    except curses.error:
        return (None, None)
    if isinstance(ch, int) and ch == curses.KEY_MOUSE:
        try:
            return ("mouse", curses.getmouse())
        except curses.error:
            return (None, None)
    if ch != "\x1b":
        return ("code", ch) if isinstance(ch, int) else ("ch", ch)
    scr.timeout(ESC_PEEK_MS)
    try:
        nxt = scr.get_wch()
    except curses.error:
        return ("esc", None)
    if nxt != "[":
        return ("code", nxt) if isinstance(nxt, int) else ("alt", nxt)
    seq = ""
    while len(seq) < 16:                      # CSI bodies here are all short
        try:
            c = scr.get_wch()
        except curses.error:
            break
        if isinstance(c, int):
            break
        seq += c
        if "@" <= c <= "~":                   # CSI final byte ends the sequence
            break
    return ("csi", seq)

def bracketed_paste(on):
    """Ask the terminal to bracket pastes so a pasted newline is text, not a
    send. Harmless noise on terminals that don't support it."""
    try:
        sys.stdout.write("\x1b[?2004h" if on else "\x1b[?2004l")
        sys.stdout.flush()
    except Exception:
        pass

# ----------------------------------------------------------------- line editor

def word_left(buf, pos):
    i = pos
    while i > 0 and not buf[i - 1].isalnum():
        i -= 1
    while i > 0 and buf[i - 1].isalnum():
        i -= 1
    return i

def word_right(buf, pos):
    i, n = pos, len(buf)
    while i < n and not buf[i].isalnum():
        i += 1
    while i < n and buf[i].isalnum():
        i += 1
    return i

class LineEdit:
    """Single-line editor state machine. The owner draws it (so it can live
    inline in any layout) and feeds it keys; Enter/Esc are the owner's to
    interpret. Readline motions, kill ring, optional ↑↓ history."""
    def __init__(self, text="", history=None):
        self.buf = text
        self.pos = len(text)
        self.kill = ""
        self.hist = list(history or [])     # newest first
        self.hidx = -1
        self.draft = ""

    def draw(self, scr, y, x, w, attr=curses.A_UNDERLINE, focused=True):
        off = max(0, self.pos - (w - 1))
        put(scr, y, x, " " * w, attr)
        put(scr, y, x, self.buf[off:off + w], attr)
        if focused:
            try:
                scr.move(y, min(x + self.pos - off, x + w - 1))
            except curses.error:
                pass

    def _hist_step(self, up):
        if not self.hist:
            return
        if self.hidx < 0:
            self.draft = self.buf
        nidx = self.hidx + (1 if up else -1)
        if -1 <= nidx < len(self.hist):
            self.hidx = nidx
            self.buf = self.draft if nidx < 0 else self.hist[nidx]
            self.pos = len(self.buf)

    def key(self, kind, val):
        """Feed one read_key() event. Returns True if consumed."""
        b, p = self.buf, self.pos
        if kind == "ch":
            if val in ("\x7f", "\x08"):
                if p: self.buf = b[:p - 1] + b[p:]; self.pos = p - 1
            elif val == "\x01": self.pos = 0                        # ^A
            elif val == "\x05": self.pos = len(b)                   # ^E
            elif val == "\x02": self.pos = max(0, p - 1)            # ^B
            elif val == "\x06": self.pos = min(len(b), p + 1)       # ^F
            elif val == "\x0b":                                     # ^K
                self.kill = b[p:] or self.kill; self.buf = b[:p]
            elif val == "\x15":                                     # ^U
                self.kill = b[:p] or self.kill
                self.buf = b[p:]; self.pos = 0
            elif val == "\x17":                                     # ^W
                i = word_left(b, p)
                self.kill = b[i:p] or self.kill
                self.buf = b[:i] + b[p:]; self.pos = i
            elif val == "\x19":                                     # ^Y
                self.buf = b[:p] + self.kill + b[p:]
                self.pos = p + len(self.kill)
            elif val == "\x10": self._hist_step(True)               # ^P
            elif val == "\x0e": self._hist_step(False)              # ^N
            elif val.isprintable():
                self.buf = b[:p] + val + b[p:]; self.pos = p + 1
            else:
                return False
            return True
        if kind == "alt":
            if val == "b": self.pos = word_left(b, p)
            elif val == "f": self.pos = word_right(b, p)
            elif val == "d":
                i = word_right(b, p)
                self.kill = b[p:i] or self.kill
                self.buf = b[:p] + b[i:]
            else:
                return False
            return True
        nav, mod = norm_key(kind, val)
        if nav in ("left", "right") and mod in (3, 5, 7):
            self.pos = word_left(b, p) if nav == "left" else word_right(b, p)
        elif nav == "left": self.pos = max(0, p - 1)
        elif nav == "right": self.pos = min(len(b), p + 1)
        elif nav == "home": self.pos = 0
        elif nav == "end": self.pos = len(b)
        elif nav == "backspace":
            if p: self.buf = b[:p - 1] + b[p:]; self.pos = p - 1
        elif nav == "del":
            if p < len(b): self.buf = b[:p] + b[p + 1:]
        elif nav == "up": self._hist_step(True)
        elif nav == "down": self._hist_step(False)
        else:
            return False
        return True

def line_edit(scr, y, x, w, text="", history=None):
    """Modal single-line edit at a fixed spot. Enter -> str, Esc -> None."""
    ed = LineEdit(text, history)
    curses.curs_set(1)
    try:
        while True:
            ed.draw(scr, y, x, w)
            scr.refresh()
            kind, val = read_key(scr)
            if kind is None:
                continue
            if kind == "esc":
                return None
            if kind == "ch" and val in ("\n", "\r"):
                return ed.buf
            ed.key(kind, val)
    finally:
        curses.curs_set(0)
        scr.timeout(TICK_MS)

# ----------------------------------------------------------------- list view

class ListView:
    """Selection + scroll state for a vertical list. The app supplies items
    and a row renderer each draw; the view owns clamping, keyboard motion,
    wheel and click behavior (click selects, second click / Enter activates)."""
    def __init__(self):
        self.sel = 0
        self.scroll = 0
        self.last_h = 10          # visible rows, updated by render()

    def clamp(self, n, h):
        self.sel = max(0, min(self.sel, n - 1))
        if self.sel < self.scroll:
            self.scroll = self.sel
        if self.sel >= self.scroll + h:
            self.scroll = self.sel - h + 1
        self.scroll = max(0, min(self.scroll, max(0, n - h)))

    def key(self, ch, n, h):
        """Handle a motion key. Returns True if consumed."""
        if ch in (curses.KEY_DOWN, ord("j")): self.sel += 1
        elif ch in (curses.KEY_UP, ord("k")): self.sel = max(0, self.sel - 1)
        elif ch == curses.KEY_NPAGE or ch == 4: self.sel += max(1, h // 2)   # ^D
        elif ch == curses.KEY_PPAGE or ch == 21: self.sel -= max(1, h // 2)  # ^U
        elif ch == ord("g") or ch == curses.KEY_HOME: self.sel = 0
        elif ch == ord("G") or ch == curses.KEY_END: self.sel = n - 1
        else:
            return False
        self.sel = max(0, min(self.sel, n - 1))
        return True

    def render(self, scr, reg, y0, x0, h, w, items, draw_row,
               on_select=None, on_activate=None, row_h=1):
        """draw_row(scr, y, idx, item, selected, w). Registers mouse regions:
        click selects (and calls on_select), double-click activates."""
        n = len(items)
        vis = max(1, h // row_h)
        self.last_h = vis
        self.clamp(n, vis)
        for i in range(vis):
            idx = self.scroll + i
            if idx >= n:
                break
            y = y0 + i * row_h
            def sel_fn(i=idx):
                prev, self.sel = self.sel, i
                if on_select:
                    on_select(i, prev)
            # Row regions FIRST, then draw_row: later registrations win in
            # Regions, so anything the renderer registers (row buttons) must
            # come after the whole-row select target or it can't be clicked.
            for dy in range(row_h):
                reg.add(y + dy, x0, x0 + w - 1, sel_fn)
                if on_activate:
                    reg.add_dbl(y + dy, x0, x0 + w - 1,
                                lambda i=idx: on_activate(i))
            draw_row(scr, y, idx, items[idx], idx == self.sel, w)
        def whl(d):
            self.scroll = max(0, min(self.scroll + d, max(0, n - vis)))
        reg.add_wheel(y0, y0 + h - 1, x0, x0 + w - 1, whl)
        return vis

# ----------------------------------------------------------------- scroll view

class ScrollView:
    """Tail-following text pane. offset counts lines scrolled up from the
    tail; 0 = follow new output."""
    def __init__(self):
        self.offset = 0

    def scroll(self, d, n=None, h=None):
        # wheel up (d<0) = back in history = larger offset
        self.offset = max(0, self.offset - d)
        if n is not None and h is not None:
            self.offset = min(self.offset, max(0, n - h))

    def key(self, ch, n, h):
        if ch in (curses.KEY_UP, ord("k")): self.scroll(-1, n, h)
        elif ch in (curses.KEY_DOWN, ord("j")): self.scroll(+1, n, h)
        elif ch == curses.KEY_PPAGE or ch == 21: self.scroll(-max(1, h // 2), n, h)
        elif ch == curses.KEY_NPAGE or ch == 4: self.scroll(+max(1, h // 2), n, h)
        elif ch == ord("g") or ch == curses.KEY_HOME: self.offset = max(0, n - h)
        elif ch == ord("G") or ch == curses.KEY_END: self.offset = 0
        else:
            return False
        return True

    def render(self, scr, reg, y0, x0, h, w, lines, wrap=False):
        """lines: [(text, attr)]. Registers a wheel region. Returns the
        (start, end) slice drawn so callers can annotate."""
        self.offset = max(0, min(self.offset, max(0, len(lines) - h)))
        start = max(0, len(lines) - h - self.offset)
        for i, (txt, a) in enumerate(lines[start:start + h]):
            put(scr, y0 + i, x0, trunc(txt, w), a)
        reg.add_wheel(y0, y0 + h - 1, x0, x0 + w - 1,
                      lambda d: self.scroll(d, len(lines), h))
        if self.offset:
            put(scr, y0, x0 + w - 8, f" ↑{self.offset} ", C.WORK)
        return start, min(len(lines), start + h)

def wrap_msgs(msgs, width, roles=("you", "claude"), role_attr=None):
    """Conversation runs -> ScrollView lines. msgs: [{'role','text','ts'}]."""
    import textwrap as _tw
    role_attr = role_attr or {"user": C.ACC | curses.A_BOLD,
                              "assistant": C.OK | curses.A_BOLD}
    lines = []
    for m in msgs or []:
        who = roles[0] if m.get("role") == "user" else roles[1]
        lines.append((f"{who} · {ago(m.get('ts'))}",
                      role_attr.get(m.get("role"), curses.A_BOLD)))
        body = curses.A_DIM if m.get("role") == "user" else 0
        for ln in _tw.wrap(m.get("text") or "", max(8, width)) or [""]:
            lines.append((ln, body))
        lines.append(("", 0))
    return lines

# ----------------------------------------------------------------- modals

TICK_MS = 120

def confirm(scr, text, yes="yes", no="no", danger=False):
    h, w = scr.getmaxyx()
    bw = min(w - 4, max(30, len(text) + 6))
    y0, x0 = h // 2 - 2, max(0, (w - bw) // 2)
    ybtn, nbtn = f"[ {yes} ]", f"[ {no} ]"
    scr.timeout(-1)
    try:
        while True:
            for i in range(4):
                put(scr, y0 + i, x0, " " * bw, curses.A_REVERSE)
            put(scr, y0 + 1, x0 + 2, trunc(text, bw - 4),
                curses.A_REVERSE | curses.A_BOLD | (C.ATT if danger else 0))
            put(scr, y0 + 2, x0 + 2, ybtn, C.ATT | curses.A_REVERSE if danger else C.BTN)
            put(scr, y0 + 2, x0 + 2 + len(ybtn) + 2, nbtn, C.BTN)
            scr.refresh()
            ch = scr.getch()
            if ch in (ord("y"), ord("Y")):
                return True
            if ch in (ord("n"), ord("N"), 27, ord("\n"), ord("q")):
                return False
            if ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bs = curses.getmouse()
                except curses.error:
                    continue
                if not (bs & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
                    continue          # ignore the split release event
                if my == y0 + 2:
                    if x0 + 2 <= mx < x0 + 2 + len(ybtn):
                        return True
                    if x0 + 2 + len(ybtn) + 2 <= mx < x0 + 2 + len(ybtn) + 2 + len(nbtn):
                        return False
    finally:
        scr.timeout(TICK_MS)

def menu(scr, title, options, sel=0):
    """Modal picker. options: [str]. Returns index or None. Number keys,
    j/k, Enter, click; Esc/q/click-outside cancels."""
    bw = min(max(len(title), max((len(o) for o in options), default=0)) + 6, 60)
    scr.timeout(-1)
    try:
        while True:
            h, w = scr.getmaxyx()
            y0 = max(1, h // 2 - len(options) // 2 - 1)
            x0 = max(0, (w - bw) // 2)
            put(scr, y0, x0, trunc(f" {title} ", bw).ljust(bw),
                curses.A_REVERSE | curses.A_BOLD)
            for i, o in enumerate(options):
                a = (C.BTN | curses.A_BOLD) if i == sel else curses.A_REVERSE
                nm = f" {i + 1} " if i < 9 else "   "
                put(scr, y0 + 1 + i, x0, (nm + o).ljust(bw), a)
            scr.refresh()
            ch = scr.getch()
            if ch in (27, ord("q")):
                return None
            if ch in (curses.KEY_UP, ord("k")): sel = (sel - 1) % len(options)
            elif ch in (curses.KEY_DOWN, ord("j")): sel = (sel + 1) % len(options)
            elif ch == ord("\n"): return sel
            elif ord("1") <= ch <= min(ord("9"), ord("0") + len(options)):
                return ch - ord("1")
            elif ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bs = curses.getmouse()
                except curses.error:
                    continue
                if not (bs & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
                    continue          # ignore the split release event
                if x0 <= mx < x0 + bw and y0 + 1 <= my <= y0 + len(options):
                    return my - y0 - 1
                return None
    finally:
        scr.timeout(TICK_MS)

def scrollbox(scr, title, build_lines, extra_keys=None, footer=None):
    """Scrollable overlay. build_lines(width) -> [(text, attr)] rebuilt each
    frame (so live data keeps updating). extra_keys: {ch: fn} run inside.
    Esc/q closes."""
    sv = ScrollView()
    scr.timeout(TICK_MS)
    while True:
        h, w = scr.getmaxyx()
        x0, y0 = 2, 1
        bw, bh = w - 4, h - 2
        for i in range(bh):
            put(scr, y0 + i, x0, " " * bw, curses.A_REVERSE)
        put(scr, y0, x0, trunc(" " + title, bw - 8).ljust(bw - 6),
            curses.A_REVERSE | curses.A_BOLD)
        put(scr, y0, x0 + bw - 6, "[ ✕ ]", C.BTN)
        lines = [(t, a | curses.A_REVERSE) for t, a in build_lines(bw - 4)]
        body_h = bh - 2 - (1 if footer else 0)
        reg = Regions()
        # inside an overlay everything is A_REVERSE; ScrollView draws as-is
        sv.offset = max(0, min(sv.offset, max(0, len(lines) - body_h)))
        start = max(0, len(lines) - body_h - sv.offset)
        for i, (txt, a) in enumerate(lines[start:start + body_h]):
            put(scr, y0 + 1 + i, x0 + 2, trunc(txt, bw - 4), a)
        if footer:
            put(scr, y0 + bh - 1, x0, trunc(" " + footer, bw).ljust(bw),
                curses.A_REVERSE | curses.A_DIM)
        scr.refresh()
        ch = scr.getch()
        if ch == -1:
            continue
        if ch in (27, ord("q")):
            return
        if extra_keys and ch in extra_keys:
            if extra_keys[ch]() == "close":
                return
            continue
        if sv.key(ch, len(lines), body_h):
            continue
        if ch == curses.KEY_MOUSE:
            try:
                _, mx, my, _, bs = curses.getmouse()
            except curses.error:
                continue
            if bs & curses.BUTTON4_PRESSED:
                sv.scroll(-3, len(lines), body_h)
            elif bs & BUTTON5:
                sv.scroll(+3, len(lines), body_h)
            elif (bs & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)) \
                    and my == y0 and mx >= x0 + bw - 6:
                return

def help_modal(scr, lines):
    h, w = scr.getmaxyx()
    bw = min(w - 2, max(len(l) for l in lines) + 4)
    y0 = max(0, (h - len(lines) - 2) // 2)
    x0 = max(0, (w - bw) // 2)
    for i, l in enumerate([""] + lines + [""]):
        put(scr, y0 + i, x0, l.center(bw) if i == 1 else ("  " + l).ljust(bw),
            curses.A_REVERSE)
    scr.timeout(-1)
    scr.getch()
    scr.timeout(TICK_MS)

# ----------------------------------------------------------------- palette

class Command:
    """A palette/keybar entry. when() gates availability per frame."""
    def __init__(self, key, label, fn, when=None, keybar=False):
        self.key = key          # display label for the key ("p", "^G", "⏎")
        self.label = label
        self.fn = fn
        self.when = when
        self.keybar = keybar    # promote into the footer keybar

    def available(self):
        return self.when() if self.when else True

def palette(scr, commands):
    """Ctrl+P command palette: substring-filter over 'key  label', Enter runs.
    Returns the chosen Command (already run by the caller) or None."""
    ed = LineEdit()
    lv = ListView()
    curses.curs_set(1)
    scr.timeout(TICK_MS)
    try:
        while True:
            h, w = scr.getmaxyx()
            cmds = [c for c in commands if c.available()]
            needle = ed.buf.lower()
            vis = [c for c in cmds if needle in (c.key + " " + c.label).lower()]
            bw = min(w - 4, 64)
            bh = min(len(vis) + 2, h - 4, 16)
            y0, x0 = 1, max(0, (w - bw) // 2)
            put(scr, y0, x0, " ⌘ ".ljust(4), curses.A_REVERSE | curses.A_BOLD)
            ed.draw(scr, y0, x0 + 4, bw - 4, curses.A_REVERSE | curses.A_UNDERLINE)
            n = len(vis)
            lv.clamp(n, bh - 2)
            for i in range(bh - 2):
                idx = lv.scroll + i
                if idx >= n:
                    put(scr, y0 + 1 + i, x0, " " * bw, curses.A_REVERSE)
                    continue
                c = vis[idx]
                a = (C.BTN | curses.A_BOLD) if idx == lv.sel else curses.A_REVERSE
                put(scr, y0 + 1 + i, x0,
                    f" {c.key:>6}  {trunc(c.label, bw - 10)}".ljust(bw), a)
            put(scr, y0 + bh - 1, x0,
                trunc(" type to filter · ⏎ run · esc close", bw).ljust(bw),
                curses.A_REVERSE | curses.A_DIM)
            scr.refresh()
            kind, val = read_key(scr, wait_ms=TICK_MS)
            if kind is None:
                continue
            if kind == "esc":
                return None
            if kind == "ch" and val in ("\n", "\r"):
                return vis[lv.sel] if vis else None
            if kind == "mouse":
                _, mx, my, _, bs = val
                if not (bs & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED)):
                    continue          # ignore the split release event
                if y0 + 1 <= my < y0 + bh - 1 and x0 <= mx < x0 + bw:
                    idx = lv.scroll + (my - y0 - 1)
                    if idx < len(vis):
                        return vis[idx]
                elif not (y0 <= my < y0 + bh):
                    return None
                continue
            nav, _mod = norm_key(kind, val)
            if nav == "up" or (kind == "ch" and val == "\x10"):
                lv.sel = max(0, lv.sel - 1)
            elif nav == "down" or (kind == "ch" and val == "\x0e"):
                lv.sel += 1
            else:
                if ed.key(kind, val):
                    lv.sel = 0
    finally:
        curses.curs_set(0)
        scr.timeout(TICK_MS)

def keybar(scr, reg, commands, extra=""):
    """Bottom-row hints built from Commands marked keybar=True; every hint is
    clickable. Rightmost slot is left for toasts."""
    h, w = scr.getmaxyx()
    x = 1
    put(scr, h - 1, 0, " " * (w - 1), curses.A_DIM)
    for c in commands:
        if not (c.keybar and c.available()):
            continue
        t = f"{c.key} {c.label}"
        if x + len(t) + 3 >= w - 14:
            break
        put(scr, h - 1, x, c.key, C.ACC | curses.A_BOLD)
        put(scr, h - 1, x + len(c.key) + 1, c.label, curses.A_DIM)
        reg.add(h - 1, x, x + len(t) - 1, c.fn)
        x += len(t) + 3
    if extra:
        put(scr, h - 1, x, trunc(extra, w - x - 14), curses.A_DIM)

# ----------------------------------------------------------------- poller

class Poller:
    """Background data-fetch loop. fetch() runs off the UI thread; the app
    reads .snap/.err from draws. force() wakes it immediately."""
    def __init__(self, fetch, interval=lambda: 2.0):
        self.fetch = fetch          # () -> (snapshot | None, err | None)
        self.interval = interval
        self.snap = None
        self.err = None
        self.on_update = None       # set by TuiApp: marks dirty
        self._stop = False
        self._force = threading.Event()

    def start(self):
        threading.Thread(target=self._loop, daemon=True).start()
        return self

    def _loop(self):
        while not self._stop:
            snap, err = self.fetch()
            if snap is not None:
                self.snap = snap
                self.err = None
            else:
                self.err = err
            if self.on_update:
                self.on_update()
            self._force.wait(self.interval())
            self._force.clear()

    def force(self):
        self._force.set()

    def stop(self):
        self._stop = True
        self._force.set()

# ----------------------------------------------------------------- app shell

class TuiApp:
    """Main-loop skeleton. Subclass and implement:
        draw(scr, reg)                 full-screen paint (regions rebuilt here)
        handle_key(scr, ch) -> bool    app keys (after global keys miss)
        commands() -> [Command]        palette + keybar source (may be static)
    Optional: tick() per iteration, on_quit(). Zones: append zone names to
    self.zones; Tab cycles self.zone through them. Set `title` on the class
    to stamp the terminal tab (OSC 2) — how you find the tab in ptyxis."""
    REDRAW_S = 0.4          # idle repaint: clocks drift, spinners animate
    title = None

    def __init__(self):
        self.dirty = True
        self.zones = []
        self.zone = None
        self.toast_msg = ""
        self.toast_t = 0.0
        self.pollers = []
        self._quit = False

    # -- helpers ---------------------------------------------------------
    def toast(self, msg):
        self.toast_msg = str(msg)
        self.toast_t = time.time()
        self.dirty = True

    def draw_toast(self, scr):
        if self.toast_msg and time.time() - self.toast_t < 3.5:
            h, w = scr.getmaxyx()
            t = trunc(self.toast_msg, w - 4)
            put(scr, h - 1, max(0, w - len(t) - 2), " " + t + " ", curses.A_REVERSE)

    def add_poller(self, p):
        p.on_update = lambda: setattr(self, "dirty", True)
        self.pollers.append(p)
        return p

    def refresh_now(self):
        for p in self.pollers:
            p.force()

    def next_zone(self, d=1):
        if self.zones:
            i = self.zones.index(self.zone) if self.zone in self.zones else 0
            self.zone = self.zones[(i + d) % len(self.zones)]
            self.dirty = True

    def quit(self):
        self._quit = True

    # -- overridables ----------------------------------------------------
    def draw(self, scr, reg): ...
    def handle_key(self, scr, ch): return False
    def commands(self): return []
    def tick(self): ...
    def on_quit(self): ...
    def min_size(self): return (10, 40)

    # -- loop ------------------------------------------------------------
    def run(self):
        try:
            curses.wrapper(self._main)
        except KeyboardInterrupt:
            pass

    def _main(self, scr):
        curses.curs_set(0)
        if hasattr(curses, "set_escdelay"):
            curses.set_escdelay(30)
        if self.title:
            try:
                sys.stdout.write(f"\x1b]2;{self.title}\x07")
                sys.stdout.flush()
            except Exception:
                pass
        init_colors()
        init_keys()
        enable_mouse()
        scr.timeout(TICK_MS)
        if self.zones and self.zone is None:
            self.zone = self.zones[0]
        for p in self.pollers:
            p.start()
        self.startup(scr)
        last_draw = 0.0
        while not self._quit:
            self.tick()
            if self.dirty or time.time() - last_draw > self.REDRAW_S:
                mh, mw = self.min_size()
                h, w = scr.getmaxyx()
                reg = Regions()
                scr.erase()
                if h < mh or w < mw:
                    put(scr, 0, 0, f"window too small (need {mw}x{mh})", C.ATT)
                    scr.refresh()
                else:
                    self.draw(scr, reg)
                    self.draw_toast(scr)
                    scr.refresh()
                self.dirty = False
                last_draw = time.time()
            ch = scr.getch()
            if ch == -1:
                continue
            if ch == curses.KEY_RESIZE:
                self.dirty = True
                continue
            if ch == curses.KEY_MOUSE:
                try:
                    _, mx, my, _, bs = curses.getmouse()
                except curses.error:
                    continue
                reg = Regions()
                scr.erase()
                self.draw(scr, reg)      # regions must match what's on screen
                if bs & curses.BUTTON4_PRESSED:
                    reg.on_wheel(my, mx, -3)
                elif bs & BUTTON5:
                    reg.on_wheel(my, mx, +3)
                elif bs & curses.BUTTON1_DOUBLE_CLICKED:
                    reg.on_dbl(my, mx)
                elif bs & (curses.BUTTON1_PRESSED | curses.BUTTON1_CLICKED):
                    # NOT RELEASED: inside a zellij pane press/release arrive
                    # split (>mouseinterval), ncurses never merges them into
                    # CLICKED, and acting on both fired every target twice.
                    # Acting on press alone is exactly one fire either way —
                    # and a modal that opens on press no longer eats a
                    # phantom click when the release lands after it closes.
                    reg.on_click(my, mx)
                self.dirty = True
                continue
            if ch == ord("\t"):
                self.next_zone(+1); continue
            if ch == curses.KEY_BTAB:
                self.next_zone(-1); continue
            if ch == 16:                                   # Ctrl+P
                cmd = palette(scr, self.commands())
                if cmd:
                    cmd.fn()
                self.dirty = True
                continue
            if self.handle_key(scr, ch):
                self.dirty = True
                continue
            if ch == ord("q"):
                break
        for p in self.pollers:
            p.stop()
        self.on_quit()

    def startup(self, scr): ...

# ----------------------------------------------------------------- suspend

def suspend(scr):
    """Context manager: leave curses, run something on the real terminal,
    restore. The house 'CLI mode' — no emulation layer, ever."""
    class _S:
        def __enter__(self):
            curses.def_prog_mode()
            curses.endwin()
        def __exit__(self, *a):
            curses.reset_prog_mode()
            enable_mouse()
            curses.curs_set(0)
            scr.clear()
    return _S()
