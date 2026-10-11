#!/usr/bin/env python3
"""Post-quantum channel: ML-KEM-768 key establishment + AES-256-GCM frames.

Replaces the ECDSA-TLS transport (Shor-vulnerable certs, harvest-now
decrypt-later). Design, honestly scoped for testnet:
- Server holds a long-term ML-KEM keypair (kem-key.pem, 0600). The PIN
  operators already distribute is sha256(public key) — same TOFU workflow
  as before, now binding a PQ key instead of an ECDSA cert.
- Per connection: client encapsulates a fresh secret to the server key;
  both sides HKDF-derive send/recv AES-256-GCM keys with distinct labels
  plus the transcript hash (binds handshake, kills tampering/downgrade).
- Server authentication is implicit (only the key holder decapsulates)
  MINUS active MITM on first connect — covered by pinning, same as before.
- Miner authentication stays at the application layer (PQ-signed submits);
  this channel buys CONFIDENTIALITY only. No identity claims beyond the pin.
- Forward secrecy per connection (fresh encaps each time); none across
  server key rotation (rotate kem-key.pem to bound exposure).

Frame format: [4B len][12B nonce][ciphertext+16B tag]. Nonces are counters
per direction (never reuse). Max frame 1MB like the old transport.
"""
import hashlib
import os
import struct

FRAME_LIMIT = 1 << 20
KEM_FILE = "gateway-kem.pem"
MAGIC = b"PQ1\n"  # multiplexing prefix: PQ handshakes start with this, everything else is legacy


def _mlkem():
    from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PrivateKey
    return MLKEM768PrivateKey


def server_keypair(path: str = KEM_FILE):
    """Load or create (0600). Returns (private_key, pin_hex)."""
    from cryptography.hazmat.primitives import serialization
    cls = _mlkem()
    if os.path.exists(path):
        with open(path, "rb") as f:
            sk = cls.from_seed_bytes(f.read())
    else:
        sk = cls.generate()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(sk.private_bytes(serialization.Encoding.Raw,
                                     serialization.PrivateFormat.Raw,
                                     serialization.NoEncryption()))
    pin = hashlib.sha256(_pub_bytes(sk)).hexdigest()
    return sk, pin


def _pub_bytes(sk) -> bytes:
    from cryptography.hazmat.primitives import serialization
    return sk.public_key().public_bytes(serialization.Encoding.Raw,
                                        serialization.PublicFormat.Raw)


def _kdf(secret: bytes, ctx: bytes, n: int = 32) -> bytes:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    return HKDF(hashes.SHA256(), n, salt=b"infer-pqchan-v1", info=ctx).derive(secret)


class Channel:
    """Framed, encrypted, ordered byte stream over a socket."""

    def __init__(self, sock, send_key: bytes, recv_key: bytes):
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        self.sock = sock
        self.tx = AESGCM(send_key)
        self.rx = AESGCM(recv_key)
        self.s_nonce = 0
        self.r_nonce = 0
        self._rbuf = b""
        self.f = self  # file-ish duck type for makefile-style use below

    def _recvn(self, n: int) -> bytes:
        while len(self._rbuf) < n:
            chunk = self.sock.recv(min(65536, n - len(self._rbuf)))
            if not chunk:
                raise ConnectionError("peer closed")
            self._rbuf += chunk
        out, self._rbuf = self._rbuf[:n], self._rbuf[n:]
        return out

    def send_frame(self, data: bytes):
        if len(data) > FRAME_LIMIT:
            raise ValueError("frame too big")
        nonce = struct.pack(">Q", self.s_nonce) + b"\x00" * 4
        self.s_nonce += 1
        ct = self.tx.encrypt(nonce, data, None)
        self.sock.sendall(struct.pack(">I", len(nonce) + len(ct)) + nonce + ct)

    def recv_frame(self) -> bytes:
        (ln,) = struct.unpack(">I", self._recvn(4))
        if ln > FRAME_LIMIT + 28 or ln < 12:
            raise ValueError("bad frame length")
        nonce = self._recvn(12)
        want = struct.pack(">Q", self.r_nonce) + b"\x00" * 4
        if nonce != want:
            raise ValueError("nonce gap/replay (want %d)" % self.r_nonce)
        self.r_nonce += 1
        ct = self._recvn(ln - 12)
        try:
            return self.rx.decrypt(nonce, ct, None)
        except Exception:
            raise ValueError("frame auth failed (tampered)")

    def makefile(self, *a, **k):
        return _Lines(self)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def close(self):
        try:
            self.sock.close()
        except Exception:
            pass


