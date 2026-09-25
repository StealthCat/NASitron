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

NASitron stores frequent numeric samples separately from recent full snapshots. Defaults are 90 days of time-series metrics and 30 days of full snapshots; both are configurable. SQLite runs in WAL mode and is persisted in the Docker volume.

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

Before a replacement can run, NASitron shows the exact guarded replacement command, requires typed confirmation and CSRF validation, and then **re-runs the live disk inventory immediately before execution**. If the selected replacement disk has become mounted, partitioned, assigned to ZFS, or otherwise ineligible, the operation is refused.

NASitron runs only:

```bash
sudo -n /usr/local/sbin/nasitron-root-helper replace <pool> <failed-guid> <replacement-by-id> <allow-conflict:0|1>
```

It deliberately does **not** add `-f`, wipe filesystem signatures, repartition disks, or otherwise force a disk into service. OpenZFS still performs its own compatibility and size checks. Successful commands normally begin a resilver, which can be monitored on the normal server dashboard.

Every attempted replacement is written to the local maintenance history with the selected devices, command, exit status, and returned pool status/output.

This feature requires the root-owned NASitron helper plus the exact helper-only sudo rule in [examples/nasitron.sudoers](examples/nasitron.sudoers). The helper revalidates the failed GUID and replacement disk and invokes `zpool replace` without a shell.

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

The recommended setup is the idempotent remote installer at `scripts/install-remote.sh`. Generate a dedicated SSH keypair on your administration workstation or another trusted machine; keep the private key for the NASitron server form and copy only the public key to the NAS.

```bash
ssh-keygen -t ed25519 -f ./nasitron-monitoring -C "NASitron monitoring"
```

From a NASitron checkout on the NAS:

```bash
sudo ./scripts/install-remote.sh --public-key-file /path/to/nasitron-monitoring.pub
```

The installer is self-contained and is also served directly by every running NASitron instance at `/install-remote.sh`. This is the simplest installation path because the remote NAS does not need GitHub access or a pre-generated SSH key:
 
```bash
curl -fsSL https://YOUR-NASITRON-HOST/install-remote.sh | sudo bash
```

When no public key is supplied, the installer generates a dedicated Ed25519 keypair, installs the public key for the `nasitron` account, and prints the private key once at completion. Copy that private key into NASitron's **Add Server → Private key** field. The temporary private-key file is deleted when the installer exits.

There is also a **Settings → NAS Installer** panel with a direct download link and a command using the current NASitron URL. The endpoint is intentionally unauthenticated because the script contains no NASitron credentials or instance secrets and must be reachable before a remote NAS has been enrolled. It is still served over NASitron's normal HTTPS path.

If the NASitron instance uses Caddy's internal CA, install/trust that CA on the remote NAS before piping the script to root. Avoid bypassing certificate validation for unattended installation.

Because this repository is private, you can alternatively fetch the same script through the GitHub Contents API with a token that can read the repository:

```bash
export GITHUB_TOKEN='github_pat_...'
export NASITRON_PUBKEY='ssh-ed25519 AAAA...'

curl -fsSL \
  -H "Authorization: Bearer $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github.raw+json" \
  "https://api.github.com/repos/StealthCat/NASitron/contents/scripts/install-remote.sh?ref=main" \
  | sudo bash -s -- --public-key "$NASITRON_PUBKEY"
```

The GitHub token is used only by `curl` to retrieve the private installer. The script contains the matching root helper inline and does not download any additional NASitron files after it starts. For a reproducible installation, replace `ref=main` with a known commit SHA.

If the repository is ever made public, the shorter raw-GitHub form also works:

```bash
curl -fsSL https://raw.githubusercontent.com/StealthCat/NASitron/main/scripts/install-remote.sh \
  | sudo bash -s -- --public-key 'ssh-ed25519 AAAA...'
```

The installer:

- installs OpenSSH, `sudo`, Python, OpenZFS utilities, smartmontools, util-linux, curl, and CA certificates on apt-based Ubuntu/Debian systems
- creates or reuses a dedicated `nasitron` service account
- installs the supplied SSH public key while preserving existing authorized keys
- locks password authentication for the NASitron account
- installs an sshd per-user hardening rule that disables forwarding, tunnels, agent forwarding, X11, and TTY allocation
- installs `/usr/local/sbin/nasitron-root-helper` as `root:root` mode `0755`
- installs and validates a narrowly scoped `/etc/sudoers.d/nasitron` rule for that helper only
- validates ZFS inventory access, block-device inventory access, and non-interactive helper sudo
- enables/reloads OpenSSH and prints the detected host/IP, SSH port, and server host-key fingerprints for entry into NASitron

