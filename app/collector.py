from __future__ import annotations

import io
import json
import shlex
import socket
from pathlib import Path
from typing import Any

import paramiko

from .config import KNOWN_HOSTS_PATH
from .crypto import decrypt
from .models import Server


class CollectorError(RuntimeError):
    pass


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

    def _load_private_key(self, text: str, passphrase: str | None):
        key_types = [paramiko.Ed25519Key, paramiko.ECDSAKey, paramiko.RSAKey]
        last_error: Exception | None = None
        for key_type in key_types:
            try:
                return key_type.from_private_key(io.StringIO(text), password=passphrase)
            except Exception as exc:
                last_error = exc
        raise CollectorError(f"Unable to parse SSH private key: {last_error}")

    def connect(self) -> None:
        client = paramiko.SSHClient()
        Path(KNOWN_HOSTS_PATH).parent.mkdir(parents=True, exist_ok=True)
        if not Path(KNOWN_HOSTS_PATH).exists():
            Path(KNOWN_HOSTS_PATH).touch(mode=0o600)
        try:
            client.load_host_keys(str(KNOWN_HOSTS_PATH))
        except Exception:
            pass

        client.set_missing_host_key_policy(
            paramiko.RejectPolicy() if self.server.strict_host_key else paramiko.AutoAddPolicy()
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
            kwargs["password"] = decrypt(self.server.password_enc)
        else:
            private_key = decrypt(self.server.private_key_enc)
            if not private_key:
                raise CollectorError("SSH key authentication selected but no private key is configured")
            kwargs["pkey"] = self._load_private_key(
                private_key, decrypt(self.server.private_key_passphrase_enc)
            )

        try:
            client.connect(**kwargs)
            if not self.server.strict_host_key:
                try:
                    client.save_host_keys(str(KNOWN_HOSTS_PATH))
                except Exception:
                    pass
        except (paramiko.SSHException, socket.error, OSError) as exc:
            client.close()
            raise CollectorError(f"SSH connection failed: {exc}") from exc
        self.client = client

    def close(self) -> None:
        if self.client:
            self.client.close()
            self.client = None

    def run(self, command: str, timeout: int = 30) -> dict[str, Any]:
        if not self.client:
            raise CollectorError("SSH connection is not open")
        full_command = f"export PATH={self.REMOTE_PATH}; {command}"
        try:
            stdin, stdout, stderr = self.client.exec_command(full_command, timeout=timeout)
            stdin.close()
            out = stdout.read().decode("utf-8", errors="replace")
            err = stderr.read().decode("utf-8", errors="replace")
            code = stdout.channel.recv_exit_status()
            return {"stdout": out, "stderr": err, "exit": code, "command": command}
        except Exception as exc:
            return {"stdout": "", "stderr": str(exc), "exit": 255, "command": command}

    def collect(self, include_smart: bool = False) -> dict[str, Any]:
        raw: dict[str, Any] = {}
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
            "arcstats": "cat /proc/spl/kstat/zfs/arcstats 2>/dev/null",
            "zpool_iostat": "zpool iostat -H -p 1 2 2>/dev/null",
            "lsblk": "lsblk -J -b -o NAME,KNAME,PATH,TYPE,SIZE,ROTA,TRAN,MODEL,SERIAL,FSTYPE,MOUNTPOINTS",
            "df": "df -P -B1 2>/dev/null",
            "services": "printf 'zfs.target='; systemctl is-active zfs.target 2>/dev/null; printf 'zfs-zed.service='; systemctl is-active zfs-zed.service 2>/dev/null",
        }
        for key, command in commands.items():
            raw[key] = self.run(command, timeout=45 if key == "zpool_iostat" else 20)

        if raw["zpool_list"]["exit"] != 0:
            raise CollectorError(
                "Unable to query zpool list: " + (raw["zpool_list"]["stderr"] or raw["zpool_list"]["stdout"]).strip()
            )

        pools = []
        for line in raw["zpool_list"]["stdout"].splitlines():
            if line.strip():
                pools.append(line.split("\t", 1)[0].split()[0])
        raw["zpool_status"] = {}
        for pool in pools:
            raw["zpool_status"][pool] = self.run(
                f"zpool status -P -L {shlex.quote(pool)}", timeout=20
            )

        raw["smart"] = {}
        if include_smart and raw["lsblk"]["exit"] == 0:
            try:
                block = json.loads(raw["lsblk"]["stdout"])
            except json.JSONDecodeError:
                block = {}
            for device in self._disk_paths(block.get("blockdevices", [])):
                prefix = "sudo -n " if self.server.sudo_for_smart else ""
                raw["smart"][device] = self.run(
                    f"{prefix}smartctl -a -j {shlex.quote(device)} 2>/dev/null", timeout=30
                )
        return raw

    def deep_collect(self) -> dict[str, Any]:
        raw = self.collect(include_smart=True)
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
            "dmesg_zfs": "(sudo -n dmesg 2>/dev/null || dmesg 2>/dev/null || true) | grep -Ei 'zfs|spl|ata|nvme|scsi|I/O error' | tail -n 500",
        }
        for key, command in commands.items():
            raw[key] = self.run(command, timeout=90)
        return raw

    @staticmethod
    def _disk_paths(nodes: list[dict[str, Any]]) -> list[str]:
        paths: list[str] = []
        for node in nodes:
            if node.get("type") == "disk" and node.get("path"):
                paths.append(str(node["path"]))
            paths.extend(SSHCollector._disk_paths(node.get("children") or []))
        return sorted(set(paths))
