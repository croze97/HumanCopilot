// palette.js — GENERATED from palette.py, do not edit:
//     python3 palette.py --emit-js > overlay/copilot-overlay@croze/palette.js
// One source of truth for the per-instance colours on every surface.
export const OVERLAY_BG = '#10141c';
export const TERMINAL_BG = '#1e1e1e';
export const TEXT_THRESHOLD = 4.5;
export const UI_THRESHOLD = 3.0;
export const PALETTE = Object.freeze([
    Object.freeze({number: 1, name: 'gold', hex: '#ffd700', fg: '#1a1a1a', xterm: 220}),
    Object.freeze({number: 2, name: 'sky', hex: '#5fafff', fg: '#1a1a1a', xterm: 75}),
    Object.freeze({number: 3, name: 'coral', hex: '#ff875f', fg: '#1a1a1a', xterm: 209}),
    Object.freeze({number: 4, name: 'green', hex: '#5fd75f', fg: '#1a1a1a', xterm: 77}),
    Object.freeze({number: 5, name: 'pink', hex: '#ff5fff', fg: '#1a1a1a', xterm: 207}),
    Object.freeze({number: 6, name: 'cyan', hex: '#00d7d7', fg: '#1a1a1a', xterm: 44}),
    Object.freeze({number: 7, name: 'rose', hex: '#ff5f87', fg: '#1a1a1a', xterm: 204}),
    Object.freeze({number: 8, name: 'violet', hex: '#af87ff', fg: '#1a1a1a', xterm: 141}),
    Object.freeze({number: 9, name: 'silver', hex: '#d0d0d0', fg: '#1a1a1a', xterm: 252}),
]);

// The entry a 1-based slot draws; past the table it wraps (the slot
// number is then what tells two instances apart).
export function entryFor(slot) {
    return PALETTE[(slot - 1) % PALETTE.length];
}
