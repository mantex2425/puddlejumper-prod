# Server-Side Sprint 1 — Empirical Evidence Base (2026-05-09 evening)

**Empirical evidence base from 2026-05-09 evening session (paste this into context):**

Before opening Server-Side Sprint 1 work, the new chat should know what was actually validated on real hardware with real Uber offers. Two test sessions, both on Andrew's device (driver `UjT1hE9eBXh2q95aSZYOkzDJ8lo1`), all OCR'd from real Uber offer cards — not synthetic, not simulated.

**Test 1 (~22:39 CDT) — Sprint 1 wiring validation, also surfaced burst-shot bug:**

Two `Offer minted:` log lines from one Uber offer card ($13.47):
- `22:39:50.599` — `019e0ff8-49c7-7b95-bd55-ed11b5d62576`
- `22:39:53.446` — `019e0ff8-54e5-717f-a324-77ca578d92fd`

Both UUIDs are RFC 9562 §5.7 compliant (third group starts with `7`, version 7 confirmed). 8-hex prefix `019e0ff8` shared = same millisecond range. Position 9 differs by exactly 2.847s wall-clock delta — proves time-ordering property end-to-end on real hardware.

Initial hypothesis: Uber re-presented the offer (Andrew didn't accept first time). Forensic instrumentation deployed to disambiguate. Result: **H1 confirmed (burst-shot duplication), not re-presentation.** Six accessibility captures from one continuous on-screen presentation across 2.97 seconds, two of which parsed successfully and minted UUIDs. This is the bug Sprint 2 (Sprint 1.5 in the design doc's renumbering) fixed via session-anchored client-side dedup.

**Test 2 (~23:04 CDT) — Sprint 2 dedup validation across 4 distinct presentations:**

| Presentation | enterOfferMode timestamp (sessionId) | UUID minted | Fare | Outcome |
|---|---|---|---|---|
| A (control) | n/a — YOLO 0 elements all 4 shots | (no mint) | n/a | Dedup didn't over-fire on negative case ✅ |
| B | 1778385880978 | `019e100f-0c25-7c84-84e9-51ed3be48a72` | $30.16 | 1 mint, 3 dedup'd ✅ |
| C | 1778385899096 | `019e100f-567b-7cfb-a439-15a1800c0357` | $22.45 | 1 mint, distinct from B ✅ |
| D | 1778385930788 | `019e100f-d0c8-7c81-89e1-01ff2eb23ffc` | $18.63 | 1 mint, distinct from B and C ✅ |

3 distinct sessionIds → 3 distinct UUIDs. Dedup invariant holds: one Uber on-screen presentation = one UUID, regardless of how many burst-shot frames the accessibility service generates. Distinct presentations correctly mint distinct UUIDs even when separated by only ~19 seconds.

**What this means for Server-Side Sprint 1:**

The producer side is verified end-to-end on real production OCR data. The server can trust the contract — Android sends exactly one `offerId` per Uber offer presentation, formatted as RFC 9562 §5.7 UUIDv7. **However, that producer-side trust does NOT eliminate the need for server-side format validation per the brief's Required Changes section** — the X3 doc's lesson was that consumer-side verification is its own discipline regardless of producer-side guarantees. Server validates format on receipt and returns 400 on invalid; that's defense-in-depth, not redundancy.

**The forward-compat verification gate** (5 real offers post-deploy, each producing populated `decision_log.trace_data->>'offer_id'` matching device logcat) builds on this evidence base. Sprint 1's success is when the server side starts persisting these UUIDs into `trace_data` and they round-trip through 5 fresh real-world drives. That closes the producer→consumer loop and unblocks Server-Side Sprint 2 (the schema migration to canonical UUID identity).

**Reference UUIDs from above are fair game as test fixtures for the validation unit tests.** They're real, verified-compliant, RFC 9562 §5.7 v7 UUIDs that round-trip through Python's `uuid.UUID().version == 7` check.
