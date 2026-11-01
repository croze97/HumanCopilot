// drive_overlay.js — the overlay's shell-free logic under plain gjs:
//     gjs -m tests/drive_overlay.js
// (extension.js itself can only run inside gnome-shell; its syntax is smoked
// separately — see selftest notes in CLAUDE.md.)
import {parseSteps, markNext, markFail, undoLast, currentStep, allResolved,
        orderImages, composeAnswer, checklistAnswer, prettyAccel, plainInline, singleInlineCode, tableCells,
        isDelimRow, parseLook, parseSpot, spotRect, spotLabel, regionOf,
        nowHint, overlayHeader, RequestFileCache, focusTarget, pickActive,
        instanceKey, instanceLabel, instanceSuffix, OpenLedger, InstanceRoster,
        queuedBehind, withdrawnText, cardBadge,
        arrivedText, pruneStash, registrySlots, instanceName, spotFrameSpec,
        isSpotActor, SpotLedger, swatchSvg, SPOT_FALLBACK,
        SPOT_NAME, SPOT_EDGE, parseResponse, looksLikeChoices, emptySubmitNote,
        matchWindows, resolveTarget, WindowBindings, targetExpired, targetErrorAnswer,
        monitorPhysicalRect, canonicalTarget, canonicalTargetVector,
        advanceGeometry, remoteLeaseActive,
        placeNearTarget, stackCards, TARGET_GRACE_S, modeHint,
        choiceAnswer, choiceDisplayText, choiceNeedsScroll, answeredRequestIds,
        procStatIdentity, callerProcessState, CALLER_PROCESS_SCHEMA,
        requestExpired, monitorEventRescans, ExpiryBackoff, StatusWriter}
    from '../overlay/copilot-overlay@croze/lib.js';
import {PALETTE, entryFor, OVERLAY_BG} from '../overlay/copilot-overlay@croze/palette.js';
import GLib from 'gi://GLib';
import {checkCropVectors} from './crop_vectors.js';

let n = 0;
function ok(cond, what) {
    if (!cond) {
        printerr(`FAIL: ${what}`);
        imports.system.exit(1);
    }
    n++;
    print(`  ok: ${what}`);
}

const cropVectorCount = checkCropVectors();
const optionsForReply = [{value: 'fixed', label: 'Fixed'}];
ok(choiceAnswer(optionsForReply, -1, '') === null,
   'empty unselected choice never manufactures an answer');
const objection = choiceAnswer(optionsForReply, -1, 'I do not know how');
ok(!objection.choice && objection.text === 'I do not know how' &&
   objection.response_completion.status === 'not_done',
   'choice note without selection preserves objection as incomplete');
ok(choiceAnswer(optionsForReply, 0, 'checked').choice.value === 'fixed',
   'explicitly selected choice remains exact');
ok(cropVectorCount > 0, `shared crop arithmetic: ${cropVectorCount} vectors`);

const answerIds = answeredRequestIds([
    'aaaaaaaaaaaa.json', 'bbbbbbbbbbbb.json.tmp-1', 'notes.txt',
    'cccccccccccc.json',
]);
ok([...answerIds].join(',') === 'aaaaaaaaaaaa,cccccccccccc',
   'only complete answer files suppress their matching request cards');
const procFields = ['S', ...Array(18).fill('0'), '99'];
const procLine = `4242 (worker ) with spaces) ${procFields.join(' ')}`;
const caller = {
    schema: CALLER_PROCESS_SCHEMA, schema_version: 1, pid: 4242,
    boot_id: 'boot-a', start_ticks: '99',
};
ok(procStatIdentity(procLine).start_ticks === '99' &&
   callerProcessState(caller, 'boot-a', procLine) === 'alive',
   'overlay caller proof parses PID start ticks without trusting the process name');
ok(callerProcessState({...caller, start_ticks: '98'}, 'boot-a', procLine) === 'dead' &&
   callerProcessState(caller, 'older-boot', procLine) === 'dead' &&
   callerProcessState(caller, 'boot-a', null) === 'dead',
   'PID reuse, reboot and a missing process are conclusive dead-owner evidence');
ok(callerProcessState({}, 'boot-a', procLine) === 'unknown' &&
   callerProcessState(caller, 'boot-a', undefined) === 'unknown' &&
   callerProcessState(caller, null, procLine) === 'unknown',
   'legacy metadata and unreadable kernel evidence stay ambiguous and are preserved');
ok(requestExpired({deadline: 99}, 100) &&
   !requestExpired({deadline: 101}, 100) &&
   !requestExpired({deadline: '99'}, 100) &&
   !requestExpired({deadline: 99}, Number.NaN),
   'only a valid elapsed request deadline marks a card expired');

// ---- immutable request spool cache
const requestFiles = new Map([
    ['a.json', JSON.stringify({id: 'a', summary: '1. first'})],
    ['b.json', JSON.stringify({id: 'b', summary: '1. second'})],
    ['bad.json', '{not json'],
]);
const requestReads = [];
const requestBytes = new TextEncoder();
const loadRequest = name => {
    requestReads.push(name);
    return requestBytes.encode(requestFiles.get(name));
};
const requestCache = new RequestFileCache();
let cachedRequests = requestCache.scan(
    ['a.json', 'b.json', 'bad.json'], loadRequest);
ok(cachedRequests.map(r => r.id).join(',') === 'a,b' && requestReads.length === 3,
   'request cache reads unseen files once and drops malformed requests');
cachedRequests = requestCache.scan(['a.json', 'b.json', 'bad.json'], loadRequest);
ok(cachedRequests.map(r => r.id).join(',') === 'a,b' && requestReads.length === 3,
   'a second unchanged scan reads no request files');
requestCache.scan(['b.json', 'bad.json'], loadRequest); // evict vanished a.json
requestFiles.set('a.json', JSON.stringify({id: 'a2', summary: '1. replaced'}));
cachedRequests = requestCache.scan(
    ['a.json', 'b.json', 'bad.json'], loadRequest);
ok(cachedRequests.map(r => r.id).join(',') === 'a2,b' && requestReads.length === 4,
   'a vanished filename is evicted and read if it reappears');

const {intro, steps} = parseSteps(
    '# Auto-open check\n\nRead **carefully**.\n\n' +
    '1. open the page — observe: it renders\n' +
    '2) press the button\n' +
    'not a step\n' +
    '10. step ten');
ok(steps.length === 3, 'numbered lines (both . and )) become steps');
ok(steps[0].text.startsWith('open the page'), 'step text kept');
ok(intro[0] === 'Auto-open check' && intro.includes('Read carefully.'),
   'intro keeps heading text, strips ** and #');
ok(intro.includes('not a step'), 'unnumbered line stays intro, not a step');

const copyable = parseSteps('Run `printf "%s\\n" "$HOME"` now.\n' +
    '1. Run `git status --short` → changes are listed\n' +
    '   - Then `git diff --check`\n' +
    '2. Compare `left` with `right`\n' +
    '```sh\npython3 -m unittest\n```');
ok(copyable.introCode[0] === 'printf "%s\\n" "$HOME"' &&
   copyable.steps[0].copyCode === 'git status --short' &&
   copyable.steps[0].sub[0].copyCode === 'git diff --check' &&
   copyable.steps[1].copyCode === null &&
   copyable.intro.includes('python3 -m unittest'),
   'copy targets preserve exact single inline code; ambiguous and fenced rows keep whole-line copy');
ok(singleInlineCode('literal \\`backtick\\` then `echo ok`') === 'echo ok' &&
   singleInlineCode('`first` and `second`') === null &&
   singleInlineCode('Run ``echo `date` now``') === 'echo `date` now',
   'escaped backticks and longer delimiters preserve code without guessing between spans');

ok(steps.every(s => s.note === ''), 'steps parse with an empty note field');
ok(currentStep(steps) === 0, 'the ▶ step starts at the first step');
ok(markNext(steps) === 0 && steps[0].done, 'mark hits the FIRST unmarked');
ok(currentStep(steps) === 1, '▶ advances with the marks');
ok(markNext(steps) === 1, 'mark walks in order');
ok(undoLast(steps) === 1 && !steps[1].done, 'undo removes the LAST mark');
ok(currentStep(steps) === 1, '▶ follows undo back — how an earlier step gets a late note');
ok(markNext(steps) === 1 && markNext(steps) === 2, 'remark continues in order');
ok(markNext(steps) === -1, 'all done -> -1, no crash');
ok(currentStep(steps) === 2, 'all done -> ▶ stays on the LAST step, not -1');
ok(currentStep([]) === -1, 'no numbered steps -> no ▶ step');

// snips are tagged with the step current at attach time; the answer orders
// them by step, stable within a step, unassigned (-1) LAST
const images = [
    {step: 2, mime: 'image/png', b64: 'c3RlcDJh'},
    {step: -1, mime: 'image/png', b64: 'bG9vc2U='},
    {step: 0, mime: 'image/png', b64: 'c3RlcDA='},
    {step: 2, mime: 'image/png', b64: 'c3RlcDJi'},
];
const ordered = orderImages(images);
ok(ordered.map(i => i.b64).join(',') === 'c3RlcDA=,c3RlcDJh,c3RlcDJi,bG9vc2U=',
   'orderImages: by step, stable within a step, unassigned last');
ok(orderImages([]).length === 0 && orderImages(null).length === 0,
   'orderImages survives empty and missing lists');

steps[1].done = false;
steps[0].note = '  renders fine, 60fps  ';
const text = composeAnswer(steps, images);
ok(text.includes('2/3 done, 1 not done'), 'answer counts honestly, not-done tallied');
ok(text.includes('○ NOT DONE 2.'), 'an unchecked step says NOT DONE — never silence');
ok(text.includes('✔ 1.') && text.includes('4 screenshot(s)'),
   'checked steps and snip count present');
ok(text.startsWith('[answered via the overlay'), 'answer names its surface');
ok(text.includes(', with per-step notes]'), 'the header says notes were typed');
const lines = text.split('\n');
ok(lines[lines.indexOf('✔ 1. open the page — observe: it renders') + 1] ===
   '   note: renders fine, 60fps',
   'the note lands UNDER its own step, trimmed');
ok(text.includes('   snips: #1') && text.includes('   snips: #2, #3'),
   'per-step snip numbers match the ordered image list');
ok(text.includes('1 snip(s) attached to no particular step'),
   'an unassigned snip is named, never silently mixed in');

const receiptReq = {
    id: 'receipt-1', request_digest: 'sha256:request',
    checklist: {
        schema: 'human-copilot.checklist-request/v1', schema_version: 1,
        steps: [{id: 'step-001'}, {id: 'step-002'}, {id: 'step-003'}],
    },
};
const surfaceAnswer = checklistAnswer(receiptReq, steps);
ok(surfaceAnswer.schema === 'human-copilot.checklist-answer/v1' &&
   surfaceAnswer.request_id === 'receipt-1' &&
   surfaceAnswer.request_digest === 'sha256:request',
   'overlay structured answer is bound to the server-issued request identity');
