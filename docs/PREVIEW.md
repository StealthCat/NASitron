# NASitron 0.7 preview

This branch implements the monitoring, usability and layout review. Existing
installations receive additive database upgrades at startup. Back up before
trying the preview. No pool property changes, snapshot deletions or scrub commands
are introduced. Drive replacement remains the only action that mutates ZFS.

## Navigation and daily use

- Dashboard starts with attention items, freshness, server scope and capacity.
  Its ARC chart explicitly selects a server. The mean ARC card is an unweighted
  average of fresh, complete reporting servers.
- Server pages have Overview, Pools, Drives, Datasets, Performance, Events and
  Settings tabs. Deep links such as `/servers/1#drives` select the correct tab.
- Pools links to **Pool & root dataset accounting**. The existing `/tanks` URL
  remains available. This view combines pool accounting with root-filesystem
  properties; "tank" is not a distinct ZFS object type.
- Inventory controls provide sorting, paging, search, column selection and
  persistent local-browser preferences. The dataset view initially shows only
  six columns. Drive filters include server and SMART health; text search also
  matches pools, serials and physical labels. Use Columns for detailed fields.
- Display density is a browser preference. Keyboard navigation, visible focus,
  reduced motion, mobile navigation focus handling and chart text summaries are
  supported.

## Health and charts

Unknown SMART is never shown as passed. Collection freshness is distinct from
storage health: reporting, partial, stale, disabled and waiting states remain
visible. Failed SMART can also have stale retained data. Pool ONLINE counts
represent last-reported states, not a guarantee of current reachability.

Charts use actual timestamps, break lines across missing collection intervals,
show request errors and provide 1h/6h/24h/7d/30d ranges. Refresh is optional and
pauses when the document is hidden. Each chart has a numerical summary and
keyboard/pointer sample inspection. Chart timestamps use the browser timezone;
server-rendered timestamps use NASITRON_TIMEZONE. The page-load timestamp is not
the telemetry collection timestamp.

Drive detail pages show identity, location, pool membership and SMART indicators.
Physical labels require a serial number and are scoped to a server. Temperature,
SMART pass/fail and sector/endurance histories use collected metrics. New SMART
indicator history is available only after upgrading, not retroactively.

## Access control

| Role | Permissions |
| --- | --- |
| Viewer | Read telemetry, inventories, alerts, forecasts and operation history |
| Operator | Viewer permissions plus manual polls, tuning-bundle collection, drive labels, alert acknowledgement/snooze, notification windows and guarded drive replacement |
| Administrator | Operator permissions plus server credentials/enrollment, host keys, server deletion, settings, tuning comparison and user administration |

Backend authorization is enforced independently of hidden UI controls. Existing
administrators remain administrators. Existing non-admin accounts become viewers;
explicitly assign Operator where needed. Permission changes invalidate sessions
through the existing session-version mechanism. Replacement history records the
acting username; old entries have an unknown actor.

## Operations and notification windows

The Operations page displays scrub/resilver scan text, progress percentage,
throughput, remaining time and last scrub completion when OpenZFS reports them.
It never invents an ETA. Select auto refresh to follow current collections.

A successful replacement command is **Accepted**, not **Complete**. Subsequent
collections can move it through Resilvering and Verifying to Complete after a
zero-error completion result and healthy, error-free vdevs. A stale or missing
pool cannot complete an operation. Historical commands or unparseable scan output
may remain unverified; consult the raw scan output and the NAS directly. Complete
means this observed resilver/topology verification succeeded, not that every
possible hardware health check passed.

Operators can schedule a server notification window to start now or within 30
days, for up to seven days. Collection and alert state updates continue. Email
notifications for pending alerts resume after the window ends if the conditions
remain active. End server windows cancels all current/future windows for that
server. Individual alerts can be snoozed independently. Acknowledgement remains
a separate marker from notification snoozing.

Alerts have server, severity, state and text filters, 50-record server-side pages,
and links to affected drives or server pool/event views. A historical drive link
can return not found if that device is no longer in the latest inventory.

