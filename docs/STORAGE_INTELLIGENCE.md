# Storage intelligence in 0.8

This release keeps the dark navy and `#FC1859` interface and all existing preview workflows.

## Monitoring views

- **Drive bays:** labeled physical drives grouped by server. Labels follow serial numbers. Click a tile to edit its location in drive details. Tiles sort by label; their positions are not hardware-detected enclosure slots.
- **Disk I/O:** eight metrics fetched together for one shared time window. Expand **Compare drives** and select up to three additional drives, then choose the comparison metric. Small multiples share time and value scales, and pointer/keyboard inspection follows the same timestamp. The busiest-drive table ranks the latest sampled busy time. One-second sampling does not capture every burst between polls.
- **Event timeline:** filter by server or event type. Combines retained alert occurrences/resolutions, replacement commands, maintenance windows and newly recorded collection/scan transitions. Historical alerts provide their first occurrence and latest resolution, not every recurrence. New transition history starts on upgrade and follows metric retention.
- **Collection status:** last duration, attempt, successful collection, next due time, SMART cycle and subsystem errors. A due time is a scheduler estimate, not a guarantee that a worker has started.
- **Settings → Database health:** database/WAL allocation, reusable space, retention and last housekeeping result. Exact row counts are calculated only when requested because large histories take time to count.
- **Snapshots:** search, sort and choose 25/50/100/250 results per page. Filtering and pagination occur on the server; current snapshot inventories still reside in the per-host JSON state.
- **Forecasts:** daily trends with compact sparklines, fit quality and uncertainty. Readings from raw and summarized history are weighted by sample count.

## Health and chart semantics

SMART health and sampling age are independent. A previously failed drive remains **Failed** between SMART scans, alongside its last-known sampling time. Collection status does not imply healthy pools; server cards display both. Missing ARC readings are excluded from fleet averages and reported as unavailable instead of zero. Dashboard colors use configured warning/critical thresholds.

Chart range changes cancel superseded requests. Performance charts retain min/max envelopes; SMART pass/fail buckets use the minimum (any failure wins). The latest performance reading remains exact. Accessible summaries include extremes, while keyboard inspection exposes each point and its range. Disk comparison controls are retained in the URL.

## History and upgrades

Database initialization adds summary/event tables and the collection-duration column without replacing existing configuration or history. Normal `git pull` and `docker compose up -d --build` applies the update.

Hourly housekeeping removes expired rows in 5,000-row transactions and gradually compacts retained history:

| Age | Storage |
| --- | --- |
| Latest 7 days | Raw observations |
| 8–30 days | Hourly summaries |
| Older than 30 days | Daily summaries |

The configured overall retention remains authoritative, including when it is shorter than these windows. Each summary stores count, sum, minimum, maximum and latest observation. Compaction and source deletion are one transaction and can retry after interruption without counting readings twice. Processing is capped at 48 source hours and 48 source days per housekeeping run; an existing backlog takes multiple runs to finish.

Summarization permanently replaces older individual samples. Historical ranges select retained bucket start times, so sub-hour/sub-day boundaries cannot recover the original readings. The chart identifies retained resolution. Deleted SQLite pages are reused; the file does not immediately shrink.

Full offline encrypted backups continue to include the complete SQLite database, including these additions. Use the backup instructions in the monitoring guide if preserving pre-compaction raw history is needed.

## Verification

The test suite covers worst-state health, raw/hourly/daily aggregation, late backfill, interrupted transactions, API bounds/access control, snapshot paging, DOM request races and batched comparisons. Browser checks cover existing pool workflows plus new pages at desktop, 390px and 320px widths.
