"""Offline password-encrypted SQLite backup. Run: python -m app.backup --help."""

import argparse
import base64
import getpass
import io
import json
import os
from pathlib import Path, PurePosixPath
import sqlite3
import tarfile
import tempfile

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"NASITRON-BACKUP-1\n"
MAX_BYTES = 512 * 1024 * 1024


def cipher(password, salt):
    key = Scrypt(salt=salt, length=32, n=2**17, r=8, p=1).derive(password.encode())
    return Fernet(base64.urlsafe_b64encode(key))


def add_bytes(archive, name, data):
    info = tarfile.TarInfo(name)
    info.mode = 0o600
    info.size = len(data)
    archive.addfile(info, io.BytesIO(data))


def create(destination, password):
    from sqlalchemy.engine import make_url
    from .config import DATA_DIR, DATABASE_URL, SECRET_KEY, KNOWN_HOSTS_PATH
    from .instance_lock import InstanceLock

    if len(password) < 12:
        raise ValueError("Use a backup password of at least 12 characters")
    url = make_url(DATABASE_URL)
    if url.drivername != "sqlite" or not url.database:
        raise ValueError("This backup utility supports file-backed SQLite only")
    if not SECRET_KEY:
        raise ValueError("NASITRON_SECRET_KEY is required")
    database = Path(url.database).resolve()
    if not database.is_file():
        raise ValueError("Database does not exist")
    destination = Path(destination).resolve()
    if destination.is_relative_to(DATA_DIR.resolve()):
        raise ValueError("Save backups outside the data directory")
    lock = InstanceLock()
    lock.acquire()
    try:
        with tempfile.TemporaryDirectory() as temp:
            snapshot = Path(temp) / "nasitron.db"
            with (
                sqlite3.connect(str(database)) as source,
                sqlite3.connect(str(snapshot)) as target,
            ):
                source.backup(target)
            payload = io.BytesIO()
            with tarfile.open(fileobj=payload, mode="w") as archive:
                files = [(snapshot, "nasitron.db")]
                for path in DATA_DIR.rglob("*"):
                    if path.is_symlink():
                        raise ValueError(
                            "Symlinks in data directory require manual backup"
                        )
                    if (
                        path.is_file()
                        and path.resolve()
                        not in {
                            database,
                            Path(str(database) + "-wal"),
                            Path(str(database) + "-shm"),
                        }
                        and path.name != "nasitron.instance.lock"
                    ):
                        files.append((path, path.relative_to(DATA_DIR).as_posix()))
                if (
                    KNOWN_HOSTS_PATH.is_file()
                    and not KNOWN_HOSTS_PATH.resolve().is_relative_to(
                        DATA_DIR.resolve()
                    )
                ):
                    files.append((KNOWN_HOSTS_PATH, "known_hosts"))
                if sum(p.stat().st_size for p, _ in files) > MAX_BYTES:
                    raise ValueError(
                        "Backup exceeds 512 MiB; use an external encrypted filesystem backup"
                    )
                names = set()
                for path, name in files:
                    if name in names or name in {
                        "recovery-environment.json",
                        "recovery-secret-key",
                    }:
                        raise ValueError("Conflicting backup member name")
                    names.add(name)
                    add_bytes(archive, name, path.read_bytes())
                env = {k: v for k, v in os.environ.items() if k.startswith("NASITRON_")}
                add_bytes(
                    archive,
                    "recovery-environment.json",
                    json.dumps(env, indent=2).encode(),
                )
                add_bytes(archive, "recovery-secret-key", SECRET_KEY.encode())
            salt = os.urandom(16)
            data = MAGIC + salt + cipher(password, salt).encrypt(payload.getvalue())
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                out.flush()
                os.fsync(out.fileno())
    finally:
        lock.release()


def restore(source, destination, password):
    source, destination = Path(source), Path(destination)
    if destination.exists():
        raise ValueError(
            "Restore destination must not exist; an existing installation is never overwritten"
        )
    if source.stat().st_size > MAX_BYTES * 2:
        raise ValueError("Archive exceeds size limit")
    raw = source.read_bytes()
    if not raw.startswith(MAGIC):
        raise ValueError("Not a NASitron backup archive")
    offset = len(MAGIC)
    try:
        payload = cipher(password, raw[offset : offset + 16]).decrypt(
            raw[offset + 16 :]
        )
    except InvalidToken as exc:
        raise ValueError("Wrong password or damaged archive") from exc
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:") as archive:
        members = archive.getmembers()
        names = set()
        for m in members:
            name = PurePosixPath(m.name)
            if (
                not m.isfile()
                or name.is_absolute()
                or ".." in name.parts
                or m.name in names
                or m.size > MAX_BYTES
            ):
                raise ValueError("Unsafe or duplicate archive member")
            names.add(m.name)
        if (
            not {"nasitron.db", "recovery-secret-key"} <= names
            or sum(m.size for m in members) > MAX_BYTES
        ):
            raise ValueError("Incomplete or oversized archive")
        destination.parent.mkdir(parents=True, exist_ok=True)
        # Stage privately and publish only after all files and SQLite integrity validate.
        with tempfile.TemporaryDirectory(dir=destination.parent) as temp:
            root = Path(temp) / "restored"
            root.mkdir(mode=0o700)
            for m in members:
                path = root / m.name
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                with os.fdopen(
                    os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), "wb"
                ) as out:
                    out.write(archive.extractfile(m).read())
            with sqlite3.connect(str(root / "nasitron.db")) as db:
                if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("Restored database failed integrity check")
            # mkdir is exclusive, so races cannot replace another destination.
            destination.mkdir(mode=0o700)
            for path in root.iterdir():
                path.rename(destination / path.name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action", required=True)
    c = sub.add_parser("create")
    c.add_argument("archive")
    r = sub.add_parser("restore")
    r.add_argument("archive")
    r.add_argument("directory")
    args = parser.parse_args()
    password = getpass.getpass("Backup password: ")
    if args.action == "create":
        if password != getpass.getpass("Confirm password: "):
            raise ValueError("Passwords do not match")
        create(args.archive, password)
    else:
        restore(args.archive, args.directory, password)
    print(
        "Backup created."
        if args.action == "create"
        else "Restored. Reapply the saved secret key and review recovery-environment.json before starting NASitron."
    )


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, RuntimeError) as exc:
        raise SystemExit(str(exc))