ok(surfaceAnswer.steps.map(s => `${s.id}:${s.verdict}`).join(',') ===
   'step-001:done,step-002:not-done,step-003:done' &&
   surfaceAnswer.steps[0].note === 'renders fine, 60fps',
   'overlay emits ordered server step ids with done/failed/not-done and trimmed notes');
ok(checklistAnswer({id: 'old'}, steps) === null &&
   checklistAnswer(receiptReq, steps.slice(0, 2)) === null,
   'legacy or cardinality-mismatched requests get no machine verdict to sign');

const bare = composeAnswer(parseSteps('1. only step').steps, []);
ok(bare.includes(', no typed notes]') && !bare.includes('note:') &&
   !bare.includes('snips:') && !bare.includes('screenshot'),
   'no notes/snips -> the answer says so and adds no phantom lines');

// ---- the fail state and the one-chord workflow
const fl = parseSteps('1. open\n2. save\n3. close').steps;
ok(!allResolved(fl), 'nothing answered -> not all resolved');
ok(markNext(fl) === 0 && markFail(fl) === 1, 'done and failed walk the same order');
ok(currentStep(fl) === 2, 'a FAILED step is answered: ▶ moves past it');
ok(!allResolved(fl), 'one step left -> Space still marks, does not send');
ok(markNext(fl) === 2 && allResolved(fl), 'last answer -> all resolved: Space now sends');
ok(markNext(fl) === -1 && markFail(fl) === -1, 'nothing left to mark either way');
ok(undoLast(fl) === 2 && !allResolved(fl), 'undo re-opens the last answered step');
markNext(fl);
const flText = composeAnswer(fl, []);
ok(flText.startsWith('[answered via the overlay — 2/3 done, 1 FAILED,'),
   'the header tallies done AND failed');
ok(flText.includes('✔ 1. open') && flText.includes('✘ FAILED 2. save') &&
   flText.includes('✔ 3. close'), 'three states, three distinct marks');
ok(!flText.includes('NOT DONE'), 'a failed step is never reported as not done');
ok(undoLast(fl) === 2 && undoLast(fl) === 1 && !fl[1].failed && !fl[1].done,
   'undo clears a failed mark too');

const acc = {'mark-done': 'S+Space', 'mark-fail': 'S+X', 'edit-note': 'S+N',
             'attach-snip': 'S+S', 'undo-done': 'S+Z', 'dismiss-request': 'S+BS',
             'submit-answer': 'S+Enter'};
const h0 = nowHint(parseSteps('1. a\n2. b').steps, acc);
ok(h0.main === 'S+Space  step 1 done   S+X  step 1 failed',
   'NOW names the ▶ step and the two chords that answer it');
ok(h0.more.includes('S+N note') && h0.more.includes('S+Z undo'),
   'the second line lists note/snip/undo/decline');
const hs = parseSteps('1. a\n2. b').steps;
markNext(hs); markFail(hs);
const h1 = nowHint(hs, acc);
ok(h1.main.startsWith('S+Enter  send 1/2 (1 failed)') && h1.main.includes('S+Space again also sends'),
   'all answered -> NOW says send, with the tally, and that Space sends too');
ok(nowHint([], acc).main.includes('S+N') && nowHint([], acc).more.includes('S+Enter'),
   'no steps -> NOW opens a typed reply and explains how to send it');
ok(nowHint(hs, {}).main.includes('?'), 'an unknown binding renders as ?, never crashes');

ok(overlayHeader({client: 'Codex', deadline: 100, queued: 0}, 40) ===
   '🧿 Codex needs your eyes · ⏳ 1:00',
   'overlay header formats the countdown without a shell widget');
ok(overlayHeader({client: 'Claude', deadline: 100, queued: 2}, 101) ===
   '🧿 Claude needs your eyes · ⏳ 0:00 · +2 queued',
   'overlay header clamps expiry and reports the live queue count');

// ---- pointing the eyes: "→ see" and "@{Window · region}"
const lk = parseLook('Click **Save** @{Firefox · top-right} → toast says "Saved"');
ok(lk.text === 'Click **Save**' && lk.see === 'toast says "Saved"',
   'the arrow splits action from observation, both trimmed');
ok(lk.spot.window === 'Firefox' && lk.spot.region === 'top-right' && !lk.spot.rect,
   'the locator names the window and the region');
ok(parseLook('plain step').see === '' && parseLook('plain step').spot === null,
   'no annotations -> empty see, null spot');
ok(parseLook('a -> b').see === 'b' && parseLook('a => b').see === 'b' &&
   parseLook('a ⇒ b').see === 'b', 'ASCII arrows work too');
ok(parseLook('run a->b').see === '', 'an arrow glued to a word is not the separator');
ok(parseSpot('Firefox top right').region === 'top-right' &&
   parseSpot('Firefox top right').window === 'Firefox',
   'a two-word region without a separator is still found');
ok(parseSpot('top-right, Firefox').window === 'Firefox', 'order does not matter');
ok(parseSpot('upper left').region === 'top-left' && parseSpot('centre').region === 'center' &&
   parseSpot('whole').region === 'full', 'synonyms normalise');
ok(parseSpot('bottom').window === '' && parseSpot('bottom').region === 'bottom',
   'no window -> a region of the primary monitor');
ok(parseSpot('Firefox').region === 'full', 'a bare window -> the whole window');
const pr = parseSpot('zellij · 70%,0,30,10');
ok(pr.window === 'zellij' && pr.rect.join(',') === '0.7,0,0.3,0.1' && pr.region === null,
   'a percent rect is read, and beats a region');
ok(parseSpot('x 0.5,0.5,0,0.2') .rect === null, 'a zero-size rect is dropped');
ok(parseSpot('   ') === null && parseSpot('') === null, 'an empty tag is no locator');
ok(regionOf('nowhere') === null, 'an unknown region word is a window word');
const base = {x: 100, y: 50, width: 1200, height: 900};
const tr = spotRect(base, parseSpot('top-right'));
ok(tr.x === 900 && tr.y === 50 && tr.width === 400 && tr.height === 300,
   'top-right is the top-right third');
const full = spotRect(base, parseSpot('Firefox'));
ok(full.x === 100 && full.width === 1200 && full.height === 900, 'full frames the window');
const tiny = spotRect(base, parseSpot('0.99,0.99,0.005,0.005'));
ok(tiny.width === 24 && tiny.height === 24, 'a rect is never smaller than the eye can find');
ok(spotLabel(parseSpot('Firefox · top-right')) === 'Firefox · top-right' &&
   spotLabel(parseSpot('bottom')) === 'bottom' &&
   spotLabel(parseSpot('Firefox')) === 'Firefox' &&
   spotLabel(parseSpot('full')) === 'whole screen' &&
   spotLabel(parseSpot('z 70%,0,30,10')) === 'z · 70%,0%,30%,10%',
   'the locator label reads back the way it was written');

const seen = parseSteps('1. Click Save @{Firefox · top-right} → toast says Saved\n2. plain');
ok(seen.steps[0].text === 'Click Save' && seen.steps[0].see === 'toast says Saved' &&
   seen.steps[0].spot.window === 'Firefox', 'parseSteps lifts both annotations out of a step');
ok(seen.steps[1].see === '' && seen.steps[1].spot === null, 'and leaves a plain step alone');
const seenTbl = parseSteps('| # | do | see |\n|---|---|---|\n| 1 | click @{Firefox · top} | toast → says Saved |');
ok(seenTbl.steps[0].text === 'click · toast' && seenTbl.steps[0].see === 'says Saved' &&
   seenTbl.steps[0].spot.region === 'top', 'annotations in any table cell are lifted too');
ok(!composeAnswer(seen.steps, []).includes('@{') && !composeAnswer(seen.steps, []).includes('→'),
   'the answer reports the action, not the annotations');

ok(prettyAccel('<Super><Alt>space') === 'Super+Alt+Space' &&
   prettyAccel('<Super><Alt>slash') === 'Super+Alt+/' &&
   prettyAccel('<Super><Alt>Return') === 'Super+Alt+Enter' &&
   prettyAccel('<Super><Alt>BackSpace') === 'Super+Alt+Backspace',
   'glossary prettifies the default chords');
ok(prettyAccel('<Control>k') === 'Control+K' && prettyAccel('') === '(unbound)',
   'rebound single letters and unbound keys render honestly');
ok(prettyAccel('<Super><Alt>a') === 'Super+Alt+A',
   'the attach-snip default (moved off Ubuntu\'s screen-reader chord, hcop-9rz) prettifies');

// ---- markdown versatility: a checklist written as a table is still a checklist
ok(plainInline('see [the wiki](http://x/y) and `code`, keep __init__ and *.log *.tmp')
   === 'see the wiki (http://x/y) and code, keep __init__ and *.log *.tmp',
   'inline: links flattened, code stripped, dunders and globs untouched');
ok(plainInline('**bold** and *em* and ~~gone~~') === 'bold and em and gone',
   'inline: bold/em/strike markers dropped');

ok(tableCells('| a | b\\|c | |').join(',') === 'a,b|c,',
   'table cells: outer pipes optional, escaped pipe kept, empty cell kept');
ok(isDelimRow('|---|:--:|--:|') && isDelimRow(' --- | --- ') &&
   !isDelimRow('---') && !isDelimRow('| a | b |'),
   'delimiter row needs pipes — a lone --- stays a horizontal rule');

const tbl = parseSteps(
    'Compare the panels:\n\n' +
    '| # | do | observe | report |\n' +
    '|---|:--:|---|---|\n' +
    '| 1 | launch it | opens at 4K | pass/fail |\n' +
    '| 2) | resize | no blur | pass/fail + scale |\n\n' +
    'Reference:\n\n' +
    '| panel | res |\n|---|---|\n| DP-1 | 4K |\n');
ok(tbl.steps.length === 2, 'table rows numbered in cell one become steps');
ok(tbl.steps[0].text === 'launch it · opens at 4K · pass/fail',
   'the rest of the row is the step text, cells joined');
ok(tbl.steps[1].text.startsWith('resize'), '"2)" numbers a row as well as "2."');
ok(!tbl.intro.some(l => l.includes('---')),
   'the delimiter row never reaches the operator');
ok(!tbl.intro.some(l => l.includes('|')), 'no raw pipes survive into intro');
ok(tbl.intro.includes('panel · res') && tbl.intro.includes('DP-1 · 4K'),
   'a table with no numbered rows is reference text, compacted not dropped');
ok(!tbl.intro.some((l, i) => !l && !tbl.intro[i - 1] && i),
   'the blanks that framed a consumed table do not double up');

ok(parseSteps('| a | b |\n|---|---|\n| x | y |').steps.length === 0,
   'an unnumbered table is never mistaken for a checklist');
ok(parseSteps('```\n1. not a step\n```\n1. a real step').steps.length === 1,
   'a numbered line inside fenced code is not a step');
ok(parseSteps('1. [x] already done\n2. [ ] pending').steps[0].text ===
   'already done',
   'a task-list marker is stripped — the overlay draws its own box');

