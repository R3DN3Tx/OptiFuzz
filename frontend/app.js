"use strict";

// `let`, not `const` -- render() below temporarily repoints this at an
// offscreen scratch element while building a new page, then back at the
// real one. Every render*/build* function appends into whatever `app`
// currently is, transparently either way.
let app = document.getElementById("app");
const breadcrumb = document.getElementById("breadcrumb");
// The scrolling element -- header lives entirely outside this (a plain,
// non-scrolling flex row above it, see index.html/styles.css), so this is
// what actually scrolls now, not window/document.
const scrollArea = document.getElementById("scroll-area");

let currentStream = null;
let currentResultsPoll = null;
let currentJobsTicker = null;

// ---- Per-tab scroll memory -----------------------------------------------
// render() clears #app and rebuilds it on every route change (including
// switching JOBS/RESULTS/TREE); while #app is briefly empty, the browser
// clamps #scroll-area's scroll position to whatever's scrollable at that
// instant (effectively 0), so without this every tab switch reset scroll
// to the top instead of returning to where that specific tab was left.
// Keyed by route (JOBS/RESULTS/TREE of the same target are three distinct
// keys, each remembering its own position), not by DOM node, since the
// whole subtree is torn down and rebuilt every time anyway.
const scrollPositions = new Map();
let currentRouteKey = null;
scrollArea.addEventListener("scroll", () => {
    if (currentRouteKey) scrollPositions.set(currentRouteKey, scrollArea.scrollTop);
});

// ---- NEW-badge watermark (Results tab) -----------------------------------
// Explicit request (2026-09-26): NEW must stay visible for the whole time
// the pentester is actually looking at Results, and only clear once they've
// left and come back -- so returning shows exactly what's new SINCE that
// last visit, not a flash that's already gone before it can be read.
// Marking seen now happens on LEAVING Results (render() below, right where
// it already tears down the outgoing route), not on entering it -- the
// opposite of when this used to fire. {projectId, targetId} while the
// Results tab is the one currently mounted, else null.
let activeResultsView = null;
// Set by the Results view while it is mounted: re-paints its visible window
// once render() has restored the real scroll position after a swap.
let resultsRepaint = null;
// Set by the Results view while mounted: scrolls to a given path and expands
// exactly its own direct children (see goToResultsAndReveal below).
let resultsReveal = null;
// One-shot "jump to this path in Results" request, consumed by render()'s
// own tail the next time it lands on this exact project+target's Results
// tab -- set by goToResultsAndReveal(), e.g. clicking a Results count in a
// JOBS/TREE Fuzzed-history panel (explicit request).
let pendingResultsReveal = null;
function goToResultsAndReveal(projectId, targetId, path) {
    pendingResultsReveal = { projectId, targetId, path };
    location.hash = `#/projects/${projectId}/targets/${targetId}/results`;
}


// "200, 301" -> Set{200, 301}. Anything that isn't a whole number is ignored.
function parseStatusFilter(text) {
    return new Set(String(text || "").split(",").map((s) => s.trim()).filter((s) => /^\d+$/.test(s)).map(Number));
}

// ---- Collapsed-group memory (Results/TREE "hide children" arrow) --------
// Same reasoning and lifetime as scrollPositions above: render() tears down
// and rebuilds Results/TREE's whole subtree on every tab switch, so a Set
// declared fresh inside renderWorkspace()/renderTreeSection() forgot every
// collapsed group the moment you left the tab. Keyed by section+target (not
// by DOM node, for the same reason as scrollPositions), one Set per
// section -- getCollapsedSet() returns the SAME Set object across
// re-renders for a given key, so add()/delete() calls on it already persist
// in-session with no separate save step needed.
//
// Also backed by the backend ui-prefs (via toggleCollapsed(), the only place a key
// is ever added/removed -- both call sites go through it instead of mutating
// the Set directly) -- explicit request: this used to only survive
// switching tabs within the same page load, not an app restart. A brand
// new parent (never toggled before, so absent from the persisted list) is
// expanded by default, same as before -- only ever adding a key to this
// list collapses it, so anything not listed stays expanded automatically.
const collapsedGroupsByRoute = new Map();
function getCollapsedSet(routeKey) {
    if (!collapsedGroupsByRoute.has(routeKey)) {
        collapsedGroupsByRoute.set(routeKey, new Set(loadPref(`collapsed:${routeKey}`, [])));
    }
    return collapsedGroupsByRoute.get(routeKey);
}
function toggleCollapsed(routeKey, key) {
    const set = getCollapsedSet(routeKey);
    if (set.has(key)) set.delete(key);
    else set.add(key);
    savePref(`collapsed:${routeKey}`, [...set]);
}

function routeKeyFor(route) {
    if (route.view === "workspace") return `workspace/${route.projectId}/${route.targetId}/${route.tab}`;
    if (route.view === "targets") return `targets/${route.projectId}`;
    return "projects";
}

// ---- Small persisted-preference helper -------------------------------------
// Per-viewer UI preferences (last wordlist picked, which result/tree groups
// are collapsed, ...) live on the backend (/api/settings/ui-prefs), not in
// localStorage: localStorage is tied to the page origin (port included), so
// a relaunch on another port lost all of them. Loaded once at startup into
// prefsCache (see ensurePrefs), so loadPref() stays synchronous for callers.
let prefsCache = null;
let prefsReady = null;

async function bootPrefs() {
    try {
        prefsCache = await api("/api/settings/ui-prefs");
    } catch (e) {
        prefsCache = {};
        return;
    }
    // One-time move of anything still sitting in this browser's localStorage.
    for (let i = localStorage.length - 1; i >= 0; i--) {
        const storageKey = localStorage.key(i);
        if (!storageKey || !storageKey.startsWith("optifuzz:")) continue;
        const key = storageKey.slice("optifuzz:".length);
        try {
            if (!(key in prefsCache)) {
                const value = JSON.parse(localStorage.getItem(storageKey));
                prefsCache[key] = value;
                await api("/api/settings/ui-prefs", { method: "PUT", body: JSON.stringify({ key, value }) });
            }
            localStorage.removeItem(storageKey);
        } catch (e) { /* keep it in localStorage and try again next launch */ }
    }
}

function ensurePrefs() {
    if (!prefsReady) prefsReady = bootPrefs();
    return prefsReady;
}

function loadPref(key, fallback) {
    if (!prefsCache || !(key in prefsCache)) return fallback;
    return prefsCache[key];
}

function savePref(key, value) {
    if (prefsCache) prefsCache[key] = value;
    api("/api/settings/ui-prefs", { method: "PUT", body: JSON.stringify({ key, value }) })
        .catch(() => { /* best-effort -- the value is still in memory for this session */ });
}

// Coalesces rapid-fire calls (every keystroke) into one, `wait` ms after
// the last one -- used for autosaving New Fuzz's per-target defaults
// (buildNewFuzzForm) so typing in e.g. Extensions doesn't fire a PUT per
// character.
function debounce(fn, wait) {
    let timer = null;
    return (...args) => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(() => fn(...args), wait);
    };
}

// ---- Icon set -------------------------------------------------------------
// STANDING RULE, so this doesn't regress again: every icon in the app is one
// of these named constants (an inline SVG, stroke="currentColor"/
// fill="currentColor" so it inherits the button's own color/active-state
// color) -- never a raw emoji character, and never a one-off inline SVG
// string built somewhere else. Toggle-state icons (fuzz launcher, mark
// interesting, add-to-tree) come in an OUTLINE/FILLED pair at the same
// stroke-width (1.4) and a consistent ~2-14 bounding box on the 16x16
// viewBox, so they all read as the same visual weight -- don't add a new
// icon at a different stroke-width or point spread without matching this.
const TRASH_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round"><path d="M2 4h12M5.5 4V2.5a1 1 0 0 1 '
    + '1-1h3a1 1 0 0 1 1 1V4M6.2 7v5M9.8 7v5M3.5 4l.7 8.9a1 1 0 0 0 1 .9h5.6a1 1 0 0 0 1-.9l.7-8.9"/></svg>';
// A small "×" for clearing a search field -- replaces the browser's own
// native search-cancel button (a plain built-in glyph, inconsistent with
// everything else here) wherever a <input type="search"> is built via
// buildSearchInput() below.
const CLOSE_ICON_SVG = '<svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.6" stroke-linecap="round"><path d="M4 4l8 8M12 4l-8 8"/></svg>';
// "Fuzz this path" -- empty/outline by default, fills solid (orange when
// active, via .icon-btn.fuzz-active) once the launcher is open, same
// open/closed language as the star below.
const LIGHTNING_POINTS = "8.7 2 3.2 9.3 6.4 9.3 5.5 14 12.3 6.6 8.9 6.6 8.7 2";
const LIGHTNING_OUTLINE_ICON_SVG = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" `
    + `stroke-width="1.4" stroke-linejoin="round"><polygon points="${LIGHTNING_POINTS}"/></svg>`;
const LIGHTNING_FILLED_ICON_SVG = `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" `
    + `stroke-width="1.4" stroke-linejoin="round"><polygon points="${LIGHTNING_POINTS}"/></svg>`;

// "Add to TREE" -- a real tree silhouette (canopy + trunk), not the
// sitemap/branch glyph this used to be. Outline while not in TREE, filled
// green (.icon-btn.in-tree) once the path has a TREE node.
const TREE_OUTLINE_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="8" cy="6" r="4.3"/><path d="M8 10.3V14M5.8 14h4.4"/></svg>';
const TREE_FILLED_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="8" cy="6" r="4.3"/><path d="M8 10.3V14M5.8 14h4.4"/></svg>';

// "Mark interesting" -- outline while unmarked, filled once marked, instead
// of the ☆/⭐ emoji pair.
const STAR_POINTS = "8 2.3 9.6 5.9 13.5 6.4 10.6 9.1 11.3 13 8 11.1 4.7 13 5.4 9.1 2.5 6.4 6.4 5.9 8 2.3";
const STAR_OUTLINE_ICON_SVG = `<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" `
    + `stroke-width="1.4" stroke-linejoin="round"><polygon points="${STAR_POINTS}"/></svg>`;
const STAR_FILLED_ICON_SVG = `<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" `
    + `stroke-width="1.4" stroke-linejoin="round"><polygon points="${STAR_POINTS}"/></svg>`;

// "Choose a folder" (outputs-mirror directory picker) -- always solid/filled
// (colored yellow via CSS, not a toggle pair like the others above), a
// plain folder silhouette.
const FOLDER_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<path d="M2 4.4a1 1 0 0 1 1-1h3.4a1 1 0 0 1 .8.4l1.3 1.7H13a1 1 0 0 1 1 1v6.1a1 1 0 0 1-1 1H3a1 1 0 0 1-1-1V4.4z"/></svg>';

// "Fuzz options" (JOBS row) -- an "i" in a circle, toggling the same
// fuzz-history panel the "Fuzzed" tag opens elsewhere, just for this one
// job. Outline while closed, blue (.job-info-btn.active, matching the
// "Fuzzed" tag's own active color) once open.
const INFO_ICON_SVG = '<svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" '
    + 'stroke-width="1.4" stroke-linecap="round" stroke-linejoin="round">'
    + '<circle cx="8" cy="8" r="6.3"/><line x1="8" y1="7.3" x2="8" y2="11.3"/><circle cx="8" cy="4.9" r="0.15" fill="currentColor"/></svg>';

// A labeled button with one of the inline SVG icons above in front of its
// text, instead of a leading emoji character. `.icon-text-btn` (flex,
// centered) is what actually keeps the icon+text centered as a unit inside
// the button -- plain inline content here left both sitting off-center,
// baseline-aligned against the button's own padding box instead of centered
// in it.
function buildIconTextButton(tag, svgIcon, label, attrs) {
    const mergedAttrs = { ...attrs, class: attrs && attrs.class ? `${attrs.class} icon-text-btn` : "icon-text-btn" };
    const btn = h(tag, mergedAttrs);
    const iconSpan = h("span", { class: "btn-icon" });
    iconSpan.innerHTML = svgIcon;
    btn.appendChild(iconSpan);
    btn.appendChild(document.createTextNode(label));
    return btn;
}

// A <input type="search"> with a custom "×" clear button (CLOSE_ICON_SVG)
// instead of the browser's own native search-cancel button -- that one is
// hidden entirely via CSS (input[type="search"]::-webkit-search-cancel-button)
// since it can't be restyled to match this app's own icon set otherwise.
// Returns {element, input}: `element` is what to append to the DOM,
// `input` is the actual <input> (same object every call site already
// reads .value from / attaches its own "input" listener to).
function buildSearchInput(attrs) {
    const outerAttrs = { ...attrs };
    const wrapClass = outerAttrs.class ? `search-field ${outerAttrs.class}` : "search-field";
    delete outerAttrs.class;
    const input = h("input", { ...outerAttrs, type: "search", class: "search-field-input" });
    const clearBtn = h("button", { type: "button", class: "search-clear-btn", title: "Clear" });
    clearBtn.innerHTML = CLOSE_ICON_SVG;
    clearBtn.hidden = !input.value;
    clearBtn.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        input.value = "";
        input.dispatchEvent(new Event("input", { bubbles: true }));
        clearBtn.hidden = true;
        input.focus();
    });
    input.addEventListener("input", () => { clearBtn.hidden = !input.value; });
    return { element: h("div", { class: wrapClass }, [input, clearBtn]), input };
}

async function api(path, opts) {
    const res = await fetch(path, {
        headers: { "Content-Type": "application/json" },
        ...opts,
    });
    if (!res.ok) {
        let detail = res.statusText;
        try {
            const body = await res.json();
            detail = body.detail || JSON.stringify(body);
        } catch (e) { /* body wasn't JSON */ }
        throw new Error(detail);
    }
    if (res.status === 204) return null;
    return res.json();
}

function h(tag, attrs, children) {
    const el = document.createElement(tag);
    // Default every input/textarea to no spellcheck (no red squiggly
    // underlines) -- set before the loop below so an explicit `spellcheck`
    // in attrs (none currently pass one) would still win.
    if (tag === "input" || tag === "textarea") el.setAttribute("spellcheck", "false");
    for (const [k, v] of Object.entries(attrs || {})) {
        if (k === "text") el.textContent = v;
        else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
        else el.setAttribute(k, v);
    }
    for (const child of children || []) {
        if (child) el.appendChild(child);
    }
    return el;
}

function statusClass(status) {
    if (status === 401 || status === 403 || status === 429) return `status status-${status}`;
    const family = Math.floor(status / 100);
    return family >= 1 && family <= 5 ? `status status-${family}` : "status status-other";
}

// ---- In-app modal (replaces native alert() for content that needs real
// styling/scrolling -- e.g. a FAILED job's full error text) --------------
function showModal(title, bodyText) {
    const overlay = h("div", { class: "modal-overlay" });
    function close() { overlay.remove(); document.removeEventListener("keydown", onKeydown); }
    function onKeydown(e) { if (e.key === "Escape") close(); }
    overlay.addEventListener("click", (e) => { if (e.target === overlay) close(); });
    document.addEventListener("keydown", onKeydown);
    overlay.appendChild(h("div", { class: "modal-box" }, [
        h("div", { class: "modal-header" }, [
            h("h3", { text: title }),
            h("button", { type: "button", class: "modal-close-btn", text: "✕", onclick: close }),
        ]),
        h("div", { class: "modal-body", text: bodyText }),
    ]));
    document.body.appendChild(overlay);
}

// ---- "Already fuzzed" history panel (shared by Results and TREE) --------
function wordlistBaseName(path) {
    if (!path) return "?";
    const parts = String(path).split(/[\\/]/);
    return parts[parts.length - 1] || path;
}