class _Lines:
    """Line-delimited JSON adapter over frames (drop-in for makefile use)."""

    def __init__(self, ch: Channel):
        self.ch = ch
        self.buf = b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()
        return False

    def write(self, data: bytes):
        for part in data.split(b"\n"):
            if part:
                self.ch.send_frame(part)

    def flush(self):
        pass

    def readline(self, limit: int = -1) -> bytes:
        while b"\n" not in self.buf:
            self.buf += self.ch.recv_frame() + b"\n"
        line, self.buf = self.buf.split(b"\n", 1)
        return line + b"\n"

    def close(self):
        self.ch.close()


def server_accept(raw_sock, server_sk) -> Channel:
    """Server side: magic already consumed. Receive encapsulation, decapsulate."""
    # client -> server: [2B kem-ct-len][kem-ct 1088B][32B client nonce part]
    hdr = _recvn_raw(raw_sock, 2)
    (ln,) = struct.unpack(">H", hdr)
    if ln != 1088:
        raise ValueError("bad kem ciphertext length")
    ct = b""
    while len(ct) < ln:
        chunk = raw_sock.recv(ln - len(ct))
        if not chunk:
            raise ConnectionError("peer closed")
        ct += chunk
    secret = server_sk.decapsulate(ct)
    c_rand = _recvn_raw(raw_sock, 32)
    transcript = hashlib.sha256(ct + c_rand).digest()
    c2s = _kdf(secret, b"c2s" + transcript)
    s2c = _kdf(secret, b"s2c" + transcript)
    return Channel(raw_sock, s2c, c2s)


def client_connect(raw_sock, server_pubkey_bytes: bytes, pin: str) -> Channel:
    """Client side: pin-check server key, encapsulate fresh secret."""
    import secrets as _s
    from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PublicKey
    if hashlib.sha256(server_pubkey_bytes).hexdigest() != pin.lower():
        raise ValueError("kem pin mismatch (possible MITM)")
    pk = MLKEM768PublicKey.from_public_bytes(server_pubkey_bytes)
    secret, ct = pk.encapsulate()  # order: (shared_secret, ciphertext)
    c_rand = _s.token_bytes(32)
    raw_sock.sendall(MAGIC + struct.pack(">H", len(ct)) + ct + c_rand)
    transcript = hashlib.sha256(bytes(ct) + c_rand).digest()
    c2s = _kdf(secret, b"c2s" + transcript)
    s2c = _kdf(secret, b"s2c" + transcript)
    return Channel(raw_sock, c2s, s2c)


def _recvn_raw(sock, n: int) -> bytes:
    out = b""
    while len(out) < n:
        chunk = sock.recv(n - len(out))
        if not chunk:
            raise ConnectionError("peer closed")
        out += chunk
    return out


def fetch_server_key(host: str, port: int, timeout: int = 15) -> bytes:
    """Fetch raw server KEM public key for out-of-band pin verification.
    Server must expose it: handle 'PQKEY' probe pre-handshake."""
    import socket as _s
    s = _s.create_connection((host, port), timeout=timeout)
    with s:
        s.sendall(b"PQKEY\n")
        ln = _recvn_raw(s, 2)
        (n,) = struct.unpack(">H", ln)
        if n > 2048:
            raise ValueError("bad key length")
        return _recvn_raw(s, n)
