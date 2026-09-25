from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from datetime import timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from cryptography import x509
from cryptography.hazmat.primitives import serialization

from .config import CADDY_ADMIN_URL, CADDY_SHARED_TLS_DIR, DATA_DIR

TLS_DIR = Path(DATA_DIR) / "tls"
CADDYFILE_PATH = TLS_DIR / "Caddyfile"
MANUAL_CERT_PATH = TLS_DIR / "manual-cert.pem"
MANUAL_KEY_PATH = TLS_DIR / "manual-key.pem"
ACME_CA_ROOT_PATH = TLS_DIR / "acme-ca-root.pem"

_CADDY_MANUAL_CERT = f"{CADDY_SHARED_TLS_DIR}/manual-cert.pem"
_CADDY_MANUAL_KEY = f"{CADDY_SHARED_TLS_DIR}/manual-key.pem"
_CADDY_CA_ROOT = f"{CADDY_SHARED_TLS_DIR}/acme-ca-root.pem"


class TLSConfigurationError(RuntimeError):
    pass


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        prefix=f".{path.name}.",
        dir=str(path.parent),
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
    except Exception:
        try:
            handle.close()
        except Exception:
            pass
        temp_path.unlink(missing_ok=True)
        raise


def write_secret_file(path: Path, data: bytes) -> None:
    _atomic_write(path, data, 0o600)