// Wordlist name only (never the full path, even nested in folders) +
// extensions used, taken from the most recent fuzz of this exact path.
function formatFuzzedTag(history) {
    if (!history || !history.length) return null;
    // Every distinct wordlist(+extensions) combo this path was ever fuzzed
    // with, newest first, not just the latest -- explicit request: two
    // fuzzes with different wordlists each need their own extensions shown,
    // not just whichever ran last.
    const seen = new Set();
    const parts = [];
    for (const entry of history) {
        const wordlists = (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?";
        const exts = (entry.extensions || []).join(",");
        const part = exts ? `${wordlists} (${exts})` : wordlists;
        if (!seen.has(part)) {
            seen.add(part);
            parts.push(part);
        }
    }
    return parts.join(", ");
}

// Positions a fixed-position floating box against an anchor element so it
// always stays fully on screen -- flips to whichever side (right-edge
// anchored instead of left-edge, above instead of below) actually has room,
// and caps its height to whatever room that side has (it scrolls
// internally past that, never past the viewport edge). Used for anything
// that needs to "float free" of a small/scrolling ancestor container
// instead of being clipped to it.
function positionFloatingBox(anchor, box, maxWidth) {
    const margin = 8;
    const rect = anchor.getBoundingClientRect();
    const vw = window.innerWidth;
    const vh = window.innerHeight;

    const boxMaxWidth = Math.min(maxWidth, vw - margin * 2);
    box.style.maxWidth = `${boxMaxWidth}px`;

    let left = rect.left;
    if (left + boxMaxWidth > vw - margin) {
        left = Math.max(margin, vw - margin - boxMaxWidth);
    }

    const spaceBelow = vh - rect.bottom - margin;
    const spaceAbove = rect.top - margin;
    let top;
    let maxHeight;
    if (spaceBelow >= 80 || spaceBelow >= spaceAbove) {
        top = rect.bottom + 4;
        maxHeight = Math.max(60, spaceBelow - 4);
    } else {
        maxHeight = Math.max(60, spaceAbove - 4);
        top = Math.max(margin, rect.top - 4 - maxHeight);
    }

    box.style.left = `${left}px`;
    box.style.top = `${top}px`;
    box.style.maxHeight = `${maxHeight}px`;
}

// ---- Right-click "Copy full path" on any clickable path link -----------
// Explicit request: every path link (Results/JOBS/TREE) needs this, and a
// nested row often only DISPLAYS its own last segment (the rest implied by
// indentation) -- the menu always copies the real, full path, never the
// shortened display text.
let openPathContextMenu = null;
function closePathContextMenu() {
    if (openPathContextMenu) {
        openPathContextMenu.remove();
        openPathContextMenu = null;
    }
}
document.addEventListener("click", closePathContextMenu);
document.addEventListener("scroll", closePathContextMenu, true);

function attachCopyPathContextMenu(element, getFullPath) {
    element.addEventListener("contextmenu", (e) => {
        e.preventDefault();
        e.stopPropagation();
        closePathContextMenu();
        const item = h("button", { type: "button", class: "path-context-menu-item", text: "Copy full path" });
        item.addEventListener("click", async (ev) => {
            ev.stopPropagation();
            try {
                await navigator.clipboard.writeText(getFullPath());
            } catch (err) { /* clipboard permission denied or unavailable -- fail silently */ }
            closePathContextMenu();
        });
        const menu = h("div", { class: "path-context-menu" }, [item]);
        menu.style.left = `${e.clientX}px`;
        menu.style.top = `${e.clientY}px`;
        document.body.appendChild(menu);
        openPathContextMenu = menu;
        // Keep it fully on-screen regardless of where the click landed.
        const rect = menu.getBoundingClientRect();
        if (rect.right > window.innerWidth) menu.style.left = `${Math.max(8, window.innerWidth - rect.width - 8)}px`;
        if (rect.bottom > window.innerHeight) menu.style.top = `${Math.max(8, window.innerHeight - rect.height - 8)}px`;
    });
}

// Currently-visible attachTooltip() boxes, keyed by their anchor element --
// swept by the single MutationObserver below instead of relying on
// mouseleave alone. Real bug: the wordlist star's tooltip (attached to a
// button that toggles its OWN "default" state on click, then triggers a
// full re-render replacing that exact button) could get shown on hover,
// then have its anchor element removed by the click's rebuild before the
// mouse ever actually left it -- no mouseleave to hide it, so the box just
// stayed on screen permanently, orphaned. The observer below notices the
// anchor is gone and hides its box, regardless of which call site caused
// the removal or why.
const _visibleTooltipBoxes = new Map(); // element -> box
new MutationObserver(() => {
    if (!_visibleTooltipBoxes.size) return;
    for (const [el, box] of _visibleTooltipBoxes) {
        if (!document.body.contains(el)) {
            box.style.display = "none";
            _visibleTooltipBoxes.delete(el);
        }
    }
}).observe(document.body, { childList: true, subtree: true });

// A custom-styled hover tooltip (dark, matching the app) instead of the
// browser's own native `title=` (rendered by the OS/WebView2 with a plain
// white background that can't be themed). Safe to attach even to an
// element that gets rebuilt/replaced later (e.g. by a click handler on
// that same element) -- see the orphan-cleanup observer above.
function attachTooltip(el, text, maxWidth) {
    const box = h("div", { class: "app-tooltip", text });
    document.body.appendChild(box);
    el.addEventListener("mouseenter", () => {
        if (!document.body.contains(el)) { box.remove(); return; }
        box.style.display = "block";
        positionFloatingBox(el, box, maxWidth || 320);
        _visibleTooltipBoxes.set(el, box);
    });
    el.addEventListener("mouseleave", () => {
        box.style.display = "none";
        _visibleTooltipBoxes.delete(el);
    });
    return el;
}

// Hovering a target card's "currently fuzzing" dot: a vertical list of
// every running job's path + progress, path muted, percent in orange --
// structured content attachTooltip's plain-text version can't do, so this
// builds its own box the same way (same .app-tooltip base + positioning).
function attachRunningJobsTooltip(el, runningJobs) {
    const box = h("div", { class: "app-tooltip running-jobs-tooltip" }, runningJobs.map((j) =>
        h("div", { class: "running-jobs-tooltip-row" }, [
            h("span", { class: "running-jobs-tooltip-path", text: pathWithoutQuery(j.path) }),
            h("span", { class: "running-jobs-tooltip-percent", text: `${j.percent}%` }),
        ])
    ));
    document.body.appendChild(box);
    el.addEventListener("mouseenter", () => {
        if (!document.body.contains(el)) { box.remove(); return; }
        box.style.display = "block";
        positionFloatingBox(el, box, 320);
    });
    el.addEventListener("mouseleave", () => { box.style.display = "none"; });
    return el;
}

// One header NAME chip -- hovering it grows a small box with the full
// "Name: value", positioned against the *viewport* (not this table's own,
// possibly small, container) so it can use whatever screen space it needs
// and open toward whichever side/direction actually has room. Closes the
// instant the mouse leaves this specific chip. Clicking it copies the same
// "Name: value" text to the clipboard, with a brief "Copied!" swap on the
// chip's own label as the only feedback (this app has no toast/snackbar
// system to reuse).
// `fullText` overrides what hover shows and click copies (default "Name: value");
// the job's query string chip passes the bare "?a=a" so it never reads like a header.
function buildHeaderNameChip(name, value, fullText = `${name}: ${value}`) {
    const chip = h("span", { class: "fuzz-history-header-chip", text: name });
    const hoverBox = h("div", { class: "fuzz-history-header-hover", text: fullText });
    chip.appendChild(hoverBox);
    chip.addEventListener("mouseenter", () => {
        hoverBox.style.display = "block";
        positionFloatingBox(chip, hoverBox, 420);
    });
    chip.addEventListener("mouseleave", () => { hoverBox.style.display = "none"; });
    let copyResetTimer = null;
    chip.addEventListener("click", async () => {
        try {
            await navigator.clipboard.writeText(fullText);
        } catch (e) {
            return; // clipboard permission denied or unavailable -- fail silently, no feedback to show
        }
        const label = chip.firstChild; // the text node set by h()'s `text:` option, before hoverBox got appended
        // Real, reported bug: reading label.textContent as "the original" broke
        // on a second click while "Copied!" was still showing -- it captured
        // "Copied!" itself as the value to restore to, and the earlier timeout
        // (still pending) later overwrote the correct restore with that wrong
        // one, leaving the label stuck on "Copied!" forever. `name` (the
        // chip's own closure variable, never mutated) is always the real
        // value to restore to; clearing any still-pending timer before
        // scheduling a new one is what stops the two from racing.
        if (copyResetTimer !== null) clearTimeout(copyResetTimer);
        label.textContent = "Copied!";
        copyResetTimer = setTimeout(() => { label.textContent = name; copyResetTimer = null; }, 1000);
    });
    return chip;
}

// Compact horizontal table: one row per fuzz job against this exact path.
// `hideStatus`: the JOBS "i" info button's own single-job case passes
// true -- explicit request: Status there is redundant (the row right
// above this exact panel already shows this exact job's own status pill),
// unlike TREE/Results' own multi-row history (a path fuzzed several times
// can genuinely have a different status each time, so Status stays there).
function buildFuzzHistoryPanel(history, hideStatus, onResultsClick) {
    if (!history || !history.length) {
        return h("div", { class: "hint", text: "Not fuzzed yet from this exact path." });
    }
    // Date now shows in every one of these panels (Results/TREE's own
    // multi-row history too, not just JOBS' single-job one, explicit
    // request 2026-09-24) -- entries reach this function in two different
    // shapes depending on caller: JOBS' own jobToHistoryEntry() flattens
    // `extensions`/`date` to the top level, but Results passes raw Job
    // objects straight through (fuzzHistoryForPath()), which only ever have
    // `config.extensions` and `started_at`/`created_at`, never a top-level
    // `extensions`/`date` at all -- reading straight from `entry.extensions`
    // silently showed "—" for every Results row's Extensions column before
    // this fix, unnoticed until Date needed the same raw-job fallback.
    const anyDate = history.some((entry) => entry.date || entry.started_at || entry.created_at);
    let anyQuery = false;
    const rows = history.map((entry) => {
        const cfg = entry.config || {};
        const extensions = entry.extensions || cfg.extensions || [];

        const headersCell = h("td", {});
        const headerEntries = Object.entries(cfg.headers || {});
        // A query string in the job's URL ("?a=a") is not a header, but it is
        // a per-request addition just like one: shown first as "query*" (hover
        // = the full query), with the footnote under the table explaining it.
        const query = queryOfUrl(entry.path || cfg.url);
        if (query) {
            headersCell.appendChild(buildHeaderNameChip("query*", query, query));
            anyQuery = true;
        }
        headerEntries.forEach(([name, value], idx) => {
            if (idx > 0 || query) headersCell.appendChild(document.createTextNode(", "));
            headersCell.appendChild(buildHeaderNameChip(name, String(value)));
        });
        if (!headerEntries.length && !query) headersCell.appendChild(document.createTextNode("—"));

        // Explicit order (2026-10-10): threads, force-ext, AUTH, auto pause,
        // THEN whatever else was actually set -- not alphabetical/insertion
        // order, so the options a pentest cares about most read first.
        const other = [];
        if (cfg.threads) other.push(`threads ${cfg.threads}`);
        if (cfg.force_extensions) other.push("force-ext");
        // "AUTH" is never part of the dirsearch config itself (purely a
        // naming convention, see api/jobs.py's own comment) -- the only record of
        // it is the "__AUTH" suffix on the job's own output_stem.
        if (entry.output_stem && entry.output_stem.endsWith("__AUTH")) other.push("AUTH");
        if (entry.pause_on_status && entry.pause_on_status.length) other.push(`auto pause ${entry.pause_on_status.join(",")}`);
        if (cfg.http_method && cfg.http_method !== "GET") other.push(cfg.http_method);
        if (cfg.proxy) other.push(`proxy ${cfg.proxy}`);
        if (cfg.extra_args && cfg.extra_args.length) other.push(String(cfg.extra_args));

        const cells = [];
        if (!hideStatus) cells.push(h("td", {}, [h("span", { class: `job-status job-${entry.status}`, text: entry.status })]));
        cells.push(
            h("td", { text: (entry.wordlist_sources || []).map(wordlistBaseName).join(", ") || "?" }),
            h("td", { text: extensions.join(",") || "—" }),
            headersCell,
            h("td", { text: other.join(" · ") || "—" }),
        );
        // Results count -- between Other options and Date. NOT shown at all in
        // JOBS' own single-job panel (hideStatus) -- explicit request: that
        // panel is narrower and the extra column overflowed it; the already-
        // visible count in the main JOBS row is the clickable one there instead
        // (see the main table's own Results cell).
        if (!hideStatus) {
            const text = entry.result_count == null ? "—" : String(entry.result_count);
            const resultsCell = h("td", { class: "jobs-col-center" });
            if (onResultsClick && entry.result_count) {
                resultsCell.appendChild(h("button", {
                    type: "button", class: "results-count-link", text, title: "Go to Results",
                    onclick: (e) => { e.stopPropagation(); onResultsClick(); },
                }));
            } else {
                resultsCell.textContent = text;
            }
            cells.push(resultsCell);
        }
        if (anyDate) {
            const dateValue = entry.date || entry.started_at || entry.created_at;
            cells.push(h("td", { class: "jobs-date-cell", text: dateValue ? formatJobDate(dateValue) : "—" }));
        }
        return h("tr", {}, cells);
    });
    const headCells = [];
    if (!hideStatus) headCells.push(h("th", { text: "Status" }));
    headCells.push(
        h("th", { text: "Wordlist" }), h("th", { text: "Extensions" }),
        h("th", { text: "Headers" }), h("th", { text: "Options" }),
    );
    if (!hideStatus) headCells.push(h("th", { text: "Results", class: "jobs-col-center" }));
    if (anyDate) headCells.push(h("th", { text: "Date" }));
    const sections = [h("thead", {}, [h("tr", {}, headCells)]), h("tbody", {}, rows)];
    if (anyQuery) {
        sections.push(h("tfoot", {}, [h("tr", {}, [
            h("td", { class: "fuzz-history-note", colspan: String(headCells.length), text: "* query = query string appended to every request (not a header)" }),
        ])]));
    }
    return h("table", { class: `fuzz-history-table${hideStatus ? " no-status" : ""}${anyDate ? " with-date" : ""}` }, sections);
}

// Full-width row under the triggering row (same shape as the inline fuzz
// launcher row) -- brief, read-only, closes on any click outside it or its
// toggle "Fuzzed" tag (wired by each caller).
function buildFuzzHistoryRow(colCount, history, onResultsClick) {
    return h("tr", { class: "fuzz-history-row" }, [
        h("td", { colspan: String(colCount) }, [
            h("div", { class: "fuzz-history-panel" }, [buildFuzzHistoryPanel(history, undefined, onResultsClick)]),
        ]),
    ]);
}

// The "Fuzzed" tag itself IS the toggle for the history panel above --
// replaces the separate 🔍 icon this used to be paired with. `detail`
// (wordlist + extensions) is shown next to it in TREE, but left out in
// Results -- just the plain "Fuzzed" label there.
function buildFuzzedTagButton(detail, isOpen, onToggle) {
    return h("button", {
        type: "button",
        class: `fuzzed-tag fuzz-history-toggle${isOpen ? " active" : ""}`,
        title: "Already fuzzed — click for history",
        text: detail ? `Fuzzed: ${detail}` : "Fuzzed",
        onclick: (e) => { e.stopPropagation(); onToggle(); },
    });
}

async function openResultUrl(url) {
    try {
        await api("/api/open-url", { method: "POST", body: JSON.stringify({ url }) });
    } catch (err) {
        alert(`Could not open browser: ${err.message}`);
    }
}

// A clickable path for JOBS/TREE rows, which only ever have a relative
// `path` (unlike a Results row, which already carries dirsearch's own
// absolute `url`) -- resolves it against the target's base_url and opens it
// the same way Results does.
function buildClickablePath(path, baseUrl, displayText) {
    let absolute;
    try { absolute = new URL(path, baseUrl).href; } catch (err) { absolute = baseUrl; }
    const link = h("a", {
        class: "result-path-link", href: "#", text: displayText != null ? displayText : path, title: absolute,
    });
    link.addEventListener("click", (e) => {
        e.preventDefault();
        e.stopPropagation();
        openResultUrl(absolute);
    });
    attachCopyPathContextMenu(link, () => path);
    return link;
}

function formatProgress(job) {
    // Real, extension-aware request counts (job.total_wordlist_requests,
    // job.completed_requests -- backend/job_manager/wordlist_resolver.py),
    // not the raw wordlist line count -- this REST-fetched view is what
    // refreshJobs() rebuilds the row from on every job.queued/started/
    // completed/failed/stopped/paused SSE event (e.g. clicking Stop), so it
    // has to use the same units as the live job.progress SSE handler
    // (below) or it visibly "reverts" to a different, wrong number the
    // moment one of those events fires. Only moves at pause/resume
    // checkpoints here (no live within-run data in a plain REST fetch) --
    // matches the live handler's own between-events cadence.
    const total = job.total_wordlist_requests || job.total_wordlist_lines;
    if (!total) return { pct: "0%", detail: "" };
    const done = job.status === "COMPLETED" ? total : Math.min(job.completed_requests, total);
    const pct = Math.round((done / total) * 100);
    return { pct: `${pct}%`, detail: ` (${done}/${total})` };
}

// The "xx%" and the "(done/total)" detail have deliberately different
// weight -- explicit request: the percentage itself stays plain/white (the
// number that actually matters at a glance), only the "(done/total)" detail
// is muted.
function renderProgressCell(cell, progress) {
    cell.innerHTML = "";
    cell.appendChild(document.createTextNode(progress.pct));
    cell.appendChild(h("span", { class: "jobs-progress-detail", text: progress.detail }));
}

function formatJobDate(iso) {
    if (!iso) return "—";
    const d = new Date(iso);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(d.getDate())}/${pad(d.getMonth() + 1)}/${d.getFullYear()}, ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function formatElapsed(ms) {
    if (ms == null) return "—";
    const totalSeconds = Math.floor(ms / 1000);
    const pad = (n) => String(n).padStart(2, "0");
    return `${pad(Math.floor(totalSeconds / 3600))}:${pad(Math.floor((totalSeconds % 3600) / 60))}:${pad(totalSeconds % 60)}`;
}

// Only one card menu (Project/Target "⋮") open across the whole page at a
// time -- opening one closes whichever other one was already open.
let openCardMenu = null;

function buildCardMenu(onDelete, onRename) {
    function closeMenu() {
        menu.hidden = true;
        if (openCardMenu === menu) openCardMenu = null;
    }

    const menu = h("div", { class: "card-menu", hidden: "hidden" }, [
        onRename ? h("button", {
            type: "button",
            text: "Rename",
            onclick: async (e) => {
                e.stopPropagation();
                closeMenu();
                await onRename();
            },
        }) : null,
        h("button", {
            class: "danger-item",
            type: "button",
            text: "Delete",
            onclick: async (e) => {
                e.stopPropagation();
                closeMenu();
                if (!confirm("This action is not reversible and will delete all related files. Continue?")) return;
                await onDelete();
            },
        }),
    ]);
    const button = h("button", {
        class: "card-menu-btn",
        type: "button",
        title: "More options",
        text: "⋮",
        onclick: (e) => {
            e.stopPropagation();
            if (!menu.hidden) {
                closeMenu();
                return;
            }
            if (openCardMenu) openCardMenu.hidden = true;
            menu.hidden = false;
            openCardMenu = menu;
        },
    });

    function onDocumentClick(e) {
        if (!document.body.contains(button)) {
            document.removeEventListener("click", onDocumentClick);
            return;
        }
        if (e.target !== button && !menu.contains(e.target)) closeMenu();
    }
    document.addEventListener("click", onDocumentClick);

    return [button, menu];
}

// In-place rename, no browser prompt() dialog: `tag` builds the display
// element (e.g. "h3"); calling the returned startEdit() (from the card's
// "Rename" menu item) turns it into a text input pre-filled with the
// current name, Enter or clicking away saves (skipped if empty or
// unchanged), Escape cancels. `onSave` persists it (throwing surfaces as an
// inline alert and reverts the display).
function buildEditableName(tag, name, onSave) {
    const container = h(tag, {});

    // Display mode is plain text -- entering edit mode is triggered
    // externally (the card's "Rename" menu item calls the returned
    // startEdit()), not by clicking the name itself.
    function renderDisplay() {
        container.innerHTML = "";
        container.onclick = null;
        container.textContent = name;
    }

    function renderEdit() {
        container.innerHTML = "";
        container.onclick = (e) => e.stopPropagation();
        const input = h("input", { class: "inline-rename-input", value: name });
        container.appendChild(input);
        input.addEventListener("click", (e) => e.stopPropagation());
        input.focus();
        input.select();

        let settled = false;
        async function commit() {
            if (settled) return;
            settled = true;
            const newName = input.value.trim();
            if (!newName || newName === name) {
                renderDisplay();
                return;
            }
            try {
                await onSave(newName);
                name = newName;
            } catch (err) {
                alert(`Could not rename: ${err.message}`);
            }
            renderDisplay();
        }
        input.addEventListener("keydown", (e) => {
            if (e.key === "Enter") {
                e.preventDefault();
                commit();
            } else if (e.key === "Escape") {
                settled = true;
                renderDisplay();
            }
        });
        input.addEventListener("blur", commit);
    }

    renderDisplay();
    return { element: container, startEdit: renderEdit };
}

// Fire-and-forget: called from render() right as Results is left (see
// activeResultsView's own comment) -- never blocks the route switch on this
// round-trip, and a failure here just means NEW badges won't have advanced
// their watermark this one time (non-critical, self-corrects next visit).
async function markResultsSeenFor(projectId, targetId, keys) {
    if (!keys.length) return;
    try {
        await api(`/api/projects/${projectId}/targets/${targetId}/results/mark-seen`, {
            method: "POST", body: JSON.stringify({ keys }),
        });
    } catch (e) { /* non-critical */ }
}

function closeStream() {
    if (currentStream) {
        currentStream.close();
        currentStream = null;
    }
    if (currentResultsPoll) {
        clearInterval(currentResultsPoll);
        currentResultsPoll = null;
    }
    if (currentJobsTicker) {
        clearInterval(currentJobsTicker);
        currentJobsTicker = null;
    }
}

function parseRoute() {
    const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
    if (parts.length >= 4 && parts[0] === "projects" && parts[2] === "targets") {
        // "jobs" (New Fuzz + Running/Jobs) is the default landing tab --
        // no sub-route segment.
        let tab = "jobs";
        if (parts[4] === "results") tab = "results";
        else if (parts[4] === "tree") tab = "tree";
        return { view: "workspace", projectId: parts[1], targetId: parts[3], tab };
    }
    if (parts.length >= 2 && parts[0] === "projects") {
        return { view: "targets", projectId: parts[1] };
    }
    return { view: "projects" };
}

async function render() {
    // Explicitly save the OUTGOING route's scroll position before touching
    // the DOM at all, then switch tracking to the new route immediately --
    // swapping #app's children further below can itself fire a scroll
    // event on #scroll-area (the browser clamps scrollTop once the content
    // is suddenly much shorter than before), and if `currentRouteKey` still
    // pointed at the old route when that fired, it would silently
    // overwrite the correct position just saved here with that clamped
    // (near-zero) one. Switching the key first means any such spurious
    // event lands on the new route instead, which gets its real position
    // restored below anyway.
    if (currentRouteKey) scrollPositions.set(currentRouteKey, scrollArea.scrollTop);

    // Leaving Results (any navigation away from it, including to a
    // different target/project) updates the seen-watermark for what was
    // just shown -- see activeResultsView's own comment above. Fire-and-
    // forget: never blocks the route switch on a network round-trip.
    if (activeResultsView) {
        markResultsSeenFor(activeResultsView.projectId, activeResultsView.targetId, activeResultsView.getKeys());
        activeResultsView = null;
    }

    closeStream();
    resultsRepaint = null;
    resultsReveal = null;
    // Real, reported bug this fixes ("tabs keep flickering" switching
    // JOBS/RESULTS/TREE): the previous approach built the new page directly
    // into the LIVE #app (hidden via visibility, then innerHTML cleared,
    // then rebuilt, then revealed) -- every await along the way (fetching
    // jobs/results/tree data) was a point where the browser could paint a
    // frame, and while #app was hidden that frame showed nothing at all:
    // a real, visible blank flash between the old tab's content
    // disappearing and the new tab's popping in, confirmed directly via a
    // MutationObserver timing capture (~50ms hidden gap; short, but well
    // above a single frame, and long enough to read as a flicker).
    //
    // Fixed by building into a detached scratch <main> instead -- a REAL
    // <main> tag (not a plain <div>) so it picks up the tag-selector
    // styles.css rule (`main { padding/max-width/margin }`) automatically,
    // giving it accurate layout for any measurement code run during
    // construction (e.g. buildNewFuzzForm-adjacent code reading
    // `tabs.offsetHeight`) -- `position: fixed` + `visibility: hidden`
    // keeps it attached to the document (so layout/offsetHeight work at
    // all, unlike a truly detached node) while contributing nothing to any
    // paint or to #scroll-area's own layout. The REAL #app's old content
    // stays fully visible and untouched on screen for the page's whole
    // build time; only the final swap (moving the finished scratch
    // content into #app, one synchronous DOM call) ever changes what's on
    // screen -- old and new content, never a blank state between them.
    const scratch = document.createElement("main");
    scratch.style.position = "fixed";
    scratch.style.top = "0";
    scratch.style.left = "0";
    scratch.style.right = "0";
    scratch.style.visibility = "hidden";
    scratch.style.pointerEvents = "none";
    document.body.appendChild(scratch);
    const liveApp = app;
    app = scratch;

    const route = parseRoute();
    const routeKey = routeKeyFor(route);
    try {
        if (route.view === "projects") await renderProjects();
        else if (route.view === "targets") await renderTargets(route.projectId);
        else await renderWorkspace(route.projectId, route.targetId, route.tab);
    } catch (err) {
        app.appendChild(h("div", { class: "error-banner", text: err.message }));
    }
    // currentRouteKey switches to the new route BEFORE the swap below, for
    // the same "a spurious clamp-scroll event must land on the new route,
    // not overwrite the old one's saved position" reason explained above.
    currentRouteKey = routeKey;
    liveApp.replaceChildren(...scratch.childNodes);
    scratch.remove();
    app = liveApp;
    // Restore this route's own remembered scroll position (0 the first
    // time it's ever visited) now that the swap above is done.
    // A pending "jump to this path" request (goToResultsAndReveal) for this
    // exact project+target's Results tab takes over the normal scroll
    // restore entirely -- it sets its own scroll position.
    let revealed = false;
    if (pendingResultsReveal && route.view === "workspace" && route.tab === "results"
        && pendingResultsReveal.projectId === route.projectId && pendingResultsReveal.targetId === route.targetId) {
        const path = pendingResultsReveal.path;
        if (resultsReveal) { resultsReveal(path); revealed = true; }
    }
    pendingResultsReveal = null; // one-shot, whether or not it could be honored
    if (!revealed) {
        scrollArea.scrollTop = scrollPositions.get(routeKey) || 0;
        if (resultsRepaint) resultsRepaint();
    }
}

// A result/tree path's grouping key: "/admin" and "/admin/" are the same
// place. dirsearch reports a redirecting directory as the bare path (e.g.
// "/api" with redirect "/api/", not "/api/" itself -- verified against a
// real run), so grouping/matching always goes through this, never raw
// string comparison.
function normalizeKey(path) {
    if (path === "/" || path === "") return "/";
    return path.endsWith("/") ? path.slice(0, -1) : path;
}

// A job's path may carry a query string ("/gs/htc/?a=a": dirsearch appends it
// to every word). The query is part of what gets requested, but the place
// being fuzzed -- what TREE, Results grouping and the "Fuzzed" tags match on --
// is only the path before it.
function pathWithoutQuery(path) {
    return String(path || "").split(/[?#]/)[0];
}

// The "?a=a" part of a path or URL ("" when there is none).
function queryOfUrl(value) {
    const text = String(value || "").split("#")[0];
    const at = text.indexOf("?");
    return at >= 0 && at < text.length - 1 ? text.slice(at) : "";
}

function parentKeyOf(path) {
    const key = normalizeKey(path);
    if (key === "/") return null;
    const lastSlash = key.lastIndexOf("/");
    return lastSlash <= 0 ? "/" : key.slice(0, lastSlash);
}

// The relative segment a Results/TREE row actually displays next to its own
// indentation -- never a leading "/" (the row's own nesting already shows
// where it sits, a leading slash was never needed to read it), a trailing
// "/" only when `isDirectory` (explicit request, correcting an earlier
// version that always added a leading slash and never added a trailing
// one).
function computeDisplaySegment(rawPath, parentPath, isDirectory) {
    const fullKey = normalizeKey(rawPath);
    let segment;
    if (parentPath !== null) {
        const parentKey = normalizeKey(parentPath);
        const prefix = parentKey === "/" ? "/" : `${parentKey}/`;
        segment = fullKey.startsWith(prefix) ? fullKey.slice(prefix.length) : fullKey.replace(/^\/+/, "");
    } else {
        segment = fullKey === "/" ? "" : fullKey.replace(/^\/+/, "");
    }
    if (isDirectory && segment && !segment.endsWith("/")) segment += "/";
    return segment || "/";
}

// Real-`tree`-command-style connector prefix for one hierarchy row --
// "├── " for a sibling with more siblings still to come after it, "└── "
// for the last one at its own level; "│   "/"    " (blank) continue or stop
// a vertical line through each ANCESTOR level, depending on whether that
// ancestor itself still had more siblings coming after it. depth 0 (a
// top-level row) gets no connector at all -- matches a real `tree`
// command's own root-relative style, and Results in particular has no
// single real root row to hang a connector off of. Rendered as plain text
// in a monospace span (see .tree-connector in styles.css) so the box-
// drawing characters actually line up vertically across rows -- a
// proportional font would misalign them.
function treeConnectorFor(depth, prefix, isLast) {
    return depth === 0 ? "" : `${prefix}${isLast ? "└── " : "├── "}`;
}
function treeChildPrefixFor(depth, prefix, isLast) {
    return depth === 0 ? "" : `${prefix}${isLast ? "    " : "│   "}`;
}

// Splits a connector string (built from the two functions above) into its
// individual 4-char units and appends one span per unit, instead of one
// span for the whole string. Real, reported bug this fixes: when a row's
// own path text wraps onto 2-3 display lines (long filename), the vertical
// "│" line of an ancestor that still has more siblings coming used to only
// show up alongside the row's FIRST display line -- .tree-connector was a
// single, naturally one-line-tall span, so there was nothing drawing that
// line down through the row's own 2nd/3rd wrapped lines, leaving a visible
// gap (see docs/identacion.png, explicit request to fix). Every unit that
// continues an ancestor's line downward (starts with "│") gets the
// "tree-connector-line" modifier class -- CSS (.tree-connector-line) gives
// it `align-self: stretch` (so it actually grows to the row's full,
// possibly-wrapped height, via .tree-path-prefix's own align-items), hides
// the glyph CHARACTER (color: transparent -- the text itself stays in the
// DOM so this unit's own width/the monospace grid is unaffected) and draws
// a pseudo-element line for the WHOLE height instead, line 1 included.
// Real, reported follow-up bug this fixes: the first version kept the
// glyph for line 1 and only used the pseudo-element line underneath it,
// so line 1 (glyph+line together) read as normal weight while every
// wrapped line below was the thin pseudo-element alone -- on a more
// deeply nested ancestor column in particular, that read as "only the
// first column actually continues, the inner ones still cut off". Every
// line of every continuing column, at any depth, is now the exact same
// element at the exact same thickness -- see .tree-connector-line's own
// comment in styles.css. Blank ("    ") ancestor units and the row's own
// final "├── "/"└── " unit (nothing needs to visually continue below
// THIS row's own branch glyph -- the next sibling row draws its own,
// fresh) are left exactly as before, plain text.
function appendConnectorUnits(pathPrefix, connector) {
    if (!connector) return;
    const units = connector.match(/.{1,4}/g) || [];
    units.forEach((unit, i) => {
        const continues = i < units.length - 1 && unit.charAt(0) === "│";
        pathPrefix.appendChild(h("span", {
            class: continues ? "tree-connector tree-connector-line" : "tree-connector",
            text: unit,
        }));
    });
}

// Builds a full ancestor-aware hierarchy from a flat list of items
// (Results rows or TREE nodes), synthesizing a lightweight placeholder for
// every intermediate path segment that has no real item of its own --
// fuzzing straight at /a/b/c (skipping /a and /a/b, neither independently
// discovered/added) used to render one flat "/a/b/c/..." row per result;
// now /c nests under a synthetic, expandable /a/b under a synthetic,
// expandable /a (explicit user request). Only ever groups the rest of the
// tree under root ("/") when root is a REAL item, not a synthetic
// placeholder for it -- TREE's root always is (tree_service.list_nodes
// auto-creates it), and explicit user request is that it's the parent of
// everything else there; Results has no real "/" item in practice, so this
// stays scoped to TREE without a separate check.
//
// `makeSynthetic(path)` builds whatever shape of placeholder item the
// caller's own row-renderer expects for a path that was never actually
// discovered/added -- Results and TREE each pass their own.
//
// Returns { childrenOf: Map(key -> [node, ...]), topLevel: [node, ...] },
// each node shaped { key, path, items: [item, ...], synthetic }.
function buildPathHierarchy(items, makeSynthetic) {
    // Two raw items can normalize to the same key -- verified against a
    // real dirsearch run: both "/admin" (a 301 redirect stub) and "/admin/"
    // (the actual 200 hit) are separate, genuinely distinct results. Real,
    // reported data-loss bug this fixes (2026-09-25): this used to keep
    // only ONE of them (preferring the non-redirect), silently discarding
    // whichever lost that tie-break forever -- "la tool necesita mostrar
    // TODOS los resultados". Every item reaching this function survives now
    // (callers are responsible for their own exact-duplicate dedup before
    // calling this -- see addResults' own comment), grouped into `items`
    // (plural) per node instead of picking a single winner. `primary` (the
    // non-redirect one when there is one, matching the old tie-break) is
    // still used for whatever needs exactly one representative for this
    // node's OWN identity -- its displayed path/segment, its children's
    // parent link -- never for deciding what's shown, only what this
    // position in the tree is "called".
    const itemsByKey = new Map();
    for (const item of items) {
        const key = normalizeKey(item.path);
        if (!itemsByKey.has(key)) itemsByKey.set(key, []);
        itemsByKey.get(key).push(item);
    }

    const allKeys = new Set();
    for (const key of itemsByKey.keys()) {
        let k = key;
        while (k && k !== "/") {
            allKeys.add(k);
            k = parentKeyOf(k);
        }
    }
    // A REAL item can itself be at root (TREE's own root node, always
    // present -- see tree_service.list_nodes) -- the walk-up above never
    // adds "/" (that's the "never group under root" rule for OTHER items'
    // ancestors), so add it explicitly here, only when a real item exists
    // there. It naturally ends up in topLevel below (parentKeyOf("/") is
    // null).
    if (itemsByKey.has("/")) allKeys.add("/");

    const nodesByKey = new Map();
    for (const key of allKeys) {
        const real = itemsByKey.get(key);
        if (real) {
            const primary = real.find((it) => !it.redirect) || real[0];
            // A TREE item the server marked synthetic (an implied ancestor that
            // still has its own derived status/fuzz history) stays synthetic --
            // it has no id until an explicit edit promotes it.
            nodesByKey.set(key, { key, path: primary.path, items: real, synthetic: primary.synthetic === true });
        } else {
            nodesByKey.set(key, { key, path: key, items: [makeSynthetic(key)], synthetic: true });
        }
    }

    const childrenOf = new Map();
    const topLevel = [];
    for (const node of nodesByKey.values()) {
        const parentKey = parentKeyOf(node.key);
        // "/" acts as a parent when it's a REAL node (TREE's always-present
        // root -- explicit request: "/" is the parent of everything else,
        // which should nest under it with its own expand/collapse arrow).
        // Never happens for Results (no real "/" item there in practice),
        // so this naturally stays scoped to TREE without a separate check.
        if (parentKey && nodesByKey.has(parentKey)) {
            if (!childrenOf.has(parentKey)) childrenOf.set(parentKey, []);
            childrenOf.get(parentKey).push(node);
        } else {
            topLevel.push(node);
        }
    }
    for (const list of childrenOf.values()) list.sort((a, b) => a.path.localeCompare(b.path));
    topLevel.sort((a, b) => a.path.localeCompare(b.path));

    return { childrenOf, topLevel };
}

// Shown to the left of the settings gear, muted -- purely informational,
// no click behavior. This just displays whatever backend/api/settings.py's
// /api/version endpoint reads from the repo-root VERSION file.
async function loadAppVersion() {
    try {
        const { version } = await api("/api/version");
        document.getElementById("app-version").textContent = `v${version}`;
    } catch (e) { /* purely cosmetic -- never block the app over this */ }
}

window.addEventListener("hashchange", () => ensurePrefs().then(render));
window.addEventListener("DOMContentLoaded", () => ensurePrefs().then(render));
window.addEventListener("DOMContentLoaded", setupSettingsMenu);
window.addEventListener("DOMContentLoaded", loadAppVersion);

// ---- Settings (header gear) ---------------------------------------------

const SETTINGS_PAGES = [
    {
        id: "browser",
        title: "Browser for opening results",
        desc: "Choose which browser opens a result's URL when you click it",
    },
    {
        id: "output-filename",
        title: "Output filenames",
        desc: "Naming pattern for each job's raw dirsearch output file (.txt)",
    },
    {
        id: "default-extensions",
        title: "Default extensions (Force extensions)",
        desc: "What dirsearch uses when Force extensions is on but a fuzz picks none",
    },
    {
        id: "wordlists-dir",
        title: "Wordlists folder",
        desc: "Folder the New Fuzz wordlist picker browses; use your own instead of the bundled one",
    },
    {
        id: "port",
        title: "Desktop app port",
        desc: "Custom local port the app listens on, instead of the built-in default",
    },
    {
        id: "ui-zoom",
        title: "Display zoom",
        desc: "Make the whole app bigger or smaller (Linux)",
        onlyWhenZoomSupported: true,
    },
    {
        id: "updates",
        title: "Updates",
        desc: "Check for a newer version and update in place; your projects are kept",
    },
];

function setupSettingsMenu() {
    const btn = document.getElementById("settings-btn");
    const panel = document.getElementById("settings-panel");

    // Any click that starts inside the panel must never reach the
    // "click outside closes it" listener below -- without this, clicking
    // "back" replaced panel's content (via innerHTML) *before* the click
    // finished bubbling, so by the time the document-level listener ran,
    // the original target (the back button) was already detached from the
    // DOM, panel.contains(target) came back false, and the whole panel
    // closed instead of just going back to the menu.
    panel.addEventListener("click", (e) => e.stopPropagation());

    // Display zoom only exists on Linux (Windows follows the system scaling by itself): the
    // backend says which one this is. The menu is rebuilt on every open, so a reply that
    // arrives after startup is picked up the first time the gear is clicked.
    let zoomSupported = false;
    api("/api/settings/ui-zoom").then((d) => { zoomSupported = !!d.supported; }).catch(() => {});

    function renderMenu() {
        panel.innerHTML = "";
        panel.appendChild(h("h3", { text: "Settings" }));
        const list = h("div", { class: "settings-menu-list" });
        for (const page of SETTINGS_PAGES) {
            if (page.onlyWhenZoomSupported && !zoomSupported) continue;
            const item = h("button", { type: "button", class: "settings-menu-item" }, [
                h("span", { class: "label-group" }, [
                    h("span", { class: "title", text: page.title }),
                    h("span", { class: "desc", text: page.desc }),
                ]),
                h("span", { class: "chevron", text: "›" }),
            ]);
            item.addEventListener("click", () => renderPage(page.id));
            list.appendChild(item);
        }
        panel.appendChild(list);
    }

    function pageHeader(title) {
        const backBtn = h("button", { type: "button", class: "settings-back-btn", title: "Back", text: "←" });
        backBtn.addEventListener("click", renderMenu);
        panel.appendChild(h("div", { class: "settings-back-row" }, [backBtn, h("h3", { text: title })]));
    }

    // Each render*Page() below is the single owner of panel's content for
    // that page: it clears panel itself, fetches its own data, then builds
    // -- no shared "clear + loading placeholder" step here, so there is
    // never a moment where two renders' output could land in panel at once.
    async function renderPage(pageId) {
        const page = SETTINGS_PAGES.find((p) => p.id === pageId);
        try {
            if (pageId === "browser") await renderBrowserPage();
            else if (pageId === "output-filename") await renderOutputFilenamePage();
            else if (pageId === "port") await renderPortPage();
            else if (pageId === "ui-zoom") await renderZoomPage();
            else if (pageId === "default-extensions") await renderDefaultExtensionsPage();
            else if (pageId === "wordlists-dir") await renderWordlistsDirPage();
            else if (pageId === "updates") renderUpdatesPage();
        } catch (err) {
            panel.innerHTML = "";
            pageHeader(page.title);
            panel.appendChild(h("div", { class: "error-banner", text: err.message }));
        }
    }

    async function renderBrowserPage() {
        // Detection touches the filesystem and can take a moment (cold disk
        // cache, slow mounts) -- show that something is happening instead of
        // leaving the previous page's content frozen until it finishes.
        panel.innerHTML = "";
        pageHeader("Browser for opening results");
        panel.appendChild(h("div", { class: "loading-label", text: "Detecting installed browsers…" }));

        const data = await api("/api/settings/browser");

        panel.innerHTML = "";
        pageHeader("Browser for opening results");

        // Only browsers OptiFuzz actually found on this machine right now
        // are listed -- never a fixed catalog of names that may not apply
        // to this install.
        const options = [{ name: "System default browser", path: null }, ...data.detected];
        for (const opt of options) {
            const isSelected = data.configured_path === opt.path;
            const row = h("div", { class: `browser-option${isSelected ? " selected" : ""}` }, [
                h("span", { text: isSelected ? "✓" : "" }),
                h("div", {}, [
                    document.createTextNode(opt.name),
                    opt.path ? h("div", { class: "path", text: opt.path }) : null,
                ]),
            ]);
            row.addEventListener("click", async () => {
                try {
                    await api("/api/settings/browser", { method: "PUT", body: JSON.stringify({ browser_path: opt.path }) });
                    await renderPage("browser");
                } catch (err) {
                    alert(`Could not save: ${err.message}`);
                }
            });
            panel.appendChild(row);
        }

        if (!data.detected.length) {
            panel.appendChild(h("div", {
                class: "not-detected-label",
                text: "No browsers detected on this machine. Set a custom path below, "
                    + "or rely on the system default.",
            }));
        }

        const knownPaths = new Set(options.map((o) => o.path));
        const isCustomConfigured = data.configured_path && !knownPaths.has(data.configured_path);
        const customInput = h("input", {
            placeholder: "Custom browser executable path…",
            value: isCustomConfigured ? data.configured_path : "",
        });
        const customError = h("div", { class: "field-error", hidden: "hidden" });
        const customBtn = h("button", {
            type: "button",
            class: isCustomConfigured ? "primary" : "",
            text: isCustomConfigured ? "✓ In use" : "Use",
        });
        customBtn.addEventListener("click", async () => {
            const value = customInput.value.trim();
            if (!value) return;
            customError.hidden = true;
            customBtn.disabled = true;
            customBtn.textContent = "Checking…";
            try {
                // The backend actually validates this is a real, launchable
                // executable for this OS (Windows: .exe with a PE header;
                // Linux: execute bit plus an ELF/shebang header)
                // before saving it -- never trusted blindly.
                await api("/api/settings/browser", { method: "PUT", body: JSON.stringify({ browser_path: value }) });
                await renderPage("browser");
            } catch (err) {
                customBtn.disabled = false;
                customBtn.textContent = "Use";
                customError.textContent = `Could not use this path: ${err.message}`;
                customError.hidden = false;
            }
        });
        panel.appendChild(h("div", { class: "custom-path-row" }, [customInput, customBtn]));
        panel.appendChild(customError);
    }

    function renderUpdatesPage() {
        panel.innerHTML = "";
        pageHeader("Updates");
        panel.appendChild(h("div", {
            class: "hint",
            text: "Downloads the latest version from the repository you installed from and refreshes the "
                + "dependencies. Your projects, targets, jobs and wordlists are kept. Running and queued jobs "
                + "are paused when you update; resume them once OptiFuzz has restarted.",
        }));
        const result = h("div", { class: "hint" });
        // Filled in by the check (it knows whether this is the desktop app, which can restart itself).
        const restartNote = h("div", { class: "hint", hidden: "hidden" });
        const checkBtn = h("button", { type: "button", text: "Check for updates" });
        const updateBtn = h("button", { type: "button", class: "primary", text: "Update now", hidden: "hidden" });
        const busy = (btn, label) => { checkBtn.disabled = true; updateBtn.disabled = true; btn.textContent = label; };
        checkBtn.addEventListener("click", async () => {
            busy(checkBtn, "Checking…");
            updateBtn.hidden = true;
            restartNote.hidden = true;
            try {
                const s = await api("/api/update/check");
                if (!s.behind) result.textContent = `You are up to date (v${s.current}).`;
                else if (s.can_update) {
                    result.textContent = `v${s.latest} is available (you have v${s.current}).`;
                    updateBtn.hidden = false;
                    restartNote.replaceChildren(h("strong", {
                        text: s.can_restart
                            ? "When the update finishes, OptiFuzz restarts by itself."
                            : "When the update finishes, close and reopen OptiFuzz to start using it.",
                    }));
                    restartNote.hidden = false;
                } else {
                    result.textContent = `v${s.latest} is available, but this copy has local changes `
                        + "(modified files or its own commits), so it cannot be updated automatically.";
                }
            } catch (err) {
                result.textContent = `Could not check: ${err.message}`;
            }
            checkBtn.disabled = false; updateBtn.disabled = false; checkBtn.textContent = "Check for updates";
        });
        updateBtn.addEventListener("click", async () => {
            busy(updateBtn, "Updating…");
            try {
                const s = await api("/api/update/apply", { method: "POST" });
                const pausedNote = s.paused_jobs ? ` ${s.paused_jobs} job(s) were paused; resume them once it is back.` : "";
                updateBtn.hidden = true;
                restartNote.hidden = true;
                if (!s.updated) {
                    result.textContent = "Already up to date.";
                } else if (s.can_restart) {
                    result.textContent = `Updated to v${s.current}. Restarting OptiFuzz…${pausedNote}`;
                    try {
                        await api("/api/update/restart", { method: "POST" });
                        return; // the window closes by itself in a moment and the new copy opens
                    } catch (err) {
                        result.textContent = `Updated to v${s.current}, but OptiFuzz could not restart itself `
                            + `(${err.message}). Close and reopen it to start using the new version.${pausedNote}`;
                    }
                } else {
                    result.textContent = `Updated to v${s.current}. Close and reopen OptiFuzz to start using it.${pausedNote}`;
                }
            } catch (err) {
                result.textContent = `Update failed: ${err.message}`;
                updateBtn.disabled = false;
            }
            checkBtn.disabled = false; updateBtn.textContent = "Update now";
        });
        panel.appendChild(h("div", { class: "field-row" }, [checkBtn, updateBtn]));
        panel.appendChild(restartNote);
        panel.appendChild(result);
    }

    async function renderOutputFilenamePage() {
        const data = await api("/api/settings/output-filename-template");

        panel.innerHTML = "";
        pageHeader("Output filenames");
        panel.appendChild(h("div", {
            class: "hint",
            text: "Placeholders: {target} (target name), {port}, {path} (fuzzed path, slashes become "
                + "underscores). Applies to jobs created from now on.",
        }));

        const input = h("input", { value: data.template });
        const preview = h("div", { class: "hint", text: `Example: ${data.example}` });
        const saveBtn = h("button", { type: "button", class: "primary", text: "Save" });
        saveBtn.addEventListener("click", async () => {
            const value = input.value.trim();
            if (!value) return;
            try {
                const result = await api("/api/settings/output-filename-template", {
                    method: "PUT",
                    body: JSON.stringify({ template: value }),
                });
                preview.textContent = `Example: ${result.example}`;
            } catch (err) {
                alert(`Could not save: ${err.message}`);
            }
        });
        panel.appendChild(h("div", { class: "field-row" }, [input, saveBtn]));
        panel.appendChild(preview);
    }

    async function renderPortPage() {
        const data = await api("/api/settings/port");

        panel.innerHTML = "";
        pageHeader("Desktop app port");
        panel.appendChild(h("div", {
            class: "hint",
            text: "The local port OptiFuzz listens on. "
                + "Takes effect the next time you restart OptiFuzz.",
        }));

        const input = h("input", {
            type: "number",
            min: "1",
            max: "65535",
            placeholder: String(data.default_port),
            value: data.configured_port ? String(data.configured_port) : "",
        });
        const fieldError = h("div", { class: "field-error", hidden: "hidden" });
        const saveBtn = h("button", { type: "button", class: "primary", text: "Save" });
        saveBtn.addEventListener("click", async () => {
            const raw = input.value.trim();
            const port = raw ? Number(raw) : null;
            fieldError.hidden = true;
            try {
                await api("/api/settings/port", { method: "PUT", body: JSON.stringify({ port }) });
                await renderPage("port");
            } catch (err) {
                fieldError.textContent = `Could not save: ${err.message}`;
                fieldError.hidden = false;
            }
        });
        panel.appendChild(h("div", { class: "field-row" }, [input, saveBtn]));
        panel.appendChild(fieldError);
    }

    async function renderZoomPage() {
        const data = await api("/api/settings/ui-zoom");

        panel.innerHTML = "";
        pageHeader("Display zoom");
        panel.appendChild(h("div", {
            class: "hint",
            text: `Makes the whole app bigger or smaller, in percent (${data.min_percent} to ${data.max_percent}). `
                + "Leave it empty for automatic. It applies right away and is remembered.",
        }));

        const input = h("input", {
            type: "number",
            min: String(data.min_percent),
            max: String(data.max_percent),
            step: "5",
            placeholder: "100",
            value: data.configured_percent ? String(data.configured_percent) : "",
        });
        const fieldError = h("div", { class: "field-error", hidden: "hidden" });
        const saveBtn = h("button", { type: "button", class: "primary", text: "Apply" });
        saveBtn.addEventListener("click", async () => {
            const raw = input.value.trim();
            const percent = raw ? Number(raw) : null;
            fieldError.hidden = true;
            try {
                await api("/api/settings/ui-zoom", { method: "PUT", body: JSON.stringify({ percent }) });
                await renderPage("ui-zoom");
            } catch (err) {
                fieldError.textContent = `Could not apply: ${err.message}`;
                fieldError.hidden = false;
            }
        });
        panel.appendChild(h("div", { class: "zoom-row" }, [input, h("span", { class: "hint", text: "%" }), saveBtn]));
        panel.appendChild(fieldError);
    }

    async function renderDefaultExtensionsPage() {
        const data = await api("/api/settings/default-extensions-fallback");

        panel.innerHTML = "";
        pageHeader("Default extensions (Force extensions)");
        panel.appendChild(h("div", {
            class: "hint",
            text: "When Force extensions is on and a fuzz's own Extensions field is empty, "
                + "dirsearch itself silently falls back to a hidden default -- not none. Leave "
                + "this blank to keep that (matches a plain dirsearch run); check the box to "
                + "use none instead, or list your own.",
        }));

        const useNone = data.configured_value === "";
        const checkbox = h("input", { type: "checkbox" });
        checkbox.checked = useNone;
        const checkboxLabel = h("label", { class: "checkbox-row" }, [checkbox, document.createTextNode(" Use no extensions instead")]);

        const input = h("input", {
            placeholder: data.dirsearch_default,
            value: data.configured_value && data.configured_value !== "" ? data.configured_value : "",
        });
        input.disabled = useNone;
        checkbox.addEventListener("change", () => { input.disabled = checkbox.checked; });

        const fieldError = h("div", { class: "field-error", hidden: "hidden" });
        const saveBtn = h("button", { type: "button", class: "primary", text: "Save" });
        saveBtn.addEventListener("click", async () => {
            const value = checkbox.checked ? "" : (input.value.trim() || null);
            fieldError.hidden = true;
            try {
                await api("/api/settings/default-extensions-fallback", {
                    method: "PUT", body: JSON.stringify({ value }),
                });
                await renderPage("default-extensions");
            } catch (err) {
                fieldError.textContent = `Could not save: ${err.message}`;
                fieldError.hidden = false;
            }
        });
        panel.appendChild(checkboxLabel);
        panel.appendChild(h("div", { class: "field-row" }, [input, saveBtn]));
        panel.appendChild(fieldError);
    }

    // Same shape as the per-project Outputs mirror folder (buildOutputsMirrorSection):
    // a path input plus the native folder button, saved on change or on pick.
    async function renderWordlistsDirPage() {
        const data = await api("/api/settings/wordlists-dir");

        panel.innerHTML = "";
        pageHeader("Wordlists folder");
        panel.appendChild(h("div", {
            class: "hint",
            text: "The folder the New Fuzz wordlist picker browses. Leave it empty to use the "
                + "wordlists folder that comes with OptiFuzz, or pick a folder you already have. "
                + "Its subfolders show up as groups. OptiFuzz only reads "
                + "from it and never changes it.",
        }));

        const dirInput = h("input", {
            class: "outputs-mirror-input",
            placeholder: data.default_dir,
            value: data.configured_dir || "",
        });
        const fieldError = h("div", { class: "field-error", hidden: "hidden" });

        async function save(newDir) {
            fieldError.hidden = true;
            try {
                const result = await api("/api/settings/wordlists-dir", {
                    method: "PUT", body: JSON.stringify({ dir: newDir }),
                });
                dirInput.value = result.configured_dir || "";
            } catch (err) {
                fieldError.textContent = `Could not save: ${err.message}`;
                fieldError.hidden = false;
            }
        }
        dirInput.addEventListener("change", () => save(dirInput.value.trim()));

        const pickBtn = h("button", { type: "button", class: "outputs-mirror-folder-btn", title: "Choose folder…" });
        pickBtn.innerHTML = FOLDER_ICON_SVG;
        pickBtn.addEventListener("click", async () => {
            if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.pick_folder) {
                alert("The native folder picker is only available inside the OptiFuzz desktop app -- type the path directly instead.");
                return;
            }
            const picked = await window.pywebview.api.pick_folder(dirInput.value.trim() || data.default_dir);
            if (picked) await save(picked);
        });

        panel.appendChild(h("div", { class: "outputs-mirror-row" }, [dirInput, pickBtn]));
        panel.appendChild(fieldError);
    }

    btn.addEventListener("click", (e) => {
        e.stopPropagation();
        const wasHidden = panel.hidden;
        panel.hidden = !panel.hidden;
        if (wasHidden) renderMenu();
    });

    document.addEventListener("click", (e) => {
        if (!panel.hidden && !panel.contains(e.target) && e.target !== btn) panel.hidden = true;
    });
}

// ---- Projects -------------------------------------------------------

async function renderProjects() {
    breadcrumb.textContent = "";
    const projects = await api("/api/projects");

    // novalidate: the submit handler already does its own manual empty
    // check below -- without this, the browser's own constraint-validation
    // popup ("Please fill out this field", localized to the OS/browser
    // language) would show instead, which can't be restyled and doesn't
    // match the rest of the app's always-English UI.
    const form = h("form", { class: "inline-form", novalidate: "novalidate", onsubmit: async (e) => {
        e.preventDefault();
        const name = form.querySelector("input").value.trim();
        if (!name) return;
        await api("/api/projects", { method: "POST", body: JSON.stringify({ name }) });
        render();
    } }, [
        h("input", { placeholder: "New project name", required: "required" }),
        h("button", { class: "primary", type: "submit", text: "+ New Project" }),
    ]);

    const { element: searchInputEl, input: searchInput } = buildSearchInput({ class: "search-input", placeholder: "Search projects…" });
    const gridBox = h("div", {});

    function renderGrid() {
        const query = searchInput.value.trim().toLowerCase();
        const visible = query ? projects.filter((p) => p.name.toLowerCase().includes(query)) : projects;

        gridBox.innerHTML = "";
        if (!projects.length) {
            gridBox.appendChild(h("div", { class: "empty", text: "No projects yet." }));
            return;
        }
        if (!visible.length) {
            gridBox.appendChild(h("div", { class: "empty", text: "No projects match your search." }));
            return;
        }
        gridBox.appendChild(h("div", { class: "card-grid" },
            visible.map((p) => {
                const { element: nameEl, startEdit } = buildEditableName("h3", p.name, async (newName) => {
                    await api(`/api/projects/${p.id}`, { method: "PATCH", body: JSON.stringify({ name: newName }) });
                    p.name = newName;
                });
                const [menuBtn, menu] = buildCardMenu(
                    async () => {
                        try {
                            await api(`/api/projects/${p.id}`, { method: "DELETE" });
                            render();
                        } catch (err) {
                            alert(`Could not delete project: ${err.message}`);
                        }
                    },
                    async () => startEdit()
                );
                return h("div", {
                    class: "card",
                    onclick: () => { location.hash = `#/projects/${p.id}`; },
                }, [
                    nameEl,
                    h("p", { text: p.id }),
                    menuBtn,
                    menu,
                ]);
            })
        ));
    }
    searchInput.addEventListener("input", renderGrid);
    renderGrid();

    app.appendChild(h("section", {}, [
        h("h2", { text: "Projects" }),
        h("div", { class: "toolbar" }, [form, searchInputEl]),
        gridBox,
    ]));
}

// ---- Targets ----------------------------------------------------------

// Optional per-project "outputs mirror": a COPY of every target's own .txt
// output is also saved under a directory the pentester picks, one subfolder
// per target (backend/services/output_mirror_service.py). Purely a
// convenience copy outside OptiFuzz's own workspace -- the primary storage
// this app actually relies on is never affected either way.
async function buildOutputsMirrorSection(projectId) {
    const { dir, placeholder } = await api(`/api/projects/${projectId}/outputs-mirror`);

    const dirInput = h("input", {
        class: "outputs-mirror-input",
        placeholder,
        value: dir || "",
    });

    async function save(newDir) {
        try {
            const result = await api(`/api/projects/${projectId}/outputs-mirror`, {
                method: "PUT", body: JSON.stringify({ dir: newDir }),
            });
            dirInput.value = result.dir || "";
        } catch (err) {
            alert(`Could not update the outputs mirror folder: ${err.message}`);
        }
    }
    dirInput.addEventListener("change", () => save(dirInput.value.trim()));

    // Native folder picker -- only available inside the desktop app (the
    // pywebview js_api bridge, backend/run_desktop.py::_Api.pick_folder);
    // a plain browser tab has no such bridge, so fall back to typing the
    // path directly there.
    const pickBtn = h("button", { type: "button", class: "outputs-mirror-folder-btn", title: "Choose folder…" });
    pickBtn.innerHTML = FOLDER_ICON_SVG;
    pickBtn.addEventListener("click", async () => {
        if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.pick_folder) {
            alert("The native folder picker is only available inside the OptiFuzz desktop app -- type the path directly instead.");
            return;
        }
        const picked = await window.pywebview.api.pick_folder(dirInput.value.trim());
        if (picked) await save(picked);
    });

    attachTooltip(
        dirInput,
        "Optional: save a copy of every finished job's .txt output into this folder, one subfolder per target. "
            + "Outputs from jobs you already ran will be copied here too. Leave empty to disable. "
            + "The primary results stored inside OptiFuzz are never affected either way."
    );

    return h("section", { class: "outputs-mirror-section" }, [
        h("h2", { text: "Outputs mirror" }),
        h("div", { class: "outputs-mirror-row" }, [dirInput, pickBtn]),
    ]);
}

async function renderTargets(projectId) {
    const project = await api(`/api/projects/${projectId}`);
    breadcrumb.innerHTML = "";
    breadcrumb.appendChild(h("a", { href: "#/", text: "Projects" }));
    breadcrumb.append(` / ${project.name}`);

    const gridBox = h("div", {});
    const { element: searchInputEl, input: searchInput } = buildSearchInput({ class: "search-input", placeholder: "Search targets (name or URL)…" });
    let allTargets = [];

    function renderGrid() {
        const query = searchInput.value.trim().toLowerCase();
        const visible = query
            ? allTargets.filter((t) => t.name.toLowerCase().includes(query) || t.base_url.toLowerCase().includes(query))
            : allTargets;

        gridBox.innerHTML = "";
        if (!allTargets.length) {
            gridBox.appendChild(h("div", { class: "empty", text: "No targets yet." }));
            return;
        }
        if (!visible.length) {
            gridBox.appendChild(h("div", { class: "empty", text: "No targets match your search." }));
            return;
        }
        const grid = h("div", { class: "card-grid" },
            visible.map((t) => {
                const { element: nameEl, startEdit } = buildEditableName("span", t.name, async (newName) => {
                    await api(`/api/projects/${projectId}/targets/${t.id}`, {
                        method: "PATCH", body: JSON.stringify({ name: newName }),
                    });
                    t.name = newName;
                });
                const [menuBtn, menu] = buildCardMenu(
                    async () => {
                        try {
                            await api(`/api/projects/${projectId}/targets/${t.id}`, { method: "DELETE" });
                            refreshTargets();
                        } catch (err) {
                            alert(`Could not delete target: ${err.message}`);
                        }
                    },
                    async () => startEdit()
                );
                let fuzzingDot = null;
                if (t.is_fuzzing) {
                    fuzzingDot = h("span", { class: "fuzzing-dot" });
                    attachRunningJobsTooltip(fuzzingDot, t.running_jobs || []);
                }
                return h("div", {
                    class: "card",
                    onclick: () => { location.hash = `#/projects/${projectId}/targets/${t.id}`; },
                }, [
                    h("h3", {}, [fuzzingDot, nameEl]),
                    h("p", { text: t.base_url }),
                    menuBtn,
                    menu,
                ]);
            })
        );
        gridBox.appendChild(grid);
    }
    searchInput.addEventListener("input", renderGrid);

    async function refreshTargets() {
        allTargets = await api(`/api/projects/${projectId}/targets`);
        renderGrid();
    }

    // Port field starts EMPTY (just a hint showing what applies by default)
    // -- but a port is always spliced into the submitted base_url, whether
    // the pentester typed one or not: an explicit value if given, otherwise
    // the scheme's own real technical default (443 for https, 80 for http).
    // (Used to default http to 8080 -- explicitly changed back to the real
    // default, 80: it also fixes a latent bug this had, see the port-suffix
    // comment below.)
    function inferDefaultPort(url) {
        return url.trim().toLowerCase().startsWith("https://") ? "443" : "80";
    }
    const nameInput = h("input", { class: "target-name-input", placeholder: "Target name (e.g. api.example.com)", required: "required" });
    const urlInput = h("input", { class: "target-url-input", placeholder: "Base URL (https://api.example.com/)", required: "required" });
    const portInput = h("input", {
        type: "number", min: "1", max: "65535", class: "port-input", placeholder: "Default",
    });
    attachTooltip(
        portInput,
        "Port for this target. Leave empty to use the default for the scheme: 443 for https, 80 for http."
    );

    const form = h("form", { class: "inline-form", novalidate: "novalidate", onsubmit: async (e) => {
        e.preventDefault();
        const name = nameInput.value.trim();
        let baseUrl = urlInput.value.trim();
        if (!name || !baseUrl) return;
        const effectivePort = portInput.value.trim() || inferDefaultPort(baseUrl);
        try {
            const parsed = new URL(baseUrl);
            // Rebuilding the URL from its parts explicitly, not
            // `parsed.port = effectivePort; baseUrl = parsed.href` -- the
            // URL API silently OMITS a port that matches its scheme's own
            // *technical* default (443 for https, 80 for http), which broke
            // splicing :443 onto an https URL. Doing it by hand also lets
            // this app apply its OWN default (443/80, the same real values)
            // the same way on purpose: when the effective port already
            // matches that default, leave it out entirely -- https/http
            // already imply it, no ":443"/":80" needed -- and only spell
            // out the port when it's actually something else.
            const isDefaultPort = effectivePort === inferDefaultPort(parsed.href);
            const portSuffix = isDefaultPort ? "" : `:${effectivePort}`;
            baseUrl = `${parsed.protocol}//${parsed.hostname}${portSuffix}${parsed.pathname}${parsed.search}${parsed.hash}`;
        } catch (err) { /* malformed URL -- let the backend's own validation reject it */ }
        await api(`/api/projects/${projectId}/targets`, {
            method: "POST",
            body: JSON.stringify({ name, base_url: baseUrl }),
        });
        await refreshTargets();
        form.reset();
    } }, [
        nameInput,
        urlInput,
        h("label", { class: "port-label", text: "Port" }),
        portInput,
        h("button", { class: "primary", type: "submit", text: "+ New Target" }),
    ]);

    app.appendChild(await buildOutputsMirrorSection(projectId));

    // Downloads every target's own export in this project -- explicit
    // request, 2026-09-24. No hide-rules forwarding here (2026-10-01):
    // Results hide rules are now a per-TARGET concept (see
    // resultsHideFilters' own comment in renderWorkspace for the bug that
    // fixed), so there's no longer a single rule set that could correctly
    // apply to every target at once -- each target's own export shows
    // everything, unfiltered, rather than silently applying some ONE
    // target's rules to all the others.
    // Two formats, picked from the small arrow beside the main button
    // (explicit request): a .zip with one separate self-contained HTML per
    // target, or a single combined HTML reproducing this own Targets page
    // (search + cards, minus anything that mutates data -- no "+ New
    // Target" form, no Outputs mirror section, no export buttons) that, in
    // turn, opens any target's own JOBS/RESULTS/TREE workspace in place,
    // exactly like clicking a card does here live -- see
    // export_service.export_project_html. Picking a format in the menu only
    // changes the main button (its label and what a click downloads); nothing
    // is downloaded until the button itself is clicked (explicit request,
    // 2026-10-17). The picked format -- not necessarily .zip -- is remembered globally
    // (backend ui-prefs, not localStorage -- see loadPref's own comment),
    // across every project and across an app restart, explicit request.
    const EXPORT_ALL_FORMATS = {
        zip: { label: "Export all targets: separate reports (.zip)", title: "Download a .zip with a self-contained HTML report of every target in this project.", path: "export-all" },
        html: { label: "Export all targets: combined report (.html)", title: "Download one combined, self-contained HTML page with every target in this project.", path: "export-all-combined" },
    };
    const exportAllMainBtn = h("button", { type: "button", class: "export-target-btn export-all-main-btn" });
    function currentExportAllFormat() {
        const saved = loadPref("export-all-format", "zip");
        return EXPORT_ALL_FORMATS[saved] ? saved : "zip";
    }
    function downloadExportAll(format) {
        window.location.href = `/api/projects/${projectId}/${EXPORT_ALL_FORMATS[format].path}`;
    }
    function setExportAllFormat(format) {
        savePref("export-all-format", format);
        exportAllMainBtn.textContent = EXPORT_ALL_FORMATS[format].label;
        exportAllMainBtn.title = EXPORT_ALL_FORMATS[format].title;
        Array.prototype.forEach.call(exportAllMenu.querySelectorAll(".filter-chip"), (btn) => {
            btn.classList.toggle("active", btn.dataset.format === format);
        });
    }
    exportAllMainBtn.addEventListener("click", () => downloadExportAll(currentExportAllFormat()));
    const exportAllArrowBtn = h("button", {
        type: "button", class: "export-target-btn export-all-arrow-btn", text: "▾",
        title: "Choose a download format",
    });
    const exportAllMenu = h("div", { class: "filters-popup export-all-menu", hidden: "hidden" }, [
        h("button", {
            type: "button", class: "filter-chip", text: "Separate reports (.zip)", "data-format": "zip",
            onclick: () => {
                exportAllMenu.hidden = true;
                setExportAllFormat("zip");
            },
        }),
        h("button", {
            type: "button", class: "filter-chip", text: "Combined report (single .html)", "data-format": "html",
            onclick: () => {
                exportAllMenu.hidden = true;
                setExportAllFormat("html");
            },
        }),
    ]);
    setExportAllFormat(currentExportAllFormat()); // sets the main button's label/title AND the right menu item's .active state
    exportAllArrowBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        exportAllMenu.hidden = !exportAllMenu.hidden;
    });
    // Same self-removing "closes on an outside click" pattern as
    // buildCardMenu's own onDocumentClick -- unregisters itself the first
    // time it fires after this button has left the DOM (navigated away
    // from this Targets page) instead of leaking a listener forever.
    function onDocumentClickCloseExportAllMenu(e) {
        if (!document.body.contains(exportAllArrowBtn)) {
            document.removeEventListener("click", onDocumentClickCloseExportAllMenu);
            return;
        }
        if (e.target !== exportAllArrowBtn && !exportAllMenu.contains(e.target)) exportAllMenu.hidden = true;
    }
    document.addEventListener("click", onDocumentClickCloseExportAllMenu);
    const exportAllGroup = h("div", { class: "export-all-group filters-dropdown" }, [
        exportAllMainBtn, exportAllArrowBtn, exportAllMenu,
    ]);

    app.appendChild(h("section", {}, [
        h("div", { class: "section-header-row" }, [h("h2", { text: "Targets" }), exportAllGroup]),
        h("div", { class: "toolbar" }, [form, searchInputEl]),
        gridBox,
    ]));

    await refreshTargets();

    // Live-update the "currently fuzzing" dot without needing to leave/reenter the page.
    currentStream = new EventSource("/api/stream");
    currentStream.onmessage = () => {};
    for (const name of ["job.queued", "job.started", "job.completed", "job.failed", "job.stopped", "job.paused"]) {
        currentStream.addEventListener(name, (evt) => {
            const data = JSON.parse(evt.data);
            if (data.job && data.job.project_id === projectId) refreshTargets();
        });
    }
}