Run `./scripts/install-remote.sh --help` for options such as using an existing public key, a custom remote username/helper path, skipping package installation, or skipping the sshd hardening drop-in. Supplying `--public-key`, `--public-key-file`, or `NASITRON_SSH_PUBLIC_KEY` disables automatic key generation.

Most `zpool status/list/iostat`, `zfs list/get`, `/proc`, and `lsblk` data remains unprivileged. SMART, diagnostic `dmesg`, blank-disk inspection, and guarded ZFS replacement are exposed only through NASitron's root helper. Do not replace the generated sudo rule with wildcard sudo access to `zpool`, `smartctl`, `wipefs`, or a shell.

### 2. Configure NASitron

```bash
git clone https://github.com/StealthCat/NASitron.git
cd NASitron
cp .env.example .env
openssl rand -hex 32
```

Put the generated value in `.env` as `NASITRON_SECRET_KEY`. **Keep this key stable**: it encrypts stored SSH and SMTP secrets. Changing it later makes those existing encrypted values unreadable.

NASitron uses a normal web sign-in form. Configure the administrator credentials and initial TLS hostname in `.env`:

```dotenv
NASITRON_WEB_USERNAME=admin
NASITRON_WEB_PASSWORD=use-a-long-unique-password
NASITRON_TLS_HOST=nas.example.com
```

Successful sign-ins create a signed, HttpOnly session cookie; HTTP Basic authentication is not used.

Start the application:

```bash
docker compose up -d --build
```

The supplied Compose stack includes Caddy. By default it publishes HTTP/HTTPS on `0.0.0.0:80` and `0.0.0.0:443`, while the NASitron application port stays private on the Compose network. Change `NASITRON_LISTEN_IP`, `NASITRON_HTTP_PORT`, and `NASITRON_HTTPS_PORT` in `.env` to choose the Docker host bind address and ports. On first start Caddy uses an on-demand internal-CA bootstrap site, so you can browse to the NAS by its IP address or local hostname without an SNI/host mismatch. The certificate is locally issued, so your browser may still require you to trust or accept Caddy's local CA. After signing in, open **Settings → TLS Certificate Management** to upload a certificate/key or switch to ACME.

### 3. Host-key behavior

By default NASitron uses trust-on-first-use (TOFU) and stores observed host keys in `/data/known_hosts`, which is inside the persistent Docker volume. You can enable strict host-key checking per monitored server after the expected key has been learned, or pre-populate that file yourself.

## Docker configuration

| Variable | Purpose | Default |
| --- | --- | --- |
| `NASITRON_SECRET_KEY` | Required encryption-key material for stored secrets | none |
| `NASITRON_DATA_DIR` | Database/known_hosts directory | `/data` |
| `NASITRON_DATABASE_URL` | SQLAlchemy database URL | SQLite in `/data/nasitron.db` |
| `NASITRON_TIMEZONE` | Display timezone | `UTC` |
| `NASITRON_WEB_USERNAME` | Required web sign-in username | none |
| `NASITRON_WEB_PASSWORD` | Required web sign-in password (minimum 12 characters) | none |
| `NASITRON_TLS_HOST` | Hostname/IP for the bootstrap internal-CA HTTPS certificate | `localhost` |
| `NASITRON_LISTEN_IP` | Docker host IP Caddy binds to | `0.0.0.0` |
| `NASITRON_HTTP_PORT` | Docker host HTTP port mapped to Caddy port 80 | `80` |
| `NASITRON_HTTPS_PORT` | Docker host HTTPS/HTTP3 port mapped to Caddy port 443 | `443` |
| `NASITRON_SESSION_TTL_SECONDS` | Signed web-session lifetime | `43200` |

The supplied Compose file persists NASitron state in `nasitron_data` and Caddy certificate/account state in dedicated `caddy_data` and `caddy_config` volumes.

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
- The web UI uses a signed, HttpOnly session cookie after form-based sign-in; HTTP Basic authentication is disabled.
- Do not grant unrestricted passwordless sudo to the monitoring account.
- The optional drive-replacement page is intentionally the only current feature that mutates ZFS state; it is limited to `zpool replace` and records every attempt.
- Install the root helper as `root:root` and grant sudo only to that helper path; do not grant the monitoring account direct passwordless access to `zpool`, `wipefs`, `flock`, or arbitrary `smartctl` arguments.
- TOFU is convenient for initial setup; strict host-key verification is preferable once keys are known.
- Monitoring, tuning-bundle collection, and all other current NASitron functions remain read-only.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt pytest
export NASITRON_DATA_DIR="$PWD/data"
export NASITRON_SECRET_KEY="development-only-secret-0123456789abcdef"
export NASITRON_WEB_USERNAME="admin"
export NASITRON_WEB_PASSWORD="development-password"
export NASITRON_ALLOW_INSECURE_HTTP="true"
export NASITRON_ALLOW_INSECURE_MAINTENANCE="true"
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

