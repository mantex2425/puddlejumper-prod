#!/usr/bin/env python3
"""
migrate_scenarios.py — One-time migration of S-scenarios from a TSV
(copy-pasted from Google Sheets) into per-scenario TOML files.

Patched from Gemini's draft 2026-04-26 to address:
  - Issue 1: unescaped quotes in TOML string fields (now uses triple-quote
    form which tolerates embedded " characters)
  - Issue 3: relative output path was fragile; now derived from script
    location so the script works from any CWD

Usage:
    1. Place TSV input at ~/puddlejumper-prod/scenarios_raw.tsv
       (paste the Google Sheet rows, including header, into that file)
    2. python3 ~/puddlejumper-prod/migrate_scenarios.py
    3. Verify with: ls -la ~/puddlejumper-prod/scenarios/

The script is idempotent: re-running overwrites existing TOML files.
Safe to re-run after editing scenarios_raw.tsv.
"""
import csv
import sys
from pathlib import Path

# --- CONFIGURATION ---
# Anchor paths to the script location, not the CWD. This makes the script
# safe to invoke from any directory.
SCRIPT_DIR = Path(__file__).resolve().parent
INPUT_FILE = SCRIPT_DIR / "scenarios_raw.tsv"
OUTPUT_DIR = SCRIPT_DIR / "scenarios"


def toml_str(value: str | None) -> str:
    """
    Encode a string as a TOML triple-quoted literal.

    Triple-quoting tolerates embedded single double-quotes (very common in
    scenario descriptions, e.g. "Driver's GPS"), so we don't have to chase
    quote-escaping edge cases. We escape backslashes (rare) and the
    extremely-rare triple-double-quote sequence.
    """
    if value is None:
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    return f'"""{escaped}"""'


def to_bool(value: str) -> bool:
    """Parse a TSV cell as a boolean. TRUE/FALSE, case-insensitive."""
    return (value or "").strip().upper() == "TRUE"


def migrate() -> int:
    if not INPUT_FILE.exists():
        print(f"Error: {INPUT_FILE} not found.")
        print("Paste your Google Sheet rows (with header) into that file first.")
        return 1

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"Output directory: {OUTPUT_DIR}")

    written = 0
    skipped = 0

    with open(INPUT_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f, delimiter="\t")

        for row in reader:
            s_id = (row.get("Scenario ID") or "").strip()

            # Skip blank rows and comments
            if not s_id or s_id.startswith("#"):
                skipped += 1
                continue

            pot_cancel = to_bool(row.get("Expected potential_cancellation", ""))

            # Build TOML content. All string fields go through toml_str()
            # to handle embedded quotes safely.
            #
            # Note: the "Expected Arc Center" column is renamed to
            # "anchor_coord" on the way in. The arc-banding semantic is
            # deprecated; the column's content (which target coord the
            # system pivots on) survives under a new name.
            toml_content = f"""id = {toml_str(s_id)}
description = {toml_str(row.get("Description", ""))}
status = {toml_str((row.get("Backtest Result") or "PENDING").strip())}
notes = {toml_str(row.get("Notes", ""))}

[start]
state = {toml_str(row.get("Starting State", ""))}

[trigger]
event = {toml_str(row.get("Trigger", ""))}
verdict = {toml_str(row.get("Verdict", ""))}

[expected]
state = {toml_str(row.get("Expected State", ""))}
potential_cancellation = {str(pot_cancel).lower()}
anchor_coord = {toml_str(row.get("Expected Arc Center", ""))}
offer_status = {toml_str(row.get("Expected offer_status", ""))}
"""

            file_path = OUTPUT_DIR / f"{s_id}.toml"
            file_path.write_text(toml_content, encoding="utf-8")
            written += 1

    print(f"Wrote {written} scenarios. Skipped {skipped} blank/comment rows.")
    return 0


if __name__ == "__main__":
    sys.exit(migrate())