// ---- Inline "Fuzz this path" launcher (shared by Results and TREE) ------
//
// "Fuzz this path" from a Results row or a TREE node no longer navigates
// anywhere -- it opens a full New Fuzz form (the exact same form JOBS uses)
// directly under that row, pre-filled and ready to launch, without moving
// tabs or disturbing scroll position. Launching it closes the section again
// and refreshes JOBS's background job list so Running/Jobs is current
// whenever the pentester does visit that tab.
//
// The built form is cached by path (not rebuilt on every re-render) so a
// live SSE-triggered Results rebuild while the pentester is mid-typing
// doesn't throw their in-progress input away -- the same DOM node just gets
// re-appended into the freshly-rebuilt row (it may lose focus, but not the
// typed values).
function createInlineFuzzLauncherManager(projectId, targetId, onOpenChange, onLaunched) {
    const open = new Set();
    const cache = new Map(); // path -> "pending" | {element, setPath}

    function isOpen(path) {
        return open.has(path);
    }
    function get(path) {
        return cache.get(path);
    }
    function toggle(path) {
        if (open.has(path)) {
            open.delete(path);
            cache.delete(path);
            onOpenChange();
            return;
        }
        open.add(path);
        cache.set(path, "pending");
        buildNewFuzzForm(projectId, targetId, async () => {
            open.delete(path);
            cache.delete(path);
            await onLaunched();
            onOpenChange();
        }).then(({ element, setPath }) => {
            if (!open.has(path)) return; // closed again before the form finished loading
            setPath(path);
            cache.set(path, { element, setPath });
            onOpenChange();
        });
        onOpenChange();
    }

    return { isOpen, get, toggle };
}

