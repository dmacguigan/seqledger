# Dismiss-issues UX review (2026-10-20)

Reviewer: UX persona (Opus). Read-only. Probe confirmed two note bugs on Streamlit 1.59.

**Confirmed bugs**
- Stale note: Projects, typed `NOTE-A`, clicked `Dismiss selected (1)`, box still held `NOTE-A`; `Dismiss all (2)` then wrote `NOTE-A` onto 2 more log rows.
- Note leaks between items: Files, typed a note with s2_R1 selected, selected s2_R2, box still showed it, `Dismiss md5 mismatch` logged it against R2.

## A. Walkthrough (first-time user)

**Sidebar.** `Show dismissed` checkbox sits last under "Filters", no help text. Sounds like a filter, not the switch that also reveals Undo.

**Projects.** Caption never mentions dismissing; asterisk legend is the 5th sentence. Select a project: warning box, bare `Note`, `Dismiss mapfile issue`. Below: issues table, download, a second `Note`, `Dismiss selected (0)` (disabled, no tooltip), `Dismiss all (N)`, and (toggle on) `Undo selected`. User must guess which Note goes with which button, whether a note is required, who sees it, that the table has tick boxes, why Dismiss selected is grey, what "all" covers. After clicking, rows vanish silently. "N issue(s) dismissed" info only shows when every issue is dismissed. Dismissed mapfile shows "OK * (dismissed by ...)" with no Undo unless toggle on.

**Samples.** Caption does not mention dismissing. `Note` + `Dismiss flags for this sample` appear with no heading, button does not name the flags. After dismissing, cell becomes `OK *`, controls vanish, no message, Undo hidden behind toggle.

**Files.** Same. One shared `Note`, one button per problem; cell says `gzip_error`/`Mismatch`, buttons say `gzip error`/`md5 mismatch`. Note stays for the second problem on the same file.

## B. Findings

- `st.text_input("Note")` x4: high: label says nothing about purpose, optionality, visibility, or batch scope. Relabel + placeholder + help (C3).
- `note_issues`: high: note survives dismiss, rewritten on next Dismiss selected / Dismiss all (confirmed). Clear in on_click callback (C1).
- `note_file`: high: note stays for next kind on same file and leaks to next selected file (confirmed). C1 + per-item keys (C2).
- `note_flags`, `note_mapfile`, `note_issues`: high: fixed keys survive selection change, note for A logged against B. Per-item keys (C2).
- Projects: med: two boxes both labelled `Note` on one page. Distinct labels (C3).
- Captions x3: med: none say selecting a row is how to dismiss (C9).
- Dismiss controls: med: float with no heading, do not name their target. Bordered container + heading (C4).
- `Dismiss selected (0)`: low: disabled with no explanation. `help="Tick rows in the table above first."`
- After dismiss: high: no feedback; with a sidebar filter on, the whole project/file vanishes and detail pane closes, looks like a crash. Toast (C1).
- "N issue(s) dismissed" info: med: only when all dismissed. Always show hidden count (C8).
- Undo: high: only renders with toggle on; mapfile info shows "dismissed by" with no way to undo. Always show status + Restore for the selected single item (C6).
- "Undo" wording: med: implies last action; really restores a specific, possibly old, dismissal. Rename "Restore" (C5).
- Label consistency: low: `OK *`, `ok *`, `Mapfile: OK *`, `<live> *`, `(dismissed)`, `(n dismissed)`; four button verb patterns. One pattern "Dismiss <thing>" / "Restore <thing>" (C5).
- Asterisk convention: med: `OK *` reads healthy for a corrupt file; same mark overloaded for partly dismissed; Grab & Go copies as `ok *`. Optional C11, minimum: legend in checkbox help.
- `Show dismissed` checkbox: med: last under Filters, no help, doubles as edit mode, state shared across views. Relabel + help + divider (C7).
- `Dismiss all (N)`: high: one click hides every live issue for everyone, equal weight to Dismiss selected, no confirmation, reuses stale note, 4-step restore. Confirmation popover (C10).
- Undo records no reason: low: optional, pass same box.
- `if st.button(...) and _record(...): st.rerun()`: low: Streamlit drops the click if label changes between render and click (count changes). Callbacks fix this (C1).

## C. Proposed redesign (ranked, smallest first)

**C1 (S). One callback for all record actions: clears note, toasts, no st.rerun().**
```python
def _act(log_path, action, keys, note_key=None, msg=""):
    note = st.session_state.get(note_key, "") if note_key else ""
    if _record(log_path, action, keys, note):
        if note_key:
            st.session_state[note_key] = ""
        st.toast(msg)
```
Buttons: `st.button(label, on_click=_act, args=(log_path, "dismiss", keys(live_sel), nkey, msg))`. Keys captured at render time, so it dismisses exactly what the user saw.
Toasts: dismiss `f"Dismissed {n} issue(s). To undo, select it again and press Restore."`; Projects table `f"Dismissed {n} issue(s). To restore, tick 'Show dismissed issues' in the sidebar."`; restore `f"Restored {n} issue(s)."`

