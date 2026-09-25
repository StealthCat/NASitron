from app.support import sanitize_diagnostics


def test_support_bundle_redacts_sensitive_and_user_properties():
    raw = {
        "zfs_get_all": {
            "stdout": (
                "tank\tkeylocation\thttps://secret.example/key\tlocal\n"
                "tank\tcom.example:token\tvery-secret\tlocal\n"
                "tank\tcompression\tlz4\tlocal\n"
            ),
            "stderr": "",
            "exit": 0,
        }
    }
    safe = sanitize_diagnostics(raw)
    text = safe["zfs_get_all"]["stdout"]
    assert "https://secret.example/key" not in text
    assert "very-secret" not in text
    assert "<redacted>" in text
    assert "<redacted:user-property>" in text
    assert "lz4" in text
