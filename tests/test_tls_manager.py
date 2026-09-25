from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from app.tls_manager import (
    TLSConfigurationError,
    build_caddyfile,
    validate_acme_directory_url,
    validate_certificate_pair,
)


def _certificate_pair(common_name: str = "nas.example.com") -> tuple[bytes, bytes]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=30))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName(common_name)]),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )
    return (
        cert.public_bytes(serialization.Encoding.PEM),
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )


def test_uploaded_certificate_must_match_private_key():
    cert, key = _certificate_pair()
    info = validate_certificate_pair(cert, key)
    assert "nas.example.com" in info["subject"]

    _other_cert, other_key = _certificate_pair("other.example.com")
    with pytest.raises(TLSConfigurationError, match="does not match"):
        validate_certificate_pair(cert, other_key)


def test_acme_directory_requires_https_and_no_credentials():
    assert validate_acme_directory_url(
        "https://ca.example/acme/directory"
    ) == "https://ca.example/acme/directory"
    with pytest.raises(TLSConfigurationError):
        validate_acme_directory_url("http://ca.example/acme/directory")
    with pytest.raises(TLSConfigurationError):
        validate_acme_directory_url("https://user:pass@ca.example/directory")


def test_caddyfile_modes_are_guarded_and_use_internal_backend():
    manual = build_caddyfile("manual")
    assert "manual-cert.pem" in manual
    assert "reverse_proxy nasitron:8080" in manual

    internal = build_caddyfile("internal", domain="2001:db8::10")
    assert "https:// {" in internal
    assert "tls internal {" in internal
    assert "on_demand" in internal
    assert "[2001:db8::10] {" not in internal

    acme = build_caddyfile(
        "acme",
        domain="nas.example.com",
        email="admin@example.com",
        acme_ca="https://ca.example/acme/directory",
        use_acme_ca_root=True,
    )
    assert "nas.example.com {" in acme
    assert 'ca "https://ca.example/acme/directory"' in acme
    assert "acme-ca-root.pem" in acme
    assert "admin 0.0.0.0:2019" in acme
