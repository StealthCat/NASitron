# NASitron

NASitron is a Dockerized, agentless monitoring dashboard for Ubuntu servers running OpenZFS. It connects to one or more NAS hosts over SSH, collects ZFS/storage/system telemetry on a configurable schedule, keeps historical metrics, raises health alerts, sends email through a configurable SMTP relay, can produce a compressed diagnostic bundle intended for ZFS tuning analysis, and provides a guarded workflow for replacing failed ZFS drives with available blank disks.

## What it monitors

**Pools and topology**

- pool health, size, allocation, free space, capacity, fragmentation, and dedup ratio
- data, mirror/RAIDZ, SLOG/log, L2ARC/cache, special, dedup, and spare vdev roles from `zpool status`
- read/write/checksum error counters
- scrub/resilver status and parsed last scrub completion time
- pool read/write IOPS and bandwidth

**Datasets and zvols**

- used, available, referenced, logical-used, snapshot-used, compression ratio, mountpoint, and dataset type

**ARC / L2ARC**

- ARC current/target/min/max sizes
- hits, misses, hit rate, MRU/MFU and prefetch counters
- L2ARC size, hits, misses, and hit rate
- raw ARC kstats are retained in each recent snapshot for future UI expansion

**Physical drives**

- block-device inventory, capacity, media type, transport, model, and serial
- mapping of disks/partitions back to pool and vdev role where possible
- SMART overall health, temperature, power-on hours
- reallocated, pending, and offline-uncorrectable sector counts
- NVMe percentage used and media errors when reported by smartmontools

**Host health**

- Ubuntu/kernel/ZFS versions, uptime, load averages
- memory and swap usage
- ZFS systemd service state
- SSH collection failures

## History and alerts

NASitron stores frequent numeric samples separately from recent full snapshots. Defaults are 90 days of time-series metrics and 7 days of full snapshots; both are configurable. SQLite runs in WAL mode and is persisted in the Docker volume.

Built-in alert conditions include:

- pool health not `ONLINE`
- warning/critical pool capacity thresholds
- ZFS read/write/checksum errors
- overdue scrubs
- SMART overall-health failure
- warning/critical drive temperatures
- pending or offline-uncorrectable sectors
- repeated SSH/collector failures

Alert state is deduplicated, remains visible until resolved, and can be acknowledged. New/re-triggered conditions can be sent through an SMTP relay.

## Failed-drive replacement

Each server page includes **Replace failed drive**, which performs a fresh live inventory over SSH and identifies:

- failed/offline ZFS leaf devices in states such as DEGRADED, FAULTED, OFFLINE, UNAVAIL, or REMOVED
- whole physical disks that currently have no filesystem, mountpoint, child partitions/mappings, or ZFS membership
- stable `/dev/disk/by-id/` paths for replacement candidates when available

Before a replacement can run, NASitron shows the exact `zpool replace` command, requires typed confirmation, validates a short-lived maintenance token, and then **re-runs the live disk inventory immediately before execution**. If the selected replacement disk has become mounted, partitioned, assigned to ZFS, or otherwise ineligible, the operation is refused.

NASitron runs only:

```bash
sudo -n zpool replace <pool> <failed-device> <replacement-device>
```

It deliberately does **not** add `-f`, wipe filesystem signatures, repartition disks, or otherwise force a disk into service. OpenZFS still performs its own compatibility and size checks. Successful commands normally begin a resilver, which can be monitored on the normal server dashboard.

Every attempted replacement is written to the local maintenance history with the selected devices, command, exit status, and returned pool status/output.

This feature requires a narrowly scoped passwordless sudo rule for `zpool replace`; see [examples/nasitron.sudoers](examples/nasitron.sudoers).

## Tuning/support bundle

The **Download tuning bundle** action performs a deeper live collection and returns:

`nasitron-<server>-<timestamp>.json.gz`

It includes the latest structured snapshot plus raw diagnostic output for:

