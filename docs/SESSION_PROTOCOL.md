# PuddleJumper Session Protocol

**Status:** how Claude and Andrew work together. Edits as workflow evolves.
**Load:** at the start of every chat session, alongside `CANONICAL_RULES.md` and `INDEX.md`.

---

## Paired Programming Cycle

Claude proposes → Gemini reviews (sometimes Grok) → consensus → Claude provides CLI execution instructions → Andrew runs → commit + push immediately, never bundle.

Claude does not write design docs ahead of code. Claude does not invent architectural decisions without ratification. When a design choice needs to be made, Claude proposes it and waits for Gemini ratification before implementation.

---

## Claude Behavior Rules

These rules supplement the user preferences and define how Claude operates throughout each session.

### CLI and psql only

Andrew copies commands into his terminal on the `puddle-jumper` VM. **Never propose Python REPL sessions or "just run this locally."** Use `python3 <<'PYEOF' ... PYEOF` heredocs when Python is needed — they run in one shot from the CLI.

### Push back when direction is wrong

**Do not just comply.** When Andrew's instinct or instruction conflicts with what Claude believes is correct, Claude pushes back with reasoning. Challenge "while we're here" impulses that scope-creep work. Honor deliberate decisions even when they break Claude's own pattern. Andrew's prior pushback ("stop the documentation horror") corrected a real waterfall pattern Claude was perpetuating — Claude should have caught it first.

### No verbose preambles

Answer directly. Explain choices only when asked or when the choice has tradeoffs worth surfacing. No "Great question!" / "Let me think about this..." / "Here's what I'm going to do..." setup before the actual answer.

### Python heredocs, never sed for code edits

`sed` eats special characters and makes surgical edits unreviewable. For code changes, use Python `str_replace` patterns or full-file rewrites with anchor verification. Reserve `sed` for read-only output transformations (`sed -n '120,140p'` to view a range).

---

## Output Formatting

- **SQL:** wrap in `psql` for CLI execution. Andrew runs from his terminal.
- **Long output:** send to `/tmp/<descriptor>.txt` then `cat`. Avoids heredoc size issues when copying to chat.
- **Apply scripts >200 lines:** L-3 envelope (Phase 1 verify + idempotency / Phase 2 in-memory transform + per-patch delta gate / Phase 3 atomic disk write + read-back + sentinel sweep).
- **File transfers:** `scp` from `/mnt/user-data/outputs/` to VM. Never raw paste of multi-line content.

---

## Paste Safety (Critical)

**NEVER paste multi-line markdown content to bash terminal.** Two failure modes hit the codebase 2026-04-27/28:

1. **`> ` blockquote prefix** is interpreted by bash as redirect operator. Pasting a markdown blockquote can truncate files to zero bytes. (`PHASE_E_PROGRESS.md` was lost this way mid-session and recovered via `git checkout HEAD --`.)

2. **Bare lines like "Floor", "Single", "1."** are interpreted by bash as commands and create empty files with those names in the working directory. (~12 garbage files cleaned up via targeted `rm` after a forensic doc paste.)

3. **Markdown link rendering corrupts displayed filenames.** When chat UI renders text containing `filename.md`, `module.py`, or similar, it linkifies them visually as `[filename.md](http://filename.md)`. Copying the rendered text pastes the link syntax literally — bash sees `[`, `]`, `(`, `)`, `:` as metacharacters. Filenames "work" (no syntax error) but land at wrong paths on disk. (`HANDOFF_REGRESSION_RESOLVED_2026-05-05.md` corruption episode, 2026-05-05.)

**Mitigation:** copy commands from inside triple-backtick code fences (rendering disabled). After any file create/rename, verify with `ls` on the parent directory. If a command's error message doesn't quite match the corruption pattern observed, suspect the chat-display layer rendering pasted output back as markdown — request a screenshot of the actual terminal before adding more defensive shell tactics.

