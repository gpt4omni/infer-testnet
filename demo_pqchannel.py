#!/usr/bin/env python3
"""Fragmented ML-KEM handshake, authenticated frames, pin and replay rejection."""
import hashlib
import os
import socket
import struct
import tempfile
import threading
import time

import pqchannel as P


def main():
    with tempfile.TemporaryDirectory(prefix="infer-pqchannel-") as directory:
        path = os.path.join(directory, "gateway-kem.pem")
        sk, pin = P.server_keypair(path)
        assert os.stat(path).st_mode & 0o777 == 0o600
        assert P.server_keypair(path)[1] == pin
        pub = P._pub_bytes(sk)
        a, b = socket.socketpair()
        a.settimeout(5)
        b.settimeout(5)
        errors = []
        def server():
            try:
                with P.server_accept(a, sk) as ch:
                    assert ch.recv_frame() == b"fragmented handshake"
                    ch.send_frame(b"authenticated reply")
                    try:
                        ch.recv_frame()
                        raise AssertionError("replayed nonce accepted")
                    except ValueError as e:
                        assert "replay" in str(e)
            except Exception as e:
                errors.append(e)
        thread = threading.Thread(target=server)
        thread.start()
        from cryptography.hazmat.primitives.asymmetric.mlkem import MLKEM768PublicKey
        secret, ct = MLKEM768PublicKey.from_public_bytes(pub).encapsulate()
        nonce = os.urandom(32)
        hdr = struct.pack(">H", len(ct))
        with b:
            b.sendall(hdr[:1])
            time.sleep(0.05)  # old recv(2) failed here
            b.sendall(hdr[1:] + ct[:17])
            time.sleep(0.01)
            b.sendall(ct[17:] + nonce)
            transcript = hashlib.sha256(ct + nonce).digest()
            ch = P.Channel(b, P._kdf(secret, b"c2s" + transcript), P._kdf(secret, b"s2c" + transcript))
            ch.send_frame(b"fragmented handshake")
            assert ch.recv_frame() == b"authenticated reply"
            ch.s_nonce = 0
            ch.send_frame(b"replay")
        thread.join(timeout=5)
        assert not thread.is_alive() and not errors, errors
        a, b = socket.socketpair()
        with a, b:
            try:
                P.client_connect(a, pub, "0" * 64)
                raise AssertionError("wrong pin accepted")
            except ValueError as e:
                assert "pin mismatch" in str(e)
            sender, receiver = P.Channel(a, b"x" * 32, b"y" * 32), P.Channel(b, b"y" * 32, b"z" * 32)
            sender.send_frame(b"wrong authentication key")
            try:
                receiver.recv_frame()
                raise AssertionError("tampered frame accepted")
            except ValueError as e:
                assert "auth failed" in str(e)
    print("PQ CHANNEL PASS: fragmented handshake, stable private key, wrong pin, nonce replay and frame tampering rejected.")


if __name__ == "__main__":
    main()