For replacement support, install the root-owned NASitron helper and grant passwordless sudo only to that helper path as shown in `examples/nasitron.sudoers`. Do not grant direct wildcard sudo access to `wipefs`, `flock`, `zpool`, or `smartctl`.


SQLite persistence is serialized across collector threads while SSH collection remains concurrent, reducing writer contention without sacrificing multi-server polling concurrency.


## NASitron 0.5 hardening

Version 0.5 tightens the monitoring model and the administrative security boundary.

### Monitoring correctness

- Pools discovered on a server are retained as an expected-pool inventory. If a previously discovered pool disappears from the imported-pool list, NASitron raises a critical missing-pool alert instead of resolving its old alerts. An intentionally retired pool can be removed from the expected inventory on the server page.
- Partial collection is tracked per subsystem. ARC, dataset, pool-topology, I/O, drive-inventory, and SMART failures retain last-known-good values with a stale marker instead of replacing them with zeros or empty tables.
- Truncated or malformed pool inventory is a failed collection. Pool topology is considered fresh only after a non-truncated status response parses successfully.
- OpenZFS JSON-status capability is detected once per server and cached, avoiding a known-to-fail JSON probe for every pool on older OpenZFS hosts.
- Vdev state is alerted independently from overall pool health.
- SMART alerts now include prefail/past-threshold findings, self-test/error-log findings, reallocated/pending/uncorrectable sectors, NVMe media errors, and configurable NVMe endurance thresholds.
- SMART cadence advances only after a valid disk inventory and an actual SMART attempt (or a valid zero-disk inventory).

### Privileged remote helper

Do not grant the SSH account wildcard sudo access to zpool, smartctl, wipefs, or flock. Install the root-owned helper instead:

~~~bash
sudo install -o root -g root -m 0755 remote/nasitron_root_helper.py /usr/local/sbin/nasitron-root-helper
sudo install -o root -g root -m 0440 examples/nasitron.sudoers /etc/sudoers.d/nasitron
sudo visudo -cf /etc/sudoers.d/nasitron
~~~

The helper accepts only four operations: SMART collection, dmesg collection, read-only wipefs inspection, and guarded drive replacement. Replacement requires a whole-disk /dev/disk/by-id path and is revalidated under a remote lock before zpool replace is executed. The helper invokes tools with argv arrays rather than a shell and never adds -f.

### SSH identity

NASitron now has an SSH host-key enrollment page. Verify the displayed SHA256 fingerprint through a trusted channel, type it back into NASitron, and enrollment atomically updates known_hosts and enables strict verification. Known-hosts load/save failures now fail closed.

### Web security and deployment

The authenticated UI requires HTTPS by default. Docker Compose includes Caddy, publishes ports 80/443, keeps NASitron's port 8080 private, and trusts forwarded scheme/client information only inside that private Compose path.

For deliberate testing on a trusted private network only:

~~~dotenv
NASITRON_ALLOW_INSECURE_HTTP=true
NASITRON_ALLOW_INSECURE_MAINTENANCE=true
~~~

All mutating forms use CSRF protection. Request bodies are bounded. NASitron emits CSP, frame-denial, MIME-sniffing, referrer, permissions, cache, and HSTS headers.

NASitron 0.5 intentionally supports one application process/replica per data directory. WEB_CONCURRENCY must be 1 and a filesystem instance lock rejects a second process using the same SQLite data directory.

### Efficiency and reproducibility

- Metric inserts use bulk SQL inserts.
- Alert state and threshold settings are loaded once per server evaluation rather than queried once per condition.
- Historical chart queries use indexed timestamp buckets and preserve the exact newest sample.
- Redundant SQLite indexes were removed.
- Replacement inventory is cached briefly while destructive execution always revalidates.
- Support-bundle generation is POST + CSRF protected.
- Production and development dependencies are exact-pinned, the Docker image uses an exact Python patch/bookworm tag, and CI builds and starts the actual Docker image.


## NASitron 0.6 authentication and HTTPS

