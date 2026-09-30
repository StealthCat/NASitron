# Storage intelligence in 0.9

This release keeps the dark navy and `#FC1859` interface and all existing preview workflows.

## Monitoring views

- **Drive bays:** manually configured enclosure rows and columns, with serial-based slot assignments. Empty slots and missing assigned drives remain visible. Operators can create layouts and assign drives; viewers can inspect them. The detected-drive list retains editable labels.
- **Disk I/O:** eight metrics fetched together for one shared time window. Expand **Compare drives** and select up to three additional drives, then choose the comparison metric. Searchable checkboxes and removable chips support touch screens. Only the required metric/drive pairs are queried (11 rather than 32 series for four drives). Small multiples share time and value scales, and pointer/keyboard inspection follows the same timestamp. The busiest-drive table ranks the latest sampled busy time. One-second sampling does not capture every burst between polls.
- **Event timeline:** filter by server or event type. Combines retained alert occurrences/resolutions, replacement commands, maintenance windows and newly recorded collection/scan transitions. Cursor pagination preserves ordering while new events arrive. Failed commands remain failed; later replacement lifecycle transitions appear separately. Historical alerts provide their first occurrence and latest resolution, not every recurrence. New transition history starts on upgrade and follows metric retention.
- **Collection status:** last duration, attempt, successful collection, next due time, SMART cycle and subsystem errors. Worker state explicitly distinguishes queued, running, completed, failed and overdue. Static system and ZFS properties refresh every 15 minutes; fast counters remain on the polling cadence. Normal host collection has a 180-second total deadline.
- **Settings → Database health:** database/WAL allocation, reusable space, retention, housekeeping state/errors/duration, and estimated backlog catch-up. Raw/hourly windows have a preview-before-apply workflow. Exact row counts are calculated only when requested because large histories take time to count.
- **Snapshots:** search, sort and choose 25/50/100/250 results per page. Filtering, sorting and pagination run in SQL against normalized inventory rows. Existing state is backfilled on first use; failed collection preserves the last good inventory.
- **Forecasts:** daily trends with compact sparklines, fit quality and uncertainty. Readings from raw and summarized history are weighted by sample count.

- **Saved views and timezone:** personal saved monitoring URLs retain host/drive/range selections and inventory filters, sorting, columns and page size. Each user can choose an IANA timezone for both server timestamps and chart labels/custom ranges. Repeated daylight-saving times select the earlier occurrence; nonexistent times are rejected.
- **Navigation:** collapsible Overview, Storage, Performance, Activity and Administration sections retain the existing navy/pink palette.

## Health and chart semantics

SMART health and sampling age are independent. A previously failed drive remains **Failed** between SMART scans, alongside its last-known sampling time. Collection status does not imply healthy pools; server cards display both. Missing ARC readings are excluded from fleet averages and reported as unavailable instead of zero. Dashboard colors use configured warning/critical thresholds.

Chart range changes cancel superseded requests. Performance charts retain min/max envelopes; SMART pass/fail buckets use the minimum (any failure wins). The last fully covered performance point uses the latest reading. Partially overlapping summary buckets are marked approximate, keep their original coverage metadata, and use a timestamp clamped to the requested range. Gap detection uses each point’s resolution so daily summaries do not hide gaps in recent raw data. Accessible summaries include extremes, while keyboard inspection exposes each point and its range. Disk comparison controls are retained in the URL.

## History and upgrades

Database initialization adds summary/event, normalized inventory, enclosure and saved-view tables plus collection-duration and user-timezone columns without replacing existing configuration or history. Normal `git pull` and `docker compose up -d --build` applies the update.

Hourly housekeeping removes expired rows in 5,000-row transactions and gradually compacts retained history:

| Age | Storage |
| --- | --- |
| Latest 7 days | Raw observations |
| 8–30 days | Hourly summaries |
| Older than 30 days | Daily summaries |

These are defaults; administrators can adjust raw and hourly windows in Database health. The configured overall retention remains authoritative, including when it is shorter than these windows. Each summary stores count, sum, minimum, maximum and latest observation. Compaction and source deletion are one transaction and can retry after interruption without counting readings twice. Processing is capped at 48 source hours and 48 source days per housekeeping run; an existing backlog takes multiple runs to finish.

Summarization permanently replaces older individual samples. Historical ranges include overlapping summaries, but cannot recover exact sub-hour/sub-day readings. A boundary summary includes the whole retained bucket and is labelled approximate. The chart identifies retained resolution. Deleted SQLite pages are reused; the file does not immediately shrink.

Full offline encrypted backups continue to include the complete SQLite database, including these additions. Use the backup instructions in the monitoring guide if preserving pre-compaction raw history is needed.

## Verification

The test suite covers worst-state health, raw/hourly/daily aggregation, late backfill, interrupted transactions, API bounds/access control, snapshot paging, DOM request races and batched comparisons. Browser checks cover existing pool workflows plus new pages at desktop, 390px and 320px widths.
