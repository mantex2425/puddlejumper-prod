-- migrations/2026-06-07_event_ledger_phase1.sql
-- Event-Ledger — greenfield Phase 1 (per docs/DESIGN_EVENT_LEDGER_2026-06-07.md +
-- docs/DESIGN_EVENT_LEDGER_DELTA_2026-06-07.md; the DELTA wins where they conflict).
--
-- APPLY AS: psql -h 10.128.0.2 -U postgres -d puddlejumper -f <thisfile>
--   MUST be applied as `postgres` (PG 16.11). The table is OWNED BY postgres; the
--   keystone is that the runtime role `atjb` (rolsuper=f, rolbypassrls=f) is a
--   non-owner with ZERO privileges until granted INSERT/SELECT — append-only is
--   therefore enforced by GRANT-default-deny, NOT by REVOKE-from-owner (a REVOKE on
--   an owner is a no-op; that is the false-enforcement version we rejected).
--
-- Idempotent throughout (CREATE TABLE IF NOT EXISTS / CREATE OR REPLACE / pg_class
-- existence checks / REVOKE-then-GRANT / ADD COLUMN IF NOT EXISTS). Safe to re-run.
--
-- Five traps honored (see the delta): composite PK (partition key in the PK),
-- GRANT-default-deny keystone, parent-level indexes only (PG16 propagates to
-- children), full-forward-window rebuild in the maintenance fn (self-heals outages),
-- NO default partition. format(): %I for partition names (create AND drop —
-- correct-by-construction; the generated name is a legal bare identifier so %s would
-- also work, but %I is the rule), %L for the range-bound literals.
--
-- Blocks 1–5 run inside one BEGIN/COMMIT. Block 6 (verification) runs AFTER commit.
-- ============================================================================

BEGIN;

-- ===== Block 1: parent partitioned table + parent indexes ====================
CREATE TABLE IF NOT EXISTS app_private.event_ledger (
    id               bigint      GENERATED ALWAYS AS IDENTITY,
    driver_id        text        NOT NULL,
    event_time       timestamptz NOT NULL DEFAULT now(),   -- §II: UTC; never naive
    event_type       text        NOT NULL,
    offer_id         text,                                  -- identity bridge; verdict-blind (§XV)
    cluster_id       text,
    arrest_id        text,
    lat              double precision,                      -- §I: raw (lat,lng); H3 derived at read
    lng              double precision,                      --      via coords_to_h3 — never stored as geometry
    gps_accuracy_m   double precision,
    cumulative_miles double precision,
    queue_snapshot   jsonb,   -- KEYFRAME: full [{offer_id,status,in_band,picked_up}] + bound id
    queue_delta      jsonb,   -- CHANGE:   {entered:[id...], left:[{offer_id, reason}]}
                              --   reason ∈ terminated|causality|ceiling|staleness|band_overshot|abandoned
                              --   (band_overshot carries a leg qualifier in the element; six reasons, not four)
    matcher_snapshot jsonb,   -- {top_candidate_offer_id, confidence_tier}  (tier edge = the 0.40 floor)
    payload          jsonb,   -- heterogeneous per type (signals/scores/reason; raw+snapped pos)
    summary          text,    -- human-readable timeline line
    -- Range-partitioned tables require the partition key in every PK/unique
    -- constraint, so the PK is composite (id alone is rejected at CREATE):
    PRIMARY KEY (id, event_time)
) PARTITION BY RANGE (event_time);

COMMENT ON TABLE app_private.event_ledger IS
    'Per-driver immutable perception-and-decision ledger (§VIII Passive lane). '
    'Append-only: owned by postgres, atjb granted INSERT/SELECT only. Never read by '
    'a runtime decision path. Day-partitioned, 14d retention via maintenance fn.';

-- Parent-level indexes ONLY. PG16 auto-propagates these to all existing AND future
-- child partitions; the maintenance function creates NO indexes.
CREATE INDEX IF NOT EXISTS ix_event_ledger_driver_time
    ON app_private.event_ledger (driver_id, event_time DESC);   -- the §6.2 lookback
CREATE INDEX IF NOT EXISTS ix_event_ledger_offer
    ON app_private.event_ledger (offer_id);
CREATE INDEX IF NOT EXISTS ix_event_ledger_type
    ON app_private.event_ledger (event_type);

