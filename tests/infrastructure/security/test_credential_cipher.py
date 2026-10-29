from __future__ import annotations

import pickle

import pytest

from backend.foundation.llm_connections import (
    ConnectionOwner,
    CredentialBinding,
    CredentialCipherError,
    EncryptedCredential,
)
from framework.shared.secret_text import SecretText
from infrastructure.security.credential_cipher import (
    CREDENTIAL_KEY_ENV,
    CREDENTIAL_KEY_VERSION_ENV,
    RETIRED_CREDENTIAL_KEYS_ENV,
    AesGcmCredentialCipher,
    CredentialKeyConfigurationError,
    CredentialKeyRing,
    generate_credential_key,
)

ALICE = CredentialBinding(ConnectionOwner("alice", tenant_id="t1"), "conn-1")
SECRET = SecretText("sk-live-alice-key")


def _cipher(**environ) -> AesGcmCredentialCipher:
    return AesGcmCredentialCipher(CredentialKeyRing.from_environment(environ))


def test_round_trip_and_fresh_nonce_per_encryption() -> None:
    cipher = _cipher(**{CREDENTIAL_KEY_ENV: generate_credential_key()})
    first = cipher.encrypt(SECRET, binding=ALICE)
    second = cipher.encrypt(SECRET, binding=ALICE)
    assert first.nonce != second.nonce and first.ciphertext != second.ciphertext
    assert b"sk-live" not in first.ciphertext
    assert cipher.decrypt(first, binding=ALICE) == SECRET
    assert "sk-live" not in repr(first)


@pytest.mark.parametrize(
    "other",
    [
        CredentialBinding(ConnectionOwner("bob", tenant_id="t1"), "conn-1"),
        CredentialBinding(ConnectionOwner("alice", tenant_id="t2"), "conn-1"),
        CredentialBinding(ConnectionOwner("alice"), "conn-1"),
        CredentialBinding(ConnectionOwner("alice", tenant_id="t1"), "conn-2"),
    ],
)
def test_ciphertext_cannot_be_opened_under_another_binding(other) -> None:
    cipher = _cipher(**{CREDENTIAL_KEY_ENV: generate_credential_key()})
    sealed = cipher.encrypt(SECRET, binding=ALICE)
    with pytest.raises(CredentialCipherError):
        cipher.decrypt(sealed, binding=other)


def test_tampering_and_wrong_key_are_detected() -> None:
    key = generate_credential_key()
    cipher = _cipher(**{CREDENTIAL_KEY_ENV: key})
    sealed = cipher.encrypt(SECRET, binding=ALICE)
    flipped = bytes([sealed.ciphertext[0] ^ 1]) + sealed.ciphertext[1:]
    with pytest.raises(CredentialCipherError):
        cipher.decrypt(EncryptedCredential(flipped, sealed.nonce, sealed.key_version), binding=ALICE)
    with pytest.raises(CredentialCipherError):
        _cipher(**{CREDENTIAL_KEY_ENV: generate_credential_key()}).decrypt(sealed, binding=ALICE)


def test_rotation_keeps_old_rows_readable_and_flags_them() -> None:
    old_key, new_key = generate_credential_key(), generate_credential_key()
    old = _cipher(**{CREDENTIAL_KEY_ENV: old_key})
    sealed_v1 = old.encrypt(SECRET, binding=ALICE)

    rotated = _cipher(**{
        CREDENTIAL_KEY_ENV: new_key,
        CREDENTIAL_KEY_VERSION_ENV: "2",
        RETIRED_CREDENTIAL_KEYS_ENV: f"1:{old_key}",
    })
    assert rotated.decrypt(sealed_v1, binding=ALICE) == SECRET
    assert rotated.needs_reencryption(sealed_v1)
    sealed_v2 = rotated.encrypt(SECRET, binding=ALICE)
    assert sealed_v2.key_version == 2 and not rotated.needs_reencryption(sealed_v2)

    retired_dropped = _cipher(**{CREDENTIAL_KEY_ENV: new_key, CREDENTIAL_KEY_VERSION_ENV: "2"})
    with pytest.raises(CredentialCipherError):
        retired_dropped.decrypt(sealed_v1, binding=ALICE)


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {CREDENTIAL_KEY_ENV: "not base64!"},
        {CREDENTIAL_KEY_ENV: "c2hvcnQ"},
        {CREDENTIAL_KEY_ENV: "密钥"},
        {CREDENTIAL_KEY_ENV: "K" * 43, CREDENTIAL_KEY_VERSION_ENV: "zero"},
        {CREDENTIAL_KEY_ENV: "K" * 43, RETIRED_CREDENTIAL_KEYS_ENV: "no-version"},
        {CREDENTIAL_KEY_ENV: "K" * 43, RETIRED_CREDENTIAL_KEYS_ENV: f"1:{'K' * 43}"},
    ],
)
def test_malformed_key_configuration_is_rejected_without_echoing_keys(environ) -> None:
    with pytest.raises(CredentialKeyConfigurationError) as caught:
        CredentialKeyRing.from_environment(environ)
    assert "K" * 43 not in str(caught.value)


def test_key_material_never_appears_in_reprs_and_secrets_do_not_pickle() -> None:
    key = generate_credential_key()
    ring = CredentialKeyRing.from_environment({CREDENTIAL_KEY_ENV: key})
    assert key not in repr(ring) and key not in repr(AesGcmCredentialCipher(ring))
    with pytest.raises(TypeError):
        pickle.dumps(SECRET)
