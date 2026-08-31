import base64
import os
from uuid import UUID

import pytest


TEST_KEY = base64.urlsafe_b64encode(bytes(range(32))).decode("ascii")
OTHER_KEY = base64.urlsafe_b64encode(bytes(reversed(range(32)))).decode("ascii")
ACCOUNT_ID = UUID("f9677b24-92ee-4b3a-9b38-d5b7d22f9911")


def _credentials_module():
    from app.services import remote_credentials

    return remote_credentials


def test_credential_vault_round_trip_returns_redacted_secret_mapping():
    credentials = _credentials_module()
    vault = credentials.CredentialVault(TEST_KEY)

    ciphertext = vault.encrypt(
        {"refresh_token": "pixiv-refresh-secret", "auth_method": "refresh_token"},
        user_id=17,
        source="pixiv",
        account_id=ACCOUNT_ID,
    )
    decrypted = vault.decrypt(
        ciphertext,
        user_id=17,
        source="pixiv",
        account_id=ACCOUNT_ID,
    )

    assert decrypted["refresh_token"] == "pixiv-refresh-secret"
    assert decrypted["auth_method"] == "refresh_token"
    assert "pixiv-refresh-secret" not in repr(decrypted)
    assert "pixiv-refresh-secret" not in str(decrypted)
    assert "pixiv-refresh-secret" not in repr(vault)


@pytest.mark.parametrize(
    ("override", "value"),
    [
        ("user_id", 18),
        ("source", "x"),
        ("account_id", UUID("5fc4a481-5d0c-45ac-b8ed-89313d0970cf")),
    ],
)
def test_credential_vault_aad_binds_every_account_identity_field(override, value):
    credentials = _credentials_module()
    vault = credentials.CredentialVault(TEST_KEY)
    ciphertext = vault.encrypt(
        {"refresh_token": "secret"},
        user_id=17,
        source="pixiv",
        account_id=ACCOUNT_ID,
    )
    identity = {"user_id": 17, "source": "pixiv", "account_id": ACCOUNT_ID}
    identity[override] = value

    with pytest.raises(credentials.CredentialDecryptionError):
        vault.decrypt(ciphertext, **identity)


def test_credential_vault_rejects_tampering_and_wrong_key():
    credentials = _credentials_module()
    vault = credentials.CredentialVault(TEST_KEY)
    ciphertext = vault.encrypt(
        {"cookie": "secret"},
        user_id=17,
        source="x",
        account_id=ACCOUNT_ID,
    )
    prefix, encoded = ciphertext.split(":", 1)
    payload = bytearray(base64.urlsafe_b64decode(encoded.encode("ascii")))
    payload[-1] ^= 1
    tampered = f"{prefix}:{base64.urlsafe_b64encode(payload).decode('ascii')}"

    with pytest.raises(credentials.CredentialDecryptionError):
        vault.decrypt(tampered, user_id=17, source="x", account_id=ACCOUNT_ID)
    with pytest.raises(credentials.CredentialDecryptionError):
        credentials.CredentialVault(OTHER_KEY).decrypt(
            ciphertext,
            user_id=17,
            source="x",
            account_id=ACCOUNT_ID,
        )


@pytest.mark.parametrize(
    "key",
    [
        "",
        "change-me-remote-credential-key",
        base64.urlsafe_b64encode(b"too-short").decode("ascii"),
        "not base64!",
    ],
)
def test_credential_vault_rejects_missing_placeholder_or_invalid_keys(key):
    credentials = _credentials_module()

    with pytest.raises(credentials.CredentialKeyError, match="REMOTE_CREDENTIAL_KEY"):
        credentials.CredentialVault(key)


def test_settings_rejects_a_configured_invalid_remote_credential_key():
    from app.config import Settings

    with pytest.raises(RuntimeError, match="REMOTE_CREDENTIAL_KEY"):
        Settings(
            database_url="postgresql+asyncpg://autogallery:strong-db@postgres:5432/autogallery",
            redis_url="redis://:strong-redis@redis:6379/0",
            meili_master_key="strong-meili-key",
            secret_key="strong-secret-key",
            admin_password="strong-admin-password",
            remote_credential_key="not-base64",
        )


def test_download_auth_override_rejects_durable_credential_paths_and_redacts_repr():
    credentials = _credentials_module()

    override = credentials.DownloadAuthenticationOverride(
        {"extractor": {"pixiv": {"refresh-token": "download-secret"}}}
    )

    assert override["extractor"]["pixiv"]["refresh-token"] == "download-secret"
    assert "download-secret" not in repr(override)
    with pytest.raises(ValueError, match="durable credential path"):
        credentials.DownloadAuthenticationOverride(
            {"extractor": {"twitter": {"cookies": "/gallerydl-config/cookies/twitter.txt"}}}
        )


def test_vault_uses_fresh_nonce_for_each_encryption():
    credentials = _credentials_module()
    vault = credentials.CredentialVault(TEST_KEY)
    kwargs = {"user_id": 17, "source": "bilibili", "account_id": ACCOUNT_ID}

    first = vault.encrypt({"SESSDATA": "secret"}, **kwargs)
    second = vault.encrypt({"SESSDATA": "secret"}, **kwargs)

    assert first != second
    assert os.path.sep not in first