// ---- a nested list under a numbered step is that step's BODY, not intro and
// not a step of its own. The break this pins (2026-08-27): the two nameservers
// a sublist named were hoisted into intro, drawn above the checklist detached
// from the step that explained them, and were first in line for the intro cap.
const nested = parseSteps(
    'Pre-flight note.\n\n' +
    '4. Now the actual flip. Replace the existing two with:\n' +
    '   - `adrian.ns.cloudflare.com`\n' +
    '   - `aiden.ns.cloudflare.com`\n\n' +
    '   and remove `wns2061.hostgator.com`. Save, then **screenshot**.\n\n' +
    '5. Wait for propagation.');
ok(nested.steps.length === 2,
   'a nested bullet is never a step — the hotkey walks the numbered lines only');
ok(nested.intro.join('|') === 'Pre-flight note.',
   'the sublist stays with its step instead of being hoisted into intro');
const sub4 = nested.steps[0].sub;
ok(sub4[0].text === '• adrian.ns.cloudflare.com' &&
   sub4[1].text === '• aiden.ns.cloudflare.com',
   'nested bullets survive, normalised to • like the TUI draws them');
ok(sub4.every(b => b.level === 1), 'one indent level -> level 1');
ok(sub4[sub4.length - 1].text.startsWith('and remove wns2061.hostgator.com'),
   'the continuation paragraph after the sublist stays in the same step');
ok(sub4[2].text === '' && sub4.length === 4,
   'the blank between sublist and continuation is kept, exactly once');
ok(nested.steps[1].text === 'Wait for propagation.' &&
   nested.steps[1].sub.length === 0,
   'the next numbered step ends the body and starts clean');

const deep = parseSteps(
    '1. outer step\n' +
    '   - level one\n' +
    '     - level two\n' +
    '       1. level three, numbered\n' +
    '     - back to level two\n' +
    '   - back to level one\n' +
    '2. next step');
ok(deep.steps.length === 2, 'a nested ORDERED item is body, not a fifth step');
ok(deep.steps[0].sub.map(b => b.level).join(',') === '1,2,3,2,1',
   'nesting level follows the indent, and unwinds back out again');
ok(deep.steps[0].sub[2].text === '1. level three, numbered',
   'a nested ordered item keeps its own number as its marker');
ok(deep.steps[0].sub[0].text === '• level one', 'deeper bullets normalise too');

const tasksub = parseSteps('1. do it\n   - [x] first\n   - [ ] second');
ok(tasksub.steps.length === 1 &&
   tasksub.steps[0].sub.map(b => b.text).join('|') === '☑ first|☐ second',
   'a nested task item draws a box, and is still not a step');

// notes and snips must still land on the right STEP NUMBER with bodies present
nested.steps[0].done = true;
nested.steps[0].note = 'saved ok';
const nestedAnswer = composeAnswer(nested.steps,
                                   [{step: 0, mime: 'image/png', b64: 'eA=='}]);
ok(nestedAnswer.includes('✔ 1. Now the actual flip') &&
   nestedAnswer.includes('   note: saved ok') &&
   nestedAnswer.includes('   snips: #1') &&
   nestedAnswer.includes('○ NOT DONE 2. Wait for propagation.'),
   'notes and snips still attach to the right step number around a body');
ok(!nestedAnswer.includes('adrian.ns'),
   'the answer reports steps, not their bodies — the report shape is unchanged');

const indentedAll = parseSteps('  1. alpha\n  2. beta');
ok(indentedAll.steps.length === 2,
   'a uniformly indented checklist is still a checklist (base indent is the first step)');
const formattedIndented = parseSteps(
    '  1. **alpha** [docs](https://example.test)   \n' +
    '      1. nested body, not a top-level step\n' +
    '  2) [ ] `beta`\t');
ok(formattedIndented.steps.length === 2 &&
   formattedIndented.steps[0].text === 'alpha docs (https://example.test)' &&
   formattedIndented.steps[1].text === 'beta',
   'base-indented formatted/task steps match the canonical responder display grammar');

// ---- per-instance palette: the generated palette.js IS the Python table
// (hcop-0p7.2). palette.py is the source of truth; this pins that the ES
// module gjs loads carries the same entries, in the same order, with the
// same hexes — a stale regeneration shows up here as well as in selftest.
const hereDir = GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]);
const [, pyOut, pyErr, pyStatus] = GLib.spawn_sync(
    hereDir, ['python3', '-c',
              'import json,os,sys; sys.path.insert(0, os.path.join(os.getcwd(), "..")); ' +
              'import palette; print(json.dumps([{k: e[k] for k in ' +
              '("number","name","hex","fg","xterm")} for e in palette.PALETTE]))'],
    null, GLib.SpawnFlags.SEARCH_PATH, null);
ok(pyStatus === 0, 'palette.py imports and dumps its table under python3'
   + (pyStatus === 0 ? '' : `: ${new TextDecoder().decode(pyErr)}`));
const pyTable = JSON.parse(new TextDecoder().decode(pyOut));
ok(PALETTE.length >= 9 && PALETTE.length === pyTable.length,
   `palette.js has the Python table's ${pyTable.length} entries`);
ok(PALETTE.every((e, i) => e.hex === pyTable[i].hex && e.number === pyTable[i].number
                          && e.name === pyTable[i].name && e.fg === pyTable[i].fg
                          && e.xterm === pyTable[i].xterm),
   'every entry matches Python: number, name, hex, fg, xterm, in order');