-- ===== Block 3 (defined before Block 2, which reuses it): maintenance function =
-- One code path for bootstrap AND steady-state. Rebuilds the FULL forward window
-- each run (self-heals a multi-day cron outage instead of refilling one day/run).
-- Drop side discovers real attached partitions via pg_inherits and drops those
-- strictly older than the cutoff — discover, never guess names. SECURITY INVOKER
-- (cron session is already postgres; DEFINER adds risk, zero value).
CREATE OR REPLACE FUNCTION app_private.event_ledger_maintain_partitions(
    forward_days int     DEFAULT 16,    -- create-ahead buffer (longer than retention is fine)
    retain_days  int     DEFAULT 14,    -- drop partitions strictly older than this
    dry_run      boolean DEFAULT false  -- true → report would-be actions, change nothing
) RETURNS TABLE(action text, partition_name text)
LANGUAGE plpgsql SECURITY INVOKER AS $fn$
DECLARE
    t0     date := (now() AT TIME ZONE 'UTC')::date;
    cutoff date := (now() AT TIME ZONE 'UTC')::date - retain_days;
    d      date;
    i      int;
    pname  text;
    vfrom  timestamptz;
    vto    timestamptz;
    child  record;
BEGIN
    -- CREATE forward window: today .. today + forward_days
    FOR i IN 0..forward_days LOOP
        d     := t0 + i;
        pname := 'event_ledger_' || to_char(d, 'YYYY_MM_DD');
        IF NOT EXISTS (
            SELECT 1 FROM pg_class
            WHERE relname = pname AND relnamespace = 'app_private'::regnamespace
        ) THEN
            IF dry_run THEN
                action := 'would_create'; partition_name := pname; RETURN NEXT;
            ELSE
                -- explicit +00 → UTC day bounds regardless of session TimeZone (§II)
                vfrom := (to_char(d,     'YYYY-MM-DD') || ' 00:00:00+00')::timestamptz;
                vto   := (to_char(d + 1, 'YYYY-MM-DD') || ' 00:00:00+00')::timestamptz;
                EXECUTE format(
                    'CREATE TABLE app_private.%I PARTITION OF app_private.event_ledger '
                    || 'FOR VALUES FROM (%L) TO (%L)', pname, vfrom, vto);
                action := 'created'; partition_name := pname; RETURN NEXT;
            END IF;
        END IF;
    END LOOP;

    -- DROP attached partitions strictly older than the cutoff. Discover real
    -- children (well-formed names only) so a history gap can't orphan a partition.
    FOR child IN
        SELECT c.relname
        FROM pg_inherits inh
        JOIN pg_class c ON c.oid = inh.inhrelid
        WHERE inh.inhparent = 'app_private.event_ledger'::regclass
          AND c.relname ~ '^event_ledger_[0-9]{4}_[0-9]{2}_[0-9]{2}$'
    LOOP
        IF to_date(right(child.relname, 10), 'YYYY_MM_DD') < cutoff THEN
            IF dry_run THEN
                action := 'would_drop'; partition_name := child.relname; RETURN NEXT;
            ELSE
                EXECUTE format('DROP TABLE IF EXISTS app_private.%I', child.relname);
                action := 'dropped'; partition_name := child.relname; RETURN NEXT;
            END IF;
        END IF;
    END LOOP;
    RETURN;
END;
$fn$;

-- ===== Block 2: bootstrap the forward buffer (reuses the maintenance fn) ======
-- Single code path: bootstrap == one steady-state run. Creates today..today+16.
SELECT app_private.event_ledger_maintain_partitions();

-- ===== Block 4: keystone — GRANT-default-deny (NOT revoke-from-owner) =========
-- atjb owns nothing here; it has zero privileges until granted. REVOKE ALL first
-- only for idempotency on re-run. Never grant UPDATE/DELETE — that is the keystone.
-- Parent-level grants suffice for the app's via-parent INSERT/SELECT; PG checks the
-- named (parent) table's privilege and routes — child partitions stay owner-only,
-- so atjb cannot touch a child directly either (keystone holds both paths).
REVOKE ALL ON app_private.event_ledger FROM atjb;
GRANT INSERT, SELECT ON app_private.event_ledger TO atjb;
REVOKE ALL ON app_private.event_ledger FROM puddlejumper_readonly;
GRANT SELECT ON app_private.event_ledger TO puddlejumper_readonly;   -- analytics: read-only