**Mandatory transfer pattern:**

- For new files: `scp` from `/mnt/user-data/outputs/` to VM
- For inline content via heredoc: use a unique sentinel verifiably absent from content (e.g., `MSG_EOF`, `PROBE_EOF`)
- For terminal output: pipe to `> /tmp/output.txt` then `cat /tmp/output.txt`

**Never recommend pasting markdown blockquote content (lines starting with `> `) directly into bash.**

---

## Pre-Modification Discipline

Before Claude proposes any code change to existing files:

1. **Read the current code.** Don't assume what's there. `cat` it, `sed` a range, `grep` for the relevant function.
2. **Read the relevant design document** if one exists in INDEX.md.
3. **Confirm understanding** with Andrew before authoring. "I'm about to change X, here's what I think it currently does, am I right?"

This prevents the "write over the top of existing functionality with assumptions" failure mode.

---

## Document-Driven Memory

Andrew cannot hold the full architecture in his head. Claude cannot remember decisions across sessions.

**The bridge:** INDEX.md.

- INDEX.md is pasted at the start of every chat session as the manifest of all decision documents.
- When Claude needs context Claude doesn't have, Claude asks Andrew to `cat` the relevant document from the VM.
- Claude never guesses at past architectural decisions. Claude either knows it from the loaded docs, or asks for the source.

When a new long-term decision is locked:

1. Build the thing (code, tests, drives)
2. Document the outcome in one focused markdown file
3. Update INDEX.md to point to it
4. Commit both
5. Next session: paste INDEX.md, Claude knows what exists

**No design documents before code ships.** Documents capture outcomes, not plans.

---

## Anti-Waterfall Discipline

Phase E sub-steps 1a-1c shipped via heavy ratification protocol because they were architectural amendments to data contracts. That protocol was correct for that work.

**That protocol is wrong for activation work.** Wiring WAI into production, building log tables, iterating against real data — these don't need design docs, sub-step closeout refreshes, or forensic write-ups for every finding.

The active discipline through launch:

- **If it isn't code or a test to prove the code, it doesn't get written.**
- **Bugs get a commit message.** Findings get one paragraph in `sprint_plan.md` if they affect future sprints.
- **No multi-hundred-line forensic docs.** The Houston shift forensic and the houston_ways audit (commits `773aa49`, `48297bf`) are exceptions because they captured field data that tests can't generate.
- **L-11 doc-currency gate is lightweight.** Run pytest + git status at session-open. Skip the full progress-doc currency check.

---

## Lessons Reference (L-N)

These are the active discipline gates from Phase D and Phase E:

| Gate | What it enforces |
|------|------------------|
| L-2  | Predict-then-verify on every gate (line counts, byte counts, sentinels) |
| L-3  | Anchor-based patch script with Phase 1/2/3 envelope |
| L-5  | Trailing-newline guard on file writes |
| L-6  | Read production artifacts verbatim before authoring |
| L-6 corollary | Inventory ALL invocation sites of any modified method signature |
| L-6 corollary extension SECOND STRIKE | `grep -rn "<symbol>" --include="*.py"` against entire repo before any rename or signature change. **CHECKLIST item, not a soft "should."** |
| L-7  | Cross-check architectural rulings before proposing |
| L-9  | Every fixture declares provenance in its docstring |
| L-9 corollary refinement | REPL probes for boundary fixtures must converge to `< 1e-10`. Default 80 iterations |
| L-10 | Gate threshold provenance categorized (cat-1 production-data-grounded / cat-2 theoretical-with-shadow-mode / cat-3 paranoia, NOT ALLOWED) |
| L-11 | Doc-currency check at session-open and session-close (lightweight version through launch) |
| L-19 | Priming `current_offer_id` without a fresh `offer_history` row creates inconsistent state the matcher reads as empty queue. Use `/api/v1/test/seed_offer` or equivalent that writes both tables atomically |
| L-20 | Before bisecting code on "X broke after deploy Y", run the existing integration harness (Bruno) against deploy Y. A 60-second pass eliminates a 5-hour bisect; correlation with deploy timing is not causation |
| L-21 | Forensic columns in `pudo_decision_context` are diagnostic legend. Pattern of which columns populate vs NULL localizes failures to subsystems within minutes (Phase 1B restoration validated 2026-05-05) |
| L-22 | Chat-rendered output is not ground truth for terminal state. When persistent character corruption survives multiple defensive workarounds AND tool errors don't quite match the apparent corruption, suspect the display layer — request a screenshot before more shell tactics. See Paste Safety hazard #3 |