Version 0.6 replaces browser HTTP Basic authentication with a normal NASitron sign-in page. Credentials still come from `NASITRON_WEB_USERNAME` and `NASITRON_WEB_PASSWORD`, but successful authentication creates a signed, HttpOnly, SameSite=Lax session cookie. Sessions expire after `NASITRON_SESSION_TTL_SECONDS` (12 hours by default), are invalidated when the configured web credentials change, and failed sign-ins are rate-limited.

The Compose deployment includes Caddy 2.11.4 as the only public-facing service. NASitron itself listens only on the private Compose network. Caddy provides an internal-CA bootstrap certificate and redirects normal HTTP traffic to HTTPS.

The **Settings → TLS Certificate Management** panel supports three modes:

- **Internal CA** for private/lab deployments and initial bootstrap.
- **Uploaded certificate** using a PEM certificate chain and matching unencrypted PEM private key. NASitron verifies the pair before Caddy reloads it.
- **ACME managed certificate** using a configurable HTTPS ACME directory URL and optional custom CA root certificate. This works with public ACME services or private/internal ACME servers.

TLS changes are sent to Caddy's admin API over the private Compose network using the Caddyfile adapter. Caddy applies valid changes without downtime and retains the old active configuration if a reload fails. The generated Caddyfile is also stored in the NASitron data volume so the selected TLS mode survives container restarts.

For public HTTP-01 or TLS-ALPN ACME validation, the ACME service must be able to reach the configured hostname on ports 80/443. Private ACME deployments may additionally upload the CA root that signs the ACME directory endpoint.


## NASitron 0.6.3 bootstrap TLS fix

Bootstrap/internal TLS now uses Caddy's internal on-demand issuer on an HTTPS catch-all site. This prevents TLS handshake failures when `NASITRON_TLS_HOST` is left at `localhost` but the UI is opened through the NAS IP address or another local hostname. ACME mode remains hostname-specific. Compose also detects the legacy persisted exact-host internal-TLS configuration and falls back to the new bootstrap configuration so existing installations can recover without deleting their data volume.


## NASitron 0.6.4 remote NAS installer

`scripts/install-remote.sh` automates remote-host preparation for NASitron. It is designed to be safely re-run, defaults to key-only SSH for the dedicated monitoring account, installs the root helper and exact helper-only sudo policy, validates the resulting access, and prints the connection details and SSH host-key fingerprints needed by the NASitron UI.


## NASitron 0.6.5 one-line remote installation

The remote installer is now fully self-contained and supports direct stdin execution with `curl ... | sudo bash -s -- ...`. Its privileged Python helper is embedded in the installer and is syntax-checked before installation, so a piped install does not need a repository checkout or a second network fetch. For private-repository use, authenticate only the initial GitHub Contents API request; the token does not need to be passed into the root shell.


## NASitron 0.6.6 locally hosted installer

Every NASitron instance now exposes the packaged self-contained installer at `/install-remote.sh`. The endpoint is public so an unenrolled NAS can retrieve it, carries no instance credentials or secrets, and is delivered through the same HTTPS front end as the rest of NASitron. The Docker image now packages `scripts/install-remote.sh`, the Settings page links to it, and the Docker smoke test downloads the served script and executes its `--help` path.


## NASitron 0.6.7 Add Server installation guidance

The **Add Server** page now includes environment-specific remote-host preparation instructions using the locally served `/install-remote.sh` curl-to-bash workflow. The page derives the installer URL from the current NASitron request and reads the active TLS mode. ACME/uploaded-certificate installations show the direct HTTPS command, while internal-CA installations show the Caddy root-CA export/transfer steps and a `curl --cacert` command rather than recommending insecure certificate bypass. The page also walks through SSH key generation and the NASitron connection settings to use after installation.


## NASitron 0.6.8 zero-prep remote enrollment

The normal remote-host installation flow is now a single paste. With trusted HTTPS, run `curl -fsSL https://NASITRON/install-remote.sh | sudo bash`. The installer generates a dedicated Ed25519 keypair, installs the public key, and prints the private key once for the Add Server form. Re-running automatic-key mode removes only the prior installer-generated key before authorizing the new one; unrelated authorized keys are preserved.

For internal-CA deployments, the Add Server page generates a one-line pinned-hash command: it downloads the installer to a temporary file with curl's certificate check bypassed, verifies the exact installer SHA-256 shown by the authenticated NASitron instance, and only then executes the verified file as root. This avoids a direct unverified `curl -k | sudo bash` pipeline while retaining a single-paste workflow.

The Add Server page is also centered consistently and Remote Host Preparation is now a collapsible header.


## NASitron 0.6.9 automatic server registration