-- ===== Block 5: driver_trip_state.ledger_state (the diff-seed) ================
ALTER TABLE app_private.driver_trip_state
    ADD COLUMN IF NOT EXISTS ledger_state jsonb;
COMMENT ON COLUMN app_private.driver_trip_state.ledger_state IS
    'Ledger BOOKKEEPING only: {last_queue_snapshot, last_matcher_snapshot, '
    'last_keyframe_at}. NOT a runtime decision input (§VIII Passive). Read at LOAD, '
    'written only on change/reap ticks inside the C4 batched savepoint. Keyframe '
    'counter deferred (C5 fork open) — trivial ADD COLUMN later if it lands there.';

COMMIT;

-- ===== Block 6: verification (post-commit, outside the txn) ===================
-- The keystone assert is the proof of the whole exercise. (NOTE: this is catalog-
-- true; the C6 §XIV.J live-PG test must additionally attempt a real UPDATE/DELETE
-- as atjb against a POPULATED CHILD partition — catalog-true ≠ runtime-true.)
DO $verify$
BEGIN
    ASSERT NOT has_table_privilege('atjb', 'app_private.event_ledger', 'UPDATE'),
        'KEYSTONE FAIL: atjb has UPDATE on event_ledger';
    ASSERT NOT has_table_privilege('atjb', 'app_private.event_ledger', 'DELETE'),
        'KEYSTONE FAIL: atjb has DELETE on event_ledger';
    ASSERT has_table_privilege('atjb', 'app_private.event_ledger', 'INSERT'),
        'atjb is missing INSERT on event_ledger';
    ASSERT has_table_privilege('atjb', 'app_private.event_ledger', 'SELECT'),
        'atjb is missing SELECT on event_ledger';
    ASSERT (SELECT count(*) FROM pg_inherits
            WHERE inhparent = 'app_private.event_ledger'::regclass) >= 17,
        'forward buffer < 17 partitions';
    ASSERT EXISTS (SELECT 1 FROM information_schema.columns
                   WHERE table_schema = 'app_private' AND table_name = 'driver_trip_state'
                     AND column_name = 'ledger_state' AND data_type = 'jsonb'),
        'driver_trip_state.ledger_state missing or not jsonb';
    RAISE NOTICE 'event_ledger Phase-1 verification PASSED (keystone + buffer + ledger_state)';
END;
$verify$;

-- Optional dry-run smoke-test of the maintenance fn (returns rows, changes nothing):
--   SELECT * FROM app_private.event_ledger_maintain_partitions(16, 14, true);
--
-- RUNTIME-TRUE keystone check (Block 6 proves only catalog-true). INSERT must succeed
-- (identity needs no separate sequence grant in PG16; a rolled-back insert still proves
-- it — no stray SMOKE row), UPDATE must raise permission denied:
--   SET ROLE atjb; BEGIN;
--     INSERT INTO app_private.event_ledger (driver_id, event_type) VALUES ('SMOKE','keyframe');
--     SAVEPOINT s; UPDATE app_private.event_ledger SET summary='x' WHERE driver_id='SMOKE'; ROLLBACK TO s;
--   ROLLBACK; RESET ROLE;
--   -- INSERT ok + UPDATE 'permission denied' = keystone holds. UPDATE ok = keystone broken, STOP.

-- ───────────────────────────────────────────────────────────────────────────
-- CRONTAB (Andrew applies on the VM as user `andrew`; house idiom, same shape as
-- the suspected_pudos_janitor / refresh_hex_pricing_cache lines — NOT part of this
-- migration):
--
--   @daily psql -h 10.128.0.2 -U postgres -d puddlejumper \
--     -c "SELECT * FROM app_private.event_ledger_maintain_partitions();" \
--     >> /home/andrew/cron_ledger.log 2>&1
--
-- Dead-man's-switch (Flaw #4): alert if cron_ledger.log shows no successful run in a
-- rolling 26h window. The 16-day forward buffer makes a multi-day miss non-fatal.
-- ───────────────────────────────────────────────────────────────────────────
