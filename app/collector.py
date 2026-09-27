from __future__ import annotations

import base64
import fcntl
import hashlib
import io
import json
import os
import shlex
import socket
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import paramiko

from .disk_io import DISK_IO_COMMAND

from .config import (
    KNOWN_HOSTS_PATH,
    MAX_DIAGNOSTIC_OUTPUT_BYTES,
    MAX_REMOTE_OUTPUT_BYTES,
    REMOTE_HELPER_PATH,
)
from .crypto import decrypt
from .models import Server


class CollectorError(RuntimeError):
    pass


class _RecordHostKeyPolicy(paramiko.MissingHostKeyPolicy):
    def missing_host_key(self, client, hostname, key):
        client.get_host_keys().add(hostname, key.get_name(), key)


@contextmanager
def _known_hosts_lock() -> Iterator[None]:
    lock_path = Path(str(KNOWN_HOSTS_PATH) + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _host_token(host: str, port: int) -> str:
    return host if port == 22 else f"[{host}]:{port}"


def _load_host_keys(path: Path) -> paramiko.HostKeys:
    keys = paramiko.HostKeys()
    if path.exists() and path.stat().st_size:
        try:
            keys.load(str(path))
        except Exception as exc:
            raise CollectorError(f"Unable to load SSH known_hosts: {exc}") from exc
    return keys


def _save_host_keys_atomic(keys: paramiko.HostKeys, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=".nasitron-known-hosts-",
        dir=str(path.parent),
        delete=False,
    )
    temp_path = Path(handle.name)
    handle.close()
    try:
        keys.save(str(temp_path))
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, path)
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise CollectorError(f"Unable to persist SSH host key: {exc}") from exc