function buildInlineFuzzLauncherBody(entry) {
    const body = h("div", { class: "inline-fuzz-launcher" });
    if (entry && entry !== "pending") {
        body.appendChild(entry.element);
    } else {
        body.appendChild(h("div", { class: "hint", text: "Loading…" }));
    }
    return body;
}

// Results-table variant: the launcher lives in its own full-width row
// directly under the result it was opened from.
function buildInlineFuzzLauncherRow(colCount, entry) {
    return h("tr", { class: "inline-fuzz-row" }, [
        h("td", { colspan: String(colCount) }, [buildInlineFuzzLauncherBody(entry)]),
    ]);
}

// ---- Target workspace ---------------------------------------------------

const ACTIVE_JOB_STATUSES = new Set(["QUEUED", "RUNNING", "PAUSED"]);

async function renderWorkspace(projectId, targetId, tab) {
    const [project, target] = await Promise.all([
        api(`/api/projects/${projectId}`),
        api(`/api/projects/${projectId}/targets/${targetId}`),
    ]);
    const TAB_LABELS = { jobs: "JOBS", results: "RESULTS", tree: "TREE" };
    breadcrumb.innerHTML = "";
    breadcrumb.appendChild(h("a", { href: "#/", text: "Projects" }));
    breadcrumb.append(" / ");
    breadcrumb.appendChild(h("a", { href: `#/projects/${projectId}`, text: project.name }));
    breadcrumb.append(` / ${target.name} / ${TAB_LABELS[tab]}`);

    const workspaceBase = `#/projects/${projectId}/targets/${targetId}`;
    // A single self-contained HTML snapshot of this Target,
    // openable later with no backend running. A plain navigation (not
    // api(), which always expects JSON) -- the endpoint's own
    // Content-Disposition: attachment header makes even the desktop app's
    // WebView2 view (a real Chromium browser control) trigger its native
    // Save As dialog on its own, no custom pywebview bridge needed.
    const exportBtn = h("button", {
        type: "button", class: "export-target-btn", text: "Export target (HTML)",
        title: "Download a self-contained HTML snapshot of this target (Tree, Results, Jobs) -- works offline, no backend needed.",
    });
    exportBtn.addEventListener("click", () => {
        // Whatever Results hide rules are active right now (resultsHideFilters,
        // declared further down in this same function -- safe to reference
        // here since this callback only ever runs on a later click, well
        // after the whole function body -- and that const -- has already run)
        // travel to the backend as a query param, so the export opens with
        // the same rows already hidden instead of resetting to everything
        // visible (explicit request, 2026-09-24) -- see export_target's own
        // docstring in backend/api/targets.py for the full rationale.
        const hideRulesParam = resultsHideFilters.length
            ? `?hide_rules=${encodeURIComponent(JSON.stringify(resultsHideFilters))}`
            : "";
        window.location.href = `/api/projects/${projectId}/targets/${targetId}/export${hideRulesParam}`;
    });
    const tabs = h("div", { class: "workspace-tabs" }, [
        h("a", { class: `workspace-tab${tab === "jobs" ? " active" : ""}`, href: workspaceBase, text: "JOBS" }),
        h("a", { class: `workspace-tab${tab === "results" ? " active" : ""}`, href: `${workspaceBase}/results`, text: "RESULTS" }),
        h("a", { class: `workspace-tab${tab === "tree" ? " active" : ""}`, href: `${workspaceBase}/tree`, text: "TREE" }),
        exportBtn,
    ]);
    app.appendChild(tabs);
    // Results/TREE's own toolbar row (search + filters + add-path) sticks
    // right below the workspace tabs while scrolling too (explicit
    // request) -- its sticky `top` needs to land exactly at the tabs bar's
    // own stuck height (`tabs.offsetHeight`, measured rather than guessed --
    // see .workspace-tabs's own docstring for why a guessed number drifts
    // out of sync). Deliberately NOT including the tabs bar's own
    // margin-bottom here: that margin is the gap between the two, and
    // .results-toolbar-sticky/.tree-toolbar-sticky's own `margin-top: -16px;
    // padding-top: 16px` (styles.css) pulls it inside the toolbar's OWN
    // sticky box instead (the exact same trick .workspace-tabs itself uses
    // against #app's padding) -- real bug this fixes: with the gap living
    // only in the tabs bar's margin (unpainted, no element's background
    // covers it), once BOTH rows were stuck the margin became a persistent
    // see-through band showing whatever scrolled behind it, instead of
    // solid background.
    //
    // Linux only (html.linux, set in index.html): measure the EXACT, fractional
    // height and keep it in sync. offsetHeight rounds to a whole pixel, but
    // here the bar is fractional (line-height 4/3 em), so a rounded `top` left
    // a sub-pixel seam between the two sticky bars. Chromium (Windows) snaps
    // everything to whole pixels when painting and never shows it; WebKitGTK
    // paints at fractional positions, so scrolled rows flashed through the
    // seam. The observer also re-measures once the bundled fonts finish
    // loading (they load asynchronously there, unlike Windows' system fonts)
    // and the bar's height changes. Windows keeps the original measurement.
    const onLinux = document.documentElement.classList.contains("linux");
    const applyTabsHeight = () => {
        // Each page render builds its own tabs bar and observer. After navigating away, the old bar is
        // detached and measures 0; without this guard its observer fires and overwrites the live
        // page's value with 0px (the toolbar then sticks UNDER the tabs and seems to vanish).
        if (!tabs.isConnected) return;
        const height = onLinux ? tabs.getBoundingClientRect().height : tabs.offsetHeight;
        if (!(height > 0)) return;
        document.documentElement.style.setProperty("--tabs-sticky-height", `${height}px`);
    };
    applyTabsHeight();
    if (onLinux && window.ResizeObserver) new ResizeObserver(applyTabsHeight).observe(tabs);

    const jobsSection = h("div", {});
    const resultsBody = h("tbody", {});
    // Results is virtualized: the full, ordered list of rows is planned as
    // cheap builder functions, and only the slice around the viewport is
    // actually built into the DOM (see paintResultsWindow). Building every
    // row up front cost seconds per tab switch on large result sets.
    let resultsPlan = [];
    // Parallel to resultsPlan -- each entry's own grouping key, in the same
    // order, so revealResultsPath() can find a path's row index without
    // building any DOM for it.
    let resultsPlanKeys = [];
    // Same order again -- each entry's own RAW path ("/api" vs "/api/"). Two
    // different results can share one key (the 301 stub "/api" and the real
    // directory "/api/"), so revealResultsPath() needs the raw path to pick
    // the exact row that was fuzzed.
    let resultsPlanPaths = [];
    // Same order as resultsPlan/resultsPlanKeys -- each entry's own
    // `seen_key` (results_service.annotate_new), or undefined for a
    // synthetic placeholder (nothing real to mark seen). Accumulated into
    // visitedSeenKeys below, NOT reset per render -- it has to survive the
    // whole visit, since collapsing/expanding/searching rebuilds the plan
    // repeatedly within one visit.
    let resultsPlanSeenKeys = [];
    // Every real result's seen_key that was actually painted into the DOM at
    // some point during THIS visit -- explicit request: a result scrolled
    // past, or left hidden behind a collapsed parent, must stay NEW even
    // after leaving the tab; only what was genuinely on screen gets marked.
    const visitedSeenKeys = new Set();
    let lastResultsHierarchy = null; // set by renderResultsTree(), read by revealResultsPath()
    let resultsRowHeight = 30; // average px per plan entry, refined as rows paint -- only a fallback
    // Measured height of every plan entry that has ever painted. Rows are NOT
    // all the same height: a long redirect URL wraps onto several lines
    // (overflow-wrap: anywhere), so one average mis-places everything computed
    // from it (spacers, scroll targets). Keyed by entryId(), so it survives the
    // plan being rebuilt; cleared when the window is resized (wrapping changes).
    const entryHeights = new Map();
    const RESULTS_OVERSCAN = 20;
    const knownJobIds = new Set();
    let hasActiveJobs = false;
    let allJobs = []; // full job list incl. config -- backs the Results "already fuzzed" popover
    let openHistoryPath = null; // Results row whose fuzz-history panel is open, if any
    let openJobInfoId = null; // JOBS row (by job id) whose own fuzz-options panel is open, if any
    let treeNodesByPath = new Map(); // exact path -> TREE node -- backs the "in TREE" add/remove icon

    // Same shape buildFuzzHistoryPanel() already expects (tree_service.fuzz_history's
    // per-entry dict) -- reused here for a single job's own row rather than
    // a path's whole history, so the JOBS "i" button can show the exact
    // same table the "Fuzzed" tag does elsewhere, just for one job.
    function jobToHistoryEntry(job) {
        return {
            job_id: job.id,
            status: job.status,
            wordlist_sources: job.wordlist_sources || [],
            extensions: (job.config && job.config.extensions) || [],
            config: job.config || {},
            result_count: job.result_count,
            output_stem: job.output_stem,
            pause_on_status: job.pause_on_status || [],
            // Only actually rendered for JOBS' own single-job panel
            // (buildFuzzHistoryPanel's hideStatus branch) -- Date moved
            // there from the main table's own row (2026-09-24), replaced
            // there by the live Rate/Errors columns.
            date: job.started_at || job.created_at,
        };
    }

    // ---- Elapsed-time tracking (JOBS table's "Elapsed" column) --------
    // Ticks live (setInterval below) while a job is QUEUED/RUNNING, freezes
    // at the final duration once terminal, and freezes-then-resumes across
    // a PAUSED span. There's no backend field for "total time spent
    // paused" (pause/resume isn't a full feature yet) -- accumulated here,
    // client-side, per job id, for this render session only; resets on a
    // page reload, same as every other per-session UI state in this file.
    const jobTimerState = new Map(); // job_id -> { pausedAccumMs, pauseStartedAt, lastStatus }

    function updateJobTimerState(job) {
        let state = jobTimerState.get(job.id);
        if (!state) {
            state = { pausedAccumMs: 0, pauseStartedAt: null, lastStatus: null };
            jobTimerState.set(job.id, state);
        }
        if (job.status === "PAUSED" && state.lastStatus !== "PAUSED") {
            state.pauseStartedAt = Date.now();
        } else if (job.status !== "PAUSED" && state.lastStatus === "PAUSED" && state.pauseStartedAt) {
            state.pausedAccumMs += Date.now() - state.pauseStartedAt;
            state.pauseStartedAt = null;
        }
        state.lastStatus = job.status;
    }

    function computeElapsedMs(job) {
        if (!job.started_at) return null;
        const state = jobTimerState.get(job.id) || { pausedAccumMs: 0, pauseStartedAt: null };
        const startedMs = new Date(job.started_at).getTime();
        if (job.status === "PAUSED") {
            const frozenAt = state.pauseStartedAt || Date.now();
            return Math.max(0, frozenAt - startedMs - state.pausedAccumMs);
        }
        if (ACTIVE_JOB_STATUSES.has(job.status)) {
            return Math.max(0, Date.now() - startedMs - state.pausedAccumMs);
        }
        const endedMs = job.ended_at ? new Date(job.ended_at).getTime() : Date.now();
        return Math.max(0, endedMs - startedMs - state.pausedAccumMs);
    }

    // JOBS' own Rate/Errors columns (2026-09-24, replacing Date's old slot --
    // Date itself moved into the fuzz-options detail panel, see
    // buildFuzzHistoryPanel's own hideStatus branch). Live values while
    // RUNNING come from the job.progress SSE handler below, which overwrites
    // these cells' text directly -- this is only the REST-fetched fallback
    // (initial render, or after a lifecycle event rebuilds the whole table),
    // where there's no live dirsearch output to read a current rate from,
    // only what's already persisted on the job itself. A RUNNING job has no
    // persisted "current rate" (only completed_requests/completed_lines are,
    // and those only advance at a checkpoint, not live) -- "—" until the
    // first real SSE tick arrives is honest; anything else (QUEUED, PAUSED,
    // terminal) has no more live ticks coming, so the average over its own
    // elapsed time so far is the real, right-forever number to show.
    function formatRateCellText(job) {
        if (job.status === "RUNNING") return "—";
        const elapsedMs = computeElapsedMs(job);
        if (!elapsedMs || !job.completed_requests) return "—";
        const avg = job.completed_requests / (elapsedMs / 1000);
        return `Avg. Rate: ${avg.toFixed(1)}/s`;
    }
    function formatErrorsCellText(job) {
        return String(job.errors || 0);
    }

    async function loadTreeNodes(skipRender = false) {
        try {
            const nodes = await api(`/api/projects/${projectId}/targets/${targetId}/tree`);
            treeNodesByPath = new Map(nodes.filter((n) => !n.synthetic).map((n) => [n.path, n]));
        } catch (e) { /* best-effort -- Results still works, just without the "in TREE" indicator */ }
        if (!skipRender) renderResultsTree();
    }

    // Pre-indexed once per refreshJobs() (not scanned per row) -- Results
    // used to re-filter the ENTIRE job list for every single row (both for
    // the "Fuzzed" tag and the "Fuzzed only" filter), an O(results × jobs)
    // cost paid on every render that got noticeably slow with many results
    // and many jobs on the same target. O(1) per row now.
    // Exact path, NOT normalized: "path/" and "path" are different things to fuzz
    // (TREE's own fuzz_history matches exactly too) -- a fuzz of "path/" must not
    // tag the "path" row as fuzzed.
    let jobsByPathKey = new Map(); // exact job path -> [job, ...], newest first
    function fuzzHistoryForPath(path) {
        return jobsByPathKey.get(path) || [];
    }

    async function refreshJobs() {
        const jobs = await api(`/api/projects/${projectId}/targets/${targetId}/jobs`);
        allJobs = jobs;
        jobsByPathKey = new Map();
        for (const j of jobs) {
            const key = pathWithoutQuery(j.path);
            if (!jobsByPathKey.has(key)) jobsByPathKey.set(key, []);
            jobsByPathKey.get(key).push(j);
        }
        for (const list of jobsByPathKey.values()) {
            list.sort((a, b) => (a.created_at < b.created_at ? 1 : -1)); // newest first, matching tree_service.fuzz_history
        }
        jobs.forEach((j) => { knownJobIds.add(j.id); updateJobTimerState(j); });
        hasActiveJobs = jobs.some((j) => ACTIVE_JOB_STATUSES.has(j.status));
        jobsSection.innerHTML = "";
        if (!jobs.length) {
            jobsSection.appendChild(h("div", { class: "empty", text: "No fuzz jobs yet." }));
            return;
        }
        const table = h("table", { class: "jobs-table" }, [
            h("thead", {}, [h("tr", {}, [
                h("th", { text: "Status" }), h("th", { text: "" }), h("th", { text: "" }), h("th", { text: "Path" }),
                h("th", { text: "Progress" }), h("th", { text: "Results", class: "jobs-col-center" }),
                h("th", { text: "Rate" }), h("th", { text: "Errors", class: "jobs-col-center" }), h("th", { text: "Elapsed" }),
                h("th", { text: "" }),
            ])]),
        ]);
        const tbody = h("tbody", {});
        for (const job of jobs.slice().reverse()) {
            // Its OWN column (between Status and the Resume/Pause/Stop
            // buttons), not sharing a cell with them -- explicit request:
            // sharing the actions cell meant that once Resume ("▶ Resume",
            // the widest label) also had to fit alongside it, the cell
            // wasn't wide enough and wrapped (icon on its own line, label
            // below it) instead of staying on one row.
            const infoOpen = openJobInfoId === job.id;
            const infoBtn = h("button", {
                type: "button",
                class: `icon-btn job-info-btn${infoOpen ? " job-info-btn-active" : ""}`,
                title: "Fuzz options this job ran with",
                onclick: () => {
                    openJobInfoId = infoOpen ? null : job.id;
                    refreshJobs();
                },
            });
            infoBtn.innerHTML = INFO_ICON_SVG;

            const actions = h("div", { style: "display:flex;gap:4px;align-items:center;" });
            if (job.status === "PAUSED") {
                actions.appendChild(h("button", {
                    class: "primary",
                    text: "▶ Resume",
                    onclick: async () => { await api(`/api/jobs/${job.id}/resume`, { method: "POST" }); refreshJobs(); },
                }));
            } else if (job.status === "QUEUED" || job.status === "RUNNING") {
                actions.appendChild(h("button", {
                    text: "⏸ Pause",
                    onclick: async () => { await api(`/api/jobs/${job.id}/pause`, { method: "POST" }); refreshJobs(); },
                }));
            }
            // Stop only exists while there is something to stop (queued, running
            // or paused) -- a finished job (completed/failed/stopped) shows no
            // Stop button at all, not a disabled one.
            if (ACTIVE_JOB_STATUSES.has(job.status)) {
                actions.appendChild(h("button", {
                    text: "Stop",
                    onclick: async () => { await api(`/api/jobs/${job.id}/stop`, { method: "POST" }); refreshJobs(); },
                }));
            }

            // A plain "🗑" emoji ignores `color` (emoji glyphs are
            // pre-colored) -- an inline SVG using currentColor is the only
            // reliable way to make this button actually render red.
            const deleteBtn = h("button", {
                class: "job-delete-btn",
                title: "Delete this job, its results, and its saved output file",
                onclick: async () => {
                    if (!confirm("Delete this job, its results, and its saved output file? This cannot be undone.")) {
                        return;
                    }
                    await api(`/api/jobs/${job.id}`, { method: "DELETE" });
                    allResults = allResults.filter((r) => r.job_id !== job.id);
                    renderResultsTree();
                    refreshJobs();
                },
            });
            deleteBtn.innerHTML = TRASH_ICON_SVG;
            if (ACTIVE_JOB_STATUSES.has(job.status)) deleteBtn.setAttribute("disabled", "disabled");

            const statusCell = h("td", {}, [h("span", { class: `job-status job-${job.status}`, text: job.status })]);
            if (job.status === "PAUSED" && job.auto_paused) {
                // pause_on_status is only ever non-empty when the
                // status-detection trigger is even armed (JobManager only
                // sets auto_paused from a crash-restart reconciliation or
                // from that trigger firing -- an empty list here can only
                // mean the former, since the trigger itself requires a
                // non-empty list to ever fire at all).
                const title = job.pause_on_status.length
                    ? `Auto-paused: status ${job.pause_on_status.join(",")} detected`
                    : "Auto-paused: the app was closed or crashed while this job was running";
                statusCell.appendChild(h("span", { class: "hint", title, text: " ⚠ auto" }));
            }
            if (job.status === "FAILED" && job.error_reason) {
                statusCell.appendChild(h("button", {
                    type: "button",
                    class: "icon-btn job-error-info",
                    title: "Why did this fail?",
                    text: "ⓘ",
                    onclick: () => showModal("Job failed", job.error_reason),
                }));
            }

            tbody.appendChild(h("tr", { "data-job-row": job.id }, [
                statusCell,
                h("td", { class: "job-info-cell" }, [infoBtn]),
                h("td", {}, [actions]),
                h("td", {}, [buildClickablePath(job.path, target.base_url, pathWithoutQuery(job.path))]),
                (() => {
                    const cell = h("td", { class: "jobs-progress-cell" });
                    renderProgressCell(cell, formatProgress(job));
                    return cell;
                })(),
                h("td", { class: "jobs-col-center" }, [
                    // JOBS' own Fuzzed-history panel never shows its own Results
                    // column (too narrow, overflowed) -- this outer count is the
                    // clickable one here instead (explicit request).
                    job.result_count
                        ? h("button", {
                            type: "button", class: "results-count-link", text: String(job.result_count),
                            title: "Go to Results",
                            onclick: () => goToResultsAndReveal(projectId, targetId, pathWithoutQuery(job.path)),
                        })
                        : h("span", { text: "0" }),
                ]),
                h("td", { class: "jobs-rate-cell", "data-rate-cell": job.id, text: formatRateCellText(job) }),
                h("td", { class: "jobs-errors-cell", "data-errors-cell": job.id, text: formatErrorsCellText(job) }),
                h("td", { class: "jobs-elapsed-cell", "data-elapsed-cell": job.id, text: formatElapsed(computeElapsedMs(job)) }),
                h("td", { class: "job-delete-cell" }, [deleteBtn]),
            ]));
            if (infoOpen) {
                tbody.appendChild(h("tr", { class: "fuzz-history-row" }, [
                    h("td", { colspan: "10" }, [
                        h("div", { class: "fuzz-history-panel" }, [buildFuzzHistoryPanel([jobToHistoryEntry(job)], true)]),
                    ]),
                ]));
            }
        }
        table.appendChild(tbody);
        jobsSection.appendChild(table);
    }

    // Results tree: a result whose path is an ancestor of another result's
    // path (e.g. "/admin/" found, then "/admin/index.html" found) groups
    // the second under the first, collapsible -- including a synthetic
    // placeholder row for an ancestor that was never itself discovered
    // (buildPathHierarchy()). A display convenience over the flat
    // auto-discovered Results list, not the manually-curated FUZZ TREE from
    // the spec (a separate feature).
    //
    // dirsearch reports a redirecting directory as the bare path (e.g.
    // "/api" with redirect "/api/", not "/api/" itself -- verified against a
    // real run), so grouping keys are normalized (trailing slash stripped)
    // rather than compared as raw strings, or "/api" would never match as
    // the parent of "/api/v1".
    let allResults = [];
    // Kept alongside allResults (not rebuilt from it on every addResults()
    // call) -- doing `new Set(allResults.map(...))` on every incoming batch
    // was an O(existing results) cost paid on every single live update,
    // several times a second per the job.result SSE handler's own comment,
    // which got noticeably slow once a target had thousands of results.
    // Keyed by the full exact-duplicate tuple (see addResults' own comment),
    // not just path.
    const seenResultKeys = new Set();
    const collapsedRouteKey = `results/${projectId}/${targetId}`; // keyed by normalized path
    const collapsedParents = getCollapsedSet(collapsedRouteKey);
    const newlyArrivedPaths = new Set(); // keyed by raw result.path

    // ---- Results filters: a free-text search, a "fuzzed only" toggle, and
    // any number of composable "hide" rules (status/size/parent path) -- a
    // result is hidden if it fully matches at least one active rule. ----
    // Persisted per target via the backend (same ui-prefs store hide rules
    // use) -- explicit request: search/Fuzzed-only/status must both stay
    // independent per target AND survive a restart, not just a tab switch.
    // A brand-new target has never saved anything under its own key, so it
    // always starts from these defaults, never another target's values.
    const resultsViewKey = `results/${projectId}/${targetId}`;
    const resultsViewMemo = loadPref(resultsViewKey, { search: "", fuzzedOnly: false, statuses: "" });
    let resultsSearchQuery = resultsViewMemo.search;
    let showFuzzedResultsOnly = resultsViewMemo.fuzzedOnly;
    let resultsStatusText = resultsViewMemo.statuses || "";
    let resultsStatusSet = parseStatusFilter(resultsStatusText);
    let resultsFiltersPopupOpen = false; // Results' "Filters ▾" dropdown -- same pattern as TREE's
    let resultsHideRulesPopupOpen = false; // "Hide rules ▾" dropdown -- the add-form AND every rule live inside it
    // Persisted via the backend (ui-prefs), not just in-session -- explicit request:
    // hide rules used to reset on every tab switch, and should survive that
    // (and an app restart, now that run_desktop.py gives WebView2 a stable
    // profile directory instead of a fresh one every launch). Keyed per
    // project+target (2026-10-01, explicit bug report) -- a single shared
    // "hideFilters" key used to mean setting a rule on ANY target applied it
    // to every other target/project too, which is not what "persists across
    // switches" was ever supposed to mean.
    // Hide rules live on the backend, per target (see results_service.set_hide_rules).
    // Filled in by loadHideRules() before the Results tab is shown.
    const resultsHideFilters = []; // [{status, size, parentPath, depth}]
    const hideRulesUrl = `/api/projects/${projectId}/targets/${targetId}/results/hide-rules`;
    async function loadHideRules() {
        try {
            const rules = await api(hideRulesUrl);
            if (!rules.length) {
                // Rules saved under the old per-target pref key (moved to ui-prefs at
                // startup) -- carried over once, so nothing already set is lost.
                const legacy = loadPref(`hideFilters:${projectId}/${targetId}`, []);
                if (legacy.length) {
                    rules.push(...legacy);
                    await api(hideRulesUrl, { method: "PUT", body: JSON.stringify({ rules: legacy }) });
                }
            }
            resultsHideFilters.push(...rules);
        } catch (e) { /* non-critical -- the tab still works, just without saved rules */ }
    }
    async function saveHideRules() {
        try {
            await api(hideRulesUrl, { method: "PUT", body: JSON.stringify({ rules: resultsHideFilters }) });
        } catch (e) {
            alert(`Could not save hide rules: ${e.message}`);
        }
    }

    // ---- Results scroll-position memory across a filter/search cycle ----
    // Explicit request: scroll down, apply a search/filter/hide-rule (the
    // now-shorter list makes the browser clamp scrollArea's own scrollTop
    // right back up), then clear it again -- the view should return to
    // where it was scrolled to before filtering, not stay clamped at the
    // top. `scrollPositions` (this file's own top-of-file per-route scroll
    // memory) can't be reused for this: its listener overwrites itself with
    // exactly that clamped value the instant the shorter list clamps it, so
    // a value captured just BEFORE the filtering state changes, independent
    // of that listener, is needed instead.
    let resultsPreFilterScrollTop = null;
    function resultsFiltersActive() {
        return !!resultsSearchQuery || showFuzzedResultsOnly || resultsStatusSet.size > 0 || resultsHideFilters.length > 0;
    }
    function withResultsFilterScrollMemory(mutate) {
        const wasActive = resultsFiltersActive();
        if (!wasActive) resultsPreFilterScrollTop = scrollArea.scrollTop;
        mutate();
        if (wasActive && !resultsFiltersActive() && resultsPreFilterScrollTop !== null) {
            const restore = resultsPreFilterScrollTop;
            resultsPreFilterScrollTop = null;
            // One frame so the now-restored (longer) list has actually
            // reflowed and is tall enough to scroll to this position again
            // -- setting it in the same tick as mutate()'s own render calls
            // it/the browser can clamp it right back down before the DOM
            // update is actually painted.
            requestAnimationFrame(() => { scrollArea.scrollTop = restore; });
        }
    }

    // depth: how many path levels below `parentPath` this rule reaches.
    // `f.depth === undefined` means a rule saved before this field existed
    // -- treated as 0 (unlimited/all descendants) so nobody's existing
    // saved rules silently start hiding less than before. A NEW rule
    // always gets an explicit depth from the add-form (1, matching its
    // own placeholder, if left blank) -- see addForm's submit handler.
    function matchesHideFilter(result, f) {
        if (f.status && String(result.status) !== String(f.status)) return false;
        if (f.size && (result.size || "").trim().toLowerCase() !== f.size.trim().toLowerCase()) return false;
        if (f.parentPath) {
            const parentKey = normalizeKey(f.parentPath);
            const resultKey = normalizeKey(result.path);
            const prefix = parentKey === "/" ? "/" : `${parentKey}/`;
            if (!resultKey.startsWith(prefix) || resultKey === parentKey) return false;
            const depth = f.depth === undefined ? 0 : f.depth;
            if (depth > 0) {
                const segments = resultKey.slice(prefix.length).split("/").length;
                if (segments > depth) return false;
            }
        }
        return true;
    }
    function passesResultFilters(r) {
        if (resultsSearchQuery && !r.path.toLowerCase().includes(resultsSearchQuery)) return false;
        if (showFuzzedResultsOnly && fuzzHistoryForPath(r.path).length === 0) return false;
        if (resultsStatusSet.size && !resultsStatusSet.has(r.status)) return false;
        if (resultsHideFilters.some((f) => matchesHideFilter(r, f))) return false;
        return true;
    }

    function addResults(newResults, isLive, skipRender = false) {
        if (!newResults.length) return;
        // Dedup here (not just at call sites): the live SSE stream and the
        // periodic disk-poll fallback (see below) can both eventually
        // deliver the same result, and two jobs run against the same path
        // report the same path independently -- both would otherwise show
        // as duplicate rows. Dedup incrementally against the persistent
        // seenResultKeys (not a Set rebuilt from allResults every call --
        // see its own declaration comment) since a single incoming batch
        // (e.g. the initial /results fetch, which concatenates every job's
        // report) can itself contain the same finding more than once.
        //
        // Real, reported bug this fixes (2026-09-25): the key used to be
        // `r.path` alone -- "already seen this path" is NOT the same thing
        // as "already seen this exact finding". A real target routinely
        // reports the SAME path more than once with genuinely DIFFERENT
        // details (a redirect stub found by one job, a real hit at a
        // slightly different URL/status/size found by another -- e.g.
        // dirsearch's own well-known "/admin" 301 stub + "/admin/" 200 real
        // hit pair), and the path-only key silently discarded every one of
        // those after the first, forever -- a genuine, reported data-loss
        // bug ("la tool necesita mostrar TODOS los resultados"), not
        // acceptable dedup behavior. The key now folds in every field that
        // makes two entries the SAME finding (status/size/redirect/url) --
        // two results only ever collapse into one when they'd render
        // completely identically anyway.
        const deduped = [];
        for (const r of newResults) {
            const key = `${r.path}\u0000${r.status}\u0000${r.size}\u0000${r.redirect || ""}\u0000${r.url}`;
            if (seenResultKeys.has(key)) continue;
            seenResultKeys.add(key);
            deduped.push(r);
        }
        newResults = deduped;
        if (!newResults.length) return;
        for (const r of newResults) {
            allResults.push(r);
            if (!isLive) continue;
            newlyArrivedPaths.add(r.path);
            // Walk up and re-open EVERY ancestor group (real or synthetic --
            // buildPathHierarchy() synthesizes a node for every intermediate
            // segment now), not just the immediate one, so a newly arrived
            // deeply-nested result is actually visible, not hidden several
            // collapsed levels down. Gated on `isLive` -- real, reported bug
            // this fixes: `seenResultKeys`/`allResults` are function-local
            // to renderWorkspace, so they start empty on every single visit
            // to the Results tab, not just once per app session. Before this
            // gate, THE INITIAL BULK LOAD (isLive: false, called fresh every
            // time) treated every already-known result as "newly arrived"
            // and ran this same re-open loop for all of them, silently
            // undoing whatever the user had manually collapsed the moment
            // they left and came back to the tab -- expand/collapse state
            // (backed by the backend ui-prefs, see toggleCollapsed()) was never
            // actually being lost, it was being overwritten right back open
            // on the very next render.
            let key = parentKeyOf(r.path);
            while (key && key !== "/") {
                collapsedParents.delete(key);
                key = parentKeyOf(key);
            }
        }
        // Coalesced to at most once per animation frame (not a synchronous
        // render here) -- job.result can fire several times a second while
        // a job is running, and a full table rebuild on every single one
        // got noticeably slow with thousands of existing rows. Every other
        // renderResultsTree() call site (a click, a filter change) stays
        // synchronous -- those never fire faster than a human can click.
        if (!skipRender) scheduleRenderResultsTree();
    }

    // `parentPath` is this row's own immediate parent's path (null at
    // depth 0) -- used only to compute displayPath (the segment shown),
    // never for indentation itself (that's --depth, set on the cell below).
    function buildResultRow(result, depth, childCount, parentPath, connector) {
        // computeDisplaySegment -- no leading "/" ever (this is a relative
        // segment shown next to its own indentation/connector, the leading
        // slash was never actually needed to read it correctly), a
        // trailing "/" only when this path is itself a directory (has
        // children, or dirsearch's own raw hit already had one -- e.g. a
        // real "/admin/" 200, not the synthetic ancestor implied by a
        // deeper path). Explicit request, correcting an earlier attempt
        // that added a leading slash instead.
        const isDirectory = childCount > 0 || result.path.endsWith("/");
        const displayPath = computeDisplaySegment(result.path, parentPath, isDirectory);
        const ownKey = normalizeKey(result.path);

        // The connector (├──/└──/│ -- see treeConnectorFor()) and the rest
        // (arrow/path/hint/Fuzzed tag) sit in two flex siblings now, NOT
        // plain inline flow -- real, reported bug that fixed: a long path
        // wrapping onto a 2nd line used to start back at the cell's own
        // left edge, cutting straight across the connector/tree-lines below
        // it instead of staying indented under its own position. A flex
        // item's own wrapped text always stays inside that item's box, to
        // the right of its (flex-shrink: 0, never-wraps) prefix sibling --
        // the inner div, not the <td> itself, same reasoning as
        // .tree-note-cell-inner elsewhere (keeps the column's own
        // table-layout: fixed width unaffected).
        const pathCell = h("td", {});
        const pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        const pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        if (childCount > 0) {
            const collapsed = collapsedParents.has(ownKey);
            const arrow = h("button", {
                type: "button", class: "result-expand", title: "Expand/collapse",
                text: collapsed ? "▸" : "▾",
            });
            arrow.addEventListener("click", (e) => {
                e.stopPropagation();
                toggleCollapsed(collapsedRouteKey, ownKey);
                renderResultsTree();
            });
            pathPrefix.appendChild(arrow);
        }
        const pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);
        const link = h("a", {
            class: "result-path-link", href: "#", text: displayPath, title: result.url,
        });
        link.addEventListener("click", (e) => {
            e.preventDefault();
            openResultUrl(result.url);
        });
        attachCopyPathContextMenu(link, () => result.path);
        pathContent.appendChild(link);
        if (childCount > 0) {
            pathContent.appendChild(h("span", { class: "hint", text: ` (${childCount})` }));
        }
        const history = fuzzHistoryForPath(result.path);
        if (history.length) {
            // No wordlist/extensions detail next to the tag here (unlike
            // TREE) -- just "Fuzzed"; the detail is one click away, in the
            // history table the tag opens.
            pathContent.appendChild(buildFuzzedTagButton(null, openHistoryPath === result.path, () => {
                openHistoryPath = openHistoryPath === result.path ? null : result.path;
                renderResultsTree();
            }));
        }

        // NEW lives here now (2026-09-26, explicit request: "a la derecha
        // del todo"), not the Status cell -- the inner div (not the <td>
        // itself, same reasoning as .tree-note-cell-inner) lets the badge
        // sit flush at the row's own right edge via margin-left: auto,
        // whether or not a redirect link is also present in this same cell.
        const redirectCell = h("td", {});
        const redirectInner = h("div", { class: "result-redirect-cell-inner" });
        redirectCell.appendChild(redirectInner);
        if (result.redirect) {
            // result.redirect can be relative (dirsearch reports it exactly
            // as the server's Location header, which isn't always
            // absolute) -- resolve it against this row's own absolute URL.
            // When that resolves to a DIFFERENT host or port, showing just
            // the path (e.g. "/new-service") would hide that the redirect
            // actually leaves this target's origin -- pentest-relevant, and
            // easy to miss -- so show the full absolute URL in that case
            // instead, with a distinct color as a visual flag.
            let resolved = null;
            try { resolved = new URL(result.redirect, result.url); } catch (e) { /* malformed Location header */ }
            const sourceOrigin = new URL(result.url).origin;
            const crossOrigin = resolved && resolved.origin !== sourceOrigin;
            // Same origin: always just the path, even if dirsearch's raw
            // Location header happened to be a full absolute URL -- the
            // full URL is reserved for actually flagging a cross-origin
            // redirect (different host/port), not shown when it's
            // identical to where this result itself lives.
            const label = resolved
                ? (crossOrigin ? resolved.href : `${resolved.pathname}${resolved.search}${resolved.hash}`)
                : result.redirect;

            const redirectLink = h("a", {
                class: `result-redirect-link${crossOrigin ? " redirect-cross-origin" : ""}`,
                href: "#", text: label,
                // Always the full resolved absolute URL, even when the
                // visible label is the shorter raw (possibly relative)
                // redirect string -- matches how the Path column's own
                // tooltip always shows the full URL too.
                title: crossOrigin
                    ? `Redirects off-target to a different host/port: ${resolved.href}`
                    : (resolved ? resolved.href : label),
            });
            redirectLink.addEventListener("click", (e) => {
                e.preventDefault();
                if (resolved) openResultUrl(resolved.href);
            });
            redirectInner.appendChild(redirectLink);
        }
        if (result.is_new) {
            redirectInner.appendChild(h("span", { class: "new-badge", title: "New since you last opened Results", text: "NEW" }));
        }

        // Synthetic ancestor (never actually discovered/fuzzed by
        // dirsearch, just an implied path segment -- see
        // buildPathHierarchy()) has no real status/size to show.
        const statusCell = result.synthetic
            ? h("td", {}, [h("span", { class: "hint tree-status-empty", text: "—" })])
            : h("td", {}, [h("span", { class: statusClass(result.status), text: String(result.status) })]);

        const fuzzOpen = fuzzLauncher.isOpen(result.path);
        const fuzzActionBtn = h("button", {
            type: "button", class: `icon-btn${fuzzOpen ? " fuzz-active" : ""}`,
            title: "Fuzz this path",
            onclick: () => fuzzLauncher.toggle(result.path),
        });
        fuzzActionBtn.innerHTML = fuzzOpen ? LIGHTNING_FILLED_ICON_SVG : LIGHTNING_OUTLINE_ICON_SVG;

        const treeNode = treeNodesByPath.get(result.path);
        const addToTreeBtn = h("button", {
            type: "button", class: `icon-btn${treeNode ? " in-tree" : ""}`,
            title: treeNode ? "In TREE — click to remove" : "Add to TREE",
            onclick: async (e) => {
                const btn = e.currentTarget;
                const existing = treeNodesByPath.get(result.path);
                if (existing) {
                    if (!confirm(
                        `Remove ${result.path} from the tree? Any notes saved on it in TREE will be lost.`
                    )) return;
                    try {
                        await api(`/api/projects/${projectId}/targets/${targetId}/tree/${existing.id}`, {
                            method: "DELETE",
                        });
                        treeNodesByPath.delete(result.path);
                        btn.classList.remove("in-tree");
                        btn.title = "Add to TREE";
                        btn.innerHTML = TREE_OUTLINE_ICON_SVG;
                    } catch (err) {
                        alert(`Could not remove from tree: ${err.message}`);
                    }
                    return;
                }
                try {
                    const node = await api(`/api/projects/${projectId}/targets/${targetId}/tree/from-result`, {
                        method: "POST",
                        body: JSON.stringify({
                            path: result.path, status: result.status,
                            size: result.size || null, job_id: result.job_id,
                        }),
                    });
                    treeNodesByPath.set(result.path, node);
                    btn.classList.add("in-tree");
                    btn.title = "In TREE — click to remove";
                    btn.innerHTML = TREE_FILLED_ICON_SVG;
                } catch (err) {
                    alert(`Could not add to tree: ${err.message}`);
                }
            },
        });
        addToTreeBtn.innerHTML = treeNode ? TREE_FILLED_ICON_SVG : TREE_OUTLINE_ICON_SVG;
        const actionsCell = h("td", { class: "result-actions" }, [fuzzActionBtn, addToTreeBtn]);

        const classes = newlyArrivedPaths.has(result.path) ? "new-row" : "";
        const rows = [h("tr", { class: classes }, [
            statusCell,
            h("td", { text: result.size || "" }),
            actionsCell,
            pathCell,
            redirectCell,
        ])];
        if (fuzzLauncher.isOpen(result.path)) {
            rows.push(buildInlineFuzzLauncherRow(5, fuzzLauncher.get(result.path)));
        }
        if (openHistoryPath === result.path) {
            rows.push(buildFuzzHistoryRow(5, history));
        }
        return rows;
    }

    function onDocumentClickCloseResultsHistory(e) {
        if (!document.body.contains(resultsBody)) {
            document.removeEventListener("click", onDocumentClickCloseResultsHistory);
            return;
        }
        if (openHistoryPath === null) return;
        if (e.target.closest(".fuzz-history-toggle") || e.target.closest(".fuzz-history-row")) return;
        openHistoryPath = null;
        renderResultsTree();
    }
    document.addEventListener("click", onDocumentClickCloseResultsHistory);

    function onDocumentClickCloseJobInfo(e) {
        if (!document.body.contains(jobsSection)) {
            document.removeEventListener("click", onDocumentClickCloseJobInfo);
            return;
        }
        if (openJobInfoId === null) return;
        if (e.target.closest(".job-info-btn") || e.target.closest(".fuzz-history-row")) return;
        openJobInfoId = null;
        refreshJobs();
    }
    document.addEventListener("click", onDocumentClickCloseJobInfo);

    // A path segment implied by a real result's path (e.g. fuzzing
    // straight at /a/b/c never independently discovers /a or /a/b) but
    // never itself discovered by dirsearch -- no real status/size/redirect,
    // but still a real, addressable URL: Fuzz/Add-to-tree stay available.
    function makeSyntheticResult(path) {
        let url;
        try { url = new URL(path, target.base_url).href; } catch (e) { url = target.base_url; }
        return { path, url, status: null, size: "", redirect: null, is_new: false, job_id: null, synthetic: true };
    }

    function appendHierarchyRows(hierarchy, node, depth, parentPath, prefix, isLast) {
        const children = hierarchy.childrenOf.get(node.key) || [];
        // The "(N)" next to a folder counts RESULTS, not tree positions: a 301
        // stub "x" and its directory "x/" share one position but are two
        // distinct results, so each one counts.
        const childResultCount = children.reduce((sum, child) => sum + child.items.length, 0);
        // Every genuinely distinct result at this position gets its own row
        // now (2026-09-25 -- see buildPathHierarchy's own comment), sharing
        // this one connector/indent since they occupy the same tree
        // position -- only the LAST one carries the expand arrow/child
        // count, so children render (and collapse) exactly once per
        // position, not once per item sharing it.
        // The row carrying the arrow/children is the LAST one, so when a 301
        // stub "x" and its directory "x/" share a position the directory goes
        // last (stable sort) -- otherwise the children hung off the stub while
        // the "Fuzzed" tag (exact path) sat on the directory row above it.
        const items = node.items.length > 1
            ? node.items.slice().sort((a, b) => Number(a.path.endsWith("/")) - Number(b.path.endsWith("/")))
            : node.items;
        items.forEach((item, i) => {
            const isLastItem = i === items.length - 1;
            // Only the bottom item of a shared position closes the branch (└);
            // the ones above it continue it (├) -- otherwise "x" and "x/" both
            // drew └ and the line looked broken.
            const connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
            resultsPlan.push(() => buildResultRow(item, depth, isLastItem ? childResultCount : 0, parentPath, connector));
            resultsPlanKeys.push(node.key);
            resultsPlanPaths.push(item.path);
            resultsPlanSeenKeys.push(item.seen_key);
        });
        if (children.length && !collapsedParents.has(node.key)) {
            const childPrefix = treeChildPrefixFor(depth, prefix, isLast);
            children.forEach((child, i) => {
                appendHierarchyRows(hierarchy, child, depth + 1, node.path, childPrefix, i === children.length - 1);
            });
        }
    }

    function renderResultsTree() {
        resultsPlan = [];
        resultsPlanKeys = [];
        resultsPlanPaths = [];
        resultsPlanSeenKeys = [];
        // Filtering happens on the flat list, before grouping: if a filter
        // hides an ancestor, its still-visible descendants just render as
        // their own top-level entries instead of disappearing too.
        const visibleResults = allResults.filter(passesResultFilters);
        const hierarchy = buildPathHierarchy(visibleResults, makeSyntheticResult);
        lastResultsHierarchy = hierarchy;

        hierarchy.topLevel.forEach((top, i) => {
            appendHierarchyRows(hierarchy, top, 0, null, "", i === hierarchy.topLevel.length - 1);
        });

        paintResultsWindow();
        newlyArrivedPaths.clear(); // don't re-flash on the next unrelated render
    }

    // Scrolls Results to `path`'s own row, expanding exactly its direct
    // children -- any grandchildren (children of those children) are forced
    // collapsed, so this always reveals exactly one level deep, regardless
    // of whatever was expanded/collapsed before (explicit request: clicking
    // a Results count in a Fuzzed-history panel, from JOBS or TREE).
    function revealResultsPath(path) {
        const key = normalizeKey(path);
        let ancestorKey = parentKeyOf(key);
        while (ancestorKey && ancestorKey !== "/") {
            collapsedParents.delete(ancestorKey);
            ancestorKey = parentKeyOf(ancestorKey);
        }
        collapsedParents.delete(key); // this path's own direct children: expanded
        if (lastResultsHierarchy) {
            const children = lastResultsHierarchy.childrenOf.get(key) || [];
            for (const child of children) {
                // A child that itself has children: force it closed, every time.
                if ((lastResultsHierarchy.childrenOf.get(child.key) || []).length) collapsedParents.add(child.key);
            }
        }
        savePref(`collapsed:${collapsedRouteKey}`, [...collapsedParents]);
        renderResultsTree();

        // The root has no row of its own in Results: its "results" are the whole list.
        if (key === "/") {
            scrollArea.scrollTop = 0;
            paintResultsWindow(false);
            return;
        }
        // Which row? A key can hold two real rows -- the 301 stub "/api" and
        // the real directory "/api/" (the one that carries the children). The
        // one whose RAW path equals what was fuzzed goes first ("path/" when
        // "path/" was fuzzed, "path" when "path" was); otherwise the last row
        // of the position, which is the one with the children.
        const candidates = [];
        resultsPlanKeys.forEach((k, i) => { if (k === key) candidates.push(i); });
        if (!candidates.length || !resultsBody.parentElement) return;
        const index = candidates.find((i) => resultsPlanPaths[i] === path) ?? candidates[candidates.length - 1];
        // The workspace tabs bar + this filter toolbar are `position: sticky`
        // INSIDE scrollArea, so once scrolled they cover the top
        // `tabs + toolbar` px of the viewport. The target row must land flush
        // BELOW them, as the first visible row, with its children right after
        // it and nothing of the rows before it peeking in (unless the list is
        // too short to scroll that far, where the browser's clamping applies).
        //
        // Converges by measuring, not by trusting an estimate: if the row is
        // not painted yet, jump to the best estimate (measured heights where
        // known) and paint; once painted, move by exactly how far it really is
        // from its place and repaint, until it sits there.
        for (let pass = 0; pass < 10; pass++) {
            const row = resultsBody.querySelector(`[data-plan-index="${index}"]`);
            if (!row) {
                scrollArea.scrollTop = Math.max(0, bodyTop() + entryOffsets()[index] - (stuckBottom() - areaTop()));
                paintResultsWindow(false);
                continue;
            }
            const delta = row.getBoundingClientRect().top - stuckBottom();
            if (Math.abs(delta) < 0.5) break;
            scrollArea.scrollTop += delta;
            paintResultsWindow(false);
        }
    }
    resultsReveal = (path) => revealResultsPath(path);

    // Builds only the plan entries inside the viewport (plus overscan) and
    // pads the rest with two spacer rows, so the scrollbar still reflects the
    // full list. The viewport position is read from the live page, so this
    // is only meaningful once the table is actually mounted -- render() calls
    // resultsRepaint() again right after it restores the scroll position.
    // ---- geometry helpers (viewport coordinates unless stated) ----
    const areaTop = () => scrollArea.getBoundingClientRect().top;
    // Where the sticky tabs bar + filter toolbar end: rows are hidden above it.
    const stuckBottom = () => areaTop() + tabs.getBoundingClientRect().height + filterBarContainer.getBoundingClientRect().height;
    // The tbody's own top within the scroll area's content (excludes the column-header row).
    const bodyTop = () => resultsBody.getBoundingClientRect().top - areaTop() + scrollArea.scrollTop;
    const entryId = (i) => `${resultsPlanKeys[i]}|${resultsPlanPaths[i]}`;
    const entryHeight = (i) => entryHeights.get(entryId(i)) ?? resultsRowHeight;
    // offsets[i] = where plan entry i starts inside the tbody, offsets[n] = total height.
    function entryOffsets() {
        const offsets = new Array(resultsPlan.length + 1);
        offsets[0] = 0;
        for (let i = 0; i < resultsPlan.length; i++) offsets[i + 1] = offsets[i] + entryHeight(i);
        return offsets;
    }
    function onResultsResize() {
        if (!resultsBody.isConnected) { window.removeEventListener("resize", onResultsResize); return; }
        entryHeights.clear(); // line wrapping depends on the width
    }
    window.addEventListener("resize", onResultsResize);

    // `keepAnchor` (default): the first row visible below the sticky bars stays
    // exactly where it is on screen across the repaint. Rows that were never
    // painted are only an estimate until they appear; without this, the moment
    // they do appear (real, taller height than estimated) everything below
    // them jumps. revealResultsPath() passes false: it positions things itself.
    function paintResultsWindow(keepAnchor = true) {
        // Not mounted (e.g. JOBS/TREE is showing, or a job's delete re-renders
        // this off-screen) -- nothing to paint into.
        if (!resultsBody.parentElement) return;
        const n = resultsPlan.length;
        // Read the layout BEFORE clearing the body: reading it while the table is
        // empty forces a layout that clamps scrollTop to 0 and loses the position.
        const top = bodyTop();
        let anchorIndex = null;
        let anchorOffset = 0;
        if (keepAnchor && n) {
            let barBottom = null; // only measured once rows exist, i.e. long after the toolbar was built
            for (const row of resultsBody.children) {
                if (row.dataset.planIndex === undefined) continue; // a spacer
                if (barBottom === null) barBottom = stuckBottom();
                const rect = row.getBoundingClientRect();
                if (rect.bottom > barBottom + 0.5) { anchorIndex = row.dataset.planIndex; anchorOffset = rect.top - barBottom; break; }
            }
        }
        const offsets = entryOffsets();
        // Largest entry index whose start is at or above y (binary search).
        const indexAt = (y) => {
            let lo = 0, hi = n;
            while (lo < hi) {
                const mid = (lo + hi + 1) >> 1;
                if (offsets[mid] <= y) lo = mid; else hi = mid - 1;
            }
            return lo;
        };
        const viewTop = scrollArea.scrollTop - top;
        const first = Math.max(0, indexAt(viewTop) - RESULTS_OVERSCAN);
        const last = Math.min(n, indexAt(viewTop + scrollArea.clientHeight) + 1 + RESULTS_OVERSCAN);
        resultsBody.innerHTML = "";
        if (!n) {
            if (allResults.length) {
                resultsBody.appendChild(h("tr", {}, [
                    h("td", { colspan: "5", class: "empty", text: "No results match your filters." }),
                ]));
            }
            return;
        }
        const spacer = (px) => h("tr", { class: "results-spacer" }, [h("td", { colspan: "5", style: `height:${px}px;padding:0;border:0` })]);
        if (first > 0) resultsBody.appendChild(spacer(offsets[first]));
        for (let i = first; i < last; i++) {
            for (const row of resultsPlan[i]()) {
                row.dataset.planIndex = String(i); // lets revealResultsPath() find where an entry really painted
                resultsBody.appendChild(row);
            }
            if (resultsPlanSeenKeys[i]) visitedSeenKeys.add(resultsPlanSeenKeys[i]);
        }
        if (last < n) resultsBody.appendChild(spacer(offsets[n] - offsets[last]));
        // Record what actually painted: each entry's real height (an entry can
        // be several <tr>), and their average as the fallback for unmeasured ones.
        // Only measure once the table is in the real page: while the page is
        // still being built it lives in a hidden scratch container with a
        // different width, where text wraps differently, and those heights
        // would poison every offset computed from them.
        const painted = new Map();
        if (scrollArea.contains(resultsBody)) {
            for (const row of resultsBody.children) {
                if (row.dataset.planIndex === undefined) continue;
                painted.set(row.dataset.planIndex, (painted.get(row.dataset.planIndex) || 0) + row.getBoundingClientRect().height);
            }
        }
        let sum = 0;
        for (const [index, height] of painted) { entryHeights.set(entryId(Number(index)), height); sum += height; }
        if (painted.size) resultsRowHeight = Math.max(12, sum / painted.size);
        if (anchorIndex !== null) {
            const row = resultsBody.querySelector(`[data-plan-index="${anchorIndex}"]`);
            if (row) {
                const shift = row.getBoundingClientRect().top - stuckBottom() - anchorOffset;
                if (Math.abs(shift) > 0.5) scrollArea.scrollTop += shift;
            }
        }
    }

    let resultsRepaintQueued = false;
    function onResultsScroll() {
        if (!resultsBody.isConnected) { scrollArea.removeEventListener("scroll", onResultsScroll); return; }
        if (resultsRepaintQueued) return;
        resultsRepaintQueued = true;
        requestAnimationFrame(() => { resultsRepaintQueued = false; paintResultsWindow(); });
    }
    scrollArea.addEventListener("scroll", onResultsScroll);
    resultsRepaint = () => paintResultsWindow();

    // Coalesces bursts of addResults() calls (job.result can fire several
    // times a second) into at most one actual rebuild per animation frame --
    // see addResults()'s own comment for why this exists.
    let resultsRenderScheduled = false;
    function scheduleRenderResultsTree() {
        if (resultsRenderScheduled) return;
        resultsRenderScheduled = true;
        requestAnimationFrame(() => {
            resultsRenderScheduled = false;
            renderResultsTree();
        });
    }

    async function loadResults(skipRender = false) {
        // Not a reset-and-reload: this doubles as a periodic live-results
        // fallback (see below), and addResults() dedups against what's
        // already shown, so re-fetching the full list repeatedly is safe
        // and won't spuriously re-expand a group the user just collapsed.
        const results = await api(`/api/projects/${projectId}/targets/${targetId}/results`);
        addResults(results, false, skipRender);
    }

    // Persistent NEW marker (2026-09-26 behavior change): only marking
    // Results as "seen" while the Results tab is actually the one mounted --
    // staying on JOBS or TREE must not silently clear NEW badges the
    // pentester hasn't had a chance to look at yet. The actual mark-seen
    // call now fires on LEAVING (render()'s own activeResultsView hook), not
    // here on entering -- this just registers that Results is the one
    // currently mounted, so render() knows to fire it once the pentester
    // navigates away.
    function markResultsSeenOnLeave() {
        if (tab !== "results") return;
        // getKeys reads visitedSeenKeys lazily (at the moment of leaving, not
        // now) -- it keeps accumulating every real result actually painted
        // on screen for as long as this visit lasts.
        activeResultsView = { projectId, targetId, getKeys: () => [...visitedSeenKeys] };
    }

    // Only Results uses this (TREE has its own instance, scoped inside
    // renderTreeSection) -- cheap to construct either way, nothing async
    // happens until toggle() is actually called.
    const fuzzLauncher = createInlineFuzzLauncherManager(projectId, targetId, renderResultsTree, refreshJobs);

    // ---- Results filter bar: search + "Fuzzed" toggle + composable "hide"
    // rules (status/size/parent path), e.g. hiding every 404, 3KB result
    // under /admin/. Rebuilt (not just re-rendered) whenever a chip is
    // added/removed/toggled -- typing in the search box itself never
    // rebuilds this, so it never loses focus mid-keystroke. ----
    const filterBarContainer = h("div", { class: "results-toolbar-sticky" });
    function renderResultsFilterUI() {
        filterBarContainer.innerHTML = "";
        const bar = h("div", { class: "filter-bar" });

        const { element: searchInputEl, input: searchInput } = buildSearchInput({
            value: resultsSearchQuery, placeholder: "Search path…",
        });
        searchInput.addEventListener("input", () => {
            withResultsFilterScrollMemory(() => {
                resultsSearchQuery = searchInput.value.trim().toLowerCase();
                resultsViewMemo.search = resultsSearchQuery;
                savePref(resultsViewKey, resultsViewMemo);
                renderResultsTree();
            });
        });
        bar.appendChild(searchInputEl);

        // Same "Filters ▾" dropdown pattern as TREE -- just the "Fuzzed"
        // toggle inside it for now, room to grow without cluttering the row.
        // Same active/count treatment as "Hide rules ▾" when one or more of
        // its own toggles are on.
        // Each status code counts on its own ("200,302" = 2, not 1).
        const activeFiltersCount = (showFuzzedResultsOnly ? 1 : 0) + resultsStatusSet.size;
        const filtersToggleBtn = h("button", {
            type: "button", class: `filter-chip${activeFiltersCount ? " active" : ""}`,
            text: `Filters ▾${activeFiltersCount ? ` (${activeFiltersCount})` : ""}`,
        });
        const filtersPopup = h("div", { class: "filters-popup" });
        if (!resultsFiltersPopupOpen) filtersPopup.setAttribute("hidden", "hidden");
        filtersPopup.appendChild(h("button", {
            type: "button", class: `filter-chip${showFuzzedResultsOnly ? " active" : ""}`, text: "Fuzzed",
            onclick: () => {
                withResultsFilterScrollMemory(() => {
                    showFuzzedResultsOnly = !showFuzzedResultsOnly;
                    resultsViewMemo.fuzzedOnly = showFuzzedResultsOnly;
                    savePref(resultsViewKey, resultsViewMemo);
                    renderResultsFilterUI();
                    renderResultsTree();
                });
            },
        }));
        const statusFilterInput = h("input", {
            class: `filter-status-input${resultsStatusText.trim() ? " active" : ""}`,
            placeholder: "e.g. 200,301", value: resultsStatusText,
            title: "Only show these status codes (comma-separated)",
        });
        statusFilterInput.addEventListener("input", () => {
            // Deliberately NOT renderResultsFilterUI() here -- that would rebuild
            // this input mid-typing. Update the toggle's own count in place instead.
            withResultsFilterScrollMemory(() => {
                resultsStatusText = statusFilterInput.value;
                resultsStatusSet = parseStatusFilter(resultsStatusText);
                resultsViewMemo.statuses = resultsStatusText;
                savePref(resultsViewKey, resultsViewMemo);
                statusFilterInput.classList.toggle("active", resultsStatusText.trim().length > 0);
                const n = (showFuzzedResultsOnly ? 1 : 0) + resultsStatusSet.size;
                filtersToggleBtn.textContent = `Filters ▾${n ? ` (${n})` : ""}`;
                filtersToggleBtn.classList.toggle("active", n > 0);
                renderResultsTree();
            });
        });
        filtersPopup.appendChild(h("label", { class: "filter-status-row" }, [h("span", { text: "Status:" }), statusFilterInput]));

        filtersToggleBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            resultsFiltersPopupOpen = !resultsFiltersPopupOpen;
            // Never both open at once -- opening one closes the other.
            if (resultsFiltersPopupOpen) resultsHideRulesPopupOpen = false;
            renderResultsFilterUI();
        });
        bar.appendChild(h("div", { class: "filters-dropdown" }, [filtersToggleBtn, filtersPopup]));

        // "Hide rules ▾" -- its own dropdown, separate from "Filters": the
        // add-form (Status/Size/Parent path + the "+ Hide" button) AND
        // every already-added rule both live inside it, hidden together
        // until opened, each added rule stacking below the form instead of
        // spilling out into the main toolbar row.
        const hideToggleBtn = h("button", {
            type: "button", class: `filter-chip${resultsHideFilters.length ? " active" : ""}`,
            text: `Hide rules ▾${resultsHideFilters.length ? ` (${resultsHideFilters.length})` : ""}`,
        });
        const hidePopup = h("div", { class: "filters-popup hide-rules-popup" });
        if (!resultsHideRulesPopupOpen) hidePopup.setAttribute("hidden", "hidden");

        const statusInput = h("input", { placeholder: "Status", title: "e.g. 404" });
        const sizeInput = h("input", { placeholder: "Size", title: "e.g. 3KB" });
        // 50% wider than Status/Size, per explicit request -- a parent
        // path is typically the longest of the three values.
        const parentInput = h("input", { class: "filter-parent-input", placeholder: "Parent path", title: "e.g. /admin/" });
        const depthInput = h("input", { type: "number", min: "0", class: "filter-depth-input", placeholder: "1" });
        attachTooltip(
            depthInput,
            "How many path levels below \"Parent path\" this rule hides. 1 (default) = direct "
                + "children only. Set to 0 to hide every descendant, no matter how deep.",
            280,
        );
        const addForm = h("form", { class: "filter-add-form" }, [
            statusInput, sizeInput, parentInput, depthInput,
            h("button", { type: "submit", text: "+ Hide" }),
        ]);
        addForm.addEventListener("submit", (e) => {
            e.preventDefault();
            const depthRaw = depthInput.value.trim();
            const f = {
                status: statusInput.value.trim(), size: sizeInput.value.trim(), parentPath: parentInput.value.trim(),
                // Explicit default (matches the input's own placeholder) --
                // NOT left undefined, which matchesHideFilter() reserves
                // for a rule saved before this field existed (treated as
                // unlimited depth there, so old rules don't silently start
                // hiding less than before).
                depth: depthRaw ? Number(depthRaw) : 1,
            };
            if (!f.status && !f.size && !f.parentPath) return;
            withResultsFilterScrollMemory(() => {
                resultsHideFilters.push(f);
                saveHideRules();
                renderResultsFilterUI();
                renderResultsTree();
            });
        });
        hidePopup.appendChild(addForm);

        // Wrapped as many-per-row (not one full-width row each) in its own
        // flex-wrap container, kept separate from the add-form above it.
        const chipsRow = h("div", { class: "hide-rules-chips" });
        for (const f of resultsHideFilters) {
            const depthSuffix = !f.parentPath ? null
                : (f.depth === undefined || f.depth === 0) ? "all descendants"
                : `depth ${f.depth}`; // explicit request: depth 1 reads as "depth 1", same as 2/3/...
            const label = [
                f.status && `status ${f.status}`, f.size,
                f.parentPath && (depthSuffix ? `${f.parentPath} (${depthSuffix})` : f.parentPath),
            ].filter(Boolean).join(" · ");
            chipsRow.appendChild(h("span", { class: "filter-chip-remove" }, [
                document.createTextNode(label),
                h("button", {
                    type: "button", text: "✕", title: "Remove filter",
                    onclick: () => {
                        withResultsFilterScrollMemory(() => {
                            const idx = resultsHideFilters.indexOf(f);
                            if (idx !== -1) resultsHideFilters.splice(idx, 1);
                            saveHideRules();
                            renderResultsFilterUI();
                            renderResultsTree();
                        });
                    },
                }),
            ]));
        }
        hidePopup.appendChild(chipsRow);
        hideToggleBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            resultsHideRulesPopupOpen = !resultsHideRulesPopupOpen;
            // Never both open at once -- opening one closes the other.
            if (resultsHideRulesPopupOpen) resultsFiltersPopupOpen = false;
            renderResultsFilterUI();
        });
        bar.appendChild(h("div", { class: "filters-dropdown" }, [hideToggleBtn, hidePopup]));

        filterBarContainer.appendChild(bar);
    }
    function onDocumentClickCloseResultsFilters(e) {
        if (!document.body.contains(filterBarContainer)) {
            document.removeEventListener("click", onDocumentClickCloseResultsFilters);
            return;
        }
        if (e.target.closest(".filters-dropdown")) return; // each dropdown's own toggle handles its own clicks
        let changed = false;
        if (resultsFiltersPopupOpen) { resultsFiltersPopupOpen = false; changed = true; }
        if (resultsHideRulesPopupOpen) { resultsHideRulesPopupOpen = false; changed = true; }
        if (changed) renderResultsFilterUI();
    }
    document.addEventListener("click", onDocumentClickCloseResultsFilters);

    let treeRefresh = null; // set below if this is the TREE tab -- lets the SSE job-event listener refresh it live

    if (tab === "jobs") {
        const { element: newFuzzForm } = await buildNewFuzzForm(projectId, targetId, refreshJobs);
        app.appendChild(h("section", {}, [
            h("h2", {}, [
                document.createTextNode("New Fuzz "),
                h("span", { class: "section-subtitle", text: "(Set default options for this target)" }),
            ]),
            newFuzzForm,
        ]));
        app.appendChild(h("section", {}, [h("h2", { text: "Running / Jobs" }), jobsSection]));
    } else if (tab === "tree") {
        const treeApi = await renderTreeSection(projectId, targetId, target.base_url, refreshJobs);
        treeRefresh = treeApi.refresh;
        app.appendChild(treeApi.element);
    } else {
        renderResultsFilterUI();
        app.appendChild(h("section", {}, [
            filterBarContainer,
            h("table", { class: "results-table" }, [
                h("thead", {}, [h("tr", {}, [
                    h("th", { text: "Status" }), h("th", { text: "Size" }), h("th", { text: "" }),
                    h("th", { text: "Path" }), h("th", { text: "Redirect" }),
                ])]),
                resultsBody,
            ]),
        ]));
    }

    // Parallel, not sequential -- independent backend fetches. Real,
    // reported bug this fixes: every tab switch used to await all of these
    // one after another before render() ever revealed the rebuilt page (see
    // render()'s own visibility-hidden comment), so the visible "flicker" on
    // each switch was really the sum of every one of these round-trips, not
    // just one.
    // One synchronous render of the Results table, here, before render()
    // swaps this page in -- and not once per load. Rendering an 8k-row table
    // several times over (once per loader, plus a deferred frame) was the
    // whole cost of a tab switch. The same pass also leaves the rows in place
    // for render()'s scroll restore, which runs right after this await.
    const initialLoads = [refreshJobs(), loadResults(true)];
    if (tab === "results") {
        initialLoads.push(loadHideRules());
        markResultsSeenOnLeave();
        initialLoads.push(loadTreeNodes(true));
    }
    await Promise.all(initialLoads);
    if (tab === "results") {
        renderResultsFilterUI(); // chips for the rules that just loaded
        renderResultsTree();
    }

    currentStream = new EventSource("/api/stream");
    currentStream.onmessage = () => {}; // named events only, default handler unused
    for (const name of ["job.queued", "job.started", "job.completed", "job.failed", "job.stopped", "job.paused"]) {
        currentStream.addEventListener(name, (evt) => {
            const data = JSON.parse(evt.data);
            if (data.job && data.job.target_id === targetId) {
                knownJobIds.add(data.job.id);
                refreshJobs();
                // TREE's own fuzz-history-per-node comes from a full node
                // refetch, not from this stream directly -- without this a
                // job started from TREE wouldn't show as running there
                // until the pentester left and came back to the tab.
                if (treeRefresh) treeRefresh();
            }
        });
    }
    currentStream.addEventListener("job.result", (evt) => {
        // Deliberately not refreshJobs() here either (see job.progress
        // below): this can fire several times a second with 2+ jobs
        // active, and a full jobsSection.innerHTML rebuild on every single
        // one was tearing down and recreating every row -- which reset
        // Progress back to its last REST-fetched (checkpoint-only, no live
        // data) value each time, producing a visible 100% -> 0% -> 100%
        // flicker.
        // Just bump this one row's Results cell.
        const data = JSON.parse(evt.data);
        if (!knownJobIds.has(data.job_id)) return;
        addResults(data.results, true);
        const row = jobsSection.querySelector(`tr[data-job-row="${data.job_id}"]`);
        const cell = row && row.children[5]; // Status(0), info(1), actions(2), Path(3), Progress(4), Results(5)
        if (cell) {
            const link = cell.querySelector(".results-count-link");
            const newCount = (Number((link || cell).textContent) || 0) + data.results.length;
            if (link) {
                link.textContent = String(newCount);
            } else {
                // Was a plain "0" (no button, nothing to go to yet) -- the first
                // live result upgrades it to the clickable count.
                const job = allJobs.find((j) => j.id === data.job_id);
                cell.innerHTML = "";
                cell.appendChild(h("button", {
                    type: "button", class: "results-count-link", text: String(newCount), title: "Go to Results",
                    onclick: () => goToResultsAndReveal(projectId, targetId, job ? pathWithoutQuery(job.path) : ""),
                }));
            }
        }
    });
    currentStream.addEventListener("job.progress", (evt) => {
        // Deliberately not refreshJobs() here -- that rebuilds the whole
        // table, and this event can fire several times a second while a
        // job is running. Just update this row's own live cells directly.
        const data = JSON.parse(evt.data);
        const row = jobsSection.querySelector(`tr[data-job-row="${data.job_id}"]`);
        const progressCell = row && row.children[4]; // Status(0), info(1), actions(2), Path(3), Progress(4)
        if (progressCell) {
            renderProgressCell(progressCell, { pct: `${data.overall_percent}%`, detail: ` (${data.overall_done}/${data.overall_total})` });
        }
        // Found by their own data attribute, not a column index -- unlike
        // Progress/Results above (unchanged positions, safe to keep as an
        // index), these two are genuinely NEW columns added in the same
        // change that introduced this data-attribute lookup, so there was
        // no reason to repeat the earlier index-based bug class here too.
        const rateCell = jobsSection.querySelector(`td[data-rate-cell="${data.job_id}"]`);
        if (rateCell) rateCell.textContent = `${data.rate}/s`;
        const errorsCell = jobsSection.querySelector(`td[data-errors-cell="${data.job_id}"]`);
        if (errorsCell) errorsCell.textContent = String(data.errors);
    });

    // Fallback for results: the live job.result stream is parsed from
    // dirsearch's stdout, which can in principle build up a backlog under
    // sustained output, while dirsearch's own on-disk JSON report is
    // written incrementally per-result and never lags -- loadResults() reads
    // that file. Re-polling it periodically while a job is active means
    // results always catch up close to real time even if the SSE stream
    // itself is behind.
    currentResultsPoll = setInterval(() => {
        if (hasActiveJobs) loadResults();
    }, 3000);

    // Ticks the JOBS table's "Elapsed" cell live, once a second, for any
    // QUEUED/RUNNING job -- just that one cell's text, not a full
    // refreshJobs() rebuild (same reasoning as the job.result/job.progress
    // SSE handlers below: a full rebuild every second would be wasteful and
    // would fight with in-progress interaction). A no-op when the JOBS tab
    // isn't the one mounted (the querySelector below just finds nothing).
    currentJobsTicker = setInterval(() => {
        for (const job of allJobs) {
            if (!ACTIVE_JOB_STATUSES.has(job.status) || job.status === "PAUSED") continue;
            const cell = jobsSection.querySelector(`td[data-elapsed-cell="${job.id}"]`);
            if (cell) cell.textContent = formatElapsed(computeElapsedMs(job));
        }
    }, 1000);
}