- `zpool status -P -L -v`
- all pool and dataset properties (`zpool get all`, `zfs get all`)
- extended dataset accounting
- verbose pool/vdev I/O samples
- `arc_summary` when installed
- ARC, dbuf, zfetch, and vdev-cache kstats
- loaded ZFS module information and module parameters
- relevant Linux VM sysctls
- CPU, memory, block-device, and mount topology
- SMART/NVMe JSON for physical disks
- recent ZFS/storage-related kernel messages when permitted

SSH passwords, SSH private keys/passphrases, NASitron's encryption key, and SMTP credentials are **never** written to the bundle. The bundle can be provided for offline analysis and tuning recommendations.

## Quick start

### 1. Prepare the Ubuntu ZFS host

Install the useful collection tools:

```bash
sudo apt update
sudo apt install openssh-server zfsutils-linux smartmontools
```

Create a dedicated account:

```bash
sudo adduser --disabled-password --gecos "" nasitron
sudo install -d -m 700 -o nasitron -g nasitron /home/nasitron/.ssh
sudoedit /home/nasitron/.ssh/authorized_keys
sudo chown nasitron:nasitron /home/nasitron/.ssh/authorized_keys
sudo chmod 600 /home/nasitron/.ssh/authorized_keys
```

Most `zpool status/list/iostat`, `zfs list/get`, `/proc` and `lsblk` data is readable without root. SMART generally is not. If you want SMART/NVMe data, install a tightly scoped sudoers rule based on [examples/nasitron.sudoers](examples/nasitron.sudoers).

Always verify the real paths first:

```bash
command -v smartctl
command -v dmesg
command -v zpool
sudo visudo -f /etc/sudoers.d/nasitron
```

The `dmesg` allowance is optional and is used only by the on-demand support bundle. The `zpool replace` allowance is optional and should be granted only if you intend to use NASitron's drive-replacement workflow.

### 2. Configure NASitron

```bash
git clone https://github.com/StealthCat/NASitron.git
cd NASitron
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `.env` as `NASITRON_SECRET_KEY`. **Keep this key stable**: it encrypts stored SSH and SMTP secrets. Changing it later makes those existing encrypted values unreadable.

Dashboard HTTP Basic authentication is required:

```dotenv
NASITRON_WEB_USERNAME=admin
NASITRON_WEB_PASSWORD=use-a-long-unique-password
```

Start the application:

```bash
docker compose up -d --build
```

Open `http://<docker-host>:8080`, choose **Add server**, and paste the dedicated SSH private key (recommended) or configure a password.

### 3. Host-key behavior

By default NASitron uses trust-on-first-use (TOFU) and stores observed host keys in `/data/known_hosts`, which is inside the persistent Docker volume. You can enable strict host-key checking per monitored server after the expected key has been learned, or pre-populate that file yourself.

## Docker configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `NASITRON_SECRET_KEY` | Required encryption-key material for stored secrets | none |
| `NASITRON_DATA_DIR` | Database/known_hosts directory | `/data` |
| `NASITRON_DATABASE_URL` | SQLAlchemy database URL | SQLite in `/data/nasitron.db` |
| `NASITRON_TIMEZONE` | Display timezone | `UTC` |
| `NASITRON_WEB_USERNAME` | Optional HTTP Basic username | empty/disabled |
| `NASITRON_WEB_PASSWORD` | Optional HTTP Basic password | empty |

The supplied Compose file persists `/data` in the `nasitron_data` named volume.

## SMTP

SMTP is configured in **Settings**. NASitron supports:

- unauthenticated relays
- username/password authentication
- STARTTLS
- implicit TLS / SMTP_SSL
- multiple comma-separated recipients

The SMTP password is encrypted at rest. Use the **Send SMTP test message** control after saving the relay settings.

## Polling behavior

The default lightweight polling interval is 60 seconds. SMART collection defaults to every 15 minutes and its most recent values are carried forward between SMART polls so the dashboard remains continuous without unnecessarily waking disks at every normal sample.