ok(PALETTE.every(e => /^#[0-9a-f]{6}$/.test(e.hex) && /^#[0-9a-f]{6}$/.test(e.fg)),
   'hexes are CSS-ready lowercase #rrggbb');
ok(Object.isFrozen(PALETTE) && Object.isFrozen(PALETTE[0]),
   'the table is frozen — nothing in the shell can edit a colour in place');
ok(entryFor(1) === PALETTE[0] && entryFor(PALETTE.length) === PALETTE[PALETTE.length - 1]
   && entryFor(PALETTE.length + 1) === PALETTE[0],
   'entryFor wraps past the table exactly like palette.entry_for');
ok(/^#[0-9a-f]{6}$/.test(OVERLAY_BG), `overlay background is exported (${OVERLAY_BG})`);

// ---- multi-instance routing (hcop-0p7.4): the focused window's zellij title
// names the session; the ACTIVE instance is the last-focused copilot pane's
// session, else the newest request's instance; OSDs name the instance.
const ft = focusTarget('hc-A | copilot · hc-A');
ok(ft.session === 'hc-A' && ft.pane === 'copilot · hc-A' && ft.copilot === true,
   'zellij title "<session> | copilot · <id>" -> the session, and it IS a copilot pane');
ok(focusTarget('hc-A | node claude — ~/x').copilot === false &&
   focusTarget('hc-A | node claude — ~/x').session === 'hc-A',
   'the session\'s agent pane is the session, but not a copilot pane');
ok(focusTarget('zellij-hub | copilot-tui · human-copilot · zellij-hub').copilot === true,
   'the TUI\'s own OSC title (an unnamed pane) also reads as a copilot pane');
ok(focusTarget('a | b | c').session === 'a' && focusTarget('a | b | c').pane === 'b | c',
   'split on the FIRST separator — pane titles may hold pipes');
ok(focusTarget('Firefox') === null && focusTarget('') === null &&
   focusTarget(null) === null && focusTarget(' | x') === null,
   'a title zellij does not own routes nothing (browser tab, plain window)');

const stamp = (session, uuid) => ({session, pane: '0', server_uuid: uuid, attached: true});
const rA1 = {id: 'a1', created: 1, instance: stamp('hc-A', 'a'.repeat(32))};
const rU = {id: 'u1', created: 2, instance: stamp(null, 'c'.repeat(32))};
const rA2 = {id: 'a2', created: 4, instance: stamp('hc-A', 'b'.repeat(32))};
const rB = {id: 'b1', created: 5, instance: stamp('hc-B', 'd'.repeat(32))};
const all = [rB, rA2, rA1];                       // deliberately unsorted
let pk = pickActive(all, {session: 'hc-A'});
ok(pk.req.id === 'a1' && pk.instance === 'hc-A' && pk.reason === 'focused',
   'focused pane wins over newest: hc-A\'s OLDEST request, though hc-B\'s is the newest overall');
pk = pickActive(all, null);
ok(pk.req.id === 'b1' && pk.instance === 'hc-B' && pk.reason === 'newest',
   'no copilot pane ever focused -> the newest request\'s instance');
pk = pickActive(all, {session: 'hc-C'});
ok(pk.req.id === 'b1' && pk.reason === 'newest',
   'a focused pane of an instance with NO open request -> newest, not nothing');
ok(pickActive([], {session: 'hc-A'}) === null && pickActive(null, null) === null,
   'nothing open -> null');
pk = pickActive([rA1, rU], null);
ok(pk.req.id === 'u1' && pk.instance === 'c'.repeat(32),
   'a non-zellij server is its own instance, keyed by uuid');
pk = pickActive([rA1, rA2], null);
ok(pk.req.id === 'a1' && pk.reason === 'newest',
   'one instance, two requests -> its oldest (the queue order both surfaces always used)');
ok(pickActive([{id: 'p', created: 1}], {session: 'hc-A'}).req.id === 'p',
   'a pre-stamp request still shows when nothing else is open');

ok(instanceKey(stamp('hc-A', 'x')) === 'hc-A' && instanceKey(stamp(null, 'x')) === 'x' &&
   instanceKey(null) === null && instanceKey({}) === null,
   'instanceKey mirrors server.py::instance_id: session, else uuid, else null');
ok(instanceLabel(stamp('hc-A', 'x')) === 'hc-A' &&
   instanceLabel(stamp(null, 'abcdef0123456789')) === 'abcdef01' &&
   instanceLabel(null) === '' && instanceLabel({}) === '',
   'an instance is named by its session, else 8 hex digits of its uuid');
ok(instanceSuffix(stamp('hc-A', 'x')) === ' · instance hc-A' && instanceSuffix(null) === '',
   'OSD text names the instance; an unstamped request adds nothing');
ok(overlayHeader({client: 'Claude', deadline: 100, queued: 1, instance: stamp('hc-A', 'x')}, 40) ===
   '🧿 Claude needs your eyes · ⏳ 1:00 · hc-A · +1 queued',
   'the header names the instance between the countdown and the queue');

// ---- concurrent requests (hcop-0p7.3): every open request of every instance
// is tracked (OpenLedger); a withdrawal is announced ONCE and touches only its
// own instance; the strip counts per instance; two arrivals in one tick are
// both seen; numbers are stable and agree with the palette registry.
const led = new OpenLedger();
const cA1 = {id: 'ca1', created: 1, instance: stamp('hc-A', 'a'.repeat(32))};
const cB1 = {id: 'cb1', created: 2, instance: stamp('hc-B', 'b'.repeat(32))};
const cC1 = {id: 'cc1', created: 3, instance: stamp('hc-C', 'c'.repeat(32))};
const answered = new Set();
let ev = led.update([cA1, cB1, cC1], answered);
ok(ev.arrived.map(r => r.id).join(',') === 'ca1,cb1,cc1' && ev.withdrawn.length === 0 && led.size === 3,
   'three requests from three instances in one tick: all three arrive, none lost');
const roster = new InstanceRoster();
let nums = roster.update([cA1, cB1, cC1].map(r => instanceKey(r.instance)));
ok(nums.get('hc-A') === 1 && nums.get('hc-B') === 2 && nums.get('hc-C') === 3,
   'instance numbers follow arrival order: A=1, B=2, C=3');
ok(cardBadge(cA1.instance, 1, false) === '1 hc-A' &&
   cardBadge(cC1.instance, 3, true) === '▶ 3 hc-C' &&
   cardBadge({session: 'human-copilot-manager-claude'}, 4, false) === '4 human-copilot…',
   'card badge: ▶ on the chords-routed one, number + session name (never colour alone), long names cut');
// answered OUT OF ORDER: B first, elsewhere (a sidepanel) — then C, by this overlay
ev = led.update([cA1, cC1], answered);
ok(ev.withdrawn.length === 1 && ev.withdrawn[0].id === 'cb1' &&
   ev.withdrawn[0].instance.session === 'hc-B' && ev.arrived.length === 0,
   'one withdrawal is reported exactly once, naming its instance');
ok(withdrawnText(ev.withdrawn) === 'Copilot request withdrawn · instance hc-B',
   'the withdrawn OSD text names hc-B — one OSD, not one per open instance');
ev = led.update([cA1, cC1], answered);
ok(ev.withdrawn.length === 0 && ev.arrived.length === 0,
   'the next tick does NOT report the same withdrawal again');
answered.add('cc1');                                   // this overlay answered C
ev = led.update([cA1], answered);
ok(ev.withdrawn.length === 0 && !answered.has('cc1') && led.size === 1,
   'a request this overlay answered itself is not a withdrawal, and is forgotten once gone');
nums = roster.update([cA1].map(r => instanceKey(r.instance)));
ok(nums.get('hc-A') === 1 && nums.size === 1,
   'instances with no request left leave the strip; A keeps its number');
const stash = new Map([['ca1', {steps: [1]}], ['cb1', {steps: [2]}], ['cc1', {}]]);
pruneStash(stash, [cA1]);
ok(stash.has('ca1') && stash.get('ca1').steps[0] === 1 && !stash.has('cb1') && !stash.has('cc1'),
   'a withdrawal prunes only the vanished requests\' stash — A\'s ticks are untouched');
// two requests landing in the SAME tick, one queued behind A, one a new instance
const cA2 = {id: 'ca2', created: 5, instance: stamp('hc-A', 'a'.repeat(32))};
const cD1 = {id: 'cd1', created: 6, instance: stamp('hc-D', 'd'.repeat(32))};
ev = led.update([cA1, cA2, cD1], answered);
ok(ev.arrived.map(r => r.id).join(',') === 'ca2,cd1' && led.size === 3,
   'two requests arriving in one tick are both reported — no lost update');
nums = roster.update([cA1, cA2, cD1].map(r => instanceKey(r.instance)));
ok(nums.get('hc-A') === 1 && nums.get('hc-D') === 2,
   'a newcomer takes the lowest free number (B and C freed 2 and 3)');
ok(queuedBehind([cA1, cA2, cD1], 'hc-A', 'ca1') === 1 && queuedBehind([cA1, cA2, cD1], 'hc-D', 'cd1') === 0,
   'a card counts the same-instance requests waiting behind it');
ok(arrivedText(ev.arrived, 'ca1') === 'Copilot: 2 requests wait · instances hc-A, hc-D — a card opened for it' &&
   arrivedText([cD1], 'ca1') === 'Copilot: a request waits · instance hc-D — a card opened for it' &&
   arrivedText([cD1], 'cd1') === '' && arrivedText([], 'x') === '',
   'arrivals that opened away from the operator\'s eyes are announced once');
ok(withdrawnText([{id: 'x', instance: cA1.instance}, {id: 'y', instance: cD1.instance}]) ===
   '2 copilot requests withdrawn · instances hc-A, hc-D' && withdrawnText([]) === '',
   'several withdrawals in one tick: one OSD, counted, every instance named');
// the palette registry (spool/palette.json) is the TUI's number: it wins
const regSlots = registrySlots(JSON.stringify({version: 1, sessions: {
    'hc-D': {slot: 7, holders: {}}, 'hc-A': {slot: 'x'}, 'hc-Z': null}}));
ok(regSlots['hc-D'] === 7 && !('hc-A' in regSlots) && !('hc-Z' in regSlots),
   'registrySlots reads integer slots and ignores junk records');
ok(Object.keys(registrySlots('{nope')).length === 0 && Object.keys(registrySlots('')).length === 0 &&
   Object.keys(registrySlots('[1,2]')).length === 0, 'an unreadable registry is no slots, never a throw');
nums = roster.update(['hc-A', 'hc-D'], regSlots);
ok(nums.get('hc-D') === 7 && nums.get('hc-A') === 1,
   'a registry slot is the strip number when the TUI assigned one');
nums = roster.update(['hc-A', 'hc-D', 'hc-E'], {'hc-E': 1});
ok(nums.get('hc-E') === 1 && nums.get('hc-A') === 2 && nums.get('hc-D') === 7,
   'a registry slot evicts a colliding roster-only number; the evicted one gets the lowest free, others keep theirs');
ok(roster.numberOf('hc-D') === 7 && roster.numberOf('nobody') === 0, 'numberOf reads the current table, 0 for unknown');
ok(new InstanceRoster().update([null, undefined, 'k', 'k']).size === 1, 'null keys and duplicates do not take numbers');

// ---- colour follows the instance (hcop-0p7.5): a spotlight frame for
// instance k wears palette[k]; a declined request takes back only its own
// frame; the enable() sweep finds frames of EVERY palette colour by NAME; a
// non-active frames stay steady; OSD text names
// number + session (+ colour name).
const frameRect = {x: 100, y: 200, width: 400, height: 300};
for (let k = 1; k <= PALETTE.length; k++) {
    const spec = spotFrameSpec(frameRect, 1, entryFor(k), k);
    ok(spec.bars.length === 4 && spec.bars.every(b => b.color === PALETTE[k - 1].hex) &&
       spec.color === PALETTE[k - 1].hex && spec.label.bg === PALETTE[k - 1].hex &&
       spec.label.fg === PALETTE[k - 1].fg && spec.name === SPOT_NAME,
       `a frame for instance ${k} carries palette[${k}] (${PALETTE[k - 1].name} ${PALETTE[k - 1].hex}) on all four bars and the label, named ${SPOT_NAME}`);
}
const specA = spotFrameSpec(frameRect, 1, entryFor(3), 3);
ok(specA.label.text === '👁 2 · instance 3',
   'the spot label names the step AND the instance number');
ok(specA.bars[0].y === 200 && specA.bars[0].height === SPOT_EDGE && specA.bars[0].width === 400 &&
   specA.bars[1].y === 200 + 300 - SPOT_EDGE &&
   specA.bars[2].x === 100 && specA.bars[2].width === SPOT_EDGE && specA.bars[2].height === 300 &&
   specA.bars[3].x === 100 + 400 - SPOT_EDGE,
   'four solid edge bars — top, bottom, left, right — never a filled box');
const specNone = spotFrameSpec(frameRect, 0, null, 0);
ok(specNone.color === SPOT_FALLBACK && specNone.bars.every(b => b.color === SPOT_FALLBACK) &&
   specNone.label.text === '👁 1' && specNone.label.fg === '#1a1a1a',
   'an unstamped request frames in the fallback yellow, label without an instance');
ok(PALETTE.every(e => e.hex !== SPOT_FALLBACK),
   'the fallback yellow is no palette entry — a fallback frame is never mistaken for an instance');

// a declined request removes only its own frames
const ledger = new SpotLedger();
const fa = {actors: ['a1', 'a2'], rect: frameRect};
const fb = {actors: ['b1'], rect: frameRect};
ledger.set('req-a', fa);
ledger.set('req-b', fb);
ok(ledger.take('req-a') === fa && ledger.get('req-a') === null && ledger.get('req-b') === fb && ledger.size === 1,
   'declining request a takes back a\'s frame and leaves b\'s on screen');
ok(ledger.take('req-a') === null && ledger.take('nobody') === null && ledger.size === 1,
   'a request with no frame yields nothing — nothing else\'s is touched');
ok(ledger.takeAll().length === 1 && ledger.size === 0,
   'disable() takes every frame, whoever owned it');

// Exercise the production actor lifecycle, rather than a second pulse model.
// Shell imports cannot load in plain gjs; inject only the actor/Chrome boundary
// into the actual spotlight methods. Saved completions simulate callbacks
// arriving after cancellation, including reactivation of the SAME frame.
const extensionPath = GLib.build_filenamev([
    GLib.path_get_dirname(GLib.filename_from_uri(import.meta.url)[0]),
    '../overlay/copilot-overlay@croze/extension.js',
]);
const extensionText = new TextDecoder().decode(GLib.file_get_contents(extensionPath)[1]);
const copyMethods = extensionText.slice(
    extensionText.indexOf('\n    _copyButton('),
    extensionText.indexOf('\n    _cardKey('));
const clipboardWrites = [];
class FakeCopyButton {
    constructor(props) { Object.assign(this, props); }
    connect(signal, fn) { this[signal] = fn; }
}
class FakeCopyRow {
    constructor(props) { Object.assign(this, props); this.children = []; }
    add_child(child) { this.children.push(child); }
}
const CopyHarness = new Function('St', 'Clutter', 'logError',
    `return class {${copyMethods}\n}`)(
    {Button: FakeCopyButton, BoxLayout: FakeCopyRow,
     ClipboardType: {CLIPBOARD: 1},
     Clipboard: {get_default: () => ({set_text: (type, value) =>
         clipboardWrites.push({type, value})})}},
    {Orientation: {HORIZONTAL: 1}, ActorAlign: {START: 1}}, error => { throw error; });
const copyHarness = new CopyHarness();
const copyAnnouncements = [];
copyHarness._osd = text => copyAnnouncements.push(text);
copyHarness._tagOf = () => ' · instance 1';
copyHarness._label = (text, style) => ({text, style, set_x_expand() {}});
const copiedRow = copyHarness._copyRow({id: 'copy'}, 'Run git status',
    'git status', 'color: white;');
copiedRow.children[1].clicked();
ok(clipboardWrites.length === 1 && clipboardWrites[0].type === 1 &&
   clipboardWrites[0].value === 'git status' &&
   copiedRow.children[1].label === 'Copy code' &&
   copyAnnouncements[0] === 'Code copied · instance 1',
   'copying a command writes exactly the code span and gives request-scoped feedback');
copyHarness._copyButton({id: 'copy'}, 'full\nrequest', 'Copy all').clicked();
ok(clipboardWrites[1].value === 'full\nrequest' &&
   !extensionText.slice(extensionText.indexOf('\n    _copyButton('),
                        extensionText.indexOf('\n    _cardKey(')).includes('_submit('),
   'Copy all retains multiline request text and copy controls do not submit an answer');
const spotlightMethods = extensionText.slice(
    extensionText.indexOf('\n    _drawSpot('),
    extensionText.indexOf('\n    // Actors a previous load'));
const spotActors = [];
class SpotTestActor {
    constructor(props) {
        Object.assign(this, props);
        this.opacity = 255;
        this.eases = [];
        this.removals = 0;
        spotActors.push(this);
    }
    set_position(x, y) { this.x = x; this.y = y; }
    set_size(width, height) { this.width = width; this.height = height; }
    set_text(text) { this.text = text; }
    ease(options) {
        if (this.destroyed)
            throw new Error('animation scheduled on a destroyed spotlight actor');
        this.eases.push(options);
        this.opacity = options.opacity;
    }
    remove_all_transitions() { this.removals++; }
    destroy() { this.destroyed = true; }
}
const SpotHarness = new Function('St', 'Main', 'Clutter', 'spotFrameSpec',
    'logError', `return class {${spotlightMethods}\n}`)(
    {Widget: SpotTestActor, Label: SpotTestActor},
    {layoutManager: {addTopChrome() {}}},
    {AnimationMode: {EASE_IN_OUT_SINE: 1}}, spotFrameSpec,
    error => { throw error; });
const spotHarness = new SpotHarness();

// Exercise the public synchronous capture refresh contract from the actual
// extension source. Shell imports are unavailable in plain gjs, so extract
// only this method and provide the private scan delegate it calls.
const refreshMethod = extensionText.slice(
    extensionText.indexOf('\n    refreshTargetsForCapture()'),
    extensionText.indexOf('\n    disable()'));
const RefreshHarness = new Function(`return class {${refreshMethod}\n}`)();
const refreshHarness = new RefreshHarness();
let refreshScans = 0;
refreshHarness._scan = () => { refreshScans++; };
refreshHarness._enabled = false;
let refreshError = null;
try {
    refreshHarness.refreshTargetsForCapture();
} catch (error) {
    refreshError = error;
}
ok(refreshError && refreshError.message === 'E_TARGET_UNRESOLVED' && refreshScans === 0,
   'disabled capture refresh refuses synchronously without scanning');
refreshHarness._enabled = true;
ok(refreshHarness.refreshTargetsForCapture() === undefined && refreshScans === 1,
   'enabled capture refresh delegates to one synchronous scan');
spotHarness._spots = new SpotLedger();
const drawPulse = (id, active, rect = frameRect) =>
    spotHarness._drawSpot(id, rect, 0, entryFor(id === 'a' ? 1 : 2), 1, active);
drawPulse('a', true);
drawPulse('b', false);
const pulseA = spotHarness._spots.get('a');
const pulseB = spotHarness._spots.get('b');
const oldAComplete = pulseA.bars[0].eases[0].onComplete;
const initialRemovals = pulseA.bars.map(b => b.removals);
ok(pulseA.bars.every(b => b.eases.length === 1) &&
   pulseB.bars.every(b => !b.eases.length && b.opacity === 255),
   'only the initially active spotlight starts an animation');
drawPulse('a', true, {...frameRect, x: 333, width: 777});
ok(spotActors.length === 10 && spotHarness._spots.get('a') === pulseA &&
   pulseA.bars[0].x === 333 && pulseA.bars[0].width === 777 &&
   pulseA.bars.every((b, i) => b.eases.length === 1 && b.removals === initialRemovals[i]),
   'moving and resizing reuse actors without cancelling or restarting the pulse');
drawPulse('a', false);
drawPulse('b', true);
oldAComplete();
ok(pulseA.bars.every((b, i) => b.opacity === 255 && b.eases.length === 1 &&
   b.removals === initialRemovals[i] + 1) &&
   pulseB.bars.every(b => b.eases.length === 1),
   'A to B stops all old transitions, restores full opacity and starts the retained B frame');
const oldBComplete = pulseB.bars[0].eases[0].onComplete;
drawPulse('b', false);
drawPulse('a', true);
oldAComplete();
oldBComplete();
ok(spotActors.length === 10 && pulseA.bars.every(b => b.eases.length === 2) &&
   pulseB.bars.every(b => b.opacity === 255 && b.eases.length === 1),
   'B to A restarts A once; callbacks from its previous pulse cannot create a second loop');
pulseA.bars[0].eases[1].onComplete();
ok(pulseA.bars.every(b => b.eases.length === 3 && b.eases[2].opacity === 255),
   'the current completion continues the new pulse normally');
const clearedComplete = pulseA.bars[0].eases[2].onComplete;
spotHarness._clearSpot('a');
clearedComplete();
ok(pulseA.actors.every(a => a.destroyed) &&
   pulseA.bars.every((b, i) => b.eases.length === 3 && b.removals === initialRemovals[i] + 3) &&
   pulseB.actors.every(a => !a.destroyed),
   'clearing mid-animation cancels and destroys only that request, and stale completions stop');
drawPulse('a', true);
clearedComplete();
ok(spotHarness._spots.get('a') !== pulseA &&
   spotHarness._spots.get('a').bars.every(b => b.eases.length === 1),
   'a replacement frame with the same owner cannot inherit the old pulse loop');
const disableComplete = spotHarness._spots.get('a').bars[0].eases[0].onComplete;
spotHarness._clearSpot();
disableComplete();
ok(spotHarness._spots.size === 0 && spotActors.every(a => a.destroyed),
   'disable clears all actors and pending completions cannot recreate animation');

// the enable() sweep: by NAME for every palette colour; f5c211 only as the pre-fix leak
ok(PALETTE.every(e => isSpotActor(SPOT_NAME, `background-color: ${e.hex};`)),
   `the orphan sweep catches a named frame of every one of the ${PALETTE.length} palette colours`);
ok(isSpotActor(SPOT_NAME, '') && isSpotActor(SPOT_NAME, null),
   'the name alone is enough — the sweep never needs the colour');
ok(isSpotActor(null, 'background-color: #f5c211;') && isSpotActor('', 'color: #f5c211'),
   'the pre-fix leak (unnamed, old yellow in an inline style) is still swept — the documented fallback');
ok(PALETTE.every(e => !isSpotActor('some-other-actor', `background-color: ${e.hex};`)) &&
   !isSpotActor(null, '') && !isSpotActor('osd-window', 'color: #ffffff;'),
   'an unnamed actor in a palette colour is NOT ours — the rule is the name, not the table');

// ---- every request visible at once (hcop-5sy): the surface models are
// computed per REQUEST, independent of which one the chords route to. Three
// concurrent targeted requests produce three frames and three card
// placements that all stay on screen; focus changes nothing about visibility.
const win = (name, x, y, wt, ht, userTime = 1) => ({
    title: `${name} — main`, wmClass: name.toLowerCase(), wmInstance: name.toLowerCase(),
    userTime, minimized: false, monitor: 0,
    frame: {x, y, width: wt, height: ht},
});
const windows = [
    win('Firefox', 60, 60, 900, 700, 5),
    win('hub', 1000, 60, 880, 700, 9),
    win('Editor', 60, 800, 1200, 300, 3),
];
const tA = {id: 'ta', created: 1, summary: 'Ctx.\n1. Look @{Firefox · full} → lit',
            instance: stamp('hc-A', 'a'.repeat(32))};
const tB = {id: 'tb', created: 2, summary: 'Ctx.\n1. Check @{hub · top-left} → row lit',
            instance: stamp('hc-B', 'b'.repeat(32))};
const tC = {id: 'tc', created: 3, summary: 'Ctx.\n1. Read @{Editor · full} → text',
            instance: stamp('hc-C', 'c'.repeat(32))};
const workArea = {x: 0, y: 28, width: 1920, height: 1052};
const CARD_W = 400, CARD_H = 220;

const specsAndRects = [tA, tB, tC].map(req => {
    const {steps} = parseSteps(req.summary);
    const cur = currentStep(steps);
    const res = resolveTarget(steps[cur].spot.window, windows);
    const frame = spotRect(res.window.frame, steps[cur].spot);
    const placed = placeNearTarget(res.window.frame, CARD_W, CARD_H, workArea,
                                   [], frame);
    return {req, res, frame, placed};
});
ok(specsAndRects.length === 3 &&
   specsAndRects.every(s => s.res.status === 'unique'),
   'three concurrent targeted requests each resolve their own unique window');
ok(specsAndRects.every(s => s.frame && s.placed && !s.placed.coversSpot),
   'all three have a frame rect and a card rect that avoids its own observation region');
const rects = specsAndRects.map(s => s.placed);
ok(!rects.some((r, i) => rects.some((o, j) => j > i &&
        r.x < o.x + o.width && r.x + r.width > o.x &&
        r.y < o.y + o.height && r.y + r.height > o.y)),
   'the three cards do not overlap each other — all visible at once');
ok(specsAndRects.every(s =>
    s.placed.x + s.placed.width <= workArea.x + workArea.width &&
    s.placed.x >= workArea.x &&
    s.placed.y >= workArea.y &&
    s.placed.y + s.placed.height <= workArea.y + workArea.height
), 'every card sits inside the work area (three frames, three cards, one screen)');
// keyboard focus changes routing, never visibility: placements do not depend
// on which request is active
ok([tA, tB, tC].every(req => {
    const {steps} = parseSteps(req.summary);
    const cur = currentStep(steps);
    const res = resolveTarget(steps[cur].spot.window, windows);
    const frame = spotRect(res.window.frame, steps[cur].spot);
    const p = placeNearTarget(res.window.frame, CARD_W, CARD_H, workArea, [], frame);
    return p.x === specsAndRects.find(s => s.req === req).placed.x;
}), 'placements are independent of the active/chords-routed request');

// the resolver distinguishes one, none, and several — it never guesses
ok(resolveTarget('firefox', windows).status === 'unique' &&
   resolveTarget('firefox', windows).window.frame.x === 60,
   'one case-insensitive match resolves unique with its frame');
ok(resolveTarget('no-such-window', windows).status === 'no-match' &&
   resolveTarget('no-such-window', windows).matches.length === 0,
   'an unmatched locator is no-match, not a fallback corner');
const twins = windows.concat([win('Firefox', 0, 0, 100, 100, 1)]);
ok(resolveTarget('firefox', twins).status === 'ambiguous' &&
   resolveTarget('firefox', twins).matches.length === 2,
   'two matches are ambiguous — the most-recently-used guess is gone');
ok(resolveTarget('', windows).status === 'untargeted' &&
   resolveTarget(null, windows).status === 'untargeted',
   'no query is untargeted, not no-match');
ok(matchWindows('FIREFOX', windows).length === 1 &&
   matchWindows('ed', windows).length === 1 &&
   matchWindows('e', windows).length === 2,
   'matching is a case-insensitive substring over title/class/instance');
// the target MOVES: re-resolving (what the 2 s scan does) puts card and
// frame beside the window's NEW geometry
const movedWindows = [win('Firefox', 1200, 500, 600, 400, 5),
                      ...windows.slice(1)];
const movedSpot = parseSpot('Firefox · full');
const movedFrame = spotRect(resolveTarget('Firefox', movedWindows).window.frame,
                            movedSpot);
const movedCard = placeNearTarget(
    resolveTarget('Firefox', movedWindows).window.frame, CARD_W, CARD_H,
    workArea, [], movedFrame);
ok(movedFrame.x === 1200 && movedFrame.y === 500 &&
   movedCard.x >= workArea.x &&
   movedCard.x + CARD_W <= workArea.x + workArea.width &&
   movedCard.y >= workArea.y &&
   movedCard.y + CARD_H <= workArea.y + workArea.height &&
   !(movedCard.x < movedFrame.x + movedFrame.width &&
     movedCard.x + CARD_W > movedFrame.x &&
     movedCard.y < movedFrame.y + movedFrame.height &&
     movedCard.y + CARD_H > movedFrame.y),
   'a moved window re-resolves: the frame follows it and the card re-anchors beside the new position, clamped and clear of the region');
const minim = windows.concat([{...win('Min', 0, 0, 100, 100), minimized: true}]);
ok(matchWindows('min', minim).length === 0,
   'a minimised window never matches — a frame around nothing helps nobody');

// target loss: bounded grace, then an explicit engine-facing error
const now = 1000.0;
ok(targetExpired(null, now) === false,
   'never seen unresolved -> no expiry (a region-only locator cannot expire)');
ok(targetExpired(now - TARGET_GRACE_S + 1, now) === false &&
   targetExpired(now - TARGET_GRACE_S, now) === true,
   'the error fires only after the FULL bounded grace period');
const errAns = targetErrorAnswer({status: 'no-match', matches: []}, 'Firefx');
ok(errAns.error.kind === 'target-unresolved' && errAns.error.reason === 'no-match' &&
   errAns.error.query === 'Firefx' && errAns.text === '' && errAns.cancelled === false,
   'a no-match target writes the machine-readable error, not an empty pass');
const errAmb = targetErrorAnswer({status: 'ambiguous', matches: [{}, {}]}, 'hub');
ok(errAmb.error.reason === 'ambiguous' && errAmb.error.matches === 2 &&
   errAmb.error.message.includes('2 windows'),
   'an ambiguous target names the ambiguity and the count');

// canonical responder geometry: logical stage coordinates are clipped before
// conversion into one monitor's physical-pixel coordinates, with edges inward.
const monitors = [
    {index: 0, x: 0, y: 0, width: 1920, height: 1080, scale: 1},
    {index: 1, x: 1920, y: -120, width: 1280, height: 800, scale: 1.5},
];
const inward = monitorPhysicalRect(
    {x: 1919.1, y: -119.4, width: 101.2, height: 52.9}, monitors[1], 1.5);
ok(inward.x === 0 && inward.y === 1 && inward.width === 150 && inward.height === 79,
   'mixed-scale geometry clips in logical space and rounds physical crop edges inward');
ok(monitorPhysicalRect(
       {x: 1910, y: -200, width: 5, height: 5}, monitors[1], 1.5) === null,
   'an empty clipped crop is rejected instead of capturing neighbouring pixels');
const canonicalWindows = [{
    title: 'Firefox', wmClass: 'firefox', wmInstance: 'Navigator',
    stableSequence: '42', monitor: 1, minimized: false,
    frame: {x: 2000, y: 0, width: 800, height: 600},
}];
const canonical = canonicalTarget(
    {window: 'Firefox', region: 'top-right', rect: null},
    canonicalWindows, monitors, 0);
ok(canonical.state === 'resolved' &&
   canonical.identity.stable_sequence === '42' &&
   canonical.geometry.coordinate_space ===
       'monitor-relative-physical-pixels/v1' &&
   canonical.geometry.monitor.logical.x === 1920 &&
   canonical.geometry.scale === 1.5,
   'canonical target binds stable Mutter identity and monitor-relative physical geometry');
// A request binds once and refreshes geometry by identity, even when labels
// change and new same-class windows arrive. Local and remote use this ledger.
const bindings = new WindowBindings();
const boundLocator = {window: 'Firefox', region: 'top-right', rect: null};
const boundSteps = [{id: 'step-001', target: boundLocator}];
const originalVector = canonicalTargetVector(boundSteps, canonicalWindows, monitors, 0, bindings);
const renamed = {...canonicalWindows[0], title: 'New document',
    wmClass: 'changed-class', wmInstance: 'changed-instance',
    frame: {...canonicalWindows[0].frame, x: 2100}};
const popup = {...canonicalWindows[0], stableSequence: '43', title: 'Firefox popup'};
const another = {...canonicalWindows[0], stableSequence: '44'};
const changedWindows = [popup, another, renamed];
ok(bindings.resolve('FIREFOX', changedWindows).window === renamed,
   'a bound window survives title/class changes and popup or unrelated same-class arrival');
const retainedVector = canonicalTargetVector(boundSteps, changedWindows, monitors, 0, bindings);
ok(retainedVector.state === 'resolved' &&
   retainedVector.targets[0].identity.stable_sequence === '42' &&
   retainedVector.targets[0].geometry.rect.x !== originalVector.targets[0].geometry.rect.x &&
   spotRect(bindings.resolve('Firefox', changedWindows).window.frame, boundLocator).x === 2633,
   'card/frame and canonical crop share the bound identity and use its new geometry');
ok(new WindowBindings().resolve('Firefox', changedWindows).status === 'ambiguous',
   'a new request still rejects ambiguous initial acquisition; it inherits no other binding');
ok(bindings.resolve('Firefox popup', changedWindows).window === popup &&
   bindings.resolve('Firefox', changedWindows).window === renamed,
   'an explicit new popup locator acquires its own identity without changing the original target');
const minimizedWindows = [popup, another, {...renamed, minimized: true}];
ok(bindings.resolve('Firefox', minimizedWindows).status === 'no-match' &&
   canonicalTargetVector(boundSteps, minimizedWindows, monitors, 0, bindings).state === 'missing' &&
   bindings.resolve('Firefox', changedWindows).window === renamed,
   'minimization hides the target and restoration resumes the same identity, never a popup');
ok(bindings.resolve('Firefox', [popup, another]).status === 'no-match' &&
   canonicalTargetVector(boundSteps, [another], monitors, 0, bindings).state === 'missing',
   'destroyed original does not transfer its frame or crop to a same-title replacement');
const lateBinding = new WindowBindings();
ok(lateBinding.resolve('Firefox', []).status === 'no-match' &&
   lateBinding.resolve('Firefox', [popup, another]).status === 'ambiguous' &&
   lateBinding.resolve('Firefox', [another]).window === another,
   'an unbound locator can acquire later after absence or initial ambiguity clears');
ok(new WindowBindings().resolve('Firefox', [{...another, stableSequence: null}]).status === 'no-match' &&
   bindings.resolve('', changedWindows).status === 'untargeted',
   'missing stable identity fails safely; untargeted steps remain untargeted');
const stableGeneration = advanceGeometry(null, {state: 'resolved', targets: originalVector.targets}, 0);
const titleOnly = canonicalTargetVector(boundSteps,
    [{...canonicalWindows[0], title: 'Renamed'}, popup], monitors, 0, bindings);
ok(advanceGeometry(stableGeneration.record, titleOnly, stableGeneration.watermark).record.generation ===
       stableGeneration.record.generation &&
   advanceGeometry(stableGeneration.record, retainedVector, stableGeneration.watermark).changed,
   'title/popup changes keep geometry generation, while movement advances it');
const exactFraction = canonicalTarget(
    {window: 'Firefox', region: null, rect: [0.101, 0.203, 0.111, 0.127]},
    canonicalWindows, monitors, 0);
ok(JSON.stringify(exactFraction.geometry.rect) ===
       JSON.stringify({x: 242, y: 363, width: 132, height: 114}),
   'fractional canonical crop rounds every edge inward without the UI 24px expansion');
const subpixel = canonicalTarget(
    {window: 'Firefox', region: null, rect: [0.99, 0.99, 0.0005, 0.0005]},
    canonicalWindows, monitors, 0);
ok(subpixel.state === 'missing' && subpixel.reason === 'empty-crop' &&
   subpixel.geometry === null,
   'a canonical crop with no whole physical pixel is missing, never enlarged to a neighbour');
// Check serialized records, including the identity retained by an empty crop.
// Distinct strings catch content moved to a different key as well as raw fields.
const privateWindow = {...canonicalWindows[0],
    title: 'PrivateDocument-Canary-917',
    wmClass: 'PrivateClass-Canary-283', wmInstance: 'PrivateInstance-Canary-654'};
for (const rect of [null, [0.99, 0.99, 0.0005, 0.0005]]) {
    for (const query of [privateWindow.title, privateWindow.wmClass, privateWindow.wmInstance]) {
        const record = canonicalTargetVector([
            {id: 'step-001', target: {window: query, region: 'full', rect}},
        ], [privateWindow], monitors, 0);
        const serialized = JSON.stringify(record);
        ok(record.state === (rect ? 'missing' : 'resolved') &&
           JSON.stringify(record.targets[0].identity) === JSON.stringify({
               kind: 'mutter-window', stable_sequence: '42', monitor: 1}) &&
           ['title', 'wm_class', 'wm_instance'].every(key => !serialized.includes(`"${key}"`)) &&
           [privateWindow.title, privateWindow.wmClass, privateWindow.wmInstance]
               .every(value => !serialized.includes(value)),
           'local title/class/instance matching publishes no window content, including empty crops');
    }
}
ok(JSON.stringify(canonicalTarget({region: 'full'}, [], monitors, 0).identity) ===
       JSON.stringify({kind: 'mutter-monitor', monitor: 0}),
   'monitor-only canonical identity stays kind and monitor');
const overflowCrop = canonicalTarget(
    {window: 'Firefox', region: null, rect: [0.9, 0, 0.5, 1]},
    canonicalWindows, monitors, 0);
ok(JSON.stringify(overflowCrop.geometry.rect) ===
       JSON.stringify({x: 1200, y: 180, width: 120, height: 900}),
   'fractional overflow clips to the target window before monitor conversion');
const beyondTarget = canonicalTarget(
    {window: 'Firefox', region: null, rect: [1.1, 0, 0.2, 1]},
    canonicalWindows, monitors, 0);
ok(beyondTarget.state === 'missing' && beyondTarget.reason === 'empty-crop' &&
   beyondTarget.geometry === null,
   'a fractional crop wholly beyond its target is empty, never a neighbouring window');
const vector = canonicalTargetVector([
    {id: 'step-001', target: {window: 'Firefox', region: 'top-right', rect: null}},
    {id: 'step-002', target: null},
], canonicalWindows, monitors, 0);
ok(vector.state === 'resolved' && vector.targets.map(t => t.id).join(',') ===
   'step-001,step-002' && vector.targets[1].state === 'untargeted',
   'one aggregate target vector preserves every server-stamped step id and order');
const candidate = {schema: 'human-copilot.target-state/v1', schema_version: 1,
                   request_id: 'r', request_revision: 'rev', ...vector};
const firstGeneration = advanceGeometry(null, candidate, 9);
const sameGeneration = advanceGeometry(firstGeneration.record,
    {...candidate, observed_at: 123, targets: candidate.targets.map(t =>
        ({...t, reason: 'metadata changed', matches: 99}))},
    firstGeneration.watermark);
const movedGeneration = advanceGeometry(sameGeneration.record,
    {...candidate, targets: candidate.targets.map((t, i) => i ? t :
        ({...t, geometry: {...t.geometry,
            rect: {...t.geometry.rect, x: t.geometry.rect.x + 1}}}))},
    sameGeneration.watermark);
ok(firstGeneration.record.generation === 10 &&
   sameGeneration.record.generation === 10 &&
   movedGeneration.record.generation === 11,
   'geometry generation survives a watermark, ignores labels/reasons, and advances only on canonical state');
const leaseReq = {id: 'r', request_revision: 'rev'};
const leaseRecord = {schema: 'human-copilot.responder-lease/v1', schema_version: 1,
                     request_id: 'r', request_revision: 'rev', expires_at: 110};
ok(remoteLeaseActive(leaseRecord, leaseReq, 100) &&
   !remoteLeaseActive(leaseRecord, leaseReq, 111) &&
   !remoteLeaseActive({...leaseRecord, request_revision: 'old'}, leaseReq, 100),
   'only an unexpired lease for this exact request revision hides a local surface');

// placement: clamping, spot avoidance, stacking on one target, wide and narrow
const narrowArea = {x: 0, y: 28, width: 800, height: 600};
const edgeTarget = {x: 700, y: 300, width: 90, height: 90};   // hugging the right edge
const pRight = placeNearTarget(edgeTarget, CARD_W, CARD_H, narrowArea, []);
ok(pRight.x + pRight.width <= narrowArea.width && pRight.x >= narrowArea.x,
   'a card beside an edge-hugging target is clamped back into the work area');
const offscreenTarget = {x: -300, y: -200, width: 200, height: 150};
const pOff = placeNearTarget(offscreenTarget, CARD_W, CARD_H, narrowArea, []);
ok(pOff.x >= narrowArea.x && pOff.y >= narrowArea.y &&
   pOff.x + pOff.width <= narrowArea.x + narrowArea.width &&
   pOff.y + pOff.height <= narrowArea.y + narrowArea.height,
   'a target partially off-screen still yields a fully visible card');
const spotRegion = {x: 100, y: 100, width: 300, height: 200};
const busyTarget = {x: 60, y: 60, width: 480, height: 360};
const pSpot = placeNearTarget(busyTarget, CARD_W, CARD_H, workArea, [], spotRegion);
ok(!(pSpot.x < spotRegion.x + spotRegion.width && pSpot.x + pSpot.width > spotRegion.x &&
     pSpot.y < spotRegion.y + spotRegion.height && pSpot.y + pSpot.height > spotRegion.y) ||
   pSpot.coversSpot,
   'the card avoids the highlighted region while reporting honestly when it cannot');
// several cards for ONE target stack without occluding each other
const oneTarget = {x: 500, y: 400, width: 600, height: 400};
const stacked = [];
for (let i = 0; i < 3; i++) {
    stacked.push(placeNearTarget(oneTarget, CARD_W, CARD_H, workArea, stacked));
}
ok(stacked.every((r, i) => !stacked.some((o, j) => j !== i &&
        r.x < o.x + o.width && r.x + r.width > o.x &&
        r.y < o.y + o.height && r.y + r.height > o.y)),
   'three cards for one target cascade apart — none occludes another');
ok(stacked.every(r => r.y >= workArea.y &&
                     r.y + r.height <= workArea.y + workArea.height),
   'cascaded cards stay inside the work area');

// the untargeted layout: concurrent cards use a bounded reading grid; overflow is honest
const sizes = [{width: 460, height: 200}, {width: 460, height: 200}, {width: 460, height: 200}];
const stack = stackCards(workArea, sizes);
ok(stack.rects.length === 3 && stack.dropped === 0 &&
   stack.rects[0].y === stack.rects[1].y && stack.rects[2].y > stack.rects[0].y &&
   stack.rects[0].x < stack.rects[1].x &&
   !stack.rects.some((r, i) => stack.rects.some((o, j) => j > i &&
        r.x < o.x + o.width && r.x + r.width > o.x &&
        r.y < o.y + o.height && r.y + r.height > o.y)),
   'three untargeted cards use a deterministic two-column grid without overlap');
const tinyArea = {x: 0, y: 28, width: 1920, height: 400};
const tight = stackCards(tinyArea, sizes);
ok(tight.rects.length === sizes.length && tight.rects[2] === null &&
   tight.dropped === 1 && tight.rects[0] && tight.rects[1],
   'a card past the work-area floor is DROPPED and counted — rects stays index-aligned (null in its slot)');
// A row is an atomic readable unit: when it cannot fit, its cards are
// omitted together rather than producing an aimless partial column.
const varied = [{width: 460, height: 380}, {width: 460, height: 380},
                {width: 460, height: 40}];
const routed = stackCards(tinyArea, varied);
ok(routed.rects.length === 3 && routed.rects[0] && routed.rects[1] &&
   routed.rects[2] === null && routed.dropped === 1,
   'varied heights: a complete first row fits and the later row is honestly omitted');
const wideArea = {x: 1920, y: 0, width: 1920, height: 1080};   // second monitor
const onTwo = stackCards(wideArea, sizes);
ok(onTwo.rects.every(r => r.x >= wideArea.x && r.x + r.width <= wideArea.x + wideArea.width),
   'the stack clamps to ITS monitor\'s work area (multi-monitor)');
const mixed = stackCards(workArea, [{width: 10, height: 10}]);
ok(mixed.rects[0].x === workArea.x + Math.round((workArea.width - 10) / 2),
   'stackCards centres exactly for an arbitrary card width');
ok(stackCards(workArea, []).rects.length === 0 && stackCards(workArea, []).dropped === 0,
   'no untargeted requests -> an empty stack');
// mixed targeted + untargeted: a targeted card sitting in the stack's column
// reflows the stack below it instead of being covered
const blocker = {x: workArea.x + workArea.width / 2 - 230, y: workArea.y,
                 width: 460, height: 300};
const reflowed = stackCards(workArea, sizes, [blocker]);
ok(reflowed.rects.length === 3 && reflowed.dropped === 0 &&
   reflowed.rects[0] !== undefined,
   'the bounded grid keeps all cards visible around a targeted card');
ok(!reflowed.rects.some(r =>
        r.x < blocker.x + blocker.width && r.x + r.width > blocker.x &&
        r.y < blocker.y + blocker.height && r.y + r.height > blocker.y),
   'no reflowed card intersects the occupied rect');
// FULL mixed model, the way extension.js::_layout composes it: targeted
// cards claim geometry first (each seeing the previous as occupied), then
// the untargeted stack reflows around them — no two of the five overlap and
// every one is inside the work area
const mixedOccupied = [];
const targetedRects = [tA, tB].map(req => {
    const {steps} = parseSteps(req.summary);
    const cur = currentStep(steps);
    const res = resolveTarget(steps[cur].spot.window, windows);
    const frame = spotRect(res.window.frame, steps[cur].spot);
    const rect = placeNearTarget(res.window.frame, CARD_W, CARD_H, workArea,
                                 mixedOccupied, frame);
    mixedOccupied.push(rect);
    return rect;
});
const mixedStack = stackCards(workArea, sizes, mixedOccupied).rects.filter(Boolean);
const allRects = targetedRects.concat(mixedStack);
ok(allRects.length === 5 && mixedStack.length === 3 &&
   !allRects.some((r, i) => allRects.some((o, j) => j > i &&
        r.x < o.x + o.width && r.x + r.width > o.x &&
        r.y < o.y + o.height && r.y + r.height > o.y)) &&
   allRects.every(r => r.x >= workArea.x && r.y >= workArea.y &&
                     r.x + r.width <= workArea.x + workArea.width &&
                     r.y + r.height <= workArea.y + workArea.height),
   'mixed targeted + untargeted: all five cards on screen, pairwise non-overlapping, inside the work area');

// ---- the response contract on the surface (hcop-0gj)
ok(parseResponse({summary: '1. a step'}).mode === 'checklist' &&
   parseResponse({summary: 'plain prose?'}).mode === 'prose',
   'legacy requests derive checklist/prose — a bare summary is never an implicit ack');
ok(parseResponse({summary: 'q', response: {mode: 'choice', options: [
    {value: 'pro', label: 'Pro', hint: '$4/mo'}, {value: 'free', label: 'Free'}]}})
   .mode === 'choice' &&
   parseResponse({summary: 'q', response: {mode: 'choice', options: [
       {value: 'pro', label: 'Pro'}]}}).options.length === 1,
   'a structured choice rides through to the surface');
ok(parseResponse({summary: 'q', response: {mode: 'choice', options: [
    'pro', null, {value: '', label: 'x'}, {label: 'no value'}]}}).options.length === 0,
   'junk options render as no options, never a crash');
const longChoice = {
    value: 'promote_now',
    label: 'Promote the exact tested release now',
    hint: 'Switch four owned links and both MCP commands; the overlay activates next login',
};
ok(choiceDisplayText(longChoice, 0, true) ===
   '▶ 1. Promote the exact tested release now — Switch four owned links and both MCP commands; the overlay activates next login',
   'choice display text preserves the complete label and consequence');
ok(!choiceNeedsScroll([
       {label: 'Keep LIVE', hint: 'No path changes'},
       {label: 'Promote', hint: 'Use the tested release'},
   ]) && choiceNeedsScroll(Array.from({length: 12}, (_, i) => ({
       label: `Complete option ${i + 1}`,
       hint: 'A deliberately long consequence remains reachable in the bounded card',
   }))) && choiceNeedsScroll([longChoice, longChoice], 18, 6),
   'choice overflow is explicit for tall sets and narrow wrapped rows');
ok(parseResponse({summary: '1. x', response: {mode: 'ack', options: []}}).mode === 'ack',
   'an explicit ack is what the engine sent, not a derived shape');
ok(parseResponse({summary: 'legacy prose', response: {mode: 'text', options: []},
    responder: {schema: 'human-copilot.responder-contract/v1', schema_version: 1,
                mode: 'choice', options: [{value: 'v', label: 'Stamped'}]}}).mode ===
   'choice',
   'the server-stamped responder contract is authoritative over legacy response fields');

ok(looksLikeChoices('Upgrade GitHub to Pro? ✔ yes · ✘ stay free') === true &&
   looksLikeChoices('a) stay free\nb) pay') === true &&
   looksLikeChoices('(a) one\n(b) two') === true &&
   looksLikeChoices('proceed? yes/no') === true,
   'decision syntax in prose is detected structurally');
ok(looksLikeChoices('1. click save\n2. reload') === false &&
   looksLikeChoices('what was the weather? plain prose') === false &&
   looksLikeChoices('A. one heading only') === false,
   'ordinary checklists and prose are not misread as decisions');

ok(emptySubmitNote('ack', false) === null,
   'an explicit ack may send empty — the one mode');
ok(emptySubmitNote('choice', false).startsWith('choose an option') &&
   emptySubmitNote('text', false).startsWith('an empty send is not an answer') &&
   emptySubmitNote('checklist', false).startsWith('an empty send is not an answer'),
   'choice/text/checklist all refuse an empty send');
ok(emptySubmitNote('prose', true).startsWith('this reads as a decision') &&
   emptySubmitNote('prose', false).length > 0,
   'choice-like prose can never silently become an empty acknowledgement');

const mhAcc = {'mark-done': 'S+Space', 'submit-answer': 'S+Enter', 'edit-note': 'S+N',
               'undo-done': 'S+Z', 'dismiss-request': 'S+BS'};
ok(modeHint('choice', [], mhAcc, -1, 3).main.includes('next option') &&
   modeHint('choice', [], mhAcc, -1, 3).main.includes('send the selected') &&
   modeHint('choice', [], mhAcc, -1, 3).more.includes('no option selected yet'),
   'a choice card NOW names the selection chords and that nothing is picked yet');
ok(modeHint('choice', [], mhAcc, 1, 3).more.includes('option 2/3 selected'),
   'the NOW line names the selection as it moves');
ok(modeHint('choice', [], mhAcc, -1, 0).main.includes('copilot-tui'),
   'a choice with no options points at the TUI');
ok(modeHint('ack', [], mhAcc).main.includes('confirm'),
   'an ack card NOW names the confirm chord');
for (const mode of ['text', 'prose', 'checklist']) {
    const hint = modeHint(mode, [], mhAcc);
    ok(hint.main.includes('S+N') && hint.main.includes('type your reply') &&
       hint.more.includes('Enter saves') && hint.more.includes('S+Enter') &&
       !hint.main.includes('empty ack'),
       `${mode} without steps offers a typed reply, never a refused empty ack`);
}
ok(modeHint('checklist', parseSteps('1. a\n2. b').steps, mhAcc).main ===
   nowHint(parseSteps('1. a\n2. b').steps, mhAcc).main,
   'checklists keep the classic NOW line');

// OSD / header text: number + session (+ colour name in words)
const coral = PALETTE[2];
ok(instanceName(stamp('hc-A', 'x'), 3, coral) === '3 hc-A (coral)' &&
   instanceName(stamp('hc-A', 'x'), 3) === '3 hc-A' &&
   instanceName(stamp('hc-A', 'x')) === 'hc-A' && instanceName(null, 3, coral) === '',
   'instanceName: number, session, colour name in words; bare label without a number; nothing unstamped');
ok(instanceSuffix(stamp('hc-A', 'x'), 3, coral) === ' · instance 3 hc-A (coral)' &&
   instanceSuffix(stamp(null, 'abcdef0123456789'), 2, PALETTE[1]) === ' · instance 2 abcdef01 (sky)' &&
   instanceSuffix(null, 3, coral) === '',
   'OSD text carries number + session (+ colour): the misattribution rule, one level up');
ok(overlayHeader({client: 'Claude', deadline: 100, queued: 1, instance: stamp('hc-A', 'x')}, 40, 3) ===
   '🧿 Claude needs your eyes · ⏳ 1:00 · 3 hc-A · +1 queued',
   'the header puts the instance number in front of the session');
const wNums = new Map([['hc-B', 2], ['hc-D', 4]]);
ok(withdrawnText([{id: 'x', instance: stamp('hc-B', 'b')}], wNums) === 'Copilot request withdrawn · instance 2 hc-B' &&
   withdrawnText([{id: 'x', instance: stamp('hc-B', 'b')}, {id: 'y', instance: stamp('hc-D', 'd')}], wNums) ===
       '2 copilot requests withdrawn · instances 2 hc-B, 4 hc-D',
   'withdrawn OSDs name the instance number when the roster knows it');
ok(arrivedText([{id: 'q', instance: stamp('hc-D', 'd')}], 'other', wNums) ===
   'Copilot: a request waits · instance 4 hc-D — a card opened for it',
   'arrival OSDs name the instance number too');
const svg = swatchSvg(coral, 3);
ok(svg.startsWith('<svg') && svg.includes(`fill="${coral.hex}"`) && svg.includes(`fill="${coral.fg}"`) &&
   svg.includes('>3</text>'),
   'the OSD swatch is the instance colour with its number drawn in the chip ink');
ok(!swatchSvg(coral, 0).includes('<text') && swatchSvg(null, 0).includes(`fill="${SPOT_FALLBACK}"`),
   'no number -> a plain swatch; no entry -> the fallback yellow');


// hcop-4q5: the overlay's idle cost. Production methods are extracted from
// extension.js and run against injected Gio/GLib doubles.
const ATTR = 4;      // any distinct enum value stands in for ATTRIBUTE_CHANGED
ok(!monitorEventRescans(ATTR, ATTR) && monitorEventRescans(1, ATTR) &&
   monitorEventRescans(0, ATTR) && monitorEventRescans(2, ATTR),
   'ATTRIBUTE_CHANGED monitor events do not trigger a rescan; others do');
ok(/monitorEventRescans\(event, Gio\.FileMonitorEvent\.ATTRIBUTE_CHANGED\)\)\s*\n\s*this\._scan\(\)/
       .test(extensionText),
   'the requests monitor handler gates its rescan on monitorEventRescans');

const backoff = new ExpiryBackoff(30);
ok(backoff.begin('a', 100) && !backoff.begin('a', 100),
   'expiry backoff: one helper in flight per request');
backoff.finish('a');
ok(!backoff.begin('a', 101) && !backoff.begin('a', 129.9),
   'expiry backoff: a finished helper still holds the window');
ok(backoff.begin('a', 130), 'expiry backoff: one more attempt once the window elapses');
backoff.finish('a');
ok(backoff.begin('b', 131), 'expiry backoff: windows are per request');
backoff.finish('b');
backoff.prune(['b']);
ok(backoff.begin('a', 131) && !backoff.begin('b', 131),
   'expiry backoff: a request that left the spool is forgotten, a live one is not');

const expireMethod = extensionText.slice(
    extensionText.indexOf('\n    _expireRequest('),
    extensionText.indexOf('\n    _writeAnswer('));
const ExpireHarness = new Function('Gio', 'GLib', 'logError', 'ExpiryBackoff',
    `return class {${expireMethod}\n}`);
let spawns = 0;
let spawnFails = false;
let nowS = 1000;
const finishers = [];
const fakeGio = {
    Subprocess: {new() {
        if (spawnFails)
            throw new Error('spawn refused');
        spawns++;
        return {communicate_utf8_async(_in, _c, cb) {
            finishers.push(() => cb({communicate_utf8_finish() {
                throw new Error('responder could not remove it');
            }}, null));
        }};
    }},
    SubprocessFlags: {STDIN_PIPE: 1, STDOUT_PIPE: 2, STDERR_PIPE: 4},
};
const fakeGLib = {get_monotonic_time: () => nowS * 1e6};
const expHarness = new (ExpireHarness(fakeGio, fakeGLib, () => {}, ExpiryBackoff))();
expHarness._responderPath = '/nonexistent/responder.py';
expHarness._spool = '/nonexistent/spool';
expHarness._expiring = new ExpiryBackoff(30);
const stale = {id: 'deadbeef0001', request_revision: 'r1', deadline: 1};
// An unremovable expired request is rescanned every TICK_S for two minutes.
for (nowS = 1000; nowS < 1120; nowS += 2) {
    expHarness._expireRequest(stale);
    while (finishers.length)
        finishers.shift()();     // the helper exits at once, request remains
}
ok(spawns === 4,
   `an unremovable expired request spawns one helper per backoff window (${spawns} in 120 s)`);
spawnFails = true;
const before = spawns;
for (nowS = 2000; nowS < 2060; nowS += 2)
    expHarness._expireRequest({id: 'deadbeef0002', deadline: 1});
ok(spawns === before,
   'a helper that fails to start is also held to the backoff window');

const statusMethod = extensionText.slice(
    extensionText.indexOf('\n    _writeRuntimeStatus('),
    extensionText.indexOf('\n    // -- spool'));
ok(!statusMethod.includes('.move(') && statusMethod.includes('file_set_contents'),
   'runtime status uses a plain rename (file_set_contents), never Gio.File.move');
const StatusHarness = new Function('Gio', 'GLib', 'logError', 'TextEncoder',
    'OVERLAY_RUNTIME_FILE', `return class {${statusMethod}\n}`);
const writes = [];
let writeFails = false;
let statusMono = 0;
let statusReal = 5000;
const statusGLib = {
    build_filenamev: parts => parts.join('/'),
    get_monotonic_time: () => statusMono * 1e6,
    get_real_time: () => statusReal * 1e6,
    FileSetContentsFlags: {CONSISTENT: 1},
    file_set_contents_full(path, data, flags, mode) {
        if (flags !== 1 || mode !== 0o600)
            throw new Error('status must be atomically replaced with private permissions');
        if (writeFails)
            throw new Error('disk full');
        writes.push({path, body: JSON.parse(new TextDecoder().decode(data))});
    },
};
const stHarness = new (StatusHarness(
    {}, statusGLib, () => {}, TextEncoder, 'overlay-runtime.json'))();
stHarness._spool = '/spool';
stHarness._views = new Map();
stHarness._cards = new Map();
stHarness._status = new StatusWriter(6);
stHarness._writeRuntimeStatus('healthy');
ok(writes.length === 1 && writes[0].path === '/spool/overlay-runtime.json' &&
   writes[0].body.state === 'healthy' && writes[0].body.observed === 5000,
   'first runtime status is written to the plain final path');
for (statusMono = 2; statusMono < 6; statusMono += 2) {
    statusReal += 2;
    stHarness._writeRuntimeStatus('healthy');
}
ok(writes.length === 1, 'unchanged status inside the heartbeat window is not rewritten');
stHarness._views.set('r1', {});
stHarness._writeRuntimeStatus('healthy');
ok(writes.length === 2 && writes[1].body.pending_requests === 1,
   'a content change is written immediately');
statusMono = 12; statusReal += 6;
stHarness._writeRuntimeStatus('healthy');
ok(writes.length === 3 && writes[2].body.observed === statusReal,
   'an unchanged status is refreshed once the heartbeat elapses, with a new stamp');
writeFails = true;
statusMono = 13;
ok(stHarness._writeRuntimeStatus('error', new Error('boom')) === false,
   'a failed status write reports failure');
writeFails = false;
statusMono = 13.5;
ok(stHarness._writeRuntimeStatus('error', new Error('boom')) === true &&
   writes.length === 4 && writes[3].body.error.message === 'boom',
   'after a failed write the same status is retried, not suppressed');

print(`all good — ${n} checks`);