// ---- FUZZ TREE ------------------------------------------------------------
//
// Distinct from Results: never auto-populated from a fuzz -- only
// "Add path" here or "Add to tree" from a Results row create a node.
// Hierarchy is derived purely from path segments (the same
// normalizeKey/parentKeyOf helpers Results uses for its own grouping), not
// stored explicitly, since a node's path IS its position in the tree.

async function renderTreeSection(projectId, targetId, baseUrl, onJobLaunched) {
    const treeUrl = `/api/projects/${projectId}/targets/${targetId}/tree`;
    const body = h("tbody", {});
    const collapsedRouteKey = `tree/${projectId}/${targetId}`; // keyed by normalized node path
    const collapsedParents = getCollapsedSet(collapsedRouteKey);
    let openHistoryPath = null; // node path whose fuzz-history panel is open, if any
    let nodes = [];
    const fuzzLauncher = createInlineFuzzLauncherManager(projectId, targetId, renderRows, onJobLaunched);

    // ---- TREE filters: free-text search + "Interesting"/"Fuzzed" toggles ----
    let treeSearchQuery = "";
    let showInterestingOnly = false;
    let showFuzzedOnlyTree = false;
    function passesTreeFilters(n) {
        if (treeSearchQuery && !n.path.toLowerCase().includes(treeSearchQuery)) return false;
        if (showInterestingOnly && !n.interesting) return false;
        if (showFuzzedOnlyTree && !(n.fuzz_history && n.fuzz_history.length)) return false;
        return true;
    }

    // Same scroll-position-across-a-filter-cycle memory as Results -- see
    // withResultsFilterScrollMemory's own comment for the full reasoning.
    let treePreFilterScrollTop = null;
    function treeFiltersActive() {
        return !!treeSearchQuery || showInterestingOnly || showFuzzedOnlyTree;
    }
    function withTreeFilterScrollMemory(mutate) {
        const wasActive = treeFiltersActive();
        if (!wasActive) treePreFilterScrollTop = scrollArea.scrollTop;
        mutate();
        if (wasActive && !treeFiltersActive() && treePreFilterScrollTop !== null) {
            const restore = treePreFilterScrollTop;
            treePreFilterScrollTop = null;
            requestAnimationFrame(() => { scrollArea.scrollTop = restore; });
        }
    }

    function onDocumentClickCloseTreeHistory(e) {
        if (!document.body.contains(body)) {
            document.removeEventListener("click", onDocumentClickCloseTreeHistory);
            return;
        }
        if (openHistoryPath === null) return;
        if (e.target.closest(".fuzz-history-toggle") || e.target.closest(".fuzz-history-row")) return;
        openHistoryPath = null;
        renderRows();
    }
    document.addEventListener("click", onDocumentClickCloseTreeHistory);

    async function refresh() {
        nodes = await api(treeUrl);
        renderRows();
    }

    // A synthetic node (an implied ancestor segment -- see
    // buildPathHierarchy() -- never itself explicitly added to the tree)
    // has no real id to PATCH/DELETE against. Interacting with one (marking
    // it interesting, writing a note) implicitly adds it first -- the same
    // "Add path" the pentester could've done by hand, just triggered by
    // that first edit instead.
    async function ensureRealNode(node) {
        if (!node.synthetic) return node;
        const real = await api(treeUrl, { method: "POST", body: JSON.stringify({ path: node.path }) });
        node.id = real.id;
        node.synthetic = false;
        return node;
    }

    function buildNodeRow(node, depth, childCount, parentPath, connector) {
        const ownKey = normalizeKey(node.path);
        // computeDisplaySegment -- see buildResultRow's own identical
        // comment (same rules, same helper, both tables): no leading "/",
        // a trailing one only when this node is itself a directory.
        const isDirectory = childCount > 0 || node.path.endsWith("/");
        const displayPath = computeDisplaySegment(node.path, parentPath, isDirectory);
        const history = node.fuzz_history || [];

        // Same two-flex-sibling layout as Results' own buildResultRow (see
        // its comment for the full story): a long path wrapping onto a 2nd
        // line now stays indented under its own content, never cutting back
        // across the connector/tree-lines of the rows below it.
        const pathCell = h("td", { class: "tree-path-cell" });
        const pathCellInner = h("div", { class: "tree-path-cell-inner" });
        pathCell.appendChild(pathCellInner);
        const pathPrefix = h("span", { class: "tree-path-prefix" });
        pathCellInner.appendChild(pathPrefix);
        appendConnectorUnits(pathPrefix, connector);
        if (childCount > 0) {
            const collapsed = collapsedParents.has(ownKey);
            const arrow = h("button", {
                type: "button", class: "result-expand", title: "Expand/collapse", text: collapsed ? "▸" : "▾",
            });
            arrow.addEventListener("click", (e) => {
                e.stopPropagation();
                toggleCollapsed(collapsedRouteKey, ownKey);
                renderRows();
            });
            pathPrefix.appendChild(arrow);
        }
        const pathContent = h("span", { class: "tree-path-content" });
        pathCellInner.appendChild(pathContent);

        // Star + fuzz icon: their own column (not indented inline before
        // the path text) -- explicit request, reversing an earlier
        // decision that kept them inline specifically so a child row's
        // icons would shift right along with its own indentation. Same
        // fixed-column treatment Results already uses for its own
        // fuzz/Add-to-TREE icons.
        const interestingBtn = h("button", {
            type: "button", class: `icon-btn tree-row-prefix-btn${node.interesting ? " active" : ""}`,
            title: "Mark interesting",
        });
        interestingBtn.innerHTML = node.interesting ? STAR_FILLED_ICON_SVG : STAR_OUTLINE_ICON_SVG;
        interestingBtn.addEventListener("click", async (e) => {
            e.stopPropagation();
            try {
                const real = await ensureRealNode(node);
                await api(`${treeUrl}/${real.id}`, {
                    method: "PATCH", body: JSON.stringify({ interesting: !node.interesting }),
                });
                await refresh();
            } catch (err) {
                alert(`Could not update: ${err.message}`);
            }
        });
        const treeFuzzOpen = fuzzLauncher.isOpen(node.path);
        const fuzzBtn = h("button", {
            type: "button", class: `icon-btn tree-row-prefix-btn${treeFuzzOpen ? " fuzz-active" : ""}`,
            title: "Fuzz this path",
        });
        fuzzBtn.innerHTML = treeFuzzOpen ? LIGHTNING_FILLED_ICON_SVG : LIGHTNING_OUTLINE_ICON_SVG;
        fuzzBtn.addEventListener("click", (e) => {
            e.stopPropagation();
            fuzzLauncher.toggle(node.path);
        });
        const actionsCell = h("td", { class: "tree-actions-cell" }, [interestingBtn, fuzzBtn]);

        // Clickable, same as a Results row's path -- opens it in the
        // configured browser, resolved against the target's base_url since
        // a TREE node only ever stores a relative path.
        pathContent.appendChild(buildClickablePath(node.path, baseUrl, displayPath));

        const sizeCell = h("td", { text: (node.status && node.status.size) || "" });

        // "Fuzzed (wordlist + extensions)" tag -- itself the toggle for the
        // compact history table below -- then a directly-editable note
        // right in the row, no dialog, saved on blur.
        const fuzzedTag = formatFuzzedTag(history);
        const noteInput = h("textarea", { rows: "1", class: "tree-note-input", placeholder: "Add a note…" });
        noteInput.value = node.note || "";
        function autoGrowNote() {
            noteInput.style.height = "auto";
            noteInput.style.height = `${noteInput.scrollHeight}px`;
        }
        requestAnimationFrame(autoGrowNote); // size correctly for a pre-filled note once actually attached to the DOM
        noteInput.addEventListener("input", autoGrowNote);
        noteInput.addEventListener("click", (e) => e.stopPropagation());
        noteInput.addEventListener("blur", async () => {
            if (noteInput.value === (node.note || "")) return;
            try {
                const real = await ensureRealNode(node);
                await api(`${treeUrl}/${real.id}`, { method: "PATCH", body: JSON.stringify({ note: noteInput.value }) });
                node.note = noteInput.value; // keep in sync without a full refresh (avoids dropping focus elsewhere)
            } catch (err) {
                alert(`Could not save note: ${err.message}`);
            }
        });
        // The flex row lives on an inner div, not the <td> itself -- see
        // .tree-note-cell-inner in styles.css for why.
        const noteCell = h("td", {}, [
            h("div", { class: "tree-note-cell-inner" }, [
                fuzzedTag ? buildFuzzedTagButton(fuzzedTag, openHistoryPath === node.path, () => {
                    openHistoryPath = openHistoryPath === node.path ? null : node.path;
                    renderRows();
                }) : null,
                noteInput,
            ]),
        ]);

        // Styled like the Running/Jobs delete button (red), fully
        // right-aligned in its own cell. The root is never removable at
        // all (backend also enforces this) -- explicit request: "que en
        // el tree salga la raiz '/' por defecto y no se pueda borrar".
        //
        // A SYNTHETIC node (never itself explicitly added -- just implied
        // by a deeper real path, e.g. "/path1" when only
        // "/path1/path2/test" was ever added) still gets a delete button
        // WHEN it has children -- explicit follow-up report: this used to
        // be the one remaining way a manually-added path's parent stayed
        // undeletable even after delete_node() itself started cascading,
        // since a synthetic node has no real id to call that with at all.
        // Goes through the by-path/subtree endpoint instead, which
        // cascades by PATH rather than by id.
        let deleteBtn = null;
        if (node.path !== "/" && (!node.synthetic || childCount > 0)) {
            deleteBtn = h("button", { type: "button", class: "job-delete-btn", title: "Remove from tree" });
            deleteBtn.innerHTML = TRASH_ICON_SVG;
            deleteBtn.addEventListener("click", async (e) => {
                e.stopPropagation();
                // Deleting a parent now cascades to any real children
                // already in the tree under it too (backend:
                // tree_service.delete_node/.delete_subtree) -- explicit
                // request: a manually-added parent path used to be
                // effectively undeletable, since a real descendant's own
                // key kept implying the parent still existed, so it just
                // reappeared as a synthetic placeholder right after.
                // childCount > 0 doesn't guarantee a REAL child gets
                // cascaded (it could be entirely synthetic intermediate
                // segments), but it's the cheap, always-available signal
                // this row already has -- worth the warning either way.
                const message = childCount > 0
                    ? `Remove ${node.path} from the tree? Any children already in the tree under it will be removed too. This does not delete any fuzz jobs or results.`
                    : `Remove ${node.path} from the tree? This does not delete any fuzz jobs or results.`;
                if (!confirm(message)) return;
                try {
                    if (node.synthetic) {
                        await api(`${treeUrl}/by-path/subtree?path=${encodeURIComponent(node.path)}`, { method: "DELETE" });
                    } else {
                        await api(`${treeUrl}/${node.id}`, { method: "DELETE" });
                    }
                    await refresh();
                } catch (err) {
                    alert(`Could not remove: ${err.message}`);
                }
            });
        }

        const status = node.status;
        const statusCell = h("td", { class: "tree-status-cell" }, [
            status
                ? h("span", { class: statusClass(status.status), text: String(status.status) })
                : h("span", { class: "hint tree-status-empty", text: "—" }),
        ]);

        const rows = [h("tr", {}, [
            statusCell,
            sizeCell,
            actionsCell,
            pathCell,
            noteCell,
            h("td", { class: "tree-delete-cell" }, [deleteBtn]),
        ])];
        // The exact same inline "Fuzz this path" launcher Results uses --
        // not a separate combined detail drawer.
        if (fuzzLauncher.isOpen(node.path)) rows.push(buildInlineFuzzLauncherRow(6, fuzzLauncher.get(node.path)));
        if (openHistoryPath === node.path) {
            rows.push(buildFuzzHistoryRow(6, history, () => goToResultsAndReveal(projectId, targetId, node.path)));
        }
        return rows;
    }

    // An implied ancestor segment (e.g. fuzzing straight at /a/b/c, never
    // independently adding /a or /a/b) that was never itself explicitly
    // added to the tree -- id null until ensureRealNode() promotes it.
    function makeSyntheticTreeNode(path) {
        return { id: null, path, interesting: false, note: "", status: null, fuzz_history: [], synthetic: true };
    }

    function appendNodeRows(hierarchy, wrapper, depth, parentPath, prefix, isLast) {
        const children = hierarchy.childrenOf.get(wrapper.key) || [];
        // Every distinct node at this position gets its own row (2026-09-25
        // -- see buildPathHierarchy's own comment; same fix as Results',
        // for the same "/admin" + "/admin/" style case) -- only the LAST
        // one carries the expand arrow/child count.
        wrapper.items.forEach((item, i) => {
            const isLastItem = i === wrapper.items.length - 1;
            const connector = treeConnectorFor(depth, prefix, isLastItem ? isLast : false);
            for (const row of buildNodeRow(item, depth, isLastItem ? children.length : 0, parentPath, connector)) {
                body.appendChild(row);
            }
        });
        if (children.length && !collapsedParents.has(wrapper.key)) {
            const childPrefix = treeChildPrefixFor(depth, prefix, isLast);
            children.forEach((child, i) => {
                appendNodeRows(hierarchy, child, depth + 1, wrapper.path, childPrefix, i === children.length - 1);
            });
        }
    }

    function renderRows() {
        body.innerHTML = "";
        if (!nodes.length) {
            body.appendChild(h("tr", {}, [h("td", { colspan: "6", class: "empty", text: "No paths in the tree yet." })]));
            return;
        }

        // Same approach as Results: filter the flat list first, then group
        // only the visible ones -- a filtered-out parent's still-visible
        // children just show up as top-level instead of disappearing too.
        const visibleNodes = nodes.filter(passesTreeFilters);
        const hierarchy = buildPathHierarchy(visibleNodes, makeSyntheticTreeNode);

        hierarchy.topLevel.forEach((top, i) => {
            appendNodeRows(hierarchy, top, 0, null, "", i === hierarchy.topLevel.length - 1);
        });

        if (!hierarchy.topLevel.length) {
            body.appendChild(h("tr", {}, [h("td", { colspan: "6", class: "empty", text: "No paths match your filters." })]));
        }
    }

    // A textarea, not an input: grows with what's typed -- wider isn't
    // possible for a flex-filled field, so it wraps and grows downward
    // instead, keeping the whole path visible even if it's extremely long.
    // "+ Add path" stays pinned to the row's right edge regardless of height
    // (the form row uses align-items: flex-start).
    const pathInput = h("textarea", { rows: "1", class: "tree-path-input", placeholder: "/path (e.g. /api/internal)" });
    function autoGrowPathInput() {
        pathInput.style.height = "auto";
        pathInput.style.height = `${pathInput.scrollHeight}px`;
    }
    pathInput.addEventListener("input", autoGrowPathInput);
    pathInput.addEventListener("keydown", (e) => {
        if (e.key === "Enter" && !e.shiftKey) {
            e.preventDefault();
            addForm.requestSubmit();
        }
    });
    const addForm = h("form", { class: "inline-form tree-add-form" }, [
        pathInput,
        h("button", { class: "primary", type: "submit", text: "+ Add path" }),
    ]);
    addForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const path = pathInput.value.trim();
        if (!path) return;
        try {
            await api(treeUrl, { method: "POST", body: JSON.stringify({ path }) });
            pathInput.value = "";
            pathInput.style.height = "";
            await refresh();
        } catch (err) {
            alert(`Could not add path: ${err.message}`);
        }
    });

    // ---- TREE filter bar: search + a "Filters ▾" dropdown holding the
    // Interesting/Fuzzed toggles (kept out of the main row -- there are
    // only two of them, not worth a chip each taking up space permanently). ----
    const { element: treeSearchInputEl, input: treeSearchInput } = buildSearchInput({ placeholder: "Search path…" });
    treeSearchInput.addEventListener("input", () => {
        withTreeFilterScrollMemory(() => {
            treeSearchQuery = treeSearchInput.value.trim().toLowerCase();
            renderRows();
        });
    });

    const filtersToggleBtn = h("button", { type: "button", class: "filter-chip", text: "Filters ▾" });
    // Same active/count treatment as "Hide rules ▾" (Results has its own
    // copy of this same pattern) -- refreshed on each toggle below rather
    // than on a full rebuild, since these two chips update their own class
    // directly instead of re-rendering the whole filter bar.
    function refreshFiltersToggleBtn() {
        const count = (showInterestingOnly ? 1 : 0) + (showFuzzedOnlyTree ? 1 : 0);
        filtersToggleBtn.classList.toggle("active", count > 0);
        filtersToggleBtn.textContent = `Filters ▾${count ? ` (${count})` : ""}`;
    }
    refreshFiltersToggleBtn();
    const filtersPopup = h("div", { class: "filters-popup", hidden: "hidden" });
    const interestingChip = buildIconTextButton("button", STAR_FILLED_ICON_SVG, "Interesting", { type: "button", class: "filter-chip" });
    interestingChip.addEventListener("click", () => {
        withTreeFilterScrollMemory(() => {
            showInterestingOnly = !showInterestingOnly;
            interestingChip.classList.toggle("active", showInterestingOnly);
            refreshFiltersToggleBtn();
            renderRows();
        });
    });
    const fuzzedChip = h("button", { type: "button", class: "filter-chip", text: "Fuzzed" });
    fuzzedChip.addEventListener("click", () => {
        withTreeFilterScrollMemory(() => {
            showFuzzedOnlyTree = !showFuzzedOnlyTree;
            fuzzedChip.classList.toggle("active", showFuzzedOnlyTree);
            refreshFiltersToggleBtn();
            renderRows();
        });
    });
    filtersPopup.appendChild(interestingChip);
    filtersPopup.appendChild(fuzzedChip);
    filtersToggleBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        filtersPopup.hidden = !filtersPopup.hidden;
    });
    function onDocumentClickCloseTreeFilters(e) {
        if (!document.body.contains(filtersPopup)) {
            document.removeEventListener("click", onDocumentClickCloseTreeFilters);
            return;
        }
        if (!filtersPopup.hidden && !filtersPopup.contains(e.target) && e.target !== filtersToggleBtn) {
            filtersPopup.hidden = true;
        }
    }
    document.addEventListener("click", onDocumentClickCloseTreeFilters);
    const filtersDropdown = h("div", { class: "filters-dropdown" }, [filtersToggleBtn, filtersPopup]);

    const filterBar = h("div", { class: "filter-bar" }, [treeSearchInputEl, filtersDropdown]);
    // Add path + search/filters on the same row (wraps to a second line on
    // a narrow window rather than overflowing).
    const toolbar = h("div", { class: "tree-toolbar tree-toolbar-sticky" }, [addForm, filterBar]);

    const section = h("section", {}, [
        toolbar,
        h("table", { class: "tree-table" }, [
            h("thead", {}, [h("tr", {}, [
                h("th", { text: "Status" }), h("th", { text: "Size" }), h("th", { text: "" }), h("th", { text: "Path" }),
                h("th", { text: "Note" }), h("th", { text: "" }),
            ])]),
            body,
        ]),
    ]);

    await refresh();
    return { element: section, refresh };
}

