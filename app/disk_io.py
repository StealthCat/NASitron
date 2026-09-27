"""Linux block-device I/O sampled without requiring sysstat or root access."""
import math

# Two snapshots from the same SSH command; uptime provides a monotonic interval.
DISK_IO_COMMAND = (
    "cat /proc/sys/kernel/random/boot_id /proc/uptime /proc/diskstats && "
    "sleep 1 && printf '\\nNASITRON_DISK_IO_NEXT\\n' && "
    "cat /proc/sys/kernel/random/boot_id /proc/uptime /proc/diskstats"
)
IO_KEYS = ("read_bps", "write_bps", "read_iops", "write_iops",
           "read_latency_ms", "write_latency_ms", "busy_pct", "queue_depth")


def parse_disk_io(text):
    """Return per-kernel-device rates, rejecting resets and incomplete samples.

    Linux diskstats sectors are always 512 bytes, regardless of device sectors.
    See https://docs.kernel.org/admin-guide/iostats.html.
    """
    def sample(part):
        lines = part.strip().splitlines()
        if len(lines) < 3:
            raise ValueError("Incomplete disk I/O sample")
        boot_id = lines[0].strip()
        uptime = float(lines[1].split()[0])
        if not math.isfinite(uptime):
            raise ValueError("Invalid disk I/O sample time")
        devices = {}
        for line in lines[2:]:
            fields = line.split()
            if len(fields) < 14:
                raise ValueError("Malformed diskstats row")
            counters = [int(v) for v in fields[3:14]]
            if min(counters) < 0:
                raise ValueError("Negative diskstats counter")
            devices[fields[2]] = (fields[:2], counters)
        return boot_id, uptime, devices

    parts = text.split("NASITRON_DISK_IO_NEXT")
    if len(parts) != 2:
        raise ValueError("Missing second disk I/O sample")
    boot_a, time_a, before = sample(parts[0])
    boot_b, time_b, after = sample(parts[1])
    seconds = time_b - time_a
    if boot_a != boot_b or not 0 < seconds <= 60:
        raise ValueError("Disk I/O sample crossed a restart or invalid interval")
    result = {}
    for name, (device_id, current) in after.items():
        old = before.get(name)
        if not old or old[0] != device_id:
            continue
        delta = [a - b for a, b in zip(current, old[1])]
        # In-flight I/O (index 8) is a gauge, not a monotonically increasing counter.
        if any(n < 0 for i, n in enumerate(delta) if i != 8):
            continue
        result[name] = {
            "read_bps": delta[2] * 512 / seconds,
            "write_bps": delta[6] * 512 / seconds,
            "read_iops": delta[0] / seconds,
            "write_iops": delta[4] / seconds,
            "read_latency_ms": delta[3] / delta[0] if delta[0] else None,
            "write_latency_ms": delta[7] / delta[4] if delta[4] else None,
            "busy_pct": min(100.0, delta[9] / (seconds * 10)),
            "queue_depth": delta[10] / (seconds * 1000),
            "sample_seconds": seconds,
        }
    return result