L-8 (live-PG smoke) reactivates after launch.

---

## Infrastructure Reference

- VM: `andrew@puddle-jumper` via IAP tunnel
- DB: `psql -h 10.128.0.2 -U postgres -d puddlejumper`
- Deploy: `bash deploy.sh` from `~/puddlejumper-prod/`
- Cloud Run region: `us-central1`
- Driver ID: `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`
- Market ID: `6a35d28b-8e6c-4d60-94aa-2661e2650863`

---

## Notes

- This document evolves. When a new protocol rule is identified (like the paste-safety rules from 2026-04-27/28), it gets added here.
- This document is **not** a place for architectural decisions. Those go in `CANONICAL_RULES.md` (eternal) or specific decision docs (referenced in `INDEX.md`).

# Note to future Claude — using `{ ... } > /tmp/file && cat /tmp/file` for recon

## The pattern

Throughout Phase 2c recon, I used this repeatedly to gather diagnostic info from Andrew's VM in one round-trip:

```bash
{
  echo "=== section 1 ==="
  grep -n "pattern" file.py
  echo ""
  echo "=== section 2 ==="
  sed -n '120,140p' file.py
} > /tmp/recon_step3.txt
cat /tmp/recon_step3.txt
```

It worked well. Andrew runs one command, pastes one block of output back, I get structured multi-section recon in a single turn. Saves 5-10 round trips per recon pass. Use this freely — it scales the value of each Andrew↔Claude exchange.

## Why the curly-brace group

`{ cmd1; cmd2; cmd3; } > file` redirects ALL stdout from the entire group to one file. The alternative — `cmd1 > file && cmd2 >> file && cmd3 >> file` — works but is ugly, error-prone, and one mistake (single `>` instead of `>>`) loses prior output. The brace group is cleaner and atomic from a redirection standpoint.

The trailing `&& cat /tmp/recon_step3.txt` runs the cat only if the redirected group succeeded. If any command in the group fails hard (set -e behavior), cat doesn't run on broken output. Most of the time this is paranoia — `grep` returning no matches still exits cleanly enough — but the `&&` is cheap insurance.

`/tmp/recon_step*.txt` as the path: ephemeral, no commit risk, no clutter in the repo. **Important:** this is `/tmp/` (Linux ephemeral) — different from `~/puddlejumper-prod/tmp/` (the project apply-script subdir per the recent_updates rule in memory). The recon files belong in `/tmp/`; apply scripts belong in `~/puddlejumper-prod/tmp/`.

## How to structure the recon block

Three rules I followed without thinking about it but should be explicit:

1. **Echo a section header before each command** — `echo "=== imports block ==="` before the actual `grep`/`sed`. When the output comes back as one wall of text, headers are the only way to find which section answered which question.
2. **Empty `echo ""` between sections** for readability when Andrew pastes it back to me.
3. **Pipe to `head -N` or `tail -N`** on each grep/find call. A `grep -rn pattern --include="*.py"` against a large repo can return thousands of lines. The recon goal is structural understanding, not exhaustive listing — first 20-50 hits per query is plenty.

## The L-22 paste-safety issue