The Add Server enrollment command now performs true end-to-end enrollment. NASitron creates a short-lived one-time enrollment record and an Ed25519 keypair before displaying the command. Only the public key and one-time enrollment proof are passed to the remote installer; the private key stays encrypted inside NASitron.

After the installer configures the remote NAS, it sends a signed callback containing the detected hostname/IP, SSH port, username, and SSH host-key fingerprint. NASitron verifies the HMAC proof, consumes the enrollment once, creates or updates the matching Server record, enables monitoring, and begins collection. The Add Server page polls the enrollment status and redirects to the new server automatically when registration completes.

For internal-CA deployments the callback may use curl's TLS verification bypass, but the registration payload is HMAC-signed with a secret that is never transmitted in the callback. The one-time enrollment expires after 30 minutes and cannot be replayed after successful registration.


## NASitron 0.6.10 seamless enrollment installer

The Add Server workflow now serves a one-time enrollment-specific installer script. The visible command no longer carries SSH public-key, callback URL, or enrollment-secret arguments. With trusted HTTPS it is simply a curl of the one-time install URL piped to sudo bash. The downloaded script already contains the NASitron-generated public key and callback context, performs remote setup, and registers the host automatically when it finishes.

Internal-CA deployments keep the same one-command experience using a temporary-file download plus SHA-256 verification before execution. The hash is calculated from the exact one-time enrollment script, not the generic installer.

The Settings page now points administrators to Add Server for automatic enrollment so the generic manual installer is not confused with the self-registering path.


## NASitron 0.6.11 enrollment reliability

Automatic registration now occurs immediately after the remote account, SSH configuration, root helper and sudo policy are installed. ZFS inventory, block-device inventory and diagnostic dmesg checks run afterward and are warnings rather than blockers, so a diagnostic permission limitation can no longer leave Add Server stuck waiting despite a usable SSH setup.

The enrollment callback now retries transient failures and has explicit connect/request timeouts. Add Server continues polling but changes its status text after roughly 30 seconds to direct the administrator to the installer terminal for the explicit registration-complete line or callback error.


## NASitron 0.6.13 tabbed settings

The Settings page is organized into independent tabs for **Email**, **Health**, **History**, **Enrollment**, and **HTTPS**. Email, health-threshold, and history-retention forms save only their own settings so changing one category cannot overwrite values in another. TLS keeps its dedicated apply workflow, and Enrollment links to the one-time automatic Add Server flow.

The active tab is reflected in the URL and preserved after Email/Health/History saves, SMTP tests, and TLS changes.


## NASitron 0.6.14 ZFS property visibility

NASitron now collects and displays OpenZFS configuration properties alongside normal pool and dataset telemetry. Pool views show pool properties with their effective value and source, highlighting values that are explicitly configured instead of defaults.

Dataset and zvol views collect explicitly set properties only, so settings such as compression, recordsize, atime, sync, quota, reservation, dedup and related options are visible without storing every inherited/default property on every dataset.

Property collection participates in partial-collection handling: if a property query fails, NASitron retains the previous property values and marks that property subsystem stale instead of erasing the last known configuration.


## NASitron 0.6.15 local user administration

NASitron now stores web-console users in its database and includes an administrator-only **Users** page for creating, editing, disabling, promoting/demoting, deleting, and changing passwords for local accounts.

On an empty user database, the existing `NASITRON_WEB_USERNAME` and `NASITRON_WEB_PASSWORD` values seed the initial administrator. After the first account exists, the database is authoritative: changing that administrator's password in the UI is not overwritten by the environment values on restart.

Passwords are stored as salted scrypt hashes. Password changes, username changes, role changes, and disabling an account increment that user's session version, immediately invalidating existing signed sessions for that account. NASitron prevents an administrator from deleting or disabling their own account or removing their own administrator role, and it prevents removal of the last enabled administrator.


## NASitron 0.6.16 SMTP and Mailjet-compatible email delivery

The **Settings → Email** tab now supports two selectable outbound email transports:

- **SMTP relay** — the existing host/port/username/password transport with STARTTLS or implicit TLS.
- **Mailjet-compatible API** — HTTPS Send API v3.1 style delivery using Basic Authentication with an API key and secret key.

The Mailjet API URL is configurable and defaults to `https://api.mailjet.com/v3.1/send`, allowing use of Mailjet itself or another HTTPS service that implements the same request format. Sender and recipient settings are shared between the transports. Mailjet API credentials and SMTP passwords are encrypted at rest with `NASITRON_SECRET_KEY`.

The test-email action uses whichever transport is currently selected, and normal alert notification retries/backoff are shared by both transports.
