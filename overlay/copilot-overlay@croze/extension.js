// copilot-overlay — the headless surface of the human-copilot MCP lane.
//
// An agent's interactive_feedback request (a file in the spool that server.py
// in hubs/human-copilot writes) appears as an always-on-top CARD. Request
// lines have copy buttons; an inline code span gets an exact "Copy code"
// action, and "Copy all" preserves the complete request. Nothing is typed
// and nothing steals focus (with the deliberate exception of a note taking
// the keyboard). Answer actions are hotkeys (rebindable in
// dconf under org.gnome.shell.extensions.copilot-overlay):
//
//   <Super><Alt>space       checklist: mark the ▶ step done — and once every
//                           step has an answer, the SAME chord sends. choice:
//                           select the next option. ack: confirm.
//   <Super><Alt>x           mark the ▶ step FAILED (it did not do that)
//   <Super><Alt>z           undo the last answered step (done or failed)
//   <Super><Alt>a           attach the clipboard snip TO THE ▶ STEP
//                           (a = attach; <Super><Alt>s is Ubuntu's screen-reader toggle)
//                           (Print/snip@croze puts it there; press
//                           repeatedly to attach several)
//   <Super><Alt>n           type a reply (an St.Entry opens in the card;
//                           Super+Alt+Return sends it, Enter saves, Esc
//                           cancels — the one moment the overlay takes focus)
//   <Super><Alt>Return      submit — checked steps / the chosen option / the
//                           typed note become the answer
//   <Super><Alt>BackSpace   decline (the agent is told, never left hanging)
//   <Super><Alt>slash       hide/show the hotkey glossary (persisted in
//                           'glossary-visible'; renders the LIVE bindings)
//
// EVERY open request is its own surface (2026-09-04, hcop-5sy). The old
// one-active-checklist-plus-instance-strip design (hcop-0p7.8) collapsed
// concurrent copilots into a queue the operator had to switch between; the
// operator asked for the opposite — "a green red and yellow border should be
// able to be on screen simultaneously". Now each request draws its own card,
// each carrying its instance NUMBER and session NAME as text next to its
// palette colour (identity is never colour alone). Clicking a card routes
// the global chords to it; the other cards stay visible and usable —
// "active" is ROUTING, never a visibility gate. Copy buttons never change
// that routing or submit an answer.
//
// Targeting (2026-09-04, hcop-453): a request whose ▶ step carries
// "@{Window · region}" is anchored to THAT window — its card is placed just
// outside the window's frame (clamped into the work area, dodging cards
// already placed, never covering the highlighted region while any other
// position works) and both card and frame follow the window as it moves or
// resizes (fresh geometry on the 2 s scan). A unique initial match is bound
// to its stable window sequence for this request, surviving title changes
// and popups. The initial resolver distinguishes one match, none, and several
// — it never silently picks the most recent,
// because a card attached to the wrong window is worse than no card. A
// locator that initially matches zero or several windows, or whose bound
// window becomes unavailable, is an ENGINE error after a
// short explicit grace period (TARGET_GRACE_S): the request is completed
// with a machine-readable target-resolution error naming no-match vs
// ambiguity — it is never moved to a screen corner and answered there, and
// the operator is never asked to guess. Untargeted requests (no window
// locator — free text, choices, acks) stack at the TOP MIDDLE of the work
// area, every one visible; a card that would pass the work-area floor is
// dropped and COUNTED, said on the last placed card, never clipped silently.
//
// Answer routing: every answer is written under its REQUEST id
// (composeAnswer in lib.js — an unchecked step reads "NOT DONE", never as
// silence; a choice carries the exact {value, label}), so concurrent
// answers can never cross. A request that vanishes because someone else
// answered it, or it timed out, is announced ONCE, naming its instance.
//
// Spotlight: a step tagged "@{Window · region}" (lib.js::parseLook) gets a
// colour-coded frame drawn on the screen itself — every resolved targeted
// request frames its own target at the same time; only the ACTIVE request's
// frame pulses (motion is attention, and reduced motion beats decoration).
// The frame is top chrome that does not take input; a window that cannot be
// found is SAID under the step (amber) for the grace period, then the
// request errors back to the engine.
//
// This lives inside gnome-shell because on GNOME Wayland nothing else can be
// both always-on-top and globally hotkeyed: keep-above is compositor-owned
// and global shortcuts outside the shell need the portal dance. Shell chrome
// (addTopChrome) plus Main.wm.addKeybinding is the sanctioned way to both.
//
// Presence: while enabled, a tiny helper process holds a SHARED flock on the
// spool's tui.lock — the same lock copilot-tui holds — so server.py knows a
// surface is watching and does NOT auto-open a terminal. GJS has no flock,
// so the helper is python3 with PDEATHSIG set: if the shell dies, the lock
// dies with it and can never go stale (the reason it's a lock, not a file).
//
// Instances (2026-09-03, hcop-0p7.4/5): requests from several zellij
// sessions may be open at once; every card names its instance (number +
// session + colour) and every OSD names the instance it acted on.

import Clutter from 'gi://Clutter';
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Meta from 'gi://Meta';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {parseSteps, markNext, markFail, undoLast, currentStep, allResolved,
        orderImages, composeAnswer, checklistAnswer, prettyAccel, modeHint, spotRect,
        spotLabel, overlayHeader, RequestFileCache, focusTarget, pickActive,
        instanceSuffix, instanceKey, OpenLedger, InstanceRoster,
        queuedBehind, withdrawnText, arrivedText, registrySlots,
        spotFrameSpec, isSpotActor, SpotLedger, swatchSvg, SPOT_FALLBACK,
        parseResponse, looksLikeChoices, emptySubmitNote, WindowBindings,
        targetExpired, targetErrorAnswer, placeNearTarget, stackCards,
        canonicalTargetVector, advanceGeometry, remoteLeaseActive,
        TARGET_GRACE_S, cardBadge, choiceAnswer, choiceDisplayText, choiceNeedsScroll,
        answeredRequestIds, callerProcessState, requestExpired,
        monitorEventRescans, ExpiryBackoff, StatusWriter}
    from './lib.js';
import {entryFor} from './palette.js';

const KEYS = ['mark-done', 'mark-fail', 'undo-done', 'attach-snip', 'edit-note',
              'submit-answer', 'dismiss-request', 'toggle-glossary'];
// The reference glossary (toggled with toggle-glossary; the NOW line is the
// everyday guide). Rows: [settings key, what it does] — accels are read LIVE
// from settings so a rebind shows as what it is. Shown on the ACTIVE card.
const GLOSSARY = [
    ['mark-done', '▶ step done / next option — when all answered, sends'],
    ['mark-fail', '▶ step FAILED (then a note says what you saw)'],
    ['edit-note', 'one-line reply under the ▶ step'],
    ['attach-snip', 'attach the clipboard snip to the ▶ step (repeat for more)'],
    ['undo-done', 'undo the last answered step'],
    ['submit-answer', 'send now (unanswered steps go out as NOT DONE)'],
    ['dismiss-request', 'decline — agent is told'],
    ['toggle-glossary', 'hide/show this list'],
];
const GUIDE = 'do the ▶ step · done or failed · note what you saw · send';
// The accent when the request has no instance colour: lib.js::SPOT_FALLBACK
// (the lane's original yellow). Frame names, edge size and the sweep predicate
// live in lib.js too, so the tests pin the same values this file draws.
const SPOT_COLOR = SPOT_FALLBACK;
const OVERLAY_W = 460;
const TICK_S = 2;
// A stale request the responder cannot remove is retried at most once per
// this window; the status heartbeat stays well under server.py's 15 s
// OVERLAY_RUNTIME_FRESH_S.
const EXPIRY_BACKOFF_S = TICK_S * 15;
const STATUS_HEARTBEAT_S = TICK_S * 3;
const OVERLAY_RUNTIME_FILE = 'overlay-runtime.json';
const TARGET_WATERMARK_FILE = 'generation-watermark.json';
// A card whose height is not yet measured (first layout) is estimated; the
// next scan replaces it with the widget's measured height.
const EST_CARD_H = 260;
// Long choice sets remain complete inside this bounded viewport. The card
// keeps its header and action hint visible while the operator scrolls every
// full label/consequence; ordinary two-option decisions use their natural
// height and show no scrollbar.
const CHOICE_VIEW_H = 260;
const ORPHAN_GRACE_S = 30;
// Snip writes image/png; the rest cover a hand-copied image from elsewhere.
const IMAGE_MIMES = ['image/png', 'image/jpeg', 'image/webp', 'image/gif'];

function spoolDir() {
    const base = GLib.getenv('HC_SPOOL') ||
        GLib.build_filenamev([GLib.get_user_runtime_dir(), 'human-copilot']);
    for (const sub of ['requests', 'answers', 'targets'])
        GLib.mkdir_with_parents(GLib.build_filenamev([base, sub]), 0o700);
    return base;
}

