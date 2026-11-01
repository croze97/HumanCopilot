// lib.js — the curses^W chrome-free half of the copilot overlay: everything
// that can be unit-tested outside gnome-shell lives here (tests/drive_overlay.js
// runs this under plain gjs). extension.js imports it; keep shell imports OUT.

// Request files are create-once and immutable until they vanish from the
// spool. Parse each filename once, including a malformed file, and reuse one
// decoder across the lifetime of the extension. `loadBytes` is injected so
// the same cache can be driven without Gio under plain gjs.
export const CALLER_PROCESS_SCHEMA = 'human-copilot-caller-process';

// Request deadlines are Unix seconds, matching server.py and responder.py.
// Invalid or missing deadlines stay visible for diagnosis; only a
// conclusively elapsed deadline can retire an otherwise valid request.
export function requestExpired(request, now = Date.now() / 1000) {
    return !!request && Number.isFinite(request.deadline) &&
        Number.isFinite(now) && request.deadline <= now;
}

export function answeredRequestIds(names) {
    return new Set((names || [])
        .filter(name => typeof name === 'string' && name.endsWith('.json') &&
                        !name.includes('.tmp'))
        .map(name => name.slice(0, -5)));
}

// Parse the two /proc/PID/stat fields needed to defeat PID reuse. The process
// name is parenthesised and may contain spaces or ')', so split after the
// final close paren: remaining index 0 is state (kernel field 3), index 19 is
// starttime (field 22). Keep ticks as text so JavaScript never rounds them.
export function procStatIdentity(raw) {
    if (typeof raw !== 'string')
        return null;
    const end = raw.lastIndexOf(')');
    if (end < 0)
        return null;
    const fields = raw.slice(end + 1).trim().split(/\s+/);
    if (fields.length <= 19 || fields[0].length !== 1 ||
            !/^[0-9]+$/.test(fields[19]))
        return null;
    return {state: fields[0], start_ticks: fields[19]};
}

// `procStat === null` means the path conclusively does not exist; undefined
// means it could not be read and therefore stays unknown. All metadata fields,
// boot id and start ticks must agree before a caller is considered alive.
export function callerProcessState(record, currentBoot, procStat) {
    if (!record || record.schema !== CALLER_PROCESS_SCHEMA ||
            record.schema_version !== 1 || !Number.isInteger(record.pid) ||
            record.pid <= 0 || typeof record.boot_id !== 'string' ||
            !record.boot_id || record.boot_id.length > 128 ||
            typeof record.start_ticks !== 'string' ||
            !/^[0-9]+$/.test(record.start_ticks))
        return 'unknown';
    if (typeof currentBoot !== 'string' || !currentBoot.trim())
        return 'unknown';
    if (currentBoot.trim() !== record.boot_id)
        return 'dead';
    if (procStat === null)
        return 'dead';
    if (typeof procStat !== 'string')
        return 'unknown';
    const actual = procStatIdentity(procStat);
    if (!actual)
        return 'unknown';
    if (actual.start_ticks !== record.start_ticks ||
            ['Z', 'X', 'x'].includes(actual.state))
        return 'dead';
    return 'alive';
}

export class RequestFileCache {
    constructor() {
        this._items = new Map();
        this._decoder = new TextDecoder();
    }

    scan(names, loadBytes) {
        const live = new Set(names);
        for (const name of this._items.keys()) {
            if (!live.has(name))
                this._items.delete(name);
        }
        const requests = [];
        for (const name of names) {
            if (!this._items.has(name)) {
                let bytes;
                try {
                    bytes = loadBytes(name);
                } catch (e) {
                    // A file can vanish between enumeration and load. Unlike
                    // immutable content, an I/O failure is worth retrying.
                    continue;
                }
                let request = null;
                try {
                    const parsed = JSON.parse(this._decoder.decode(bytes));
                    if (parsed && parsed.id && typeof parsed.summary === 'string')
                        request = parsed;
                } catch (e) {
                    // Visible request files are complete by contract. Cache a
                    // malformed/non-request file too, instead of retrying it
                    // every two seconds forever.
                }
                this._items.set(name, request);
            }
            const request = this._items.get(name);
            if (request)
                requests.push(request);
        }
        return requests;
    }
}

