-- Ops queries against the reconciliation database. Run with:
--   recon --db data/demo/recon.db sql sql/demo_queries.sql [--name <part of a name>]
-- Amounts are stored as exact decimal text; CAST(... AS REAL) below is for display/sorting only.
-- Every query is pinned to the latest run, so results are reproducible for that run id.

-- name: 1. break exposure by rail and type
WITH latest AS (SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1)
SELECT m.rail,
       m.break_type,
       COUNT(*) AS breaks,
       ROUND(SUM(CASE m.break_type
                   WHEN 'AMOUNT_MISMATCH'  THEN ABS(CAST(m.amount_delta AS REAL))
                   WHEN 'UNMATCHED_LEDGER' THEN ABS(CAST(m.ledger_amount AS REAL))
                   ELSE ABS(CAST(m.external_amount AS REAL))
                 END), 2) AS exposure,
       m.asset
FROM match_results m JOIN latest USING (run_id)
WHERE m.status = 'BREAK'
GROUP BY m.rail, m.break_type, m.asset
ORDER BY exposure DESC;

-- name: 2. open break aging (days outstanding at run cutoff)
WITH latest AS (SELECT run_id, as_of FROM runs ORDER BY created_at DESC LIMIT 1),
first_seen AS (
    SELECT re.result_id, MIN(e.occurred_at) AS first_event_at
    FROM result_events re
    JOIN latest l ON l.run_id = re.run_id
    JOIN events e ON e.event_id = re.event_id
    GROUP BY re.result_id
)
SELECT m.break_type,
       CASE WHEN age_days < 3 THEN '0-3d' WHEN age_days < 7 THEN '3-7d' ELSE '7d+' END AS age_bucket,
       COUNT(*) AS breaks,
       ROUND(MAX(age_days), 1) AS oldest_days
FROM (
    SELECT m.*, julianday(l.as_of) - julianday(f.first_event_at) AS age_days
    FROM match_results m
    JOIN latest l ON l.run_id = m.run_id
    JOIN first_seen f ON f.result_id = m.result_id
    WHERE m.status = 'BREAK'
) m
GROUP BY m.break_type, age_bucket
ORDER BY m.break_type, age_bucket;

-- name: 3. reserves coverage trend (window functions)
WITH latest AS (SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1)
SELECT as_of_date,
       asset,
       claimed_balance,
       verified_balance,
       coverage,
       rolling_min,
       printf('%+.6f', CAST(coverage AS REAL)
                       - LAG(CAST(coverage AS REAL)) OVER (PARTITION BY asset ORDER BY as_of_date)) AS day_change,
       CASE WHEN CAST(coverage AS REAL) < 1 THEN 'SHORTFALL' ELSE 'OK' END AS flag
FROM coverage_snapshots JOIN latest USING (run_id)
ORDER BY asset, as_of_date DESC
LIMIT 10;

-- name: 4. lineage of the largest break (break -> events -> source file)
WITH latest AS (SELECT run_id FROM runs ORDER BY created_at DESC LIMIT 1),
top_break AS (
    SELECT m.run_id, m.result_id, m.break_type, m.amount_delta, m.ledger_amount, m.external_amount
    FROM match_results m JOIN latest USING (run_id)
    WHERE m.status = 'BREAK'
    ORDER BY ABS(CAST(COALESCE(m.amount_delta, m.ledger_amount, m.external_amount) AS REAL)) DESC
    LIMIT 1
)
SELECT t.result_id,
       t.break_type,
       re.side,
       e.source,
       e.source_event_id,
       e.amount,
       e.occurred_at,
       b.origin AS source_file
FROM top_break t
JOIN result_events re ON re.run_id = t.run_id AND re.result_id = t.result_id
JOIN events e ON e.event_id = re.event_id
JOIN ingest_batches b ON b.batch_id = e.batch_id
ORDER BY re.side;

-- name: 5. idempotency evidence (batches loaded vs runs created)
SELECT 'ingest batches' AS item, COUNT(*) AS n FROM ingest_batches
UNION ALL SELECT 'events (current versions)', COUNT(*) FROM events
UNION ALL SELECT 'event revisions kept', COUNT(*) FROM event_revisions
UNION ALL SELECT 'reconciliation runs', COUNT(*) FROM runs;