export default class CopilotOverlayExtension extends Extension {
    enable() {
        this._enabled = true;
        this._spool = spoolDir();
        // Do not canonicalize this path: the installed extension is itself a
        // symlink into the workspace. Kernel traversal must follow that link
        // before applying ../.., otherwise canonicalization points beside the
        // install link at ~/.local/share/gnome-shell/responder.py.
        this._responderPath = GLib.build_filenamev(
            [this.path, '..', '..', 'responder.py']);
        this._geometryWatermark = this._loadGeometryWatermark();
        this._settings = this.getSettings();
        this._requestCache = new RequestFileCache();
        this._views = new Map();   // request id -> view (its OWN state, alive
                                   // for the request's whole life: steps,
                                   // ticks, notes, images, selection)
        this._cards = new Map();   // request id -> {box, header, key} drawn card
        this._req = null;          // the ACTIVE view: chord routing ONLY
        this._pinned = null;       // request id pinned by a card click
        this._flash = null;        // transient status line in the active card
        this._flashTimer = 0;
        this._noteStep = -1;       // step whose note entry is open, -1 = none
        this._noteReq = null;      // request owning the open entry
        this._entry = null;        // the open St.Entry, only while a note is open
        this._grab = null;         // the modal grab held while typing a note
        this._spots = new SpotLedger(); // request id -> its OWN frame's actors
        this._focus = null;        // {session} of the last-focused copilot pane
        this._ledger = new OpenLedger();   // every open request, by id
        this._roster = new InstanceRoster(); // instance key -> instance number
        this._answered = new Set(); // ids THIS overlay completed: not "withdrawn"
        this._expiring = new ExpiryBackoff(EXPIRY_BACKOFF_S); // per stale request
        this._status = new StatusWriter(STATUS_HEARTBEAT_S);
        this._presence = null;      // held only after a card layout succeeds
        this._sweepOrphanSpots();
        for (const name of KEYS) {
            Main.wm.addKeybinding(name, this._settings,
                Meta.KeyBindingFlags.NONE, Shell.ActionMode.ALL,
                () => this._onKey(name));
        }
        // Which session's copilot pane is the operator on? Title-only (see
        // lib.js::focusTarget) — no subprocess ever runs on this signal.
        this._focusSig = global.display.connect('notify::focus-window',
            () => this._onFocus());
        this._onFocus();
        const reqDir = Gio.File.new_for_path(
            GLib.build_filenamev([this._spool, 'requests']));
        this._monitor = reqDir.monitor_directory(Gio.FileMonitorFlags.NONE, null);
        this._monitor.connect('changed', (_m, _file, _other, event) => {
            if (monitorEventRescans(event, Gio.FileMonitorEvent.ATTRIBUTE_CHANGED))
                this._scan();
        });
        // Belt to the monitor's braces: the countdown needs a tick anyway,
        // and the tick also notices an expired/withdrawn request, follows a
        // moved/resized target window, and runs the target grace clock.
        this._tick = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_S,
            () => { this._scan(); return GLib.SOURCE_CONTINUE; });
        this._scan();
    }

    /** Same-process capture callers need current canonical geometry now. */
    refreshTargetsForCapture() {
        if (!this._enabled)
            throw new Error('E_TARGET_UNRESOLVED');
        this._scan();
    }

    disable() {
        this._enabled = false;
        for (const name of KEYS)
            Main.wm.removeKeybinding(name);
        if (this._focusSig) {
            global.display.disconnect(this._focusSig);
            this._focusSig = 0;
        }
        if (this._tick) {
            GLib.source_remove(this._tick);
            this._tick = 0;
        }
        this._monitor?.cancel();
        this._monitor = null;
        this._dropPresence();
        this._dropNoteGrab();
        for (const id of [...this._views.keys()])
            this._dropView(id);        // every card and frame, whoever owned it
        this._clearSpot();             // and any frame with no view left
        this._settings = null;
    }

    // -- focus routing ------------------------------------------------------
    // The focused window's title names the zellij session when zellij owns
    // it ("<session> | <pane title>"); the pane half naming copilot means the
    // operator is on that session's copilot pane, which routes the global
    // chords there. Clicking a card pins a request (this._pinned); focusing a
    // copilot pane releases the pin — an explicit focus beats a click.
    _onFocus() {
        let title = null;
        try {
            title = global.display.focus_window?.get_title?.() ?? null;
        } catch (e) {
            return;
        }
        const f = focusTarget(title);
        if (!f || !f.copilot)
            return;
        this._pinned = null;
        if (this._focus?.session === f.session)
            return;
        this._focus = {session: f.session};
        this._scan();                  // chord routing may change
    }

    // -- presence -----------------------------------------------------------
    _holdPresence() {
        if (this._presence)
            return;
        const py = 'import ctypes, fcntl, os, signal, sys\n' +
            'ctypes.CDLL(None).prctl(1, signal.SIGTERM)\n' +   // PR_SET_PDEATHSIG
            'fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o600)\n' +
            'fcntl.flock(fd, fcntl.LOCK_SH)\n' +
            'signal.pause()\n';
        try {
            this._presence = Gio.Subprocess.new(
                ['python3', '-c', py,
                 GLib.build_filenamev([this._spool, 'tui.lock'])],
                Gio.SubprocessFlags.NONE);
        } catch (e) {
            logError(e, 'copilot-overlay: presence helper failed — server may ' +
                        'auto-open a terminal alongside the overlay');
        }
    }

    _dropPresence() {
        this._presence?.force_exit();
        this._presence = null;
    }

    // A held global lock is the server's promise that an operator surface is
    // actually usable. Keep a small, non-sensitive heartbeat beside it so
    // status can distinguish a rendered surface from a live process whose
    // widget construction is throwing. The file carries counts, never prompt
    // text. If even this diagnostic cannot be committed, fail open: release
    // presence so the server opens a terminal fallback.
    _writeRuntimeStatus(state, error = null) {
        const final = GLib.build_filenamev([this._spool, OVERLAY_RUNTIME_FILE]);
        const now = GLib.get_monotonic_time() / 1e6;
        const body = {
            schema: 'human-copilot-overlay-runtime',
            schema_version: 1,
            state,
            observed: GLib.get_real_time() / 1e6,
            pending_requests: this._views?.size ?? 0,
            rendered_cards: this._cards?.size ?? 0,
            // Additive live-code evidence: status can distinguish source on
            // disk from a shell process that has actually loaded hcop-m8n.
            answer_filter_version: 1,
            choice_reply_version: 1,
            window_binding_version: 1,
            expiry_filter_version: 1,
        };
        if (error) {
            body.error = {
                name: String(error.name || 'Error').slice(0, 80),
                message: String(error.message || error).slice(0, 240),
            };
        }
        // Unchanged content inside the heartbeat window is already committed.
        if (!this._status.due(body, now))
            return true;
        try {
            // Plain write-and-rename: no gvfs metadata D-Bus round trip.
            const data = new TextEncoder().encode(JSON.stringify(body));
            GLib.file_set_contents_full(final, data,
                GLib.FileSetContentsFlags.CONSISTENT, 0o600);
            this._status.wrote(body, now);
            return true;
        } catch (e) {
            logError(e, 'copilot-overlay: runtime status write failed');
            this._status.failed();
            return false;
        }
    }

    // -- spool --------------------------------------------------------------
    _scan() {
        try {
            this._scanOnce();
            if (this._writeRuntimeStatus('healthy'))
                this._holdPresence();
            else
                this._dropPresence();
        } catch (e) {
            logError(e, 'copilot-overlay: request render failed');
            this._writeRuntimeStatus('error', e);
            this._dropPresence();
        }
    }

    _scanOnce() {
        let names = [];
        try {
            const dir = Gio.File.new_for_path(
                GLib.build_filenamev([this._spool, 'requests']));
            const en = dir.enumerate_children('standard::name',
                Gio.FileQueryInfoFlags.NONE, null);
            let info;
            while ((info = en.next_file(null)) !== null) {
                const n = info.get_name();
                if (n.endsWith('.json') && !n.includes('.tmp'))
                    names.push(n);
            }
            en.close(null);
        } catch (e) {
            logError(e, 'copilot-overlay: spool scan failed');
            throw e;
        }
        const answerFiles = this._answerFiles();
        if (this._grab) {
            // A remote lease is an ownership change, not a cosmetic refresh.
            // Preserve the text currently in the entry, release the keyboard,
            // and hide promptly instead of waiting to lose at final commit.
            const editing = this._noteReq ? this._views.get(this._noteReq) : null;
            const editingFile = editing && names.includes(editing.id + '.json');
            const editingAnswered = editing && answerFiles.has(editing.id);
            const editingExpired = editing && requestExpired(editing.req);
            if (editing && this._remoteLease(editing.req)) {
                if (this._entry) {
                    if (this._noteStep >= 0 && editing.steps[this._noteStep])
                        editing.steps[this._noteStep].note = this._entry.get_text();
                    else
                        editing.note = this._entry.get_text();
                }
                editing.remoteLeased = true;
                this._dropNoteGrab();
                this._osd('Another responder owns this request — draft retained' +
                          this._tagOf(editing));
            } else if (!editing || !editingFile || editingAnswered || editingExpired) {
                if (editing)
                    this._dropNoteGrab();
                if (editingExpired)
                    this._osd('Copilot request expired — draft was not sent' +
                              this._tagOf(editing));
                else if (editingAnswered)
                    this._osd('Another responder already answered — draft retained' +
                              this._tagOf(editing));
            } else {
                // An active note entry owns the card's widgets. Do not rebuild
                // it on ordinary target/countdown scans, but keep scanning
                // lifecycle state so expiry can never strand the modal.
                return;
            }
        }
        const rawReqs = this._requestCache.scan(names, n => {
            const [, bytes] = Gio.File.new_for_path(GLib.build_filenamev(
                [this._spool, 'requests', n])).load_contents(null);
            return bytes;
        });
        this._reclaimAnsweredOrphans(rawReqs, answerFiles);
        // A complete answer is already the operator's verdict. Never show
        // that request again while its caller performs normal cleanup — or
        // after the caller has died and left an orphan pair. Mark it answered
        // in the ledger too, so another surface's answer is not announced as
        // a mysterious withdrawal.
        for (const id of answerFiles.keys())
            this._answered.add(id);
        const expired = rawReqs.filter(r => requestExpired(r));
        for (const req of expired)
            this._expireRequest(req);
        this._expiring.prune(rawReqs.map(r => r.id));
        const reqs = rawReqs.filter(r => !answerFiles.has(r.id) &&
                                         !requestExpired(r));
        reqs.sort((a, b) => {
            const byCreated = Number(a.created) - Number(b.created);
            return byCreated || String(a.id).localeCompare(String(b.id));
        });
        // every open request, by id: what vanished since the last scan is
        // announced ONCE, naming its instance; what this overlay completed
        // itself is not news
        const {withdrawn, arrived} = this._ledger.update(reqs, this._answered);
        // a withdrawal takes exactly its own card, frame and state away —
        // never anything another request owns
        for (const id of [...this._views.keys()]) {
            if (!reqs.some(r => r.id === id))
                this._dropView(id);
        }
        // instance numbers: the palette registry's slot for a session when it
        // has one (the TUI's number), else stable lowest-free
        const numbers = this._roster.update(
            reqs.map(r => instanceKey(r.instance)), this._readRegistry());
        if (withdrawn.length)
            this._osd(withdrawnText(withdrawn, numbers));
        // a view per open request; one this overlay already completed is not
        // resurrected while the server has yet to unlink its file
        for (const r of reqs) {
            if (!this._views.has(r.id) && !this._answered.has(r.id))
                this._views.set(r.id, this._makeView(r));
            const v = this._views.get(r.id);
            if (v) {
                v.req = r;
                const leased = this._remoteLease(r);
                if (leased && !v.remoteLeased)
                    this._osd('Another responder owns this request — local draft retained' +
                              this._tagOf(v));
                v.remoteLeased = leased;
            }
        }
        this._pruneTargetRecords(reqs);
        const localReqs = reqs.filter(r => !this._views.get(r.id)?.remoteLeased);
        // chord routing: a clicked card pins its request; the last-focused
        // copilot pane's session wins over the newest request (pickActive)
        let pick = pickActive(localReqs, this._focus);
        if (this._pinned && localReqs.some(r => r.id === this._pinned))
            pick = {req: localReqs.find(r => r.id === this._pinned), reason: 'pinned'};
        this._req = pick ? this._views.get(pick.req.id) || null : null;
        const localArrivals = arrived.filter(r =>
            !this._views.get(r.id)?.remoteLeased);
        if (localArrivals.length)
            this._osd(arrivedText(localArrivals, this._req?.id, numbers));
        this._layout();
    }

    _pruneTargetRecords(reqs) {
        const live = new Set(reqs.map(req => req.id));
        try {
            const dirPath = GLib.build_filenamev([this._spool, 'targets']);
            const dir = Gio.File.new_for_path(dirPath);
            const en = dir.enumerate_children('standard::name',
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
            let info;
            while ((info = en.next_file(null)) !== null) {
                const name = info.get_name();
                if (name === TARGET_WATERMARK_FILE || !name.endsWith('.json') ||
                        name.includes('.tmp'))
                    continue;
                const id = name.slice(0, -5);
                if (/^[A-Za-z0-9._@+-]{1,128}$/.test(id) && !live.has(id))
                    this._deleteFile(GLib.build_filenamev([dirPath, name]));
            }
            en.close(null);
        } catch (_error) {
            // Publication itself remains fail-open through _scan's outer guard.
        }
    }

    // spool/palette.json -> {session: slot}; absent or unreadable -> {}.
    _readRegistry() {
        try {
            const [, bytes] = Gio.File.new_for_path(
                GLib.build_filenamev([this._spool, 'palette.json'])).load_contents(null);
            return registrySlots(new TextDecoder().decode(bytes));
        } catch (e) {
            return {};
        }
    }

    // Complete answer files by request id. Temporary files never enter this
    // map, so an in-progress writer cannot hide its request.
    _answerFiles() {
        const out = new Map();
        try {
            const dir = Gio.File.new_for_path(
                GLib.build_filenamev([this._spool, 'answers']));
            const en = dir.enumerate_children('standard::name,time::modified',
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
            let info;
            const names = [];
            const modified = new Map();
            while ((info = en.next_file(null)) !== null) {
                const name = info.get_name();
                names.push(name);
                modified.set(name,
                    Number(info.get_attribute_uint64('time::modified')));
            }
            en.close(null);
            for (const id of answeredRequestIds(names))
                out.set(id, {modified: modified.get(id + '.json')});
        } catch (e) {
            // The server normally creates this directory before a request is
            // visible. If it vanishes during logout, the request scan remains
            // the conservative source and nothing is reclaimed.
        }
        return out;
    }

    _smallText(path) {
        try {
            const [, bytes] = Gio.File.new_for_path(path).load_contents(null);
            if (!bytes || bytes.length > 8192)
                return undefined;
            return new TextDecoder().decode(bytes);
        } catch (e) {
            return undefined;
        }
    }

    _notFound(error) {
        try {
            return error.matches(Gio.io_error_quark(), Gio.IOErrorEnum.NOT_FOUND);
        } catch (_ignored) {
            return false;
        }
    }

    _callerState(record) {
        const boot = this._smallText('/proc/sys/kernel/random/boot_id');
        if (!record || !Number.isInteger(record.pid) || record.pid <= 0)
            return callerProcessState(record, boot, undefined);
        let procStat;
        try {
            const [, bytes] = Gio.File.new_for_path(
                `/proc/${record.pid}/stat`).load_contents(null);
            procStat = bytes && bytes.length <= 8192
                ? new TextDecoder().decode(bytes) : undefined;
        } catch (e) {
            procStat = this._notFound(e) ? null : undefined;
        }
        return callerProcessState(record, boot, procStat);
    }

    _deleteFile(path) {
        try {
            Gio.File.new_for_path(path).delete(null);
            return true;
        } catch (e) {
            if (this._notFound(e))
                return true;
            logError(e, 'copilot-overlay: orphan cleanup failed');
            return false;
        }
    }

    _reclaimAnsweredOrphans(reqs, answers) {
        const now = GLib.get_real_time() / 1e6;
        for (const req of reqs) {
            const answer = answers.get(req.id);
            if (!answer || now - answer.modified < ORPHAN_GRACE_S)
                continue;
            // The id becomes a path segment below. Requests produced by this
            // server are twelve hex characters; retain the broader established
            // safe segment alphabet for compatibility with fixture/legacy ids.
            if (typeof req.id !== 'string' || req.id.length > 128 ||
                    req.id === '.' || req.id === '..' ||
                    !/^[A-Za-z0-9._@+-]+$/.test(req.id))
                continue;
            if (this._callerState(req.caller_process) !== 'dead')
                continue;
            const requestPath = GLib.build_filenamev(
                [this._spool, 'requests', req.id + '.json']);
            // Request first: if a later unlink fails, no answered question can
            // reappear as actionable. Ambiguous and live owners never get here.
            if (!this._deleteFile(requestPath))
                continue;
            this._deleteFile(GLib.build_filenamev(
                [this._spool, 'requests', req.id + '.arrival-nudge']));
            this._deleteFile(GLib.build_filenamev(
                [this._spool, 'answers', req.id + '.json']));
        }
    }

    _readJson(path, limit = 1024 * 1024) {
        try {
            const [, bytes] = Gio.File.new_for_path(path).load_contents(null);
            if (!bytes || bytes.length > limit)
                return null;
            const value = JSON.parse(new TextDecoder().decode(bytes));
            return value && typeof value === 'object' && !Array.isArray(value)
                ? value : null;
        } catch (_error) {
            return null;
        }
    }

    _writePrivateJson(path, value) {
        const tmp = path + '.tmp-' + GLib.uuid_string_random();
        const data = new TextEncoder().encode(JSON.stringify(value));
        const file = Gio.File.new_for_path(tmp);
        try {
            const stream = file.replace(
                null, false, Gio.FileCreateFlags.PRIVATE, null);
            stream.write_bytes(new GLib.Bytes(data), null);
            stream.close(null);
            file.move(Gio.File.new_for_path(path), Gio.FileCopyFlags.OVERWRITE,
                      null, null);
        } catch (error) {
            try {
                file.delete(null);
            } catch (_ignored) {
                // A temporary metadata file is never part of the protocol.
            }
            throw error;
        }
    }

    _loadGeometryWatermark() {
        const targetDir = GLib.build_filenamev([this._spool, 'targets']);
        const watermarkPath = GLib.build_filenamev(
            [targetDir, TARGET_WATERMARK_FILE]);
        const watermark = this._readJson(watermarkPath);
        let generation = Number.isInteger(watermark?.generation) &&
            watermark.generation >= 0 ? watermark.generation : 0;
        try {
            const dir = Gio.File.new_for_path(targetDir);
            const en = dir.enumerate_children('standard::name',
                Gio.FileQueryInfoFlags.NOFOLLOW_SYMLINKS, null);
            let info;
            while ((info = en.next_file(null)) !== null) {
                const name = info.get_name();
                if (!name.endsWith('.json') || name === TARGET_WATERMARK_FILE ||
                        name.includes('.tmp'))
                    continue;
                const record = this._readJson(GLib.build_filenamev([targetDir, name]));
                if (Number.isInteger(record?.generation) && record.generation > generation)
                    generation = record.generation;
            }
            en.close(null);
        } catch (_error) {
            // The directory was just created; zero is a safe first watermark.
        }
        return generation;
    }

    _targetPath(id) {
        return GLib.build_filenamev([this._spool, 'targets', id + '.json']);
    }

    _publishTarget(v, windows, monitors) {
        const contract = v.req?.responder;
        if (!contract || contract.schema !==
                'human-copilot.responder-contract/v1' ||
                contract.schema_version !== 1 ||
                typeof v.req.request_revision !== 'string')
            return;
        const primary = Main.layoutManager.primaryMonitor?.index ?? 0;
        const vector = canonicalTargetVector(
            Array.isArray(contract.steps) ? contract.steps : [],
            windows, monitors, primary, v.windowBindings);
        const candidate = {
            schema: 'human-copilot.target-state/v1', schema_version: 1,
            request_id: v.id, request_revision: v.req.request_revision,
            observed_at: GLib.get_real_time() / 1e6,
            state: vector.state, targets: vector.targets,
        };
        const path = this._targetPath(v.id);
        const previous = this._readJson(path);
        const advanced = advanceGeometry(previous, candidate,
                                         this._geometryWatermark);
        if (advanced.watermark > this._geometryWatermark) {
            // Persist the high-water mark first. A crash can leave a harmless
            // gap, never a generation that regresses after Shell reload.
            this._writePrivateJson(GLib.build_filenamev(
                [this._spool, 'targets', TARGET_WATERMARK_FILE]), {
                schema: 'human-copilot.target-generation-watermark/v1',
                schema_version: 1, generation: advanced.watermark,
            });
            this._geometryWatermark = advanced.watermark;
        }
        this._writePrivateJson(path, advanced.record);
        v.geometryGeneration = advanced.record.generation;
    }

    _remoteLease(req) {
        const lease = this._readJson(GLib.build_filenamev(
            [this._spool, 'responder', 'leases', req.id + '.json']), 64 * 1024);
        return remoteLeaseActive(lease, req, GLib.get_real_time() / 1e6);
    }

    _expireRequest(req) {
        if (!req || typeof req.id !== 'string' ||
            !this._expiring.begin(req.id, GLib.get_monotonic_time() / 1e6))
            return;
        const command = {
            schema: 'human-copilot.responder-command/v1', schema_version: 1,
            op: 'expire', request_id: req.id,
            revision: typeof req.request_revision === 'string'
                ? req.request_revision : null,
        };
        let process;
        try {
            process = Gio.Subprocess.new(
                ['python3', this._responderPath, '--one-shot', '--spool', this._spool],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE |
                Gio.SubprocessFlags.STDERR_PIPE);
        } catch (error) {
            this._expiring.finish(req.id);
            logError(error, 'copilot-overlay: expiry helper failed to start');
            return;
        }
        process.communicate_utf8_async(JSON.stringify(command) + '\n', null,
            (proc, result) => {
                this._expiring.finish(req.id);
                try {
                    proc.communicate_utf8_finish(result);
                } catch (error) {
                    logError(error, 'copilot-overlay: expiry helper failed');
                }
            });
    }

    _writeAnswer(id, obj, {lifecycle = false, success = 'Answer sent'} = {}) {
        const v = this._views.get(id);
        if (!v || v.commitPending)
            return false;
        v.commitPending = true;
        const snapshot = JSON.stringify({request_id: id, lifecycle, answer: obj});
        // Exact retries reuse the id; editing a failed draft creates a new id.
        // A prepared WAL therefore cannot be resumed with different bytes.
        const submissionId = 'overlay-' + GLib.compute_checksum_for_string(
            GLib.ChecksumType.SHA256, snapshot, -1).slice(0, 24);
        const command = {
            schema: 'human-copilot.responder-command/v1', schema_version: 1,
            op: lifecycle ? 'submit-system' : 'submit-local', request_id: id,
            submission_id: submissionId, answer: obj,
        };
        let process;
        try {
            process = Gio.Subprocess.new(
                ['python3', this._responderPath, '--one-shot', '--spool', this._spool],
                Gio.SubprocessFlags.STDIN_PIPE | Gio.SubprocessFlags.STDOUT_PIPE |
                Gio.SubprocessFlags.STDERR_PIPE);
        } catch (error) {
            v.commitPending = false;
            logError(error, 'copilot-overlay: responder helper failed to start');
            this._osd('Copilot answer could not be committed' + this._tagOf(v));
            return false;
        }
        process.communicate_utf8_async(JSON.stringify(command) + '\n', null,
            (proc, result) => {
                let reply = null;
                try {
                    const [ok, stdout, stderr] = proc.communicate_utf8_finish(result);
                    if (!ok)
                        throw new Error(stderr || 'responder helper exited unsuccessfully');
                    const lines = (stdout || '').trim().split('\n').filter(Boolean);
                    if (lines.length !== 1)
                        throw new Error('responder helper returned invalid JSONL');
                    reply = JSON.parse(lines[0]);
                } catch (error) {
                    logError(error, 'copilot-overlay: responder helper failed');
                }
                const current = this._views?.get(id);
                if (current)
                    current.commitPending = false;
                if (!this._enabled)
                    return;
                const event = reply?.event?.type;
                if (reply?.ok === true) {
                    this._answered.add(id);
                    this._osd(success + this._tagOf(v));
                    this._dropView(id);
                    this._layout();
                    return;
                }
                if (event === 'already-answered') {
                    this._answered.add(id);
                    this._osd('Another responder already answered — nothing overwritten' +
                              this._tagOf(v));
                    this._dropView(id);
                } else if (event === 'conflict') {
                    if (current)
                        current.remoteLeased = true;
                    this._osd('Another responder owns this request — draft retained' +
                              this._tagOf(v));
                } else {
                    this._osd('Copilot answer was not sent' +
                              (event ? ` (${event})` : '') + this._tagOf(v));
                }
                this._layout();
            });
        return true;
    }

    // -- views --------------------------------------------------------------
    // A view is a request's WHOLE surface state: its parsed steps, the
    // operator's ticks, notes, snips and option selection, and the target
    // watch. Views are per REQUEST, so an answer, decline, withdrawal or
    // error on one can never reset or reroute another's.
    _makeView(req) {
        const parsed = parseSteps(req.summary);
        const {mode, options} = parseResponse(req);
        const stamped = req.responder?.schema ===
            'human-copilot.responder-contract/v1' &&
            req.responder.schema_version === 1 &&
            Array.isArray(req.responder.steps) ? req.responder.steps : null;
        const steps = stamped ? stamped.map((spec, index) => ({
            ...(parsed.steps[index] || {sub: [], done: false, failed: false, note: ''}),
            id: spec.id,
            text: typeof spec.instruction === 'string' ? spec.instruction : '',
            see: typeof spec.observation === 'string' ? spec.observation : '',
            spot: spec.target && typeof spec.target === 'object' ? spec.target : null,
        })) : parsed.steps;
        return {
            id: req.id,
            req,
            steps,
            intro: parsed.intro,
            introCode: parsed.introCode,
            mode, options,
            sel: -1,             // chosen option (choice mode), -1 = none yet
            note: '',            // request-level reply (text/choice/ack)
            images: [],          // [{step, mime, b64}]
            windowBindings: new WindowBindings(),
            targetQuery: null,   // the locator the target watch is on
            unresolvedSince: null,
            remoteLeased: false,
            commitPending: false,
        };
    }

    _dropView(id) {
        const card = this._cards.get(id);
        if (card) {
            card.box.destroy();
            this._cards.delete(id);
        }
        this._clearSpot(id);     // its OWN frame, nobody else's
        this._views.delete(id);
        if (this._pinned === id)
            this._pinned = null;
        if (this._req?.id === id)
            this._req = null;
        if (this._noteReq === id)
            this._dropNoteGrab();     // withdrawn mid-note frees the keyboard
    }

    // -- layout -------------------------------------------------------------
    // One pass computes every card's geometry and content, reuses what is
    // already drawn, and draws each spotlight frame. Per request: a targeted
    // card beside its window; everything else in the top-middle stack.
    _layout() {
        const records = this._windowRecords();
        const monitors = this._monitorRecords();
        const now = GLib.get_monotonic_time() / 1e6;
        const views = [...this._views.values()]
            .sort((a, b) => (Number(a.req.created) - Number(b.req.created)) ||
                  String(a.id).localeCompare(String(b.id)));
        // resolve targets first: a targeted card claims geometry beside its
        // window; anything untargeted (or unresolved) stacks top-middle
        const plans = new Map();       // id -> {rect, frame, res, entry, number,…}
        const stack = [];              // {v, plan} pairs, in created order
        const occupied = [];
        for (const v of views) {
            this._publishTarget(v, records, monitors);
            if (v.remoteLeased) {
                this._clearSpot(v.id);
                continue;
            }
            const inst = this._instanceOf(v);
            const cur = currentStep(v.steps);
            // Once every checklist step is resolved the operator still sees
            // the card until a deliberate send. Keep its last window anchor
            // during that final transition; WindowBindings retains the same
            // Mutter identity through a rename or popup.
            const completed = cur >= 0 ? v.steps.slice(0, cur + 1) : v.steps;
            const spot = (cur >= 0 && v.steps[cur].spot) ||
                [...completed].reverse().find(s => s.spot)?.spot || null;
            const plan = {rect: null, frame: null, res: null, spot, cur,
                          entry: inst.entry, number: inst.number, dropped: 0};
            if (spot && spot.window) {
                const res = v.windowBindings.resolve(spot.window, records);
                plan.res = res;
                if (res.status === 'unique') {
                    v.targetQuery = spot.window;
                    v.unresolvedSince = null;
                    const work = this._workAreaFor(res.window.monitor);
                    plan.frame = spotRect(res.window.frame, spot);
                    plan.rect = placeNearTarget(res.window.frame, OVERLAY_W,
                        this._cardHeight(v), work, occupied, plan.frame);
                    occupied.push(plan.rect);
                } else {
                    // no match / ambiguous: say it on a stack card, run the
                    // grace clock, then error back to the engine — never a
                    // corner fallback, never a guess among several windows
                    if (v.targetQuery !== spot.window || v.unresolvedSince === null)
                        v.unresolvedSince = now;
                    v.targetQuery = spot.window;
                    if (targetExpired(v.unresolvedSince, now)) {
                        this._writeAnswer(
                            v.id, targetErrorAnswer(res, spot.window), {
                                lifecycle: true,
                                success: 'Copilot target error (' + res.status +
                                    ') — the request went back to the agent',
                            });
                        continue;
                    }
                }
            } else {
                v.unresolvedSince = null;
                if (spot) {           // a region-only locator still frames
                    const mon = Main.layoutManager.primaryMonitor;
                    plan.frame = spotRect({x: mon.x, y: mon.y, width: mon.width,
                                           height: mon.height}, spot);
                }
            }
            if (plan.rect)
                plans.set(v.id, plan);
            else
                stack.push({v, plan});
        }
        // the top-middle stack for everything without a placed window target
        if (stack.length) {
            const sizes = stack.map(({v}) => ({width: OVERLAY_W,
                                               height: this._cardHeight(v)}));
            const mon = Main.layoutManager.primaryMonitor;
            const work = this._workAreaFor(mon.index);
            const {rects, dropped} = stackCards(work, sizes, occupied);
            // rects is index-aligned with `stack` (null = that card did not
            // fit); the overflow count rides the LAST card that DID fit
            const lastPlaced = rects.reduce((acc, r, i) => r ? i : acc, -1);
            stack.forEach(({v, plan}, i) => {
                plan.rect = rects[i] || null;
                plan.dropped = dropped && i === lastPlaced ? dropped : 0;
                plans.set(v.id, plan);
                if (plan.rect)
                    occupied.push(plan.rect);
            });
        }
        // draw: cards whose content key changed are rebuilt; the rest only
        // follow their rect. A card with no rect (stack overflow) is NOT
        // drawn — its count is said on the last placed card, never clipped
        // silently, and it draws no frame either (a frame for an unreadable
        // ask would be an invitation with nothing attached).
        const drawn = new Set();
        for (const [id, plan] of plans) {
            if (!plan.rect)
                continue;
            drawn.add(id);
            this._drawCard(this._views.get(id), plan);
        }
        for (const id of [...this._cards.keys()]) {
            if (!drawn.has(id)) {
                const card = this._cards.get(id);
                card.box.destroy();
                this._cards.delete(id);
            }
        }
        // frames for every placed request that has one — targeted windows at
        // the same time, active one pulsing (reduced motion for the rest)
        for (const [id, plan] of plans) {
            const v = this._views.get(id);
            if (plan.frame && plan.rect && v) {
                this._drawSpot(id, plan.frame, plan.cur,
                               plan.entry, plan.number, this._req === v);
            } else {
                this._clearSpot(id);
            }
        }
    }

    _instanceOf(v) {
        const key = instanceKey(v.req.instance);
        const number = key ? this._roster.numberOf(key) : 0;
        return {number, entry: number > 0 ? entryFor(number) : null};
    }

    _cardHeight(v) {
        const card = this._cards.get(v.id);
        if (card)
            return card.box.get_preferred_height(OVERLAY_W)[1] || EST_CARD_H;
        return EST_CARD_H;
    }

    _workAreaFor(monIdx) {
        try {
            const wa = Main.layoutManager.getWorkAreaForMonitor(monIdx || 0);
            return {x: wa.x, y: wa.y, width: wa.width, height: wa.height};
        } catch (e) {
            const mon = Main.layoutManager.primaryMonitor;
            return {x: mon.x, y: mon.y, width: mon.width, height: mon.height};
        }
    }

    // Fresh geometry for the resolver — one record per window actor. Views
    // retain stableSequence bindings, never a title or a stale rectangle.
    _windowRecords() {
        const out = [];
        try {
            for (const actor of global.get_window_actors()) {
                const w = actor.meta_window;
                if (!w)
                    continue;
                const fr = w.get_frame_rect();
                out.push({title: w.get_title() || '',
                          wmClass: w.get_wm_class() || '',
                          wmInstance: w.get_wm_class_instance() || '',
                          userTime: w.get_user_time(), minimized: !!w.minimized,
                          monitor: w.get_monitor(),
                          stableSequence: String(w.get_stable_sequence()),
                          frame: {x: fr.x, y: fr.y,
                                  width: fr.width, height: fr.height}});
            }
        } catch (e) {
            logError(e, 'copilot-overlay: window scan failed');
        }
        return out;
    }

    _monitorRecords() {
        const out = [];
        const monitors = Main.layoutManager.monitors || [];
        for (let index = 0; index < monitors.length; index++) {
            const mon = monitors[index];
            let scale;
            try {
                scale = global.display.get_monitor_scale(index);
            } catch (_error) {
                scale = 1;
            }
            out.push({index, x: mon.x, y: mon.y, width: mon.width,
                      height: mon.height, scale});
        }
        return out;
    }

    // -- hotkeys ------------------------------------------------------------
    _onKey(name) {
        if (name === 'submit-answer' && this._noteReq) {
            // A modal St.Entry normally consumes the global accelerator. Make
            // the advertised submit chord work from inside the editor too.
            this._closeNote(true);
            this._submit();
            return;
        }
        if (this._noteStep >= 0 || this._noteReq) {
            // a rebuild mid-typing would destroy the entry under the cursor;
            // the note editor owns the keyboard until it closes
            this._osd('Finish the note first — Enter saves, Esc cancels');
            return;
        }
        if (name === 'toggle-glossary') {
            // a preference, not an action on the request — works regardless,
            // and persists so a hidden glossary stays hidden next request
            const vis = !this._settings.get_boolean('glossary-visible');
            this._settings.set_boolean('glossary-visible', vis);
            this._osd(vis ? 'Hotkey glossary shown' : 'Hotkey glossary hidden');
            this._layout();
            return;
        }
        const v = this._req;
        if (!v) {
            this._osd('No copilot request open');
            return;
        }
        if (v.remoteLeased) {
            this._osd('Another responder owns this request — draft retained' +
                      this._tagOf(v));
            return;
        }
        if (v.commitPending) {
            this._osd('Copilot answer is still being committed' + this._tagOf(v));
            return;
        }
        const steps = v.steps;
        const key = k => prettyAccel(this._settings.get_strv(k)[0]);
        const tag = this._tag();       // every OSD names the instance it acted on
        if (name === 'mark-done') {
            if (v.mode === 'choice') {
                if (!v.options.length) {
                    this._osd('No options arrived — answer in copilot-tui' + tag);
                    return;
                }
                v.sel = (v.sel + 1) % v.options.length;   // wraps: no dead end
                const o = v.options[v.sel];
                this._osd(`Option ${v.sel + 1}/${v.options.length}: ${o.label}` +
                          ` — ${key('submit-answer')} sends` + tag);
                this._layout();
                return;
            }
            if (v.mode === 'ack') {
                this._submit();        // confirm IS the done state
                return;
            }
            if (!steps.length) {
                this._openNote();
                return;
            }
            if (allResolved(steps)) {
                this._submit();        // the same chord, pressed once more, sends
                return;
            }
            const i = markNext(steps);
            this._osd(`Step ${i + 1}/${steps.length} done` +
                      (allResolved(steps)
                          ? ` — all answered: ${key('mark-done')} again sends`
                          : '') + tag);
        } else if (name === 'mark-fail') {
            if (v.mode !== 'checklist' || !steps.length) {
                this._osd('This request has no steps to fail — ' +
                          `${key('dismiss-request')} declines` + tag);
                return;
            }
            if (allResolved(steps)) {
                this._osd(`Every step is answered — ${key('undo-done')} to change one` + tag);
                return;
            }
            const i = markFail(steps);
            this._osd(`Step ${i + 1} FAILED — ${key('edit-note')} to say what you saw` +
                      (allResolved(steps) ? `, ${key('submit-answer')} sends` : '') + tag);
        } else if (name === 'undo-done') {
            const i = undoLast(steps);
            this._osd((i < 0 ? 'Nothing to undo' : `Step ${i + 1} unanswered again`) + tag);
        } else if (name === 'attach-snip') {
            this._attachClipboard(0);
            return;                    // refresh happens in the callback
        } else if (name === 'edit-note') {
            this._openNote();
            return;                    // _openNote refreshes with the entry in
        } else if (name === 'submit-answer') {
            this._submit();
            return;
        } else if (name === 'dismiss-request') {
            this._writeAnswer(
                v.id, {text: '', cancelled: true, images: []},
                {success: 'Copilot request declined'});
            return;
        }
        this._layout();
    }

    // The active request's instance as the operator knows it: its instance
    // number (the roster's — the palette registry's slot when there is one)
    // and the palette entry that number draws. {number: 0, entry: null} for
    // an unstamped request, which then wears the fallback accent.
    _active() {
        const v = this._req;
        return v ? this._instanceOf(v) : {number: 0, entry: null};
    }

    _tagOf(v) {
        const {number, entry} = this._instanceOf(v);
        return instanceSuffix(v.req.instance, number, entry);
    }

    // " · instance 3 hc-A (coral)": every OSD names the instance it acted on,
    // number and colour included.
    _tag() {
        const v = this._req;
        return v ? this._tagOf(v) : '';
    }

    _submit() {
        const v = this._req;
        if (!v)
            return;
        const steps = v.steps;
        const images = orderImages(v.images)
            .map(img => ({mime: img.mime, b64: img.b64}));
        if (v.mode === 'choice') {
            const answer = choiceAnswer(v.options, v.sel, v.note, images);
            if (!answer) {
                this._osd('Choose an option or type a reply — ' +
                          prettyAccel(this._settings.get_strv('mark-done')[0]) +
                          ' steps through them' + this._tag());
                return;
            }
            this._writeAnswer(v.id, answer,
                {success: answer.choice ? `Sent: option "${answer.choice.label}"`
                                        : 'Sent: reply without a selection'});
            return;
        }
        if (v.mode === 'checklist' && steps.length) {
            const done = steps.filter(s => s.done).length;
            const failed = steps.filter(s => s.failed).length;
            const answer = {
                text: composeAnswer(steps, v.images),
                cancelled: false, images};
            const checklist = checklistAnswer(v.req, steps);
            if (checklist)
                answer.checklist = checklist;
            this._writeAnswer(v.id, answer, {
                success: `Sent: ${done}/${steps.length} done` +
                    (failed ? `, ${failed} failed` : '') +
                    ` · ${v.images.length} snip(s)`,
            });
            return;
        }
        // text / prose / ack: the note IS the answer. An empty send is an
        // answer only for an explicitly confirmation-only request (hcop-0gj).
        const empty = emptySubmitNote(v.mode, looksLikeChoices(v.req.summary));
        if (!(v.note || '').trim() && !images.length && empty !== null) {
            this._osd(empty + ' · add your reply here' + this._tag());
            return;
        }
        this._writeAnswer(v.id, {
            text: (v.note || '').trim(), cancelled: false, images}, {
            success: 'Sent' + (images.length ? ` · ${images.length} snip(s)` : ''),
        });
    }

    _attachClipboard(mimeIdx) {
        const v = this._req;
        if (!v)
            return;
        if (mimeIdx >= IMAGE_MIMES.length) {
            this._osd('Clipboard holds no image — Print-snip first');
            return;
        }
        const mime = IMAGE_MIMES[mimeIdx];
        St.Clipboard.get_default().get_content(St.ClipboardType.CLIPBOARD, mime,
            (_cb, bytes) => {
                const view = this._views.get(v.id) || this._req;
                if (!view)
                    return;            // withdrawn while the clipboard answered
                const data = bytes?.get_data();
                if (!data || !data.length) {
                    this._attachClipboard(mimeIdx + 1);
                    return;
                }
                const step = currentStep(view.steps);
                view.images.push({step, mime, b64: GLib.base64_encode(data)});
                const where = step >= 0 ? ` → step ${step + 1}` : '';
                this._flashMsg(`📸 snip attached (#${view.images.length}${where})`);
                this._osd(`Snip attached (#${view.images.length}${where})` +
                          this._tagOf(view));
                this._layout();
            });
    }

    // -- per-request note ---------------------------------------------------
    // The one moment the overlay takes keyboard focus: an St.Entry opens
    // under the ▶ step (checklist) or at the foot of the card (text, choice,
    // ack), a modal grab routes keys to it. Enter saves and Esc cancels;
    // Super+Alt+Return submits the current text directly. While it is open,
    // ordinary scans refuse to rebuild (a rebuild would destroy the entry
    // mid-keystroke), but lifecycle scans still retire expired/answered cards.
    _openNote() {
        const v = this._req;
        if (!v)
            return;
        if (v.mode === 'checklist' && v.steps.length) {
            this._noteStep = currentStep(v.steps);
            if (this._noteStep < 0) {
                this._osd('This request has no numbered steps to note');
                return;
            }
        } else {
            this._noteStep = -1;
        }
        this._noteReq = v.id;
        this._layout();                // builds the card WITH the entry in it
        if (!this._entry) {            // refresh failed some way; don't wedge
            this._noteStep = -1;
            this._noteReq = null;
            return;
        }
        try {
            this._grab = Main.pushModal(this._entry,
                {actionMode: Shell.ActionMode.ALL});
        } catch (e) {
            logError(e, 'copilot-overlay: could not grab for the note entry');
            this._noteStep = -1;
            this._noteReq = null;
            this._entry = null;
            this._osd('Could not take the keyboard for the note');
            this._layout();
            return;
        }
        this._entry.grab_key_focus();
        const ct = this._entry.clutter_text;
        ct.connect('activate', () => this._closeNote(true));
        ct.connect('key-press-event', (_a, ev) => {
            const symbol = ev.get_key_symbol();
            const state = ev.get_state();
            const superMask = Clutter.ModifierType.SUPER_MASK ??
                Clutter.ModifierType.MOD4_MASK;
            if ((symbol === Clutter.KEY_Return || symbol === Clutter.KEY_KP_Enter) &&
                    (state & superMask) &&
                    (state & Clutter.ModifierType.MOD1_MASK)) {
                this._closeNote(true);
                this._submit();
                return Clutter.EVENT_STOP;
            }
            if (symbol === Clutter.KEY_Escape) {
                this._closeNote(false);
                return Clutter.EVENT_STOP;
            }
            return Clutter.EVENT_PROPAGATE;
        });
    }

    _closeNote(save) {
        if (!this._noteReq)
            return;
        const v = this._views.get(this._noteReq);
        if (save && this._entry && v) {
            if (this._noteStep >= 0 && v.steps[this._noteStep])
                v.steps[this._noteStep].note = this._entry.get_text();
            else
                v.note = this._entry.get_text();
        }
        this._dropNoteGrab();
        this._osd((save ? 'Note saved' : 'Note unchanged') +
                  (v ? this._tagOf(v) : ''));
        this._layout();
    }

    _dropNoteGrab() {
        if (this._grab) {
            Main.popModal(this._grab);
            this._grab = null;
        }
        this._noteStep = -1;
        this._noteReq = null;
        this._entry = null;
    }

    // The OSD's icon is the active instance's swatch — its colour with its
    // number on it (lib.js::swatchSvg as a Gio.BytesIcon) — because the OSD's
    // text cannot be tinted; the text still names number, session and colour
    // in words (_tag). No active request, or anything failing here → the
    // plain eye icon: an OSD must never be lost to its decoration.
    _osd(text) {
        Main.osdWindowManager.showAll(this._osdIcon(), text);
    }

    _osdIcon() {
        const {number, entry} = this._active();
        if (entry) {
            try {
                const svg = new TextEncoder().encode(swatchSvg(entry, number));
                return Gio.BytesIcon.new(GLib.Bytes.new(svg));
            } catch (e) {
                logError(e, 'copilot-overlay: swatch icon failed; using the plain one');
            }
        }
        return Gio.ThemedIcon.new('view-reveal-symbolic');
    }

    _flashMsg(text) {
        this._flash = {text, until: GLib.get_monotonic_time() / 1e6 + 5};
        if (this._flashTimer)
            GLib.source_remove(this._flashTimer);
        this._flashTimer = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, 5,
            () => {
                this._flashTimer = 0;
                this._flash = null;
                this._layout();
                return GLib.SOURCE_REMOVE;
            });
    }

    // -- drawing ------------------------------------------------------------
    // Everything a card renders goes into its key: identical key -> the card
    // only follows its rect; changed key -> rebuilt. The countdown is NOT in
    // the key — its label is set in place each scan so the 2 s tick never
    // rebuilds a widget tree just to move a digit.
    _label(text, style) {
        const label = new St.Label({text, style});
        label.clutter_text.set_line_wrap(true);
        // WORD_CHAR: an unbreakable run (a Wine `Z:\...` path has no spaces)
        // falls back to a mid-word break instead of overflowing the card
        // uncut — hcop-7os.
        label.clutter_text.set_line_wrap_mode(2 /* Pango.WrapMode.WORD_CHAR */);
        label.clutter_text.set_ellipsize(0 /* Pango.EllipsizeMode.NONE */);
        return label;
    }

    _copyButton(v, value, label = 'Copy') {
        const button = new St.Button({
            label, can_focus: true, reactive: true,
            y_align: Clutter.ActorAlign.START,
            style_class: 'button',
            style: 'font-size: 9pt; padding: 2px 6px; margin-left: 6px;',
        });
        button._hcCopy = true;
        button.connect('clicked', () => {
            try {
                St.Clipboard.get_default().set_text(St.ClipboardType.CLIPBOARD, value);
                this._osd(`${label === 'Copy code' ? 'Code' : 'Text'} copied` +
                          this._tagOf(v));
            } catch (e) {
                logError(e, 'copilot-overlay: clipboard write failed');
                this._osd('Copy failed' + this._tagOf(v));
            }
        });
        return button;
    }

    _copyRow(v, display, code, style, plain = display) {
        if (!display)
            return this._label(display, style);
        const row = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL, x_expand: true,
        });
        const content = this._label(display, style);
        content.set_x_expand(true);
        row.add_child(content);
        row.add_child(this._copyButton(v, code || plain,
                                       code ? 'Copy code' : 'Copy'));
        return row;
    }

    _cardKey(v, plan) {
        const marks = v.steps.map(s =>
            (s.done ? 'd' : s.failed ? 'f' : 'o') + (s.note || '').length).join('');
        return [v.id, v.mode, v.options.length, v.sel,
                (v.note || '').trim().length, marks, v.images.length,
                v.intro.length, v.steps.length,
                plan.cur ?? -1, plan.entry ? plan.entry.hex : '',
                plan.number, plan.dropped || 0,
                plan.res ? plan.res.status : 'untargeted',
                v.unresolvedSince !== null
                    ? Math.max(0, Math.ceil(TARGET_GRACE_S -
                        (GLib.get_monotonic_time() / 1e6 - v.unresolvedSince))) : -1,
                this._req === v ? 1 : 0,
                this._settings.get_boolean('glossary-visible') ? 1 : 0,
                this._flash && GLib.get_monotonic_time() / 1e6 < this._flash.until
                    ? this._flash.text : '',
                this._noteReq === v.id ? this._noteStep : -2,
        ].join('|');
    }

    _drawCard(v, plan) {
        const key = this._cardKey(v, plan);
        let card = this._cards.get(v.id);
        if (card && card.key !== key) {
            card.box.destroy();
            card = null;
            this._cards.delete(v.id);
        }
        if (!card) {
            const built = this._buildCard(v, plan);
            Main.layoutManager.addTopChrome(built.box);
            card = {box: built.box, header: built.header, key};
            this._cards.set(v.id, card);
        }
        card.box.set_position(plan.rect.x, plan.rect.y);
        // the countdown rides the header label without a rebuild
        if (card.header)
            card.header.set_text(overlayHeader(v.req, undefined, plan.number));
    }

    _headerBox(v, plan) {
        const accent = plan.entry ? plan.entry.hex : SPOT_COLOR;
        const active = this._req === v;
        const line = new St.BoxLayout({
            orientation: Clutter.Orientation.HORIZONTAL, x_expand: true,
        });
        // the instance badge: colour + NUMBER + session name — identity is
        // never colour alone (the ▶ marks the card the chords act on)
        const badge = new St.Label({
            text: cardBadge(v.req.instance, plan.number, active),
            style: (plan.entry
                ? `background-color: ${plan.entry.hex}; color: ${plan.entry.fg};`
                : 'background-color: #4a5260; color: #e8edf3;') +
                   'font-size: 10pt; font-weight: bold; border-radius: 6px;' +
                   'padding: 1px 7px; margin-right: 8px;',
        });
        line.add_child(badge);
        const header = this._label(overlayHeader(v.req, undefined, plan.number),
            `font-weight: bold; color: ${plan.entry ? accent : '#8ec6ff'};`);
        line.add_child(header);
        return {line, header};
    }

    _buildCard(v, plan) {
        const active = this._req === v;
        const accent = plan.entry ? plan.entry.hex : SPOT_COLOR;
        const now = GLib.get_monotonic_time() / 1e6;
        const box = new St.BoxLayout({
            orientation: Clutter.Orientation.VERTICAL,
            reactive: true,                 // a click selects (routes chords)
            width: OVERLAY_W,
            style: 'background-color: rgba(16,20,28,0.92); color: #d7dce3;' +
                   `border: ${active ? '2' : '1'}px solid ` +
                   (plan.entry ? accent : 'rgba(120,180,255,0.55)') + ';' +
                   'border-radius: 10px; padding: 12px 14px;' +
                   'font-size: 12.5pt;' + (active ? '' : 'opacity: 0.92;'),
        });
        box.connect('button-press-event', (_actor, event) => {
            for (let source = event.get_source(); source && source !== box;
                    source = source.get_parent()) {
                if (source._hcCopy)
                    return Clutter.EVENT_PROPAGATE;
            }
            if (this._req !== v) {
                this._pinned = v.id;
                this._osd('Copilot: your keys act here now' + this._tagOf(v));
                this._scan();
            }
            return Clutter.EVENT_PROPAGATE;
        });
        const {line: head, header} = this._headerBox(v, plan);
        header.set_x_expand(true);
        head.add_child(this._copyButton(v, v.req.summary, 'Copy all'));
        box.add_child(head);

        // unresolved target: say it, amber, with the time the engine error
        // fires — the operator always knows why a card is about to vanish
        if (plan.res && plan.res.status !== 'unique') {
            const left = Math.max(0, Math.ceil(TARGET_GRACE_S -
                (now - (v.unresolvedSince ?? now))));
            const where = plan.res.status === 'ambiguous'
                ? `“${v.targetQuery}” matches ${plan.res.matches.length} windows`
                : `no window matching “${v.targetQuery}”`;
            box.add_child(this._label(
                `⌖ ${where} — sent back to the agent in ${left}s`,
                'color: #f0a35e; font-weight: bold;'));
        }
        const queued = queuedBehind([v.req], instanceKey(v.req.instance), v.id);
        if (queued)
            box.add_child(this._label(
                `+${queued} more from this instance — answer in copilot-tui`,
                'color: #8a94a2; font-size: 9pt; font-style: italic;'));

        // intro is capped so a long preamble can't push the checklist off the
        // screen — but the cut is SHOWN, never silent: a dropped line could be
        // the one that says what to look at
        const INTRO_MAX = 6;
        const SUB_MAX = 6;
        const isChoice = v.mode === 'choice';
        for (let i = 0; i < Math.min(v.intro.length, INTRO_MAX); i++)
            box.add_child(this._copyRow(v, v.intro[i], v.introCode[i],
                'color: #aab4c0; font-size: 11.5pt;'));
        if (v.intro.length > INTRO_MAX)
            box.add_child(this._label(
                `… +${v.intro.length - INTRO_MAX} more lines (full text in copilot-tui)`,
                'color: #8a94a2; font-size: 9pt; font-style: italic;'));

        if (isChoice) {
            // Keyboard mark-done / submit-answer is the only selection path.
            // The row's button copies its text; pressing the rest of the row
            // must not select an option or switch the active card.
            const optionBox = new St.BoxLayout({
                orientation: Clutter.Orientation.VERTICAL,
                x_expand: true,
            });
            v.options.forEach((o, i) => {
                const sel = i === v.sel;
                const row = this._copyRow(v,
                    choiceDisplayText(o, i, sel),
                    null,
                    (sel
                        ? `background-color: ${accent}; color: ${plan.entry ? plan.entry.fg : '#1a1a1a'};`
                          + 'font-weight: bold;'
                        : 'color: #e8edf3;') +
                        'border-radius: 6px; padding: 3px 6px; margin-top: 3px;',
                    `${o.label}${o.hint ? ` — ${o.hint}` : ''}`);
                row.set_reactive(true);
                row.connect('button-press-event', (_actor, event) => {
                    for (let source = event.get_source(); source && source !== row;
                            source = source.get_parent()) {
                        if (source._hcCopy)
                            return Clutter.EVENT_PROPAGATE;
                    }
                    return Clutter.EVENT_STOP;
                });
                row.set_x_expand(true);
                optionBox.add_child(row);
            });
            if (v.options.length) {
                box.add_child(new St.ScrollView({
                    child: optionBox,
                    hscrollbar_policy: St.PolicyType.NEVER,
                    vscrollbar_policy: St.PolicyType.AUTOMATIC,
                    overlay_scrollbars: true,
                    enable_mouse_scrolling: true,
                    x_expand: true,
                    style: `max-height: ${CHOICE_VIEW_H}px;`,
                }));
            }
            if (choiceNeedsScroll(v.options))
                box.add_child(this._label(
                    `↕ scroll for all ${v.options.length} complete options`,
                    'color: #8a94a2; font-size: 8.5pt; margin-top: 2px;'));
            if (!v.options.length)
                box.add_child(this._label(
                    'no options arrived — answer in copilot-tui',
                    'color: #f0a35e;'));
            if (this._noteReq === v.id && this._noteStep === -1)
                box.add_child(this._noteEntry(v, null));
            else if ((v.note || '').trim())
                box.add_child(this._label(`✎ ${v.note.trim()}`,
                    'color: #d3b874; font-size: 11.5pt;'));
        } else {
            for (let i = 0; i < v.steps.length; i++) {
                const s = v.steps[i];
                const isCur = i === plan.cur;
                const glyph = s.done ? '☑' : s.failed ? '✘' : '☐';
                box.add_child(this._copyRow(v,
                    `${isCur ? '▶' : ' '} ${glyph} ${i + 1}. ${s.text}`,
                    s.copyCode,
                    s.done ? 'color: #6fd08c; margin-top: 4px;'
                           : s.failed ? 'color: #f28b82; margin-top: 4px;'
                           : isCur
                           ? 'color: #e8edf3; font-weight: bold; margin-top: 4px;'
                           : 'color: #aab4c0; margin-top: 4px;', s.text));
                if (s.see)
                    box.add_child(this._copyRow(v, `👁 ${s.see}`, null,
                        (isCur ? `color: ${accent}; font-weight: bold;`
                               : 'color: #9c8a4a;') +
                        'font-size: 11.5pt; margin-left: 18px;', s.see));
                if (s.spot) {
                    const where = spotLabel(s.spot);
                    const miss = isCur && plan.res && plan.res.status !== 'unique';
                    box.add_child(this._label(
                        `⌖ ${where}` + (miss ? ` — ${plan.res.status === 'ambiguous'
                                              ? 'several windows match' : 'window not found'}` : ''),
                        (miss ? 'color: #f0a35e;'
                              : isCur ? `color: ${accent};` : 'color: #78828e;') +
                        'font-size: 9pt; margin-left: 18px;'));
                }
                const sub = s.sub || [];
                for (const b of sub.slice(0, SUB_MAX))
                    box.add_child(this._copyRow(v, b.text, b.copyCode,
                        'color: #9aa6b4; font-size: 9.5pt; margin-left: ' +
                        `${14 + (b.level - 1) * 14}px;`));
                if (sub.length > SUB_MAX)
                    box.add_child(this._label(
                        `… +${sub.length - SUB_MAX} more lines (full text in copilot-tui)`,
                        'color: #8a94a2; font-size: 9pt; font-style: italic;' +
                        'margin-left: 14px;'));
                if (this._noteReq === v.id && this._noteStep === i) {
                    box.add_child(this._noteEntry(v, s));
                } else if ((s.note || '').trim()) {
                    box.add_child(this._label(`✎ ${s.note.trim()}`,
                        'color: #d3b874; font-size: 11.5pt; margin-left: 18px;'));
                }
                const snips = v.images.filter(img => img.step === i).length;
                if (snips)
                    box.add_child(this._label(`📎 ${snips} snip(s)`,
                        'color: #6fa8d0; font-size: 9.5pt; margin-left: 18px;'));
            }
            if (this._noteReq === v.id && this._noteStep === -1)
                box.add_child(this._noteEntry(v, null));
            else if ((v.mode !== 'checklist' || !v.steps.length) && (v.note || '').trim())
                box.add_child(this._label(`✎ ${v.note.trim()}`,
                    'color: #d3b874; font-size: 9.5pt;'));
        }
        if (v.images.length && !isChoice)
            box.add_child(this._label(
                `📎 ${v.images.length} snip(s) attached`,
                'color: #6fd08c; margin-top: 6px;'));
        if (plan.dropped)
            box.add_child(this._label(
                `… +${plan.dropped} more request(s) waiting — answer some to see them`,
                'color: #f0a35e; font-weight: bold; margin-top: 6px;'));
        if (this._flash && now < this._flash.until)
            box.add_child(this._label(this._flash.text,
                `color: ${accent}; font-weight: bold; margin-top: 6px;`));
        // NOW: the one chord that moves THIS request forward, from the live
        // bindings — mode-aware (choice/ack/checklist)
        const accels = {};
        for (const k of KEYS)
            accels[k] = prettyAccel(this._settings.get_strv(k)[0]);
        const hint = modeHint(v.mode, v.steps, accels, v.sel, v.options.length);
        box.add_child(this._label(`NOW  ${hint.main}`,
            `color: ${accent}; font-weight: bold; margin-top: 8px;` +
            'border-top: 1px solid rgba(120,180,255,0.25); padding-top: 6px;'));
        box.add_child(this._label(hint.more,
            'color: #8a94a2; font-size: 8.5pt; margin-top: 2px;'));
        if (active) {
            if (this._settings.get_boolean('glossary-visible')) {
                box.add_child(this._label(GUIDE,
                    'color: #8ec6ff; font-size: 8.5pt; font-weight: bold;' +
                    'margin-top: 8px;'));
                for (const [key, what] of GLOSSARY)
                    box.add_child(this._label(`${accels[key]}  —  ${what}`,
                        'color: #78828e; font-size: 8.5pt; margin-top: 1px;'));
            } else {
                box.add_child(this._label(
                    `${accels['toggle-glossary']}  —  all keys`,
                    'color: #78828e; font-size: 8.5pt; margin-top: 4px;'));
            }
        }
        return {box, header};
    }

    _noteEntry(v, step) {
        const text = step ? (step.note || '') : (v.note || '');
        const entry = new St.Entry({
            text,
            hint_text: step ? 'what you saw — Super+Alt+Return sends, Enter saves, Esc cancels'
                            : 'your reply — Super+Alt+Return sends, Enter saves, Esc cancels',
            can_focus: true,
            reactive: true,
            x_expand: true,
            style: 'font-size: 11.5pt; margin: 2px 0 2px 18px;' +
                   'background-color: rgba(255,255,255,0.08);' +
                   'color: #e8edf3; border-radius: 6px;' +
                   'border: 1px solid rgba(120,180,255,0.55);' +
                   'padding: 3px 6px;',
        });
        this._entry = entry;
        return entry;
    }

    // submit a SPECIFIC view (an option row answers its own card, active or
    // not) — routing the chords stays a separate concern from answering.
    _submitView(v) {
        const prev = this._req;
        this._req = v;
        this._submit();
        this._req = this._views.has(prev?.id) ? prev : null;
    }

    // -- spotlight ----------------------------------------------------------
    // Every resolved targeted request (and every region-only locator) draws
    // its OWN frame, named for and owned by its REQUEST. Activity only picks
    // which frame pulses — motion is attention, and non-active frames stay
    // steady (reduced motion without losing identity). An existing frame
    // FOLLOWS its target: same actors, new geometry.
    //
    // 2026-09-02 (hcop-o9m): on GNOME 50 `addTopChrome(actor, {…})` throws
    // "Unrecognized parameter" — AFTER the actor is already a child of
    // uiGroup. Three rules: (1) no params — the actors are reactive:false;
    // (2) record the actors in the ledger BEFORE anything that can throw,
    // and destroy them if it does; (3) four solid edge bars, never one
    // widget with a border/box-shadow.
    _drawSpot(owner, rect, step, entry = null, number = 0, pulse = true) {
        const existing = this._spots.get(owner);
        if (existing && existing.bars.length === 4 && existing.label && rect) {
            const spec = spotFrameSpec(rect, step, entry, number);
            spec.bars.forEach((b, i) => {
                existing.bars[i].set_position(b.x, b.y);
                existing.bars[i].set_size(b.width, b.height);
            });
            existing.label.set_text(spec.label.text);
            existing.label.set_position(spec.label.x, spec.label.y);
            existing.rect = rect;
            this._setSpotPulse(owner, existing, pulse);
            return;
        }
        this._clearSpot(owner);
        if (!rect)
            return;
        const spec = spotFrameSpec(rect, step, entry, number);
        const bars = spec.bars.map(b => new St.Widget({
            name: spec.name, reactive: false,
            x: b.x, y: b.y, width: b.width, height: b.height,
            style: `background-color: ${b.color};`,
        }));
        const chip = new St.Label({
            name: spec.name, reactive: false,
            text: spec.label.text,
            style: `background-color: ${spec.label.bg}; color: ${spec.label.fg};` +
                   'font-weight: bold; font-size: 9.5pt; padding: 1px 7px;' +
                   'border-radius: 6px;',
        });
        const actors = [...bars, chip];
        const frame = {actors, bars, label: chip, rect};
        this._spots.set(owner, frame);
        try {
            for (const a of actors)
                Main.layoutManager.addTopChrome(a);
        } catch (err) {
            logError(err, 'copilot-overlay: could not draw the spotlight');
            this._clearSpot(owner);
            return;
        }
        chip.set_position(spec.label.x, spec.label.y);
        this._setSpotPulse(owner, frame, pulse);
    }

    _setSpotPulse(owner, frame, pulse) {
        if (frame.pulsing === pulse)
            return;                    // geometry updates keep the current beat
        frame.pulsing = pulse;
        const generation = frame.pulseGeneration = (frame.pulseGeneration || 0) + 1;
        for (const b of frame.bars) {
            b.remove_all_transitions();
            b.opacity = 255;
        }
        if (!pulse)
            return;
        const beat = dim => {
            // An old completion may arrive after A -> B -> A. Frame identity
            // alone cannot distinguish that stale beat from the new pulse.
            if (this._spots.get(owner) !== frame || !frame.pulsing ||
                frame.pulseGeneration !== generation)
                return;
            for (const b of frame.bars) {
                b.ease({
                    opacity: dim ? 110 : 255, duration: 650,
                    mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                    onComplete: b === frame.bars[0] ? () => beat(!dim) : null,
                });
            }
        };
        beat(true);
    }

    // Take back the frame owned by request `id` — and ONLY that one: a
    // declined, answered, withdrawn or errored request never touches
    // another's. With no id (disable()), every frame goes.
    _clearSpot(id) {
        const frames = id === undefined ? this._spots.takeAll()
                                        : [this._spots.take(id)].filter(Boolean);
        for (const {actors} of frames) {
            for (const a of actors) {
                a.remove_all_transitions();
                a.destroy();
            }
        }
    }

    // Actors a previous load of this extension left in uiGroup (a throw between
    // add_child and our bookkeeping, or a shell that reloaded us without
    // disable()). Ours are found by NAME (lib.js::isSpotActor — whatever
    // palette colour they wear, so the sweep never depends on the table); the
    // pre-fix leak (hcop-o9m) carried only the old yellow in an inline style
    // and no name, so that one colour is still matched as the documented
    // fallback.
    _sweepOrphanSpots() {
        for (const a of Main.layoutManager.uiGroup.get_children()) {
            const style = typeof a.get_style === 'function' ? (a.get_style() || '') : '';
            if (isSpotActor(a.name, a instanceof St.Widget ? style : ''))
                a.destroy();
        }
    }
}