// Inline markdown -> plain text, the overlay's half of the TUI's md_inline():
// emphasis/code/strike markers dropped, links flattened to "text (url)". Like
// the TUI it never touches `_`, so `__init__` and MALLOC_ARENA_MAX survive a
// checklist intact where CommonMark would eat them as emphasis.
export function plainInline(s) {
    // A backslash-escaped marker is parked in the private-use plane for the
    // length of the pass: leaving the backslash in place is not enough —
    // `\*literal\*` would still read as an emphasis pair and lose its stars.
    return (s || '')
        .replace(/\\([\\`*_{}[\]()#+.!|~<>-])/g,
                 (_m, c) => String.fromCharCode(0xE000 + c.charCodeAt(0)))
        .replace(/(!?)\[([^\]]*)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g,
                 (_m, bang, text, url) => bang
                     ? (text || url)
                     : (!text || text === url ? url : `${text} (${url})`))
        .replace(/<((?:https?|ftp|mailto):[^>\s]+)>/g, '$1')
        .replace(/`+([^`]+)`+/g, '$1')
        .replace(/~~([^~]+)~~/g, '$1')
        .replace(/\*\*([^*]+)\*\*/g, '$1')
        .replace(/(?<![\w*])\*([^*\s](?:[^*]*[^*\s])?)\*(?![\w*])/g, '$1')
        .replace(/[\uE000-\uE07F]/g,
                 c => String.fromCharCode(c.charCodeAt(0) - 0xE000));
}

// A single inline code span is usually the exact command an operator needs.
// Multiple spans are ambiguous, so their row keeps a whole-line copy action.
export function singleInlineCode(s) {
    const spans = [...(s || '').matchAll(/(?<![\\`])(`+)(?!`)([^\n]*?)(?<!`)\1(?!`)/g)];
    return spans.length === 1 ? spans[0][2] : null;
}

// A GFM pipe row -> its cells; outer pipes optional, \| is a literal pipe.
export function tableCells(row) {
    let s = (row || '').trim();
    if (s.startsWith('|'))
        s = s.slice(1);
    if (s.endsWith('|') && !s.endsWith('\\|'))
        s = s.slice(0, -1);
    const out = [];
    let cur = '';
    for (let i = 0; i < s.length; i++) {
        if (s[i] === '\\' && s[i + 1] === '|') {
            cur += '|';
            i++;
        } else if (s[i] === '|') {
            out.push(cur.trim());
            cur = '';
        } else {
            cur += s[i];
        }
    }
    out.push(cur.trim());
    return out;
}

// The ---|:--:|--- line under a table's header. Pipes are REQUIRED, so a lone
// '---' stays a horizontal rule and never opens a table.
export function isDelimRow(row) {
    if (!row || !row.includes('|'))
        return false;
    const cells = tableCells(row);
    return cells.length > 0 && cells.every(c => /^:?-+:?$/.test(c));
}

// ---- pointing the operator's eyes. Two step-level annotations, both
// written INLINE in the step so one `summary` string carries everything to
// both surfaces (no second argument to keep in sync with the step numbers):
//
//   "1. Click Save @{Firefox · top-right} → toast says Saved"
//
//   "→ what should change" (also ->, =>, ⇒): the OBSERVATION. Both surfaces
//   draw it highlighted under the step; it is the one thing the operator
//   looks for, separated from the action so a glance finds it.
//   "@{Window · region}": a SPOTLIGHT locator. `Window` is a title/class
//   substring (omit it for a region of the primary monitor); `region` is one
//   of the nine cells (top-left … center … bottom-right), `full`, or
//   "x,y,w,h" as fractions/percent of the window. The overlay draws a
//   pulsing frame there while the step is the ▶ one; the TUI prints it.
const LOOK_ARROW = /\s(?:→|->|=>|⇒)\s+(.+)$/;
const SPOT_TAG = /@\{([^}]*)\}/;
const SPOT_RECT = /(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?\s*,\s*(\d*\.?\d+)%?/;

// Region name -> [x, y, w, h] as fractions of the base rectangle. Edges are a
// quarter deep, corners and the centre a third square: a spotlight, not a
// measurement — it says WHERE to look, the "→" says what for.
export const REGIONS = {
    'full': [0, 0, 1, 1],
    'top-left': [0, 0, 1 / 3, 1 / 3], 'top': [0, 0, 1, 1 / 4],
    'top-right': [2 / 3, 0, 1 / 3, 1 / 3],
    'left': [0, 0, 1 / 4, 1], 'center': [1 / 3, 1 / 3, 1 / 3, 1 / 3],
    'right': [3 / 4, 0, 1 / 4, 1],
    'bottom-left': [0, 2 / 3, 1 / 3, 1 / 3], 'bottom': [0, 3 / 4, 1, 1 / 4],
    'bottom-right': [2 / 3, 2 / 3, 1 / 3, 1 / 3],
};

// "Top Right", "upper_left", "centre", "middle", "whole" -> a REGIONS key,
// or null. Lenient on purpose: this is typed by a model, not a parser.
export function regionOf(word) {
    const w = (word || '').toLowerCase().trim()
        .replace(/[\s_]+/g, '-')
        .replace(/^upper-/, 'top-').replace(/^lower-/, 'bottom-')
        .replace(/^(centre|middle)$/, 'center')
        .replace(/^(whole|all|window|everything|entire)$/, 'full');
    return Object.prototype.hasOwnProperty.call(REGIONS, w) ? w : null;
}

// The inside of an @{…} tag -> {window, region, rect} or null when empty.
// A rect wins over a region name; everything that is not a region is the
// window name, so "@{Firefox top-right}", "@{Firefox · top-right}" and
// "@{top-right, Firefox}" all mean the same thing.
export function parseSpot(inner) {
    let s = (inner || '').trim();
    if (!s)
        return null;
    let rect = null;
    s = s.replace(SPOT_RECT, (_m, a, b, c, d) => {
        rect = [a, b, c, d].map(Number);
        return ' ';
    });
    if (rect && rect.some(v => v > 1))
        rect = rect.map(v => v / 100);
    if (rect && (rect[2] <= 0 || rect[3] <= 0))
        rect = null;
    let region = null;
    const words = [];
    for (const part of s.split(/\s*[·|;,]\s*/)) {
        const toks = part.split(/\s+/).filter(Boolean);
        for (let i = 0; i < toks.length; i++) {
            const two = i + 1 < toks.length ? regionOf(`${toks[i]}-${toks[i + 1]}`) : null;
            const one = regionOf(toks[i]);
            if (!region && two) {
                region = two;
                i++;
            } else if (!region && one) {
                region = one;
            } else {
                words.push(toks[i]);
            }
        }
    }
    const win = words.join(' ').trim();
    if (!win && !region && !rect)
        return null;
    return {window: win, region: region || (rect ? null : 'full'), rect};
}

// Step text -> {text, see, spot}: the action with both annotations removed,
// the observation ("" when none) and the locator (null when none).
export function parseLook(text) {
    let s = text || '';
    let spot = null;
    s = s.replace(SPOT_TAG, (_m, inner) => {
        spot = parseSpot(inner);
        return ' ';
    });
    let see = '';
    const m = LOOK_ARROW.exec(s);
    if (m) {
        see = m[1].trim();
        s = s.slice(0, m.index);
    }
    return {text: s.replace(/\s{2,}/g, ' ').trim(), see, spot};
}

// Locator + base rectangle {x, y, width, height} -> the rectangle to frame.
// Never smaller than a few px per side, so a "0.99,0.99,0.01,0.01" tag still
// produces something the eye can find.
export function spotRect(base, spot) {
    const f = (spot && spot.rect) || REGIONS[(spot && spot.region) || 'full'];
    const [fx, fy, fw, fh] = f;
    const x = base.x + fx * base.width;
    const y = base.y + fy * base.height;
    return {
        x: Math.round(x), y: Math.round(y),
        width: Math.max(24, Math.round(Math.min(fw, 1 - fx) * base.width)),
        height: Math.max(24, Math.round(Math.min(fh, 1 - fy) * base.height)),
    };
}

// "Firefox · top-right" — how a locator is named to the operator.
export function spotLabel(spot) {
    if (!spot)
        return '';
    const parts = [];
    if (spot.window)
        parts.push(spot.window);
    if (spot.rect)
        parts.push(spot.rect.map(v => Math.round(v * 100) + '%').join(','));
    else if (spot.region && (spot.region !== 'full' || !spot.window))
        parts.push(spot.region === 'full' ? 'whole screen' : spot.region);
    return parts.join(' · ');
}

// A cell holding nothing but a step number: "1", "2.", "3)".
const STEP_CELL = /^\d{1,3}[.)]?$/;
const joinCells = cells => cells.filter(c => c).join(' · ');

// Sub-line grammar (the body of a step: nested lists and continuation prose).
const SUB_BULLET = /^([-*+])\s+(.*)$/;
const SUB_ORDERED = /^(\d{1,3}[.)])\s+(.*)$/;
const SUB_TASK = /^\[([ xX])\]\s+(.*)$/;

// Leading whitespace as a COLUMN count (a tab is four columns). Indent is the
// only thing that says "this line belongs to the step above", so it has to be
// measured the same way for every line.
function indentOf(raw) {
    return ((raw || '').match(/^[ \t]*/)[0]).replace(/\t/g, '    ').length;
}

// Agent summary -> {intro: [lines], steps: [{text, sub, done:false, note:''}]}.
// A step is a numbered markdown list item ("1. do the thing" / "2) other") OR
// a pipe-table row whose FIRST CELL is the step number — a checklist written
// as a "| # | do | observe | report |" table is the shape the doctrine asks
// for, and before this it produced zero steps and a dead mark-done hotkey.
// A table with no numbered rows is reference material: its rows compact to
// "a · b · c" intro lines (the delimiter row dropped — column rules mean
// nothing in the overlay's proportional font). Everything else is intro text.
// Fenced code is passed through verbatim, so a "1." inside a snippet is not
// mistaken for a step.
//
// Lines INDENTED PAST the step's own marker are that step's body, collected in
// `step.sub` as {text, level} and drawn under it — a nested bullet list, and
// the continuation paragraph that may follow it. Before this they were hoisted
// into `intro`, where they rendered above the checklist, detached from the step
// they explained, and were the first thing eaten by the 8-line intro cap: a
// sublist naming two nameservers simply vanished. A nested item is body, never
// a step of its own, so the mark-done hotkey still walks exactly the top-level
// numbered lines and notes/snips keep their step numbers.
export function parseSteps(summary) {
    const intro = [];
    const introCode = [];
    const steps = [];
    const src = (summary || '').split('\n');
    const addIntro = (s, raw = s) => {
        // keep interior blanks, but not leading ones and never two in a row —
        // a consumed table block leaves the blanks that framed it behind
        if (!s && (!intro.length || !intro[intro.length - 1]))
            return;
        intro.push(s);
        introCode.push(singleInlineCode(raw));
    };
    const addStep = (text, raw = text) => {
        const look = parseLook(text.replace(/^\[([ xX])\]\s+/, ''));
        steps.push({text: look.text, see: look.see, spot: look.spot, sub: [],
                    copyCode: singleInlineCode(raw),
                    done: false, failed: false, note: ''});
    };

    // Body-of-a-step state. `cur` is the step being filled (-1 = we are in
    // intro), `base` its marker's indent column, `nest` the stack of deeper
    // indent columns seen inside it (its length is the nesting level), and
    // `blanks` the blank lines held back — a blank is emitted only when the
    // body actually continues, so a step never ends in dangling whitespace.
    let cur = -1, base = 0, nest = [], blanks = 0, fenceSub = false;
    const closeStep = () => {
        cur = -1;
        nest = [];
        blanks = 0;
    };
    const subLevel = ind => {
        while (nest.length && ind < nest[nest.length - 1])
            nest.pop();
        if (!nest.length || ind > nest[nest.length - 1])
            nest.push(ind);
        return nest.length;
    };
    const pushSub = (text, level, raw = text) => {
        if (cur < 0)
            return;
        const body = steps[cur].sub;
        for (let k = 0; k < blanks; k++) {
            if (body.length && body[body.length - 1].text)
                body.push({text: '', level: 1, copyCode: null});
        }
        blanks = 0;
        body.push({text, level, copyCode: singleInlineCode(raw)});
    };
    // A nested list item keeps a marker; anything else is continuation prose.
    // Bullets normalise to "•" and "- [x]" to a box, exactly as the TUI draws
    // them — the overlay has no list layout, so the glyph IS the nesting cue.
    const addSub = raw => {
        const level = subLevel(indentOf(raw));
        let s = raw.trim();
        let marker = '';
        const m = SUB_BULLET.exec(s) || SUB_ORDERED.exec(s);
        if (m) {
            marker = /^[-*+]$/.test(m[1]) ? '•' : m[1];
            s = m[2];
            const t = SUB_TASK.exec(s);
            if (t) {
                marker = /[xX]/.test(t[1]) ? '☑' : '☐';
                s = t[2];
            }
        }
        pushSub((marker ? marker + ' ' : '') + plainInline(s), level, s);
    };

    let fence = false;
    for (let i = 0; i < src.length; i++) {
        const raw = src[i];
        const t = raw.trim();
        const ind = indentOf(raw);
        if (t.startsWith('```') || t.startsWith('~~~')) {
            if (!fence)
                fenceSub = cur >= 0 && ind > base;
            fence = !fence;
            continue;
        }
        if (fence) {
            if (fenceSub)
                pushSub(raw.trim(), nest.length || 1);
            else
                addIntro(raw);
            continue;
        }
        if (raw.includes('|') && isDelimRow(src[i + 1])) {
            closeStep();
            const head = tableCells(raw).map(plainInline);
            const rows = [];
            const rawRows = [];
            let j = i + 2;
            for (; j < src.length; j++) {
                if (!src[j].includes('|') || !src[j].trim() || isDelimRow(src[j]))
                    break;
                rows.push(tableCells(src[j]).map(plainInline));
                rawRows.push(src[j]);
            }
            i = j - 1;
            const checklist = rows.some(r => STEP_CELL.test(r[0] || ''));
            if (!checklist)
                addIntro(joinCells(head), raw);
            for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
                const r = rows[rowIndex];
                if (checklist && STEP_CELL.test(r[0] || '')) {
                    // annotations may sit in ANY cell; lift them out per cell
                    // so the joined text is the action alone
                    let see = '', spot = null;
                    const cells = r.slice(1).map(c => {
                        const l = parseLook(c);
                        see = see || l.see;
                        spot = spot || l.spot;
                        return l.text;
                    });
                    const tag = spot ? ` @{${spotLabel(spot).replace(/ · /g, ' · ')}}` : '';
                    addStep((joinCells(cells) || r[0]) + tag + (see ? ` → ${see}` : ''),
                            rawRows[rowIndex]);
                }
                else
                    addIntro(joinCells(r), rawRows[rowIndex]);
            }
            continue;
        }
        if (!t) {
            if (cur >= 0)
                blanks++;
            else
                addIntro('');
            continue;
        }
        const line = plainInline(raw);
        const m = line.match(/^\s*(\d+)[.)]\s+(.*\S)/);
        if (m && !(cur >= 0 && ind > base)) {
            addStep(m[2], raw);
            cur = steps.length - 1;
            base = ind;
            nest = [];
            blanks = 0;
            continue;
        }
        if (cur >= 0 && ind > base) {
            addSub(raw);
            continue;
        }
        closeStep();
        addIntro(line.replace(/^#+\s*/, '').trim(), raw);
    }
    while (intro.length && !intro[intro.length - 1]) {
        intro.pop();
        introCode.pop();
    }
    return {intro, introCode, steps};
}

// A step is RESOLVED once the operator has said done OR failed; the ▶ step
// and the hotkeys walk the unresolved ones in order.
const resolved = s => s.done || s.failed;

// Mark the next unresolved step done; returns its index or -1 when none left.
export function markNext(steps) {
    const i = steps.findIndex(s => !resolved(s));
    if (i >= 0)
        steps[i].done = true;
    return i;
}

// Mark the next unresolved step FAILED — the one-key "it did not do that".
// Same walk as markNext, so the two chords can be pressed in any mix.
export function markFail(steps) {
    const i = steps.findIndex(s => !resolved(s));
    if (i >= 0)
        steps[i].failed = true;
    return i;
}

// Undo the LAST resolved step (done or failed); returns its index or -1.
export function undoLast(steps) {
    for (let i = steps.length - 1; i >= 0; i--) {
        if (resolved(steps[i])) {
            steps[i].done = false;
            steps[i].failed = false;
            return i;
        }
    }
    return -1;
}

// True once every step has an answer — the moment Space (mark-done) turns
// into "send", so one chord walks the whole checklist and submits it.
export function allResolved(steps) {
    return steps.length > 0 && steps.every(resolved);
}

// The step the operator is ON — where a typed reply and an attached snip land.
// It is the first unresolved step (the one being performed), or the LAST step
// once everything is answered (post-pass evidence belongs to the end, not to
// a step that doesn't exist). -1 only when the summary had no numbered steps.
// Undo moves it back, which is also how you return to note an earlier step.
export function currentStep(steps) {
    if (!steps.length)
        return -1;
    const i = steps.findIndex(s => !resolved(s));
    return i >= 0 ? i : steps.length - 1;
}

// The contextual guide: exactly what to press NOW, and the three keys that
// are useful alongside it. `accels` maps settings keys to their pretty
// chords (rendered LIVE from settings, so a rebind shows as itself). Returns
// {main, more}: the main line is the one action that moves the pass forward.
export function nowHint(steps, accels) {
    const a = k => accels[k] || '?';
    const more = `${a('edit-note')} note · ${a('attach-snip')} snip · ` +
                 `${a('undo-done')} undo · ${a('dismiss-request')} decline`;
    if (!steps.length) {
        return {main: `${a('edit-note')}  type your reply`,
                more: `Enter saves your reply · ${a('submit-answer')} sends · ` +
                      `${a('dismiss-request')} declines`};
    }
    if (allResolved(steps)) {
        const done = steps.filter(s => s.done).length;
        const failed = steps.filter(s => s.failed).length;
        return {main: `${a('submit-answer')}  send ${done}/${steps.length}` +
                      (failed ? ` (${failed} failed)` : '') +
                      `  ·  ${a('mark-done')} again also sends`,
                more};
    }
    const cur = currentStep(steps);
    return {main: `${a('mark-done')}  step ${cur + 1} done   ` +
                  `${a('mark-fail')}  step ${cur + 1} failed`,
            more};
}

// ---- multi-instance routing (2026-09-03, hcop-0p7.4). Every request carries
// instance = {session, pane, server_uuid, attached} (server.py). The overlay
// renders ONE checklist — the ACTIVE instance's (design decision hcop-0p7.8):
// the instance whose copilot pane was last focused, else the instance of the
// newest request. The focus signal is the focused window's TITLE: zellij owns
// the title of an attached session as "<session> | <pane title>" (verified in
// zellij-hub 2026-08-29), and the sidepanel pane is named "copilot · <id>" by
// server.py, so a title whose pane half names copilot means the operator is
// on that session's copilot pane. Title-only, by design: a list-clients
// subprocess from GJS would block the shell. Limitation: a window that does
// not carry zellij's title (zellij-hub's WEB attach is a browser tab) routes
// nothing and the newest request stays active.

// Focused window title -> {session, pane, copilot} or null when it is not a
// zellij-owned title. Split on the FIRST " | ": pane titles may hold pipes.
export function focusTarget(title) {
    const t = title || '';
    const i = t.indexOf(' | ');
    if (i <= 0)
        return null;
    const session = t.slice(0, i).trim();
    const pane = t.slice(i + 3).trim();
    if (!session)
        return null;
    return {session, pane, copilot: /copilot/i.test(pane)};
}

// The grouping key of a request's stamp — server.py::instance_id's twin: the
// zellij session when set, else the server uuid, else null (an unstamped
// request belongs to every surface).
export function instanceKey(inst) {
    if (!inst)
        return null;
    return inst.session || inst.server_uuid || null;
}

// How an instance is NAMED to the operator: the session, or the first 8 hex
// digits of a non-zellij server's uuid. The NUMBER and colour go in front of
// it (instanceName) wherever the strip number is known — hcop-0p7.5.
export function instanceLabel(inst) {
    if (!inst)
        return '';
    if (inst.session)
        return inst.session;
    return inst.server_uuid ? String(inst.server_uuid).slice(0, 8) : '';
}

// "3 hc-A (coral)": the instance as the operator reads it once its strip
// number is known — number first (load-bearing: nine hues are not
// distinguishable for every operator), then the session, then the palette
// entry's colour NAME in words, because an OSD cannot be tinted and the colour
// has to survive as text. With no number (0) it is the bare label.
export function instanceName(inst, number = 0, entry = null) {
    const l = instanceLabel(inst);
    if (!l)
        return '';
    if (!(number > 0))
        return l;
    return `${number} ${l}` + (entry && entry.name ? ` (${entry.name})` : '');
}

// " · instance 3 hc-A (coral)" for an OSD, '' for an unstamped request —
// every OSD names the instance it acted on, number and colour included, so a
// chord landing on the wrong copilot is visible the moment it happens.
export function instanceSuffix(inst, number = 0, entry = null) {
    const l = instanceName(inst, number, entry);
    return l ? ` · instance ${l}` : '';
}

// The request the overlay shows. `reqs` are the open requests (any order);
// `focus` is {session} of the last-focused copilot pane, or null. Returns
// {req, instance, reason} — reason 'focused' when that session has an open
// request (its OLDEST one: the queue order both surfaces always used),
// 'newest' when the fallback picked the newest request's instance (again its
// oldest request), or null when nothing is open. A focused pane whose
// session has no request falls through to the fallback.
export function pickActive(reqs, focus) {
    const sorted = [...(reqs || [])].sort((a, b) => (a.created || 0) - (b.created || 0));
    if (!sorted.length)
        return null;
    if (focus && focus.session) {
        const mine = sorted.filter(r => r.instance && r.instance.session === focus.session);
        if (mine.length)
            return {req: mine[0], instance: focus.session, reason: 'focused'};
    }
    const key = instanceKey(sorted[sorted.length - 1].instance);
    const same = sorted.filter(r => instanceKey(r.instance) === key);
    return {req: same[0], instance: key, reason: 'newest'};
}

// The only text that changes on the overlay's periodic scan. Keeping this in
// the shell-free module makes the countdown/queue formatting testable without
// constructing (or even importing) an St widget. A stamped request's header
// also names its instance — with its strip number in front when known.
export function overlayHeader(req, nowSeconds = Date.now() / 1000, number = 0) {
    const left = Math.max(0, Math.floor((req.deadline || 0) - nowSeconds));
    const mins = `${Math.floor(left / 60)}:${String(left % 60).padStart(2, '0')}`;
    const inst = instanceName(req.instance, number);
    return `🧿 ${req.client} needs your eyes · ⏳ ${mins}` +
           (inst ? ` · ${inst}` : '') +
           (req.queued ? ` · +${req.queued} queued` : '');
}

// Snips are tagged with the step that was current when they were attached;
// the answer sends them ORDERED BY STEP so "snips: #k" under a step names the
// k-th image the agent receives. Stable within a step (attach order), and a snip
// with no step (a summary with no numbered lines) sorts last, not first —
// unassigned must never displace the numbering of assigned ones.
export function orderImages(images) {
    return (images || [])
        .map((img, i) => ({img, i}))
        .sort((a, b) =>
            ((a.img.step >= 0 ? a.img.step : Infinity) -
             (b.img.step >= 0 ? b.img.step : Infinity)) || (a.i - b.i))
        .map(x => x.img);
}

// GTK accelerator string -> what a human calls the chord: '<Super><Alt>slash'
// -> 'Super+Alt+/'. Used by the glossary, which renders the LIVE bindings
// from settings rather than a hardcoded legend — a rebound key must show as
// what it is, or the glossary teaches the wrong chord.
const KEY_NAMES = {
    space: 'Space', slash: '/', backslash: '\\', Return: 'Enter',
    BackSpace: 'Backspace', Escape: 'Esc', period: '.', comma: ',',
    minus: '-', equal: '=', grave: '`',
};
export function prettyAccel(accel) {
    if (!accel)
        return '(unbound)';
    const parts = [];
    let rest = accel;
    for (const m of accel.matchAll(/<([^>]+)>/g))
        parts.push(m[1]);
    rest = rest.replace(/<[^>]+>/g, '');
    parts.push(KEY_NAMES[rest] ??
        (rest.length === 1 ? rest.toUpperCase() : rest));
    return parts.join('+');
}

// The submitted answer text. The agent reads this as the report, so it has to
// say four things unambiguously: it came from the overlay, exactly which
// steps passed, FAILED, or were NOT DONE (an omitted step must never read as
// a passed one, and "failed" must never be inferred from "not done"), what
// the operator typed under each step, and which of the attached images
// belongs to which step. `images` is the RAW tagged list; the numbering here
// is computed over orderImages() of it, i.e. over the order the images
// actually go out — the caller must send that order.
export function composeAnswer(steps, images) {
    const ordered = orderImages(images);
    const anyNote = steps.some(s => (s.note || '').trim());
    const done = steps.filter(s => s.done).length;
    const failed = steps.filter(s => s.failed).length;
    const notDone = steps.length - done - failed;
    const tally = [`${done}/${steps.length} done`];
    if (failed)
        tally.push(`${failed} FAILED`);
    if (notDone)
        tally.push(`${notDone} not done`);
    const lines = [`[answered via the overlay — ${tally.join(', ')}` +
                   `${anyNote ? ', with per-step notes' : ', no typed notes'}]`];
    steps.forEach((s, i) => {
        const mark = s.done ? '✔' : s.failed ? '✘ FAILED' : '○ NOT DONE';
        lines.push(`${mark} ${i + 1}. ${s.text}`);
        const note = (s.note || '').trim();
        if (note)
            lines.push(`   note: ${note}`);
        const nums = [];
        ordered.forEach((img, k) => {
            if (img.step === i)
                nums.push(`#${k + 1}`);
        });
        if (nums.length)
            lines.push(`   snips: ${nums.join(', ')}`);
    });
    const loose = ordered.filter(
        img => !(img.step >= 0 && img.step < steps.length)).length;
    if (loose)
        lines.push(`${loose} snip(s) attached to no particular step.`);
    if (ordered.length)
        lines.push(`${ordered.length} screenshot(s) attached, numbered in ` +
                   `the order they follow this text.`);
    return lines.join('\n');
}

// The additive machine answer paired with composeAnswer's human-facing text
// (hcop-d1f). Step ids and the request digest come only from server.py's
// immutable request contract; the surface supplies verdicts and notes. Old
// requests without that contract keep their exact answer shape.
export function checklistAnswer(req, steps) {
    const contract = req && req.checklist;
    const specs = contract && contract.steps;
    if (!contract || contract.schema !== 'human-copilot.checklist-request/v1' ||
        contract.schema_version !== 1 || !Array.isArray(specs) || !specs.length ||
        !Array.isArray(steps) || specs.length !== steps.length ||
        typeof req.id !== 'string' || !req.id ||
        typeof req.request_digest !== 'string' || !req.request_digest)
        return null;
    const rows = [];
    for (let i = 0; i < specs.length; i++) {
        const id = specs[i] && specs[i].id;
        const step = steps[i];
        if (typeof id !== 'string' || !id || !step ||
            (step.done && step.failed) || typeof (step.note ?? '') !== 'string')
            return null;
        rows.push({
            id,
            verdict: step.done ? 'done' : step.failed ? 'failed' : 'not-done',
            note: (step.note || '').trim(),
        });
    }
    return {
        schema: 'human-copilot.checklist-answer/v1',
        schema_version: 1,
        request_id: req.id,
        request_digest: req.request_digest,
        steps: rows,
    };
}

// ---- concurrent requests (2026-09-03, hcop-0p7.3). N requests from N
// instances may be open at once. The overlay still renders ONE checklist (the
// active instance's, pickActive) and adds an INSTANCE STRIP: one chip per
// instance with an open request — its number, its label and its pending
// count — the active chip marked. Everything here is bookkeeping the shell
// does not need to be involved in, so it is testable under plain gjs.

// The open-request ledger: which request ids were open at the last scan and
// which instance each belonged to, so a scan can say exactly which requests
// VANISHED — answered in a sidepanel, timed out, cancelled — ONCE each,
// whatever the overlay happened to be showing, and which arrived. `answered`
// is the set of ids this surface wrote answers for itself: their vanishing
// is not news, and they are forgotten here once gone.
export class OpenLedger {
    constructor() {
        this._open = new Map();        // id -> instance stamp (or null)
    }

    update(reqs, answered = new Set()) {
        const live = new Map((reqs || []).map(r => [r.id, r]));
        const withdrawn = [], arrived = [];
        for (const [id, instance] of this._open) {
            if (live.has(id))
                continue;
            this._open.delete(id);     // reported exactly once: it is gone now
            if (!answered.has(id))
                withdrawn.push({id, instance});
        }
        for (const [id, r] of live) {
            if (!this._open.has(id))
                arrived.push(r);
            this._open.set(id, r.instance && typeof r.instance === 'object' ? r.instance : null);
        }
        for (const id of [...answered]) {
            if (!live.has(id))
                answered.delete(id);
        }
        return {withdrawn, arrived};
    }

    get size() {
        return this._open.size;
    }
}

// One OSD for everything withdrawn in a scan — a single withdrawal names its
// instance, several are counted and name every instance touched. Never one
// OSD per instance still open, and never N for one request.
export function withdrawnText(withdrawn, numbers = null) {
    const w = withdrawn || [];
    if (!w.length)
        return '';
    const num = inst => (numbers && numbers.get(instanceKey(inst))) || 0;
    if (w.length === 1)
        return 'Copilot request withdrawn' + instanceSuffix(w[0].instance, num(w[0].instance));
    const names = [...new Set(w.map(x => instanceName(x.instance, num(x.instance))).filter(Boolean))];
    return `${w.length} copilot requests withdrawn` +
           (names.length ? ` · instances ${names.join(', ')}` : '');
}

// One OSD for the arrivals a scan did NOT put where the operator is looking —
// a targeted card opens beside its own window, possibly on another monitor —
// so a second session's call is not silent.
export function arrivedText(arrived, shownId, numbers = null) {
    const a = (arrived || []).filter(r => r.id !== shownId);
    if (!a.length)
        return '';
    const num = inst => (numbers && numbers.get(instanceKey(inst))) || 0;
    const names = [...new Set(a.map(r => instanceName(r.instance, num(r.instance))).filter(Boolean))];
    return (a.length === 1 ? 'Copilot: a request waits' :
            `Copilot: ${a.length} requests wait`) +
           (names.length ? ` · instance${names.length > 1 ? 's' : ''} ${names.join(', ')}` : '') +
           ' — a card opened for it';
}

// Stash entries (request id -> saved ticks/notes/snips) of requests that have
// vanished are garbage; only THOSE are dropped — a withdrawal touches nothing
// another instance's request had saved.
export function pruneStash(states, reqs) {
    const live = new Set((reqs || []).map(r => r.id));
    for (const id of [...states.keys()]) {
        if (!live.has(id))
            states.delete(id);
    }
    return states;
}

// spool/palette.json (palette.py's registry) -> {sessionKey: slot}. The slot
// palette.py assigned a session is the number the TUI draws next to it; the
// strip uses the same number when the registry has one, so both surfaces
// agree. Anything unreadable is an empty map, never a throw.
export function registrySlots(text) {
    const out = {};
    let reg;
    try {
        reg = JSON.parse(text || '');
    } catch (e) {
        return out;
    }
    const sessions = reg && typeof reg === 'object' ? reg.sessions : null;
    if (!sessions || typeof sessions !== 'object')
        return out;
    for (const [key, rec] of Object.entries(sessions)) {
        const slot = rec && rec.slot;
        if (Number.isInteger(slot) && slot > 0)
            out[key] = slot;
    }
    return out;
}

// Instance numbers for the strip. `keys` are the instance keys of the open
// requests in CREATED order (duplicates and null — an unstamped request —
// are fine). A number is stable for as long as the instance has an open
// request: the registry's slot when it has one (authoritative — it is what
// the TUI shows), else the number it had last scan, else the lowest number
// nobody on the strip holds. An instance with no open request left frees its
// number; the next newcomer may take it, exactly like palette.py's slots.
export class InstanceRoster {
    constructor() {
        this._numbers = new Map();
    }

    update(keys, registry = {}) {
        const live = [];
        for (const k of keys || []) {
            if (k !== null && k !== undefined && !live.includes(k))
                live.push(k);
        }
        const next = new Map();
        const used = new Set();
        for (const k of live) {
            const slot = registry[k];
            if (Number.isInteger(slot) && slot > 0) {
                next.set(k, slot);
                used.add(slot);
            }
        }
        for (const k of live) {
            const old = this._numbers.get(k);
            if (!next.has(k) && old && !used.has(old)) {
                next.set(k, old);
                used.add(old);
            }
        }
        for (const k of live) {
            if (next.has(k))
                continue;
            let n = 1;
            while (used.has(n))
                n++;
            next.set(k, n);
            used.add(n);
        }
        this._numbers = next;
        return new Map(next);
    }

    numberOf(key) {
        return this._numbers.get(key) || 0;
    }
}

// The NOW line for a non-checklist request (hcop-0gj): a choice names the
// selection chords, an ack names the confirm; checklists fall through to
// nowHint. `sel` is the highlighted option (-1 = none yet).
export function modeHint(mode, steps, accels, sel = -1, optCount = 0) {
    const a = k => accels[k] || '?';
    const more = `${a('edit-note')} note · ${a('undo-done')} undo · ` +
                 `${a('dismiss-request')} decline`;
    if (mode === 'choice') {
        const picked = sel >= 0 && sel < optCount
            ? `option ${sel + 1}/${optCount} selected` : 'no option selected yet';
        return {main: optCount
                    ? `${a('mark-done')}  next option   ${a('submit-answer')}  send the selected`
                    : 'no options arrived — answer in copilot-tui',
                more: `${picked} · ${more}`};
    }
    if (mode === 'ack')
        return {main: `${a('submit-answer')}  confirm`, more};
    return nowHint(steps, accels);
}

// ---- every request is a first-class surface (2026-09-04, hcop-5sy). The
// one-active-checklist-plus-strip design (hcop-0p7.8) collapsed concurrent
// copilots into a queue the operator had to switch; the operator asked for
// the opposite: "a green red and yellow border should be able to be on
// screen simultaneously". Now EVERY open request gets its own visible card
// (and, when its ▶ step carries a window locator, its own frame). What
// remains of "active" is ROUTING ONLY: which request the global chords act
// on. Identity is never colour alone — every card carries its instance
// NUMBER and its session NAME as text next to the palette colour.

// The instance badge on a card: "▶ 2 hc-A" — the ▶ marks the request the
// chords act on, the NUMBER is load-bearing (nine hues are not
// distinguishable for every operator) and the session name is cut to keep a
// 460 px card readable.
export function cardBadge(instance, number, active) {
    const label = instanceLabel(instance);
    const cut = label.length > 14 ? label.slice(0, 13) + '…' : label;
    return `${active ? '▶ ' : ''}${number > 0 ? number + ' ' : ''}${cut}`;
}

// How many requests of the same instance wait behind this one — shown on the
// card so a session with several open asks is not silently collapsed.
export function queuedBehind(reqs, activeKey, shownId) {
    return (reqs || []).filter(r => r.id !== shownId && instanceKey(r.instance) === activeKey).length;
}

// ---- colour follows the instance (2026-09-03, hcop-0p7.5). The spotlight
// frame, its label, the overlay header and every OSD wear the ACTIVE
// instance's palette entry (palette.js::entryFor of its strip number), so a
// frame on the screen says which copilot asked for the look. Only the active
// instance draws frames (design decision hcop-0p7.8); a non-active instance
// whose ▶ step carries a locator pulses its chip on the strip instead.
// Everything here is geometry and bookkeeping the shell need not be involved
// in, so tests/drive_overlay.js can pin it under plain gjs.

// The frame colour when the request carries no instance (a pre-stamp request,
// or one whose strip number is not known yet): the lane's original spotlight
// yellow. It is also the colour the pre-fix leak carried, which is why the
// orphan sweep still matches it (isSpotActor).
export const SPOT_FALLBACK = '#f5c211';
export const SPOT_NAME = 'copilot-spot';   // actor name: enable() finds and sweeps ours by it
export const SPOT_EDGE = 4;                // px, the frame's bar thickness
export const SPOT_INK = '#1a1a1a';         // the label's ink when no entry says otherwise

// The actors a spotlight is made of: four solid edge bars (never one widget
// with a border — a filled box is impossible by construction) and a label
// naming the ▶ step and the instance number, all in the instance's colour and
// all named SPOT_NAME. `entry` is the palette entry (or null → SPOT_FALLBACK).
// Returns {name, color, bars: [{x, y, width, height, color}], label: {text,
// bg, fg, x, y}} — extension.js turns it into St actors 1:1.
export function spotFrameSpec(rect, step, entry = null, number = 0, edge = SPOT_EDGE) {
    const color = entry && entry.hex ? entry.hex : SPOT_FALLBACK;
    const fg = entry && entry.fg ? entry.fg : SPOT_INK;
    const e = edge;
    const bars = [
        [rect.x, rect.y, rect.width, e],                      // top
        [rect.x, rect.y + rect.height - e, rect.width, e],    // bottom
        [rect.x, rect.y, e, rect.height],                     // left
        [rect.x + rect.width - e, rect.y, e, rect.height],    // right
    ].map(([x, y, width, height]) => ({x, y, width, height, color}));
    const text = `👁 ${step + 1}` + (number > 0 ? ` · instance ${number}` : '');
    return {name: SPOT_NAME, color, bars,
            label: {text, bg: color, fg, x: rect.x + 4, y: Math.max(0, rect.y - 24)}};
}

// The orphan sweep's predicate (extension.js::_sweepOrphanSpots): ours by
// NAME, whatever colour they wear — a frame of every palette entry is named
// SPOT_NAME, so the sweep never depends on the colour table. The one colour
// match left is the documented pre-fix leak (hcop-o9m): those actors had no
// name and only the old yellow in an inline style.
export function isSpotActor(name, style) {
    if (name === SPOT_NAME)
        return true;
    return typeof style === 'string' && style.includes(SPOT_FALLBACK.slice(1));
}

// The frames on screen, by the REQUEST that owns them. A decline, an answer
// or a withdrawal takes exactly its own frame back; nothing else's. (Only the
// active request ever has one — this is the bookkeeping that keeps it so, and
// what disable() empties.)
export class SpotLedger {
    constructor() {
        this._frames = new Map();     // request id -> {actors, rect}
    }

    set(id, frame) {
        this._frames.set(id, frame);
    }

    get(id) {
        return this._frames.get(id) || null;
    }

    // Remove and return the frame owned by `id` (null when it has none).
    take(id) {
        const f = this._frames.get(id) || null;
        this._frames.delete(id);
        return f;
    }

    takeAll() {
        const all = [...this._frames.values()];
        this._frames.clear();
        return all;
    }

    get size() {
        return this._frames.size;
    }
}

// ---- the response contract, surface-side (2026-09-04, hcop-0gj). server.py
// validates mode/options at the boundary; the surfaces render what survived.
// A legacy request (no `response` key, or mode null) keeps today's behaviour
// exactly: numbered steps -> checklist, anything else -> prose. "prose" is
// NOT a mode an engine can send — it is the derived shape of a legacy
// summary with no steps, and the only place the choice-like guard fires.

export const RESPONSE_MODES = ['checklist', 'text', 'choice', 'ack'];

export function parseResponse(req) {
    const stamped = req && typeof req.responder === 'object' && req.responder &&
        req.responder.schema === 'human-copilot.responder-contract/v1' &&
        req.responder.schema_version === 1 ? req.responder : null;
    const resp = stamped ||
        (req && typeof req.response === 'object' && req.response ? req.response : {});
    const mode = RESPONSE_MODES.includes(resp.mode) ? resp.mode : null;
    const options = Array.isArray(resp.options)
        ? resp.options.filter(o => o && typeof o === 'object' &&
                                   typeof o.value === 'string' && o.value &&
                                   typeof o.label === 'string' && o.label)
        : [];
    if (!mode)
        return {mode: parseSteps(req ? req.summary || '' : '').steps.length
                    ? 'checklist' : 'prose', options: []};
    return {mode, options};
}

// Structural decision-syntax detection — the JS twin of server.py's
// looks_like_choices (keep them identical). Conservative on purpose: only
// obvious option syntax counts, never an NLP guess.
const CHOICE_DINGBAT = /[✔✘][^✔✘]*[✔✘]/;
const CHOICE_LETTERED = /^\(?[A-Za-z][).]\s+\S/;
const CHOICE_YESNO = /\byes\s*(?:\/|or)\s*no\b/i;

export function looksLikeChoices(summary) {
    const lines = (summary || '').split('\n').map(l => l.trim()).filter(Boolean);
    if (lines.some(l => CHOICE_DINGBAT.test(l)))
        return true;
    if (lines.filter(l => CHOICE_LETTERED.test(l)).length >= 2)
        return true;
    return lines.some(l => CHOICE_YESNO.test(l));
}

// What submit does with nothing to say. An EMPTY acknowledgement is an
// answer ONLY when the request explicitly declared itself confirmation-only
// (hcop-0gj) — null means "may send", anything else is the note shown
// instead. (A checklist never hits this: its report text is always composed.)
export function emptySubmitNote(mode, choiceLike) {
    if (mode === 'ack')
        return null;
    if (mode === 'choice')
        return 'choose an option first — click one, or take the keyboard';
    if (mode === 'prose' && choiceLike)
        return 'this reads as a decision — answer in your own words; the ' +
               'agent will be asked to resend real options';
    return 'an empty send is not an answer — add a note or decline';
}

// Choice rows must carry the whole decision, not a shortened button label.
// The shell renderer uses the same pure text here for both wrapping and its
// bounded-height scroll hint, so a long consequence can never disappear from
// one path while remaining in the other.
export function choiceAnswer(options, selected, note = '', images = []) {
    const option = selected >= 0 ? options[selected] : null;
    const text = (note || '').trim();
    if (!option && !text)
        return null;
    const answer = {text, cancelled: false, images};
    if (option)
        answer.choice = {value: option.value, label: option.label};
    else
        answer.response_completion = {
            schema: 'human-copilot.response-completion/v1', schema_version: 1,
            mode: 'choice', status: 'not_done'};
    return answer;
}

export function choiceDisplayText(option, index, selected = false) {
    const label = option && typeof option.label === 'string' ? option.label : '';
    const hint = option && typeof option.hint === 'string' ? option.hint : '';
    return `${selected ? '▶' : ' '} ${index + 1}. ${label}` +
           (hint ? ` — ${hint}` : '');
}

export function choiceNeedsScroll(options, charsPerLine = 48, visibleLines = 8) {
    const width = Math.max(8, Math.floor(charsPerLine));
    const budget = Math.max(1, Math.floor(visibleLines));
    let lines = 0;
    for (let i = 0; i < (options || []).length; i++)
        lines += Math.max(1, Math.ceil(choiceDisplayText(options[i], i).length / width));
    return lines > budget;
}

// ---- targeting (2026-09-04, hcop-453). A request whose ▶ step carries
// "@{Window · region}" is anchored to THAT window: its card is placed
// beside it and its frame follows it. Resolution distinguishes exactly one
// match, none, and several — it never silently picks the most recent one,
// because a card that attaches to the wrong window is worse than no card.
// `windows` are plain records (extension.js maps Meta.Windows into them):
// {title, wmClass, wmInstance, userTime, minimized, monitor, frame}.

export function matchWindows(query, windows) {
    const q = (query || '').toLowerCase();
    return (windows || []).filter(w => !w.minimized &&
        [w.title, w.wmClass, w.wmInstance]
            .some(h => (h || '').toLowerCase().includes(q)));
}

export function resolveTarget(query, windows) {
    if (!query)
        return {status: 'untargeted', matches: []};
    const matches = matchWindows(query, windows);
    if (matches.length === 1)
        return {status: 'unique', window: matches[0], matches};
    if (matches.length === 0)
        return {status: 'no-match', matches};
    return {status: 'ambiguous', matches};
}

// One ledger per request, shared by its visible frame and canonical geometry.
// Titles/classes locate a window ONCE; later scans use its Mutter identity
// with fresh geometry. Keep a lost binding: a replacement with the same title
// must not silently inherit the request or its remote capture (hcop-l7e).
export class WindowBindings {
    constructor() {
        this._windows = new Map();
    }

    resolve(query, windows) {
        const key = (query || '').toLowerCase();
        if (!key)
            return {status: 'untargeted', matches: []};
        if (this._windows.has(key)) {
            const sequence = this._windows.get(key);
            const matches = (windows || []).filter(w =>
                !w.minimized && String(w.stableSequence ?? '') === sequence);
            if (matches.length === 1)
                return {status: 'unique', window: matches[0], matches};
            return {status: matches.length ? 'ambiguous' : 'no-match', matches};
        }
        const resolution = resolveTarget(query, windows);
        if (resolution.status === 'unique') {
            const sequence = String(resolution.window.stableSequence ?? '');
            if (!sequence)
                return {status: 'no-match', matches: []};
            this._windows.set(key, sequence);
        }
        return resolution;
    }
}

// A targeted request whose window is GONE is an error to the calling
// engine — but windows open late, so the failure is bounded by an explicit
// grace period (this many seconds of unresolved watching, measured from the
// first scan that saw it unresolved or its locator changed). After it, the
// request is completed with a target-resolution error instead of being
// moved to a screen corner and answered there.
export const TARGET_GRACE_S = 10;

export function targetExpired(unresolvedSince, now, graceS = TARGET_GRACE_S) {
    return unresolvedSince !== null && (now - unresolvedSince) >= graceS;
}

export function targetErrorAnswer(resolution, query) {
    // The answer JSON surfaces write for an unresolved target; server.py
    // turns it into an isError result that names no-match vs ambiguity and
    // tells the engine to review the locator.
    const ambiguous = resolution.status === 'ambiguous';
    return {
        text: '', cancelled: false, images: [],
        error: {
            kind: 'target-unresolved', reason: ambiguous ? 'ambiguous' : 'no-match',
            query, matches: resolution.matches.length,
            message: ambiguous
                ? `the locator '@{${query} …}' matched ${resolution.matches.length} windows; it must match exactly one`
                : `no window matching '@{${query} …}'`,
        },
    };
}

// ---- canonical responder geometry (hcop-4jq). Mutter's frame and monitor
// rectangles are in logical stage coordinates. A remote crop is expressed
// relative to ONE monitor in physical pixels, which avoids the undefined
// global-pixel origin on mixed-scale layouts. Clip in logical space first,
// then round every edge inward: capture can lose a fractional edge pixel but
// can never gain a neighbouring one.

export function monitorPhysicalRect(rect, monitor, scale) {
    if (!rect || !monitor || !Number.isFinite(scale) || scale <= 0)
        return null;
    for (const value of [rect.x, rect.y, rect.width, rect.height,
                         monitor.x, monitor.y, monitor.width, monitor.height]) {
        if (!Number.isFinite(value))
            return null;
    }
    const left = Math.max(rect.x, monitor.x);
    const top = Math.max(rect.y, monitor.y);
    const right = Math.min(rect.x + rect.width, monitor.x + monitor.width);
    const bottom = Math.min(rect.y + rect.height, monitor.y + monitor.height);
    const x1 = Math.ceil((left - monitor.x) * scale);
    const y1 = Math.ceil((top - monitor.y) * scale);
    const x2 = Math.floor((right - monitor.x) * scale);
    const y2 = Math.floor((bottom - monitor.y) * scale);
    if (x2 <= x1 || y2 <= y1)
        return null;
    return {x: x1, y: y1, width: x2 - x1, height: y2 - y1};
}

function targetIdentity(window, monitor) {
    if (window) {
        // Window content is used locally to resolve locators, never published
        // as canonical identity for downstream consumers (hcop-lp1).
        return {
            kind: 'mutter-window',
            stable_sequence: String(window.stableSequence ?? ''),
            monitor: monitor.index,
        };
    }
    return {kind: 'mutter-monitor', monitor: monitor.index};
}

function canonicalLogicalRect(base, locator) {
    const fraction = locator?.rect || REGIONS[locator?.region || 'full'];
    if (!base || !fraction || fraction.length !== 4)
        return null;
    const [x, y, width, height] = fraction;
    if (![x, y, width, height].every(Number.isFinite) || width <= 0 || height <= 0)
        return null;
    // The monitor clip below protects monitor bounds; this clip separately
    // protects the requested window/base. A permissive fractional locator
    // must never capture a neighbouring window just because both fit on the
    // same monitor.
    const left = Math.max(0, Math.min(1, x));
    const top = Math.max(0, Math.min(1, y));
    const right = Math.max(0, Math.min(1, x + width));
    const bottom = Math.max(0, Math.min(1, y + height));
    if (right <= left || bottom <= top)
        return null;
    return {x: base.x + left * base.width, y: base.y + top * base.height,
            width: (right - left) * base.width,
            height: (bottom - top) * base.height};
}

export function canonicalTarget(locator, windows, monitors, primaryIndex = 0,
                                bindings = null) {
    if (!locator)
        return {state: 'untargeted', identity: null, geometry: null};
    let base, window = null, resolution = null;
    if (locator.window) {
        resolution = bindings ? bindings.resolve(locator.window, windows)
                              : resolveTarget(locator.window, windows);
        if (resolution.status !== 'unique') {
            return {state: resolution.status === 'ambiguous' ? 'ambiguous' : 'missing',
                    reason: resolution.status, matches: resolution.matches.length,
                    identity: null, geometry: null};
        }
        window = resolution.window;
        base = window.frame;
    }
    const monitorIndex = window ? window.monitor : primaryIndex;
    const monitor = (monitors || []).find(m => m.index === monitorIndex);
    if (!monitor)
        return {state: 'missing', reason: 'monitor-missing', matches: 0,
                identity: null, geometry: null};
    if (!base)
        base = {x: monitor.x, y: monitor.y,
                width: monitor.width, height: monitor.height};
    // Unlike the visible highlight, a capture contract never grows a tiny
    // region to the UI's 24px minimum and never rounds outward in logical
    // space. Only monitorPhysicalRect performs final inward edge rounding.
    const logical = canonicalLogicalRect(base, locator);
    const physical = monitorPhysicalRect(logical, monitor, monitor.scale);
    if (!physical)
        return {state: 'missing', reason: 'empty-crop', matches: 0,
                identity: targetIdentity(window, monitor), geometry: null};
    return {
        state: 'resolved',
        identity: targetIdentity(window, monitor),
        geometry: {
            coordinate_space: 'monitor-relative-physical-pixels/v1',
            rect: physical,
            scale: monitor.scale,
            monitor: {index: monitor.index,
                      logical: {x: monitor.x, y: monitor.y,
                                width: monitor.width, height: monitor.height}},
        },
    };
}

export function canonicalTargetVector(steps, windows, monitors, primaryIndex = 0,
                                      bindings = null) {
    const targets = (steps || []).map(step => ({
        id: step.id,
        ...canonicalTarget(step.target, windows, monitors, primaryIndex, bindings),
    }));
    let state = 'untargeted';
    if (targets.some(target => target.state === 'ambiguous'))
        state = 'ambiguous';
    else if (targets.some(target => target.state === 'missing'))
        state = 'missing';
    else if (targets.some(target => target.state === 'resolved'))
        state = 'resolved';
    return {state, targets};
}

function geometryFingerprint(record) {
    // Observation time and diagnostics are not crop identity.
    // Generation changes only with canonical state, stable identity, physical
    // rect, scale or monitor logical transform, across the complete ordered
    // step vector.
    return JSON.stringify({
        state: record.state,
        targets: (record.targets || []).map(target => ({
            id: target.id,
            state: target.state,
            identity: target.identity ? {
                kind: target.identity.kind,
                stable_sequence: target.identity.stable_sequence ?? null,
                monitor: target.identity.monitor,
            } : null,
            geometry: target.geometry,
        })),
    });
}

export function advanceGeometry(previous, candidate, watermark = 0) {
    const priorGeneration = Number.isInteger(previous?.generation)
        ? previous.generation : 0;
    const floor = Math.max(Number.isInteger(watermark) ? watermark : 0,
                           priorGeneration);
    const unchanged = previous &&
        geometryFingerprint(previous) === geometryFingerprint(candidate);
    const generation = unchanged && priorGeneration > 0
        ? priorGeneration : floor + 1;
    return {
        record: {...candidate, generation},
        watermark: Math.max(floor, generation),
        changed: !!previous && !unchanged,
    };
}

export function remoteLeaseActive(lease, request, now) {
    return !!lease && lease.schema === 'human-copilot.responder-lease/v1' &&
        lease.schema_version === 1 && lease.request_id === request?.id &&
        lease.request_revision === request?.request_revision &&
        Number.isFinite(lease.expires_at) && lease.expires_at > now;
}

// ---- placement (hcop-453). Pure geometry so tests can pin it: a targeted
// card sits OUTSIDE its target's frame, clamped into the target's work
// area, dodging anything already placed and — while any other position
// works — never covering the highlighted observation region. Deterministic:
// candidates are tried in a fixed order and the first fitting one wins.
const CARD_GAP = 8;
const CASCADE = 24;

function intersects(a, b) {
    return a && b && a.x < b.x + b.width && a.x + a.width > b.x &&
           a.y < b.y + b.height && a.y + a.height > b.y;
}

function clampRect(rect, workArea) {
    const x = Math.min(Math.max(rect.x, workArea.x),
                       workArea.x + workArea.width - rect.width);
    const y = Math.min(Math.max(rect.y, workArea.y),
                       workArea.y + workArea.height - rect.height);
    return {clipped: x !== rect.x || y !== rect.y,
            rect: {x, y, width: rect.width, height: rect.height}};
}

// Shift `rect` by ±CASCADE, downward first, up to `tries` shifts, until it
// clears every occupied rect — the stack for several cards on one target.
function dodge(rect, occupied, workArea, tries = 8) {
    if (!occupied || !occupied.length)
        return rect;
    const base = {...rect};
    for (let i = 0; i <= tries; i++) {
        for (const dy of (i === 0 ? [0] : [i * CASCADE, -i * CASCADE])) {
            const cand = clampRect({...base, y: base.y + dy}, workArea).rect;
            if (!occupied.some(o => intersects(cand, o)))
                return cand;
        }
    }
    return clampRect(base, workArea).rect;     // out of room: the clamp wins
}

export function placeNearTarget(frame, cardW, cardH, workArea, occupied = [],
                                spot = null) {
    // Candidates: the four sides outside the target, aligned to it. A side
    // is only rejected for COVERING the observation region while another
    // candidate exists that does not — priority order does the rest.
    const candidates = [
        {x: frame.x + frame.width + CARD_GAP, y: frame.y},                    // right
        {x: frame.x - cardW - CARD_GAP, y: frame.y},                          // left
        {x: frame.x, y: frame.y + frame.height + CARD_GAP},                   // below
        {x: frame.x, y: frame.y - cardH - CARD_GAP},                          // above
    ];
    let fallback = null;
    for (const cand of candidates) {
        const {rect} = clampRect({...cand, width: cardW, height: cardH}, workArea);
        const coversSpot = intersects(rect, spot);
        if (!coversSpot && !occupied.some(o => intersects(rect, o)))
            return {...rect, coversSpot: false, dodged: false};
        if (!fallback && !coversSpot)
            fallback = {rect, coversSpot, dodged: true};
    }
    if (fallback) {                            // clear of the spot, not of cards
        return {...dodge(fallback.rect, occupied, workArea),
                coversSpot: false, dodged: true};
    }
    const last = clampRect({...candidates[0], width: cardW, height: cardH},
                           workArea).rect;     // tiny target on a tiny screen
    return {...dodge(last, occupied, workArea), coversSpot: true, dodged: true};
}

// The untargeted layout: a deterministic, bounded two-column reading grid.
// A lone card remains centred, but concurrent requests fill left-to-right in
// rows instead of becoming an aimless centre-column. Every one is fully
// visible or honestly omitted from the bounded work area.
// `occupied` (already-placed targeted cards) reflows the column: a row that
// would cover one slides below it instead — targeted and untargeted
// requests coexist without occlusion. A card that would pass the bottom of
// the work area is NOT placed silently: rects is INDEX-ALIGNED with sizes —
// rects[i] is sizes[i]'s slot, null when that card did not fit — and
// `dropped` counts the nulls, so a caller can name the overflow on the last
// card that DID fit (a smaller later card still gets its own slot).
export function stackCards(workArea, sizes, occupied = null, gap = CARD_GAP) {
    const rects = Array((sizes || []).length).fill(null);
    let dropped = 0;
    let y = workArea.y;
    const list = sizes || [];
    const widest = Math.max(1, ...list.map(s => s.width || 1));
    const columns = list.length > 1 && workArea.width >= widest * 2 + gap ? 2 : 1;
    const cellW = workArea.width / columns;
    for (let first = 0; first < list.length; first += columns) {
        const row = list.slice(first, first + columns);
        let ry = y;
        const rowRects = row.map((size, col) => {
            const x = Math.max(workArea.x, Math.min(
                workArea.x + Math.round(col * cellW + (cellW - size.width) / 2),
                workArea.x + workArea.width - size.width));
            return {x, y: ry, width: size.width, height: size.height};
        });
        if (occupied) {
            let moved = true;
            while (moved) {              // each pass strictly lowers ry
                moved = false;
                for (const o of occupied) {
                    if (rowRects.some(r => intersects(r, o))) {
                        ry = Math.max(ry, o.y + o.height + gap);
                        rowRects.forEach(r => { r.y = ry; });
                        moved = true;
                    }
                }
            }
        }
        const rowHeight = Math.max(...row.map(s => s.height));
        if (ry + rowHeight > workArea.y + workArea.height) {
            dropped += row.length;
            continue;
        }
        rowRects.forEach((rect, col) => { rects[first + col] = rect; });
        y = ry + rowHeight + gap;
    }
    return {rects, dropped};
}

// An OSD cannot be tinted, but its icon can be anything: a swatch of the
// instance colour with the number on it, as an SVG the extension wraps in a
// Gio.BytesIcon. The number is drawn in the entry's ink (`fg`), the same pair
// the strip chips use.
export function swatchSvg(entry, number = 0, size = 96) {
    const bg = entry && entry.hex ? entry.hex : SPOT_FALLBACK;
    const fg = entry && entry.fg ? entry.fg : SPOT_INK;
    const r = Math.round(size * 0.18);
    const text = number > 0 ? String(number) : '';
    const fontSize = text.length > 1 ? Math.round(size * 0.5) : Math.round(size * 0.62);
    return `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" ` +
           `viewBox="0 0 ${size} ${size}">` +
           `<rect x="0" y="0" width="${size}" height="${size}" rx="${r}" ry="${r}" fill="${bg}"/>` +
           (text ? `<text x="50%" y="50%" dominant-baseline="central" text-anchor="middle" ` +
                   `font-family="sans-serif" font-weight="bold" font-size="${fontSize}" ` +
                   `fill="${fg}">${text}</text>` : '') +
           `</svg>`;
}

// A GFileMonitor on the requests directory also reports ATTRIBUTE_CHANGED
// (chmod/touch, e.g. a responder re-asserting modes). Content and presence
// events are what change a rescan's outcome; an attribute event never does.
// `events` are Gio.FileMonitorEvent values; `attributeChanged` is that enum's
// ATTRIBUTE_CHANGED so this file stays free of gi imports.
export function monitorEventRescans(event, attributeChanged) {
    return event !== attributeChanged;
}

// Per-request expiry backoff. A stale request the responder cannot remove
// (refused, failed to start, still present after it exited) would otherwise
// spawn a --one-shot responder on every scan. `begin` admits at most one
// helper per request per `windowS`, and never while one is in flight.
// Times are seconds from any monotonic-enough clock; callers pass `now`.
export class ExpiryBackoff {
    constructor(windowS) {
        this.windowS = windowS;
        this._inflight = new Set();
        this._until = new Map();     // request id -> earliest next attempt
    }

    /** True (and the attempt is recorded) when a helper may be spawned. */
    begin(id, now) {
        if (typeof id !== 'string' || this._inflight.has(id))
            return false;
        const until = this._until.get(id);
        if (until !== undefined && now < until)
            return false;
        this._inflight.add(id);
        this._until.set(id, now + this.windowS);
        return true;
    }

    /** The helper exited (or never started); the backoff window still holds. */
    finish(id) {
        this._inflight.delete(id);
    }

    /** Forget requests that left the spool, so a reused id starts fresh. */
    prune(liveIds) {
        const live = new Set(liveIds);
        for (const id of [...this._until.keys()]) {
            if (!live.has(id) && !this._inflight.has(id))
                this._until.delete(id);
        }
    }
}

// Runtime-status write policy: write when the content (everything except the
// `observed` stamp) changed, or as a heartbeat once `heartbeatS` has passed
// since the last successful write. Status freshness is judged by `observed`
// (server.py OVERLAY_RUNTIME_FRESH_S = 15), so the heartbeat must stay well
// under that.
export class StatusWriter {
    constructor(heartbeatS) {
        this.heartbeatS = heartbeatS;
        this._key = null;
        this._at = -Infinity;
    }

    static keyOf(body) {
        const {observed: _observed, ...rest} = body;
        return JSON.stringify(rest);
    }

    due(body, now) {
        return StatusWriter.keyOf(body) !== this._key ||
            now - this._at >= this.heartbeatS;
    }

    wrote(body, now) {
        this._key = StatusWriter.keyOf(body);
        this._at = now;
    }

    /** A failed write must be retried at the next opportunity. */
    failed() {
        this._key = null;
        this._at = -Infinity;
    }
}