L-22 says: chat-rendered output is not ground truth for terminal state. When a chat UI renders text containing things like `filename.md`, `module.py`, or URLs, it linkifies them visually as `[filename.md](http://filename.md)` — and when Andrew copies that rendered output back to paste into the next message, the link syntax can leak into the paste.

**This bit me at least twice in this conversation.** Andrew's pasted output had `[fix.py](http://fix.py)` mixed into the script body, `python3 -m pytest tests/test_bead_on_[wire.py](http://wire.py)` in the command line, and `bead_on_[wire.py](http://wire.py)` in the file path. Each time, the actual terminal state was fine — only the chat-display-then-paste-back round trip mangled it.

### How `{ ... } > /tmp/file && cat` interacts with L-22

The `cat` step is what triggers L-22 most often. When Andrew runs `cat /tmp/recon_step3.txt`, the chat client receives the file contents, renders them with linkification, and that rendered version is what Andrew sees. If he later copies that to paste back into chat, the linkification can stick to the copy.

**The mitigation isn't to avoid the pattern.** The pattern is genuinely useful and the alternative (5-10 individual commands) is worse. The mitigations are:

1. **Don't include the recon output in path-like strings I'll need to manipulate later.** If a recon step produces a list of filenames like `bead_on_wire.py`, I should not also paste those filenames into command-line examples in the same response — Andrew might copy from either spot, and one of them will be linkified. Keep filenames in code blocks (triple backticks) where rendering is suppressed, and keep recon output to plain text I just need to read.
    
2. **When Andrew pastes back, treat anything that looks like `[name.py](http://name.py)` as a chat-display artifact, not a real terminal token.** Read past it. Do not try to use the corruption as a clue for "what is on disk" — it's purely a display layer issue.
    
3. **For the apply scripts themselves** (the substantive output): always deliver via `create_file` → `present_files` → `scp`. Never paste multi-line script content in chat for Andrew to copy. The recon pattern is OK because the output is short and read-only; the apply scripts are larger and execution-critical, so they get the file-handoff treatment.
    
4. **When sanity-checking my own reasoning against pasted recon output**, the test is: does the meaningful content of the paste tell me what I need? The fact that filenames render with brackets and URLs is irrelevant noise. If I get distracted by the brackets and start trying to debug them, I'm reading the display layer instead of the data.
    
5. **One specific anti-pattern to avoid:** running `tee /tmp/file` AND `cat /tmp/file` in the same command. That doubles the output (Andrew sees the live tee output AND the cat playback) and makes it look like the script ran twice. Pick one — `>` plus `cat` for redirected-then-displayed, OR just `tee` for live-and-saved. Andrew hit this once in the audit script and I had to flag the duplication. Stick to the `{ ... } > /tmp/file && cat /tmp/file` pattern; don't mix in tee.
    

## When to use it vs. when not to

**Use the recon-block pattern when:**

- Multiple related questions answered by short, read-only commands (grep/sed/wc/ls)
- You need structural understanding before authoring code
- The total output is bounded (under ~300 lines paste-back)
- You'd otherwise be doing 3+ separate commands in sequence

**Don't use it when:**

- The output will be huge (use `head -N` to bound, or split across multiple chats)
- One of the commands MIGHT mutate state — keep mutating commands separate so Andrew can review before running each
- The commands need different working directories — run them separately so Andrew sees each prompt
- A single command would suffice — don't pad with ceremony for a one-line query

## TL;DR for future self

`{ ... } > /tmp/recon_stepN.txt && cat /tmp/recon_stepN.txt` is the right shape for multi-question recon. Use section headers liberally, bound output with `head`/`tail`, keep recon files in `/tmp/` not the project tmp dir. The L-22 paste-back corruption is real but unavoidable at the chat UI layer — read past `[name.py](http://name.py)` artifacts and trust the actual content. Always deliver substantial code via `create_file`/`scp`, never via chat-paste. Don't combine `tee` and `cat` in the same command.