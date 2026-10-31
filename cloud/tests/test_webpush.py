"""Web Push message encryption (RFC 8291) and VAPID (RFC 8292), checked by decrypting the
way a browser does."""

import json

import jwt
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from cctv_cloud.services.webpush import (
    Vapid,
    allowed_endpoint,
    b64url_decode,
    b64url_encode,
    encrypt,
    new_vapid_private_key,
)


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def browser_keys() -> tuple[ec.EllipticCurvePrivateKey, str, bytes]:
    private = ec.generate_private_key(ec.SECP256R1())
    public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    return private, b64url_encode(public), b"0123456789abcdef"


def browser_decrypt(body: bytes, private: ec.EllipticCurvePrivateKey, auth: bytes) -> bytes:
    """What the user agent does on receipt (RFC 8291 section 3)."""
    salt, record_size, key_length = body[:16], int.from_bytes(body[16:20], "big"), body[20]
    server_public = body[21 : 21 + key_length]
    ciphertext = body[21 + key_length :]
    assert record_size == 4096 and len(ciphertext) <= record_size
    ua_public = private.public_key().public_bytes(Encoding.X962, PublicFormat.UncompressedPoint)
    shared = private.exchange(
        ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), server_public)
    )
    ikm = _hkdf(auth, shared, b"WebPush: info\x00" + ua_public + server_public, 32)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    plain = AESGCM(cek).decrypt(nonce, ciphertext, None)
    assert plain.endswith(b"\x02")  # last-record delimiter, no padding
    return plain[:-1]


def test_a_browser_can_decrypt_the_message():
    private, p256dh, auth = browser_keys()
    message = json.dumps({"title": "حرج: شخص بعد ساعات العمل", "url": "https://x/e"}).encode()
    body = encrypt(message, p256dh, b64url_encode(auth))
    assert browser_decrypt(body, private, auth) == message
    # A fresh key and salt each time: the same alert never looks the same twice.
    assert encrypt(message, p256dh, b64url_encode(auth)) != body


def test_vapid_header_is_signed_for_the_push_service():
    vapid = Vapid.from_private(new_vapid_private_key(), "mailto:ops@example.com")
    header = vapid.authorization("https://fcm.googleapis.com/fcm/send/abc:def")
    token = header.split("t=", 1)[1].split(",", 1)[0]
    key = header.split("k=", 1)[1]
    assert key == vapid.public_key and len(b64url_decode(key)) == 65
    public = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), b64url_decode(key))
    claims = jwt.decode(token, public, algorithms=["ES256"], audience="https://fcm.googleapis.com")
    assert claims["sub"] == "mailto:ops@example.com"


def test_only_known_push_services_are_contacted():
    assert allowed_endpoint("https://fcm.googleapis.com/fcm/send/abc")
    assert allowed_endpoint("https://updates.push.services.mozilla.com/wpush/v2/x")
    assert allowed_endpoint("https://db5p.notify.windows.com/w/?token=x")
    assert allowed_endpoint("https://web.push.apple.com/QGx")
    for url in (
        "http://fcm.googleapis.com/fcm/send/abc",  # not https
        "https://fcm.googleapis.com.evil.example/x",
        "https://evilnotify.windows.com/x",
        "https://169.254.169.254/latest/meta-data",
        "https://fcm.googleapis.com:8443/x",
    ):
        assert not allowed_endpoint(url), url