// ---- New Fuzz form ------------------------------------------------------

async function fetchWordlistTreeForNewFuzz() {
    try {
        return await api("/api/wordlists");
    } catch (e) {
        return { type: "dir", name: "wordlists", children: [] }; // missing/unreadable -- picker just starts empty
    }
}
async function fetchConcurrencyForNewFuzz(projectId, targetId) {
    try {
        return (await api(`/api/projects/${projectId}/targets/${targetId}/concurrency`)).max_concurrent_jobs;
    } catch (e) {
        return 2; // fall back to the default shown below
    }
}
async function fetchFuzzDefaultsForNewFuzz(projectId, targetId) {
    try {
        return await api(`/api/projects/${projectId}/targets/${targetId}/fuzz-defaults`);
    } catch (e) {
        return {}; // starts blank -- this target's own hardcoded defaults still apply
    }
}

async function buildNewFuzzForm(projectId, targetId, onSubmitted) {
    // Parallel, not sequential -- three independent fetches (real, reported
    // flicker/lag on every tab switch this used to contribute to, see
    // renderWorkspace's own comment above its initialLoads Promise.all).
    // Whatever was last configured (never launched) for THIS target --
    // pre-fills every field below. {} (nothing saved yet, a brand new
    // target) means "use this form's own hardcoded defaults untouched";
    // any saved data is a full snapshot of every field from the last save
    // (see saveFuzzDefaults below), never a partial/merged one, so there's
    // no per-field ambiguity about what "not present" would mean. Explicit
    // request, 2026-09-23: configuring the form (without ever launching a
    // fuzz) should still be remembered, and both the main New Fuzz section
    // AND every inline "Fuzz this path" launcher from Results/TREE share
    // this same function, so fixing it here covers both for free.
    const [wordlistTree, currentConcurrency, fuzzDefaults] = await Promise.all([
        fetchWordlistTreeForNewFuzz(),
        fetchConcurrencyForNewFuzz(projectId, targetId),
        fetchFuzzDefaultsForNewFuzz(projectId, targetId),
    ]);
    const hasSavedDefaults = Object.keys(fuzzDefaults).length > 0;

    const pathInput = h("input", { placeholder: "/path/ (e.g. /api/)", value: "/" });
    const extInput = h("input", {
        placeholder: "Extensions (php,html)",
        value: hasSavedDefaults ? (fuzzDefaults.extensions || []).join(",") : "",
    });
    const threadsInput = h("input", {
        type: "number", value: String(hasSavedDefaults ? fuzzDefaults.threads || 5 : 5), min: "1", style: "width:70px",
    });
    const wordlistPicker = buildWordlistPicker(
        wordlistTree,
        hasSavedDefaults && fuzzDefaults.wordlist_files && fuzzDefaults.wordlist_files[0]
            ? fuzzDefaults.wordlist_files[0]
            : null,
    );

    // .checked set as a live DOM PROPERTY below, not via h()'s attrs (which
    // only ever ADDS the "checked" attribute -- setAttribute("checked", "")
    // has no way to represent "absent/false", so a conditional false would
    // have needed skipping the key entirely; a direct property assignment
    // afterward is simpler and unambiguous either way).
    const forceExtCheckbox = h("input", { type: "checkbox" });
    forceExtCheckbox.checked = hasSavedDefaults ? Boolean(fuzzDefaults.force_extensions) : true;
    // Purely a saved-output-filename tag -- never touches the actual
    // dirsearch invocation. See api/jobs.py::create_job.
    const authenticatedCheckbox = h("input", { type: "checkbox" });
    authenticatedCheckbox.checked = hasSavedDefaults && Boolean(fuzzDefaults.authenticated);
    const pauseStatusInput = h("input", {
        // 503 added alongside 429 (2026-09-25, explicit request) -- both are
        // a target telling the scanner to back off (429: rate limited, 503:
        // overloaded/unavailable, often the same underlying rate-limit or
        // WAF response in practice), so pausing on either by default is the
        // same "stop before hammering a struggling target" intent 429 alone
        // already had.
        value: hasSavedDefaults ? (fuzzDefaults.pause_on_status || []).join(",") : "429,503", style: "width:90px",
    });
    const headersRows = h("div", { class: "headers-rows" });
    const concurrencyInput = h("input", {
        type: "number", min: "1", value: String(currentConcurrency), style: "width:60px",
    });
    concurrencyInput.addEventListener("change", async () => {
        const value = Number(concurrencyInput.value);
        if (!value || value < 1) {
            concurrencyInput.value = String(currentConcurrency);
            return;
        }
        try {
            const result = await api(`/api/projects/${projectId}/targets/${targetId}/concurrency`, {
                method: "PUT",
                body: JSON.stringify({ max_concurrent_jobs: value }),
            });
            currentConcurrency = result.max_concurrent_jobs;
        } catch (err) {
            alert(`Could not update concurrency: ${err.message}`);
            concurrencyInput.value = String(currentConcurrency);
        }
    });

    // One combined "Name: Value" field per header, not two separate ones --
    // explicit request. Only the text BEFORE the first ":" is ever treated
    // as the name (see the submit handler below) -- a value that itself
    // contains a colon (e.g. "Bearer abc:def") never gets misparsed as a
    // second header.
    function addHeaderRow(name, value) {
        const combined = name ? `${name}: ${value || ""}` : "";
        // A textarea, not an input: a long value (a JWT, a big cookie
        // string) wraps onto new lines below instead of scrolling off to
        // the right where it can't be read. Collapsed to one line with an
        // ellipsis while not focused; expands to show every line, growing
        // downward, while focused -- collapses again on blur (clicking
        // elsewhere, or into another header row).
        const combinedInput = h("textarea", {
            class: "header-combined-input", rows: "1",
            placeholder: "Header name: Header value (e.g. Authorization: Bearer …)",
        });
        combinedInput.value = combined;

        function autoGrow() {
            combinedInput.style.height = "auto";
            combinedInput.style.height = `${combinedInput.scrollHeight}px`;
        }
        combinedInput.addEventListener("focus", () => {
            combinedInput.classList.add("expanded");
            autoGrow();
        });
        combinedInput.addEventListener("input", () => {
            if (combinedInput.classList.contains("expanded")) autoGrow();
        });
        combinedInput.addEventListener("blur", () => {
            combinedInput.classList.remove("expanded");
            combinedInput.style.height = "";
        });

        const row = h("div", { class: "header-row" }, [
            combinedInput,
            h("button", { type: "button", title: "Remove header", text: "×", onclick: () => row.remove() }),
        ]);
        row._combinedInput = combinedInput;
        headersRows.appendChild(row);
    }
    if (hasSavedDefaults) {
        for (const [name, value] of Object.entries(fuzzDefaults.headers || {})) addHeaderRow(name, value);
    }

    // A textarea, not an input: grows downward as content wraps or the
    // pentester adds more flags, instead of scrolling off to the right
    // (same auto-grow-height technique as .tree-path-input / .header-value-input).
    const customOptionsInput = h("textarea", {
        rows: "1", class: "custom-options-input", placeholder: "--max-rate 50 --timeout 10",
    });
    // .value set as a property, not via h()'s attrs -- a <textarea> has no
    // real `value` ATTRIBUTE (its rendered content comes from a child text
    // node/the `.value` property instead), so setAttribute("value", ...)
    // is silently a no-op for it. Same reasoning addHeaderRow's own
    // combinedInput.value assignment above already relies on.
    if (hasSavedDefaults) customOptionsInput.value = fuzzDefaults.extra_args || "";
    function autoGrowCustomOptions() {
        customOptionsInput.style.height = "auto";
        customOptionsInput.style.height = `${customOptionsInput.scrollHeight}px`;
    }
    customOptionsInput.addEventListener("input", autoGrowCustomOptions);

    const advanced = h("details", { class: "advanced" }, [
        h("summary", { text: "Advanced options" }),
        h("div", { class: "advanced-body" }, [
            h("div", { class: "advanced-row" }, [
                h("label", {
                    title: "For each wordlist word with no extension/trailing slash, also tests "
                        + "word/ (as a directory) and word.<ext> for every configured extension "
                        + "-- not just the bare word.",
                }, [forceExtCheckbox, document.createTextNode("Force extensions (-f)")]),
                h("label", {
                    title: "Tags this fuzz's saved output filename with \"__AUTH\" after the path "
                        + "-- purely a naming convention for telling an authenticated fuzz's report "
                        + "apart when browsing outputs/ directly; never touches the actual request.",
                }, [authenticatedCheckbox, document.createTextNode("Authenticated")]),
                h("span", { class: "hint", text: "If enabled, the saved output filename will get \"__AUTH\" appended." }),
            ]),
            h("div", { class: "advanced-row" }, [
                h("label", { text: "Auto-pause on status codes" }),
                pauseStatusInput,
                h("span", {
                    class: "hint",
                    text: "Job pauses as soon as one of these is seen (e.g. 429 = rate limited / site down). "
                        + "Resume it manually from Running/Jobs once the target is back up.",
                }),
            ]),
            h("div", { class: "advanced-row" }, [
                h("label", { text: "Concurrent fuzzes" }),
                concurrencyInput,
                h("span", {
                    class: "hint",
                    text: "Applies immediately, only to this target -- other targets keep their own limit.",
                }),
            ]),
            h("div", { class: "headers-group" }, [
                h("div", { class: "hint", text: "Extra headers (-H), one per row" }),
                headersRows,
                h("button", { type: "button", text: "+ Add header", onclick: () => addHeaderRow() }),
            ]),
            h("div", { class: "custom-options-group" }, [
                h("label", { text: "Custom options" }),
                customOptionsInput,
                h("span", {
                    class: "hint",
                    text: "Any other dirsearch flag not covered above, exactly as on the command line. "
                        + "Appended after everything else.",
                }),
            ]),
        ]),
    ]);

    // Shared between the submit handler and the target-defaults autosave
    // below -- both need the exact same "read every field's current
    // value" snapshot.
    function collectExtensions() {
        return extInput.value.split(",").map((s) => s.trim()).filter(Boolean);
    }
    function collectPauseOnStatus() {
        return pauseStatusInput.value
            .split(",").map((s) => s.trim()).filter(Boolean)
            .map(Number).filter((n) => !Number.isNaN(n));
    }
    function collectHeaders() {
        const headers = {};
        headersRows.querySelectorAll(".header-row").forEach((row) => {
            const raw = row._combinedInput.value;
            const colonIndex = raw.indexOf(":");
            if (colonIndex === -1) return; // no ":" at all -- not a valid "Name: Value" entry yet
            const name = raw.slice(0, colonIndex).trim();
            const value = raw.slice(colonIndex + 1).trim();
            if (name) headers[name] = value;
        });
        return headers;
    }

    // Autosaves the form's own current state as this target's fuzz
    // defaults -- explicit request: configuring the form (extensions,
    // wordlist, threads, advanced options, headers, custom options; NOT
    // path, which stays contextual to wherever a fuzz is actually
    // launched from) should be remembered WITHOUT ever needing to launch a
    // fuzz. Debounced so typing doesn't fire a PUT per keystroke; fields
    // that change all-at-once (checkboxes, the wordlist picker, adding/
    // removing a header) save immediately instead (see the listeners
    // below).
    const saveFuzzDefaultsNow = async () => {
        try {
            await api(`/api/projects/${projectId}/targets/${targetId}/fuzz-defaults`, {
                method: "PUT",
                body: JSON.stringify({
                    wordlist_files: wordlistPicker.getValue() ? [wordlistPicker.getValue()] : [],
                    extensions: collectExtensions(),
                    threads: Number(threadsInput.value) || 5,
                    force_extensions: forceExtCheckbox.checked,
                    authenticated: authenticatedCheckbox.checked,
                    pause_on_status: collectPauseOnStatus(),
                    headers: collectHeaders(),
                    extra_args: customOptionsInput.value.trim(),
                }),
            });
        } catch (e) { /* best-effort -- just won't be remembered this time */ }
    };
    const saveFuzzDefaultsDebounced = debounce(saveFuzzDefaultsNow, 600);
    extInput.addEventListener("input", saveFuzzDefaultsDebounced);
    threadsInput.addEventListener("input", saveFuzzDefaultsDebounced);
    pauseStatusInput.addEventListener("input", saveFuzzDefaultsDebounced);
    customOptionsInput.addEventListener("input", saveFuzzDefaultsDebounced);
    forceExtCheckbox.addEventListener("change", saveFuzzDefaultsNow);
    authenticatedCheckbox.addEventListener("change", saveFuzzDefaultsNow);
    wordlistPicker.onSelect(saveFuzzDefaultsNow);
    // Header rows come and go dynamically (+ Add header / the row's own ×)
    // -- a delegated listener on the whole group catches typing in any of
    // them (debounced) and both add/remove immediately, rather than
    // re-binding a listener to each row by hand.
    headersRows.addEventListener("input", saveFuzzDefaultsDebounced);
    headersRows.addEventListener("click", (e) => {
        if (e.target.closest("button")) saveFuzzDefaultsNow(); // the row's own "×" remove button
    });

    const form = h("form", { class: "fuzz-form" }, [
        h("div", { class: "inline-form" }, [
            pathInput,
            wordlistPicker.element,
            extInput,
            h("label", { text: "threads" }),
            threadsInput,
            buildIconTextButton("button", LIGHTNING_FILLED_ICON_SVG, "Fuzz", { class: "primary", type: "submit" }),
        ]),
        advanced,
    ]);

    form.addEventListener("submit", async (e) => {
        e.preventDefault();
        const extensions = collectExtensions();
        const pauseOnStatus = collectPauseOnStatus();
        const headers = collectHeaders();
        const wordlistPath = wordlistPicker.getValue();
        if (!wordlistPath) {
            alert("Select a wordlist first.");
            return;
        }

        try {
            await api(`/api/projects/${projectId}/targets/${targetId}/jobs`, {
                method: "POST",
                body: JSON.stringify({
                    path: pathInput.value || "/",
                    wordlist_files: [wordlistPath],
                    extensions,
                    threads: Number(threadsInput.value) || 5,
                    force_extensions: forceExtCheckbox.checked,
                    authenticated: authenticatedCheckbox.checked,
                    pause_on_status: pauseOnStatus,
                    headers,
                    extra_args: customOptionsInput.value.trim(),
                }),
            });
            await onSubmitted();
        } catch (err) {
            alert(`Could not start fuzz: ${err.message}`);
        }
    });

    return {
        element: form,
        setPath: (path) => { pathInput.value = path; pathInput.focus(); },
    };
}