**C2 (S). Per-item note keys:** `f"note_mapfile_{pid}"`, `f"note_issues_{pid}"`, `f"note_flags_{pid}_{sid}"`, `f"note_file_{rel_path}"`. Widget state drops when the widget stops rendering, so each item starts empty. Tests using `key="note_mapfile"` need updating.

**C3 (S). Note wording.**
- Labels: Projects mapfile `"Reason for dismissing the mapfile issue (optional)"`; Projects table `"Reason for dismissing the ticked issues (optional)"`; Samples `"Reason for dismissing these flags (optional)"`; Files `"Reason for dismissing (optional)"`.
- `placeholder="e.g. known truncated delivery; re-sequenced as P12"`
- `help="Saved with your username in the shared dismissal log. Everyone sees it when 'Show dismissed issues' is on."`

**C4 (S). Group controls** in `st.container(border=True)` with heading: `**Dismiss this mapfile issue**`, `**Dismiss known issues**`, `f"**Dismiss flags for {sid}: {flags}**"`, `f"**Dismiss problems with {filename}**"`. Caption: `"Dismissing hides a known issue from everyone. Nothing in the catalog changes, and it can be restored."`

**C5 (S). Button labels.** Dismiss: `Dismiss mapfile issue`, `Dismiss flags`, `f"Dismiss {kind}"`, `f"Dismiss {n} ticked issue(s)"` (help "Tick rows in the table above first."). Restore: `Restore mapfile issue`, `Restore flags`, `f"Restore {kind}"`, `f"Restore {n} ticked issue(s)"`.

**C6 (S). Status + Restore always visible for the selected single item**, regardless of sidebar toggle: `st.info(f"Dismissed by {user} on {date}" + (f": {note}" if note else ""))` plus Restore button. Drop "OK *" from the mapfile line.

**C7 (S). Sidebar toggle:** `st.checkbox("Show dismissed issues", key="show_dismissed", help="Show issues someone dismissed, with who, when, and why, and the buttons to restore them. A cell ending in * has a dismissed issue.")`, after an `st.divider()`.

**C8 (S). Hidden-count notice in Projects:** when toggle off and some dismissed, `st.caption(f"{n_dis} dismissed issue(s) hidden. Tick 'Show dismissed issues' in the sidebar to see or restore them.")`.

**C9 (S). Captions:** Samples `"Select a row to list its files below, or to dismiss its flags."`; Projects `"Select a row for its mapfile explanation and data-files & integrity issues below, where known issues can be dismissed."`; Files `"Select a row to show its sample info below, or to dismiss its problems."`

**C10 (S). Guard on Dismiss all:**
```python
with st.popover(f"Dismiss all {n} issues...", disabled=live.empty):
    st.write(f"Hide all {n} issues in {pid} from everyone? Grab & Go will copy "
             "these files as if they were OK.")
    st.button("Yes, dismiss all", type="primary", on_click=_act,
              args=(log_path, "dismiss", keys(live), f"note_issues_{pid}",
                    f"Dismissed {n} issue(s)."))
```

**C11 (M, optional). Retire `OK *`.** Fully dismissed cell reads `dismissed`; partly dismissed `<live> (+n dismissed)`; Grab & Go `ok (n dismissed)`. Touches `_rollup`, `_project_status`, `_mark_files`, Samples loop, "Only mapfile issues" filter, docs, tests. Only if C7's legend is not enough.

C1, C2, C3, C6, C10 together fix all three complaints. Rest is cheap polish.

## D. Harmful surprises in current code

1. Wrong note in the permanent log (confirmed): stale note written onto later dismissals incl. Dismiss all; note for one item logged against the next selected. Append-only, so it stays.
2. One-click mass hide of real data loss: Dismiss all hides missing/corrupt/empty/mismatch for everyone, no confirm; Grab & Go then copies them as `ok *`.
3. `OK *` hides bad data in plain sight; CSV export keeps raw `gzip_error`/`Mismatch`, so screen and CSV disagree.
4. Items vanish after dismiss when a filter is active; detail pane closes; no message.
5. No undo path in sight for a just-dismissed item.
6. Silent no-op clicks when a button's count label changes between render and click.
7. Selection reset hitting wrong rows: not a risk now (issues table key hashes row identities). Keep that property; never key the table on pid alone.
8. Low: last writer wins on a repeated dismiss; undo records no reason.