## Capacity and snapshot inventory

Forecasts fit daily means over the latest 30 days and require at least seven
sampled days. They use the configured warning/critical capacity thresholds and
show fit quality plus a residual-based range. The range is not a calibrated
probability interval: workload changes, snapshots, deletions and expansion can
invalidate it. Inspect freshness before relying on a forecast.

Snapshot inventory runs at the SMART/detail cadence, rather than every fast
poll. It is read-only and shows creation time, used space and referenced space.
Sort by creation time to review retention. Truncated/failed inventory retains the
previous complete inventory with its original timestamp and an error. This
release does not enforce snapshot-retention policies or delete snapshots.

Compression cards explicitly show unweighted means. Dataset means include root
and child datasets and must not be interpreted as an aggregate space-saving
ratio. No nested used-space totals are presented as independent storage savings.

## Tuning comparison

Settings → Backup & tuning comparison accepts two NASitron JSON/JSON.gz bundles.
It compares pool/dataset properties, module parameters, selected Linux VM sysctls,
CPU fields and drive hardware identities. Runtime counters are excluded from
this comparison. Native key locations and user-defined ZFS property values stay
redacted. Files are not saved by the feature.

Each upload is limited to 8 MiB and expanded JSON to 32 MiB. The comparison route
has a dedicated 17 MiB total request limit; other routes retain the configured
small request-body limit. The comparison does not apply any tuning changes.

## Encrypted backup and restore

`python -m app.backup` is an offline utility. It prompts for a password of at
least 12 characters, derives an encryption key with scrypt, and authenticates the
archive with Fernet. It refuses to overwrite an archive or an existing restore
directory. Keep the password separately from the backup.

The archive contains:

- A consistent SQLite database copy, including users, credentials, settings,
  labels, alerts and history.
- Regular files beneath NASITRON_DATA_DIR, including uploaded TLS configuration.
- An external configured known_hosts file when applicable.
- NASITRON_SECRET_KEY and the current NASITRON_* environment values, encrypted
  inside the archive.

Stop NASitron first; its process lock prevents an online backup. The utility
supports file-backed SQLite and at most 512 MiB of unencrypted content. For larger
installations, use a tested external encrypted backup of the stopped database,
data directory, deployment configuration and secret key. Symlinks are refused.

In the same configured environment as the service:

```bash
python -m app.backup create /secure-backups/nasitron.nsb
python -m app.backup restore /secure-backups/nasitron.nsb /recovery/nasitron
```

For Docker Compose, stop the application, create a private writable host backup
directory, and use the existing service image and data volume. Running this
one-off backup command as root allows writing a root-owned archive into that
private host directory; it does not change application ownership:

```bash
docker compose stop nasitron
mkdir -m 700 backups
docker compose run --rm --no-deps --user root \
  -v "$PWD/backups:/backup" nasitron \
  python -m app.backup create /backup/nasitron.nsb
docker compose start nasitron
```

For recovery, use a new directory and keep the application stopped while
switching data/configuration:

```bash
docker compose run --rm --no-deps --user root \
  -v "$PWD/backups:/backup:ro" -v "$PWD/recovery:/recovery" nasitron \
  python -m app.backup restore /backup/nasitron.nsb /recovery/nasitron
```

The restore verifies SQLite integrity before publishing files. Review the
restored `recovery-environment.json`; reapply the exact value from
`recovery-secret-key` as NASITRON_SECRET_KEY through your normal secret-management
process. Point the service at the restored database/data directory and correct
its ownership to the application user. Review path overrides from the original
host rather than copying them blindly. Keep the recovery files private.

The Compose `.env`, Compose file and **Caddy's separate data volume** must also be
backed up through your deployment backup process. They are outside the utility's
data-directory archive; Caddy's internal CA and managed certificates may live in
that separate volume. Restart only after those deployment settings and keys have
been restored or intentionally replaced. Do not run old and restored copies
against the same NAS while testing recovery.