def _public_key_der(value: Any) -> bytes:
    return value.public_bytes(
        serialization.Encoding.DER,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def validate_certificate_pair(cert_pem: bytes, key_pem: bytes) -> dict[str, str]:
    if not cert_pem or not key_pem:
        raise TLSConfigurationError("Both a certificate chain and private key are required.")
    try:
        certificates = x509.load_pem_x509_certificates(cert_pem)
    except ValueError as exc:
        raise TLSConfigurationError("The uploaded certificate is not valid PEM.") from exc
    if not certificates:
        raise TLSConfigurationError("The uploaded certificate file contains no certificates.")
    try:
        private_key = serialization.load_pem_private_key(key_pem, password=None)
    except (TypeError, ValueError) as exc:
        raise TLSConfigurationError(
            "The uploaded private key must be valid, unencrypted PEM."
        ) from exc

    leaf = certificates[0]
    if not hmac_public_key_match(leaf.public_key(), private_key.public_key()):
        raise TLSConfigurationError(
            "The uploaded private key does not match the leaf certificate."
        )

    not_after = getattr(leaf, "not_valid_after_utc", None)
    if not_after is None:
        not_after = leaf.not_valid_after.replace(tzinfo=timezone.utc)
    return {
        "subject": leaf.subject.rfc4514_string() or "Unknown",
        "issuer": leaf.issuer.rfc4514_string() or "Unknown",
        "not_after": not_after.isoformat(),
        "chain_length": str(len(certificates)),
    }


def hmac_public_key_match(cert_key: Any, private_key: Any) -> bool:
    # Public-key DER contains no secret material; direct equality is appropriate
    # and avoids key-type-specific comparisons.
    return _public_key_der(cert_key) == _public_key_der(private_key)


def validate_ca_root(ca_pem: bytes) -> int:
    if not ca_pem:
        raise TLSConfigurationError("The ACME CA root file is empty.")
    try:
        certificates = x509.load_pem_x509_certificates(ca_pem)
    except ValueError as exc:
        raise TLSConfigurationError("The ACME CA root is not valid PEM.") from exc
    if not certificates:
        raise TLSConfigurationError("The ACME CA root contains no certificates.")
    return len(certificates)


def manual_certificate_status() -> dict[str, Any]:
    result: dict[str, Any] = {
        "configured": MANUAL_CERT_PATH.exists() and MANUAL_KEY_PATH.exists(),
        "ca_root_configured": ACME_CA_ROOT_PATH.exists(),
    }
    if MANUAL_CERT_PATH.exists():
        try:
            certificates = x509.load_pem_x509_certificates(MANUAL_CERT_PATH.read_bytes())
            if certificates:
                leaf = certificates[0]
                not_after = getattr(leaf, "not_valid_after_utc", None)
                if not_after is None:
                    not_after = leaf.not_valid_after.replace(tzinfo=timezone.utc)
                result.update(
                    {
                        "subject": leaf.subject.rfc4514_string() or "Unknown",
                        "issuer": leaf.issuer.rfc4514_string() or "Unknown",
                        "not_after": not_after.isoformat(),
                    }
                )
        except (OSError, ValueError):
            result["certificate_error"] = "Stored certificate could not be parsed."
    return result


def validate_acme_directory_url(value: str) -> str:
    url = value.strip()
    if len(url) > 2048:
        raise TLSConfigurationError("ACME directory URL is too long.")
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise TLSConfigurationError(
            "ACME directory URL must be an HTTPS URL without embedded credentials."
        )
    if parsed.fragment:
        raise TLSConfigurationError("ACME directory URL must not contain a fragment.")
    return url


def _quote(value: str) -> str:
    return json.dumps(value)


def _base_caddyfile() -> str:
    return """{
    admin 0.0.0.0:2019
}
"""


def build_caddyfile(
    mode: str,
    *,
    domain: str = "",
    email: str = "",
    acme_ca: str = "",
    use_acme_ca_root: bool = False,
) -> str:
    if mode == "manual":
        return (
            _base_caddyfile()
            + """
http:// {
    redir https://{host}{uri} permanent
}

https:// {
    tls """
            + _quote(_CADDY_MANUAL_CERT)
            + " "
            + _quote(_CADDY_MANUAL_KEY)
            + """
    encode zstd gzip
    reverse_proxy nasitron:8080
}
"""
        )

    if not domain:
        raise TLSConfigurationError("A TLS hostname or IP address is required.")

    if mode == "internal":
        return (
            _base_caddyfile()
            + domain
            + """ {
    tls internal
    encode zstd gzip
    reverse_proxy nasitron:8080
}
"""
        )

    if mode != "acme":
        raise TLSConfigurationError("TLS mode must be internal, manual, or acme.")
    if not acme_ca:
        raise TLSConfigurationError("An ACME directory URL is required.")

    tls_header = "    tls"
    if email:
        tls_header += " " + _quote(email)
    lines = [
        _base_caddyfile().rstrip(),
        "",
        f"{domain} {{",
        tls_header + " {",
        f"        ca {_quote(acme_ca)}",
    ]
    if use_acme_ca_root:
        lines.append(f"        ca_root {_quote(_CADDY_CA_ROOT)}")
    lines.extend(
        [
            "    }",
            "    encode zstd gzip",
            "    reverse_proxy nasitron:8080",
            "}",
            "",
        ]
    )
    return "\n".join(lines)


def _post_caddyfile(caddyfile: str) -> None:
    request = urllib.request.Request(
        CADDY_ADMIN_URL,
        data=caddyfile.encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "text/caddyfile",
            "Cache-Control": "must-revalidate",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            if response.status >= 300:
                raise TLSConfigurationError(
                    f"Caddy rejected the configuration with HTTP {response.status}."
                )
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read(4096).decode("utf-8", errors="replace").strip()
        except Exception:
            detail = ""
        raise TLSConfigurationError(
            "Caddy rejected the TLS configuration"
            + (f": {detail}" if detail else f" (HTTP {exc.code})")
        ) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise TLSConfigurationError(
            f"Unable to contact the internal Caddy admin endpoint: {exc}"
        ) from exc


def persist_and_apply_caddyfile(caddyfile: str) -> None:
    previous = CADDYFILE_PATH.read_bytes() if CADDYFILE_PATH.exists() else None
    _atomic_write(CADDYFILE_PATH, caddyfile.encode("utf-8"), 0o600)
    try:
        _post_caddyfile(caddyfile)
    except Exception:
        if previous is None:
            CADDYFILE_PATH.unlink(missing_ok=True)
        else:
            _atomic_write(CADDYFILE_PATH, previous, 0o600)
        raise