The scheduler can monitor multiple servers concurrently. Each host's collection run is de-duplicated so a slow SSH poll cannot overlap itself.

## Security notes

- Use a dedicated, minimally privileged SSH account.
- Prefer SSH keys over passwords.
- Keep `NASITRON_SECRET_KEY` outside source control and back it up with the persistent database.
- Enable dashboard authentication or place NASitron behind an authenticated reverse proxy before exposing it beyond a trusted network.
- Do not grant unrestricted passwordless sudo to the monitoring account.
- The optional drive-replacement page is intentionally the only current feature that mutates ZFS state; it is limited to `zpool replace` and records every attempt.
- Protect the web UI with HTTP Basic authentication or an authenticated reverse proxy before granting the remote account `zpool replace` capability.
- TOFU is convenient for initial setup; strict host-key verification is preferable once keys are known.
- Monitoring, tuning-bundle collection, and all other current NASitron functions remain read-only.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
export NASITRON_DATA_DIR="$PWD/data"
export NASITRON_SECRET_KEY="development-only-secret"
uvicorn app.main:app --reload --port 8080
```

Tests:

```bash
python -m compileall -q app
python -m pytest -q
```

## Current scope

NASitron is read-only for monitoring and tuning collection, with one deliberately narrow maintenance exception: the guarded failed-drive replacement workflow can execute `zpool replace`. It does not expose general remote shell access or arbitrary ZFS administration. Future extensions can add more specialized views (per-vdev latency, snapshot-growth analysis, ZED event ingestion, Prometheus export, and additional notification transports) without changing the collection model.


## 0.4 hardening changes

NASitron 0.4 treats the application as an authenticated administrative interface. `NASITRON_SECRET_KEY` must be at least 32 characters, `NASITRON_WEB_USERNAME` is required, and `NASITRON_WEB_PASSWORD` must be at least 12 characters. All mutating forms use CSRF protection. `/healthz` remains public for container health checks.

Drive replacement requires HTTPS by default. On a trusted private management network only, this can be intentionally overridden with `NASITRON_ALLOW_INSECURE_MAINTENANCE=true`. HTTP Basic credentials should otherwise always be protected by an HTTPS reverse proxy. When TLS terminates at a reverse proxy, set `NASITRON_FORWARDED_ALLOW_IPS` to that trusted proxy IP/network so Uvicorn may honor `X-Forwarded-Proto`; do not use `*` unless untrusted clients cannot reach NASitron directly.

SMART JSON is parsed even when `smartctl` returns a non-zero health bitmask, preventing a current failing result from being replaced by an older healthy sample. Partial telemetry refreshes are tracked explicitly and do not resolve alerts for subsystems that failed to refresh.

Current state is updated every poll, while full JSON snapshots are written on a configurable interval (15 minutes by default). Database retention cleanup runs hourly, historical chart responses are downsampled to a bounded number of points, and SMART samples are not duplicated between SMART polling intervals.

SMTP delivery is separated from the collection transaction. Pending alerts are batched by server and relay failures use retry backoff.

The replacement workflow now prefers structured OpenZFS status JSON when available, targets failed vdevs by immutable GUID, checks representative size, kernel holders and `wipefs --no-act` signatures, revalidates on the same SSH session, and serializes execution with a remote `flock`. Existing scrub/resilver activity requires an explicit conflict override.

Support bundles have bounded per-command output, one concurrent generator per server, temporary-file streaming, and redaction of `keylocation`, `keystatus`, and administrator-defined ZFS user-property values.

For replacement support, the dedicated SSH account additionally needs narrowly scoped read-only `wipefs --no-act` access and the exact `flock ... zpool replace` command shown in `examples/nasitron.sudoers`. Do not grant unrestricted passwordless sudo.


SQLite persistence is serialized across collector threads while SSH collection remains concurrent, reducing writer contention without sacrificing multi-server polling concurrency.