## Validation

The Python suite covers legacy pages plus permissions, health-state semantics,
resilver state transitions, notification suppression, snapshots, forecasts,
bundle limits and encrypted restore. GitHub Actions also runs Chromium desktop
and mobile interaction checks with synthetic telemetry, publishes screenshots,
and retains the existing Docker and Compose checks. CI fixture hosts are never
polled. A live ZFS replacement or remote installation is not exercised by these
synthetic checks.

### Pool disk hierarchy

The Pools page now preserves the collected pool → vdev → device hierarchy,
including nested replacement groups and separate data, log, cache, special,
dedup and spare sections. Groups open by default and can be collapsed individually
or together. Each level shows its reported state and read/write/checksum error
counts; these are not I/O rates or summed child counters. Matched devices link to
drive details and show physical capacity, model, serial, location, SMART and
temperature. Unmatched devices remain visible without guessed drive identities.
Reported vdev size is shown when supplied by ZFS; usable RAIDZ capacity is not
estimated from physical drive sizes. No extra remote commands are required.

### Disk I/O history and collection fix

Fixed a malformed SMART metric insertion that caused `_row()` argument errors
and rolled back collection whenever a current SMART pass/fail result was present.
The next successful poll clears the server's last collection error.

The Disk I/O page provides server/disk selection, 15m, 1h, 6h, 24h, 7d and 30d
presets, custom start/end times in the browser timezone, and optional auto refresh.
It charts read/write throughput, read/write IOPS, read/write completion latency,
busy time and average queue depth. Shared ranges apply to all eight charts and
are preserved in the page URL. Historical disks remain selectable while their
metrics remain within the configured retention period.

Collection reads Linux `/proc/diskstats` twice about one second apart on every
regular poll. It needs neither sysstat nor additional sudo permissions and is
independent of SMART scheduling. Rates use measured uptime deltas and 512-byte
sectors per the [Linux diskstats documentation](https://docs.kernel.org/admin-guide/iostats.html).
Only physical disks from the inventory are stored, avoiding partition double
counting. Interrupted samples and counter resets create gaps; idle intervals
record zero throughput/IOPS but no latency when no operations completed. Samples
do not capture bursts between polls. Busy time is not an NVMe saturation score.
History starts after upgrade; no historical measurements are backfilled.

### Verbose pool status and pool tabs

Each pool has a separate, keyboard-accessible tab labeled with pool name, server
and health. Tab URLs remain stable across refreshes and distinguish identically
named pools on different servers. Regular polling now requests
`zpool status -v -p -P -L`, retaining the full text alongside structured topology.
The page displays multiline status/action advice, scrub or resilver progress,
verbose permanent-error paths (including unresolved object IDs), and other
reported sections. The full command output is available in a disclosure below
the device configuration. Incomplete verbose collection is explicitly labeled.
Device rows show aligned state and error counters; drive identity and SMART
information are expandable. Verbose file lists become available after the next
successful poll following deployment.

### Capacity tables in pool tabs

Each pool tab includes all default `zpool list -v` columns: name, size, allocated,
free, checkpoint, expandable space, fragmentation, capacity, dedup ratio, health,
and alternate root. Collection uses explicit properties, exact numbers and
resolved full device paths. Status supplies indentation; rows stay in the command's
order, with pool totals emphasized and alternating device rows. Parent/child
allocations overlap and are not summed. Missing/not-applicable values remain dashes;
zero is shown as zero. Byte values have exact-byte tooltips. Failed polls retain
previous capacity rows with a stale label and their original timestamp. Narrow
screens scroll the table horizontally while the name column stays visible.

`zfs list` has no `-v` flag. A separate expandable table shows each pool's root
and child filesystems/zvols with used, available, referenced, compression and
mountpoint data from the existing dataset collection. Pool physical allocation
and dataset accounting remain separate. New verbose capacity measurements appear
after deployment and the next successful poll.