class SSHCollector:
    REMOTE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    def __init__(self, server: Server):
        self.server = server
        self.client: paramiko.SSHClient | None = None

    def __enter__(self) -> "SSHCollector":
        self.connect()
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

    @staticmethod
    def parse_private_key(text: str, passphrase: str | None):
        key_types = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
        last_error: Exception | None = None
        for key_type in key_types:
            try:
                return key_type.from_private_key(io.StringIO(text), password=passphrase)
            except Exception as exc:
                last_error = exc
        raise CollectorError(f"Unable to parse SSH private key: {last_error}")

    @staticmethod
    def fetch_host_key(server: Server) -> dict[str, str]:
        sock = socket.create_connection((server.host, server.port), timeout=10)
        transport = paramiko.Transport(sock)
        try:
            transport.start_client(timeout=10)
            key = transport.get_remote_server_key()
            digest = hashlib.sha256(key.asbytes()).digest()
            fingerprint = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
            return {
                "algorithm": key.get_name(),
                "key_base64": key.get_base64(),
                "fingerprint": fingerprint,
                "host_token": _host_token(server.host, server.port),
            }
        except Exception as exc:
            raise CollectorError(f"Unable to fetch SSH host key: {exc}") from exc
        finally:
            transport.close()
            sock.close()

    @staticmethod
    def enroll_host_key(server: Server, expected_fingerprint: str) -> dict[str, str]:
        candidate = SSHCollector.fetch_host_key(server)
        if candidate["fingerprint"] != expected_fingerprint:
            raise CollectorError(
                "The SSH host key changed between inspection and enrollment."
            )
        key = paramiko.PKey.from_type_string(
            candidate["algorithm"],
            base64.b64decode(candidate["key_base64"]),
        )
        with _known_hosts_lock():
            keys = _load_host_keys(Path(KNOWN_HOSTS_PATH))
            keys.add(candidate["host_token"], candidate["algorithm"], key)
            _save_host_keys_atomic(keys, Path(KNOWN_HOSTS_PATH))
        return candidate

    def connect(self) -> None:
        client = paramiko.SSHClient()
        with _known_hosts_lock():
            keys = _load_host_keys(Path(KNOWN_HOSTS_PATH))
        client._host_keys = keys
        client.set_missing_host_key_policy(
            paramiko.RejectPolicy() if self.server.strict_host_key else _RecordHostKeyPolicy()
        )

        kwargs: dict[str, Any] = {
            "hostname": self.server.host,
            "port": self.server.port,
            "username": self.server.username,
            "timeout": 10,
            "banner_timeout": 10,
            "auth_timeout": 10,
            "look_for_keys": False,
            "allow_agent": False,
        }
        if self.server.auth_type == "password":
            password = decrypt(self.server.password_enc)
            if not password:
                raise CollectorError("SSH password authentication selected but no password is configured")
            kwargs["password"] = password
        else:
            private_key = decrypt(self.server.private_key_enc)
            if not private_key:
                raise CollectorError("SSH key authentication selected but no private key is configured")
            kwargs["pkey"] = self.parse_private_key(
                private_key, decrypt(self.server.private_key_passphrase_enc)
            )

        try:
            client.connect(**kwargs)
            if not self.server.strict_host_key:
                with _known_hosts_lock():
                    latest = _load_host_keys(Path(KNOWN_HOSTS_PATH))
                    for hostname, entries in client.get_host_keys().items():
                        for keytype, key in entries.items():
                            latest.add(hostname, keytype, key)
                    _save_host_keys_atomic(latest, Path(KNOWN_HOSTS_PATH))
        except (paramiko.SSHException, socket.error, OSError, CollectorError) as exc:
            client.close()
            if isinstance(exc, CollectorError):
                raise
            raise CollectorError(f"SSH connection failed: {exc}") from exc
        self.client = client

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    @staticmethod
    def _append_limited(
        parts: list[bytes],
        chunk: bytes,
        current_size: int,
        limit: int,
    ) -> tuple[int, bool]:
        if current_size >= limit:
            return current_size, True
        remaining = limit - current_size
        parts.append(chunk[:remaining])
        return current_size + min(len(chunk), remaining), len(chunk) > remaining

    def run(
        self,
        command: str,
        timeout: int = 30,
        *,
        max_output_bytes: int | None = None,
    ) -> dict[str, Any]:
        if not self.client:
            raise CollectorError("SSH connection is not open")

        limit = max_output_bytes or MAX_REMOTE_OUTPUT_BYTES
        full_command = (
            f"export PATH={self.REMOTE_PATH}; export LC_ALL=C; export LANG=C; export TZ=UTC; "
            + command
        )

        try:
            stdin, stdout, _stderr = self.client.exec_command(full_command, timeout=timeout)
            stdin.close()
            channel = stdout.channel
            deadline = time.monotonic() + timeout
            out_parts: list[bytes] = []
            err_parts: list[bytes] = []
            out_size = 0
            err_size = 0
            out_truncated = False
            err_truncated = False

            while True:
                progressed = False
                while channel.recv_ready():
                    chunk = channel.recv(65536)
                    out_size, truncated = self._append_limited(
                        out_parts, chunk, out_size, limit
                    )
                    out_truncated = out_truncated or truncated
                    progressed = True
                while channel.recv_stderr_ready():
                    chunk = channel.recv_stderr(65536)
                    err_size, truncated = self._append_limited(
                        err_parts, chunk, err_size, limit
                    )
                    err_truncated = err_truncated or truncated
                    progressed = True

                if (
                    channel.exit_status_ready()
                    and not channel.recv_ready()
                    and not channel.recv_stderr_ready()
                ):
                    break
                if time.monotonic() >= deadline:
                    channel.close()
                    raise TimeoutError(f"Remote command timed out after {timeout} seconds")
                if not progressed:
                    time.sleep(0.01)

            code = channel.recv_exit_status()
            out = b"".join(out_parts).decode("utf-8", errors="replace")
            err = b"".join(err_parts).decode("utf-8", errors="replace")
            if out_truncated:
                out += f"\n[NASitron truncated stdout at {limit} bytes]\n"
            if err_truncated:
                err += f"\n[NASitron truncated stderr at {limit} bytes]\n"
            return {
                "stdout": out,
                "stderr": err,
                "exit": code,
                "command": command,
                "stdout_truncated": out_truncated,
                "stderr_truncated": err_truncated,
            }
        except Exception as exc:
            return {
                "stdout": "",
                "stderr": str(exc),
                "exit": 255,
                "command": command,
                "stdout_truncated": False,
                "stderr_truncated": False,
            }

    @staticmethod
    def _strict_pool_names(result: dict[str, Any]) -> list[str]:
        if result.get("exit") != 0:
            raise CollectorError(
                "Unable to query zpool list: "
                + (result.get("stderr") or result.get("stdout") or "").strip()
            )
        if result.get("stdout_truncated") or result.get("stderr_truncated"):
            raise CollectorError("zpool list output exceeded the configured capture limit")
        pools: list[str] = []
        for line in result.get("stdout", "").splitlines():
            if not line.strip():
                continue
            parts = line.split("\t")
            if len(parts) != 8 or not parts[0]:
                raise CollectorError("zpool list returned malformed output")
            pools.append(parts[0])
        if len(pools) != len(set(pools)):
            raise CollectorError("zpool list returned duplicate pool names")
        return pools

    def collect(self, include_smart: bool = False) -> dict[str, Any]:
        raw: dict[str, Any] = {
            "smart_sampled": include_smart,
            "capabilities": {},
        }
        commands = {
            "hostname": "hostname -f 2>/dev/null || hostname",
            "os_release": "cat /etc/os-release 2>/dev/null",
            "kernel": "uname -r",
            "uptime": "cat /proc/uptime",
            "loadavg": "cat /proc/loadavg",
            "meminfo": "cat /proc/meminfo",
            "zfs_version": "zfs --version 2>&1",
            "zpool_list": "zpool list -Hp -o name,size,alloc,free,frag,cap,dedup,health",
            "zfs_list": "zfs list -Hp -t filesystem,volume -o name,type,used,avail,refer,mountpoint,compressratio,logicalused,usedbysnapshots",
            "zfs_get": "zfs get -H -p -o name,property,value,source -s local,received all",
            "arcstats": "cat /proc/spl/kstat/zfs/arcstats 2>/dev/null",
            "zpool_iostat": "zpool iostat -H -p 1 2 2>/dev/null",
            "disk_io": DISK_IO_COMMAND,
            "lsblk": "lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,UUID,PTTYPE,PARTTYPE,MOUNTPOINTS",
            "services": "printf 'zfs.target='; systemctl is-active zfs.target 2>/dev/null || true; printf 'zfs-zed.service='; systemctl is-active zfs-zed.service 2>/dev/null || true",
        }
        for key, command in commands.items():
            raw[key] = self.run(command, timeout=45 if key == "zpool_iostat" else 20)

        if include_smart:
            raw["zfs_snapshots"] = self.run(
                "zfs list -Hp -t snapshot -o name,creation,used,refer -s creation", timeout=45)
        pools = self._strict_pool_names(raw["zpool_list"])

        raw["zpool_get"] = {}
        raw["zpool_status"] = {}
        raw["zpool_status_json"] = {}
        json_capability = self.server.zpool_status_json_supported
        for index, pool in enumerate(pools):
            quoted = shlex.quote(pool)
            raw["zpool_get"][pool] = self.run(
                f"zpool get -H -p -o name,property,value,source all {quoted}",
                timeout=20,
            )
            raw["zpool_status"][pool] = self.run(
                f"zpool status -v -p -P -L {quoted}", timeout=20
            )

            if json_capability is False:
                raw["zpool_status_json"][pool] = {
                    "stdout": "",
                    "stderr": "unsupported",
                    "exit": 2,
                    "stdout_truncated": False,
                    "stderr_truncated": False,
                }
                continue

            json_result = self.run(
                f"zpool status -j --json-int -P -L {quoted}", timeout=20
            )
            raw["zpool_status_json"][pool] = json_result
            if json_capability is None and index == 0:
                try:
                    payload = json.loads(json_result.get("stdout", ""))
                    valid_json = isinstance(payload.get("pools"), dict)
                except (json.JSONDecodeError, AttributeError):
                    valid_json = False
                json_capability = bool(
                    json_result.get("exit") == 0
                    and not json_result.get("stdout_truncated")
                    and valid_json
                )

        raw["capabilities"]["zpool_status_json"] = json_capability

        raw["smart"] = {}
        raw["smart_inventory_ok"] = raw["lsblk"]["exit"] == 0 and not raw["lsblk"].get(
            "stdout_truncated"
        )
        if include_smart and raw["smart_inventory_ok"]:
            try:
                block = json.loads(raw["lsblk"]["stdout"])
            except json.JSONDecodeError:
                block = {}
                raw["smart_inventory_ok"] = False
            if raw["smart_inventory_ok"]:
                helper = shlex.quote(REMOTE_HELPER_PATH)
                for device in self._disk_paths(block.get("blockdevices", [])):
                    if self.server.sudo_for_smart:
                        command = (
                            f"sudo -n {helper} smartctl {shlex.quote(device)} 2>/dev/null"
                        )
                    else:
                        command = f"smartctl -a -j {shlex.quote(device)} 2>/dev/null"
                    raw["smart"][device] = self.run(command, timeout=45)

        raw["smart_attempted_count"] = len(raw["smart"])
        return raw

    def deep_collect(self) -> dict[str, Any]:
        raw = self.collect(include_smart=True)
        helper = shlex.quote(REMOTE_HELPER_PATH)
        commands = {
            "zpool_status_verbose": "zpool status -P -L -v",
            "zpool_get_all": "zpool get -Hp all",
            "zfs_get_all": "zfs get -Hp all",
            "zfs_list_extended": "zfs list -Hp -t all -o name,type,used,avail,refer,logicalused,logicalreferenced,usedbysnapshots,usedbydataset,usedbychildren,usedbyrefreservation,compressratio,mountpoint",
            "zpool_iostat_verbose": "zpool iostat -v -p 1 2",
            "arc_summary": "command -v arc_summary >/dev/null && arc_summary || true",
            "zfs_module_parameters": "grep -H . /sys/module/zfs/parameters/* 2>/dev/null || true",
            "zfs_kstats_arc": "cat /proc/spl/kstat/zfs/arcstats 2>/dev/null || true",
            "zfs_kstats_dbuf": "cat /proc/spl/kstat/zfs/dbufstats 2>/dev/null || true",
            "zfs_kstats_zfetch": "cat /proc/spl/kstat/zfs/zfetchstats 2>/dev/null || true",
            "zfs_kstats_vdev_cache": "cat /proc/spl/kstat/zfs/vdev_cache_stats 2>/dev/null || true",
            "zfs_modinfo": "modinfo zfs 2>/dev/null || true",
            "lscpu": "lscpu",
            "memory": "free -b; echo; vmstat -s",
            "lsblk_full": "lsblk -J -b -O",
            "mounts": "findmnt -J -b 2>/dev/null || mount",
            "sysctl_relevant": "sysctl vm.dirty_background_bytes vm.dirty_background_ratio vm.dirty_bytes vm.dirty_ratio vm.swappiness vm.min_free_kbytes 2>/dev/null || true",
            "dmesg_zfs": f"(sudo -n {helper} dmesg 2>/dev/null || true) | grep -Ei 'zfs|spl|ata|nvme|scsi|I/O error' | tail -n 500",
        }
        for key, command in commands.items():
            raw[key] = self.run(
                command,
                timeout=90,
                max_output_bytes=MAX_DIAGNOSTIC_OUTPUT_BYTES,
            )
        return raw

    @staticmethod
    def _disk_paths(nodes: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        for node in nodes:
            if node.get("type") == "disk" and node.get("path"):
                paths.append(str(node["path"]))
            paths.extend(SSHCollector._disk_paths(node.get("children") or []))
        return sorted(set(paths))