// ---- Wordlist picker (searchable, nested-folder-aware dropdown) --------

function buildWordlistPicker(tree, targetDefaultPath) {
    let selectedPath = "";
    let onSelectCallback = null; // set via onSelect() below, after construction
    // Which folders are currently expanded, keyed by their own prefix
    // string (e.g. "subdir1/subdir2/") -- persists across refreshTree()
    // rebuilds (star-toggling a wordlist, selecting one, or typing a
    // search all call it). Real, reported bug this fixes: renderDir() used
    // to start every folder collapsed on every single rebuild, so
    // starring a wordlist buried in one or more folders visibly snapped
    // them all shut right as you clicked the star.
    const expandedDirs = new Set();
    // The starred "default for new fuzzes" wordlist (app-wide, like
    // lastWordlist below) -- at most one at a time; starring a different
    // one replaces it outright (overwriting the pref, not toggling a set).
    let defaultWordlist = loadPref("defaultWordlist", null);

    const btnLabel = h("span", { text: "Select wordlist…" });
    const button = h("button", { type: "button", class: "wordlist-picker-btn" }, [btnLabel]);
    const { element: searchInputEl, input: searchInput } = buildSearchInput({ placeholder: "Search wordlists…" });
    const treeContainer = h("div", { class: "wordlist-tree" });
    const popup = h("div", { class: "wordlist-popup", hidden: "hidden" }, [searchInputEl, treeContainer]);
    const root = h("div", { class: "wordlist-picker" }, [button, popup]);

    function flatten(node, prefix) {
        let out = [];
        for (const child of node.children || []) {
            if (child.type === "file") out.push({ file: child, label: prefix + child.name });
            else out = out.concat(flatten(child, prefix + child.name + "/"));
        }
        return out;
    }
    const allFiles = flatten(tree, "");

    function select(file, label) {
        selectedPath = file.path;
        btnLabel.textContent = label;
        popup.hidden = true;
        refreshTree();
        savePref("lastWordlist", { path: file.path, label });
        if (onSelectCallback) onSelectCallback();
    }

    function fileRow(file, label) {
        const isDefault = defaultWordlist && defaultWordlist.path === file.path;
        const starBtn = h("button", {
            type: "button", class: `icon-btn wl-default-star${isDefault ? " active" : ""}`,
        });
        starBtn.innerHTML = isDefault ? STAR_FILLED_ICON_SVG : STAR_OUTLINE_ICON_SVG;
        attachTooltip(
            starBtn,
            "Default wordlist for NEW targets. This target already keeps whatever you pick below as its own default, starred or not.",
            280,
        );
        starBtn.addEventListener("click", (e) => {
            e.stopPropagation(); // never also select() this row
            defaultWordlist = isDefault ? null : { path: file.path, label };
            savePref("defaultWordlist", defaultWordlist);
            refreshTree();
        });

        const row = h("div", { class: "wl-file" }, [starBtn, document.createTextNode(label)]);
        if (file.path === selectedPath) row.classList.add("selected");
        row.addEventListener("click", () => select(file, label));
        return row;
    }

    function renderDir(node, prefix) {
        const container = h("div", { class: "wl-children" });
        const entries = node.children || [];
        if (!entries.length) {
            container.appendChild(h("div", { class: "wl-empty", text: "(empty)" }));
            return container;
        }
        for (const child of entries) {
            if (child.type === "dir") {
                const dirKey = prefix + child.name + "/";
                const childBox = renderDir(child, dirKey);
                const isExpanded = expandedDirs.has(dirKey);
                childBox.hidden = !isExpanded;
                const arrow = h("span", { text: isExpanded ? "▾" : "▸" });
                const label = h("div", { class: "wl-dir-label" }, [arrow, document.createTextNode(child.name)]);
                label.addEventListener("click", () => {
                    childBox.hidden = !childBox.hidden;
                    arrow.textContent = childBox.hidden ? "▸" : "▾";
                    if (childBox.hidden) expandedDirs.delete(dirKey);
                    else expandedDirs.add(dirKey);
                });
                container.appendChild(h("div", { class: "wl-dir" }, [label, childBox]));
            } else {
                container.appendChild(fileRow(child, prefix + child.name));
            }
        }
        return container;
    }

    function renderFlat(matches) {
        const container = h("div", { class: "wl-children" });
        if (!matches.length) {
            container.appendChild(h("div", { class: "wl-empty", text: "No matches." }));
            return container;
        }
        matches.forEach((m) => container.appendChild(fileRow(m.file, m.label)));
        return container;
    }

    function refreshTree() {
        treeContainer.innerHTML = "";
        const query = searchInput.value.trim().toLowerCase();
        if (query) {
            treeContainer.appendChild(renderFlat(allFiles.filter((f) => f.label.toLowerCase().includes(query))));
        } else {
            treeContainer.appendChild(renderDir(tree, ""));
        }
    }
    searchInput.addEventListener("input", refreshTree);

    button.addEventListener("click", (e) => {
        e.stopPropagation();
        popup.hidden = !popup.hidden;
        if (!popup.hidden) searchInput.focus();
    });

    function onDocumentClick(e) {
        if (!document.body.contains(root)) {
            document.removeEventListener("click", onDocumentClick);
            return;
        }
        if (!root.contains(e.target)) popup.hidden = true;
    }
    document.addEventListener("click", onDocumentClick);

    // Priority, per explicit request: THIS TARGET's own saved default
    // (set via New Fuzz's autosaved options, 2026-09-23 -- see
    // buildNewFuzzForm) first, then the starred "default" wordlist
    // (app-wide, any project/target), then the last one actually picked
    // (also app-wide), then the hardcoded fallback -- each only if that
    // exact file still exists in this tree.
    const targetDefaultFile = targetDefaultPath && allFiles.find((f) => f.file.path === targetDefaultPath);
    const starredFile = defaultWordlist && allFiles.find((f) => f.file.path === defaultWordlist.path);
    const remembered = loadPref("lastWordlist", null);
    const rememberedFile = remembered && allFiles.find((f) => f.file.path === remembered.path);
    // Last resort: the bundled example wordlist if present, else simply the
    // first wordlist in the tree, so a fresh install never starts empty.
    const bundledFallback = allFiles.find((f) => f.label === "common_big.txt")
        || allFiles.find((f) => f.label === "test_wordlist.txt")
        || allFiles[0];
    const initialFile = targetDefaultFile || starredFile || rememberedFile || bundledFallback;
    if (initialFile) {
        selectedPath = initialFile.file.path;
        btnLabel.textContent = targetDefaultFile ? targetDefaultFile.label
            : starredFile ? defaultWordlist.label
            : rememberedFile ? remembered.label
            : initialFile.label;
    }
    refreshTree();

    return {
        element: root,
        getValue: () => selectedPath,
        onSelect: (callback) => { onSelectCallback = callback; },
    };
}
