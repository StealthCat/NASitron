# NASitron 1.0 storage workspace

Open a server and choose **Storage workspace**, or use Maintenance. Administrator access and a secure connection are required. Disk & pool actions remain available from the workspace. Physical disk commands use short `/dev/disk/by-id` basenames, resolved and validated by the remote helper; Linux `zdN` devices remain virtual ZFS volumes.

## Upgrade from 0.11.x

1. Back up NASitron's persistent data volume, including the SQLite database and encryption key, before updating the container. Version 1.0 adds tables for schedules, job runs, storage inventory and capacity samples; it preserves existing servers, labels, bays and historical telemetry.
2. Install the 1.0 remote helper on each NAS using the [remote installation instructions](../README.md#privileged-remote-helper). Re-run the installer with your **existing public key** using `--public-key-file` to preserve the current login. Automatic-key mode generates a new key; do not use it unintentionally during an upgrade. Older helpers cannot update themselves through the new UI.
3. Open the workspace, select **Refresh live inventory**, then reload after collection finishes. Host & helper should report 1.0.0. Cached inventory also refreshes hourly for enabled servers; normal disk statistics keep their configured collection cadence.
4. Create schedules explicitly. Upgrading alone does not create jobs or change pools, datasets or snapshots. Keep NASitron configured as a single application worker, as required by its scheduler and host-operation locks.

## Features

| Workspace area | What it provides |
| --- | --- |
| Expansion planner | Per-vdev current/projected usable capacity, bay locations and a disk-by-disk replacement checklist. Partial replacement does not unlock a RAIDZ vdev's larger capacity. Missing identities/sizes remain unknown. |
| Snapshots & recovery | Create/delete exact snapshots, hold/release, clone for inspection and reviewed rollback. Separate hourly/daily/weekly schedules provide retention tiers. |
| Schedules & replication | Persistent snapshot, replication, scrub and SMART policies; pause/resume/edit/run-now controls; transfer cancellation, byte progress, run history and overdue/failure alerts. |
| Diagnosis | Disk latency, queue depth, utilization, throughput, SMART findings, seven-day ranges and physical bay links; vdev totals with mapping completeness and outlier counts. |
| Datasets & capacity | Create filesystems/zvols; set or inherit supported properties; mount/unmount; live/snapshot/child/refreservation usage, quotas and forecasts. |
| Event timeline | Searchable host ZFS events, scan and collection events, policy changes and recent audited administrative commands. |
| Host & helper | OpenZFS/command/RAIDZ-expansion capability detection, reviewed helper update and rollback. |

## Scheduled work

Schedules have five fields: minute, hour, day-of-month, month, weekday. Weekdays use Unix numbering: Sunday is 0 or 7, Monday is 1; names such as `sun` work too. Examples: `0 * * * *` hourly, `0 2 * * *` daily at 02:00, `0 3 * * 0` Sunday at 03:00. When both day-of-month and weekday are restricted, **both must match**. Time zones are explicit; daylight-saving transitions follow APScheduler's local wall-clock behavior. Prefer UTC if an unambiguous cadence is needed.

Enabling a policy requires typing `ENABLE <name>` and authorizes its recurring actions and retention deletions. Each policy has a unique ownership prefix. Retention only removes that policy's snapshots on the exact dataset, keeps the requested newest count and skips held snapshots or snapshots with clones. There is no recursive retention. Edit schedule, name, retention, bandwidth and overdue threshold in place; pause and create a new policy to change its target or type.

Due runs are persisted before execution. Overlapping work on a host is deferred. Downtime coalesces missed schedules into one run; jobs interrupted during execution become **unknown** rather than being replayed automatically. Inspect the NAS before manually retrying an unknown outcome. Run history and command audits are retained in the database; the workspace shows the latest 100 runs.

Scrub and SMART job success means the host accepted the command, not that the physical scan finished successfully. Operations shows sampled scrub/resilver progress and results. The schedules page also shows sampled ATA SMART self-test status, vendor duration estimates and the disk's recorded test history when available. Create one SMART policy per physical disk and stagger test windows. Starting a SMART test is blocked while a pool is scrubbing/resilvering or that disk already reports a running test. Schedule scrub windows separately from long tests. Overdue thresholds for these policies track successful command submission; existing health alerts track detected errors.

## Replication

Register both NAS hosts and pin their SSH host-key fingerprints first. Choose an exact source dataset and a dedicated **new child dataset** on the destination, whose parent already exists. Both hosts need the 1.0 helper. Streams pass through NASitron over pinned SSH connections, so its network link and availability affect transfer speed. The optional MiB/s cap applies to the stream bytes forwarded by NASitron.

Replication uses raw ZFS sends, incremental bases matched by snapshot GUID, and resumable receives. The destination stays unmounted and read-only, tagged with its policy owner. An unrelated existing destination is rejected. No forced receive rollback, recursive send or automatic destruction of unrelated snapshots is performed. Source and destination OpenZFS must support the stream features; native errors are recorded when they do not.

Cancelling a transfer closes the stream and retains the partial receive. A subsequent run checks for a resume token before starting a new incremental transfer. It may finish an earlier snapshot; the freshness timestamp reflects that verified snapshot's creation time. Success requires matching a destination snapshot GUID to the source. The shared incremental base is held on the source with a policy-specific tag. After advancing, the old base's policy hold is released. Failed transfers may leave protective holds requiring inspection before manual cleanup. Policy retention also applies to its destination snapshots after a verified transfer.

## Reviewed actions and recovery

Interactive changes follow command preview, typed confirmation and single-use submission. The helper rechecks identities/properties at execution. Snapshot rollback does not use recursive or force flags, so newer dependent snapshots must be addressed separately. Filesystem clones start with `canmount=noauto` and `mountpoint=none`; choose a permitted mountpoint under `/mnt` or `/srv`, then mount explicitly. Zvol recovery clones start read-only.

Editable properties are compression, recordsize, quota, refquota, reservation, refreservation, atime, readonly, sync and mountpoint. Values are validated on the NAS. `sync=disabled` and arbitrary root mountpoints are not exposed. Zvol creation uses a specified, reserved size. Property tables show local, inherited and default sources.

The expansion planner is an estimate and never starts replacements. It subtracts parity/mirroring but excludes metadata, padding, slop and other allocation overhead. Replace one drive at a time, wait for resilver completion and verify pool health. After all required members are larger, review `autoexpand` or `online -e` under Disk & pool actions. RAIDZ width expansion is a separate action, gated by detected host and pool feature support.

Dataset forecasts use net growth from at least seven sampled days in the last 30 days and currently reported available space. Hourly samples are retained for 90 days. Deletions, reservations, workload changes and shared pool usage can invalidate a projection. Parent/child capacity rows overlap and must not be summed. Diagnosis outliers are investigation hints, not failure predictions.

## Helper maintenance

The initial 1.0 helper installation is manual. Subsequent **Host & helper → Update helper** previews fetch the current main commit from the fixed StealthCat/NASitron repository over verified HTTPS, download the immutable commit's helper, compile-check it and pin its SHA-256 digest. Execution downloads and verifies the same candidate; if main changed, generate a new preview. This trusts the repository's main branch and HTTPS; it is not a separately signed release channel.

The helper is installed atomically and keeps a root-owned, non-writable-by-others `.previous` file. Rollback uses that file through the same review flow. Neither operation changes sudo permissions. The NAS needs outbound HTTPS access to GitHub for updates. Helper updates are explicit and never run on a schedule.

## Validation and operating limits

Automated tests cover parsing, command allowlists, retention ownership, cron numbering, capacity estimates, API authorization/CSRF, restart state, transfer forwarding/GUID checks, helper digest verification and rendered form behavior. These use synthetic hosts; they do not certify a particular NAS, controller or OpenZFS version. Desktop/mobile browser checks run in CI. The local editing environment cannot launch Chromium because socket creation is blocked, so local UI verification uses server-rendered HTML and DOM interaction tests.

ZFS events are collected from the host ring hourly; events can be lost if that ring wraps between collections. Displayed event times are ingestion times, with original host timestamps retained in the message. The workspace shows stale inventory with its collection error rather than pretending a failed refresh succeeded.
