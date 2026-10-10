#!/usr/bin/env python3
"""Minimal P2P transport: miners talk to gateway over TCP, not function calls.

Protocol: newline-delimited JSON frames. Ops:
  register {miner_id, model, price} -> {ok, msg}
  get_job  {} -> {job_id, task, is_eval}   (server assigns, rotates TASKS)
  submit   {job_id, miner_id, code, latency_ms} -> {passed, payout_watts}
Server alone touches the ledger (single writer + lock). Scoring/checking
identical to gateway.dispatch — transport changed, rules didn't.

Same-machine v0: loopback TCP, real sockets, JSON wire format. WAN needs
auth/encryption (miner PQ signatures on submit) — next rung, noted.
"""
import json
import socket
import threading

RECV_LIMIT = 1 << 20


def send_msg(f, obj):
    f.write((json.dumps(obj) + "\n").encode())
    f.flush()


def recv_msg(f):
    line = f.readline(RECV_LIMIT)
    if not line:
        raise ConnectionError("peer closed")
    return json.loads(line.decode())


class GatewayServer:
    def __init__(self, conn, host="127.0.0.1", port=19191, tls=False,
                 cert_path="gateway-cert.pem", key_path="gateway-key.pem",
                 peers=(), block_mode=False, chain_path="chain.dat",
                 bits=2 ** 236, mine_interval=15, reward_addr="",
                 block_reward=5_000_000, empty_every=30):
        from eval import TASKS
        from chain import Chain, load_chain
        self.conn, self.tasks = conn, list(TASKS)
        self.lock = threading.Lock()
        self.seq = 0
        self.peers = list(peers)
        self.self_id = f"{host}:{port}"
        self.tls, self.ctx = tls, None
        # block mode: submits queue in mempool, settle ONLY via mined blocks
        self.block_mode = block_mode
        self.chain_path = chain_path
        self.bits = bits
        self.mine_interval = mine_interval
        self.mempool: list = []
        self.applied_tip = ""
        self.reward_addr = reward_addr
        self.block_reward = block_reward
        self.empty_every = empty_every
        self._idle_rounds = 0
        self.hits = {}  # ip -> deque of recent expensive-op timestamps
        try:
            self.chain = load_chain(chain_path)
        except ValueError as e:
            print(f"chain load failed (fail-closed): {e}", flush=True)
            self.chain = Chain()
        if self.chain.tip:
            print(f"loaded chain: height={self.chain.height} tip={self.chain.tip[:16]}..", flush=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        if tls:
            fp = ensure_cert(cert_path, key_path)
            import ssl
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.load_cert_chain(cert_path, key_path)
            print(f"TLS on, cert fingerprint (pin): {fp}", flush=True)

    def _recv_or_http(self, f):
        """Read one frame; answer HTTP healthchecks (Railway edge, uptime
        monitors) with 200 OK instead of dropping them. Returns None if it
        was HTTP, else the parsed JSON message."""
        line = f.readline(RECV_LIMIT)
        if not line:
            raise ConnectionError("peer closed")
        if line[:3] in (b"GET", b"HEA", b"POS", b"OPT"):
            body = b'{"ok":true,"service":"infer-gateway","protocol":"tcp+json"}'
            f.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: "
                    + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
            f.flush()
            return None
        return json.loads(line.decode())

    def _throttle(self, ip: str, limit=30, window=60) -> bool:
        """Sliding-window per-IP cap on expensive ops. False = over limit."""
        import collections
        import time as _t
        now = _t.time()
        dq = self.hits.setdefault(ip, collections.deque())
        while dq and now - dq[0] > window:
            dq.popleft()
        if len(dq) >= limit:
            return False
        dq.append(now)
        return True

    def serve_forever(self):
        while True:
            cs, addr = self.sock.accept()
            ip = addr[0] if isinstance(addr, tuple) else "local"
            if self.tls:
                try:
                    cs = self.ctx.wrap_socket(cs, server_side=True)
                except Exception:
                    cs.close()
                    continue
            threading.Thread(target=self.handle, args=(cs, ip), daemon=True).start()

    def handle(self, cs, ip="local"):
        from miner import sha
        from eval import TASKS, check, score_job
        from ledger import log_job, register_miner
        try:
            with cs, cs.makefile("rwb") as f:
                hello = self._recv_or_http(f)
                if hello is None:  # was an HTTP healthcheck; answered already
                    return
                if hello.get("op") in ("get_tip", "get_block", "peers", "submit_block", "mempool"):
                    self.handle_wan(f, hello, ip)  # one-shot WAN ops, no session needed
                    return
                if hello.get("op") == "register_pq":
                    self.handle_wan(f, hello, ip)  # WAN miners register PQ first
                    hello = recv_msg(f)
                if hello.get("op") != "register":
                    return send_msg(f, {"ok": False, "msg": "register first"})
                with self.lock:
                    register_miner(self.conn, hello["miner_id"], hello.get("model", "?"),
                                   float(hello.get("price", 0)))
                send_msg(f, {"ok": True, "msg": "registered"})
                while True:
                    req = recv_msg(f)
                    if req.get("op") == "get_job":
                        if not self._throttle(ip):
                            send_msg(f, {"ok": False, "msg": "rate limited, slow down"})
                            return
                        with self.lock:
                            self.seq += 1
                            task = self.tasks[self.seq % len(self.tasks)]
                            jid = f"p2p-{self.seq}"
                        send_msg(f, {"job_id": jid, "task": task, "is_eval": False})
                    elif req.get("op") == "submit":
                        if not self._throttle(ip):
                            send_msg(f, {"ok": False, "msg": "rate limited, slow down"})
                            return
                        t = TASKS[req["task"]]
                        passed = check(req["code"], t["tests"])
                        tokens = max(1, len(req["code"]) // 4)
                        with self.lock:
                            m = self.conn.execute(
                                "SELECT price_per_1k FROM miners WHERE id=?",
                                (req["miner_id"],)).fetchone()
                        s, pay = score_job(passed, tokens, (m[0] if m else 0.02),
                                           float(req.get("latency_ms", 0)))
                        if not passed:
                            pay = 0
                        job_tx = {"type": "job", "miner": req["miner_id"], "ph": sha(t["prompt"]),
                                  "oh": sha(req["code"]), "tokens": tokens,
                                  "lat": float(req.get("latency_ms", 0)),
                                  "eval": False, "passed": passed, "score": s, "pay": pay}
                        if self.block_mode:
                            ok, msg = self.mempool_add(job_tx)
                            send_msg(f, {"passed": passed, "payout_watts": pay,
                                         "confirmed": False, "mempool": msg})
                        else:
                            with self.lock:
                                log_job(self.conn, req["miner_id"], sha(t["prompt"]),
                                        sha(req["code"]), tokens, float(req.get("latency_ms", 0)),
                                        False, passed, s, pay)
                            send_msg(f, {"passed": passed, "payout_watts": pay})
                    elif not self.handle_wan(f, req, ip):
                        send_msg(f, {"ok": False, "msg": "unknown op"})
        except (ConnectionError, json.JSONDecodeError, KeyError):
            pass

    def handle_wan(self, f, req, ip="local"):
        """WAN ops: PQ-registered miners, signed submits, peer gossip.
        Returns True if handled."""
        from ledger import register_pq, next_ots
        from miner import sha
        import pq
        op = req.get("op")
        if op == "register_pq":
            with self.lock:
                register_pq(self.conn, req["addr"], req["root_hex"])
            send_msg(f, {"ok": True})
            return True
        if op == "next_ots":
            with self.lock:
                n = next_ots(self.conn, req["miner_id"])
            send_msg(f, {"ots": n})
            return True
        if op == "peers":
            send_msg(f, {"peers": [self.self_id] + self.peers})
            return True
        if op == "mempool":
            with self.lock:
                send_msg(f, {"queued": len(self.mempool), "tip": self.chain.tip,
                             "height": self.chain.height})
            return True
        if op == "get_tip":
            send_msg(f, {"tip": self.chain.tip, "height": self.chain.height})
            return True
        if op == "get_block":
            blk = self.chain.blocks.get(req.get("hash", ""))
            if blk is None:
                send_msg(f, {"ok": False, "msg": "unknown block"})
            else:
                send_msg(f, {"ok": True, "block": blk})
            return True
        if op == "submit_block":
            if not self._throttle(ip, limit=60):
                send_msg(f, {"ok": False, "msg": "rate limited"})
                return True
            with self.lock:
                ok, msg = self.chain.add(req["block"])
                if ok:
                    self.persist(req["block"])
                    self.apply_tip_locked()
            send_msg(f, {"ok": ok, "msg": msg, "tip": self.chain.tip,
                         "height": self.chain.height})
            return True
        if op == "submit_signed":
            if not self._throttle(ip):
                send_msg(f, {"ok": False, "msg": "rate limited, slow down"})
                return True
            from eval import TASKS, check, score_job
            from ledger import log_job
            code = req["code"]
            mid = req["miner_id"]
            msg = f"submit:{req['job_id']}:{sha(code)}"
            with self.lock:
                row = self.conn.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (mid,)).fetchone()
            if row is None:
                send_msg(f, {"ok": False, "msg": "miner PQ account unregistered"})
                return True
            pkg, ots = req["sig_pkg"], int(req["sig_pkg"]["ots"])
            with self.lock:
                dup = self.conn.execute("SELECT 1 FROM ots_used WHERE addr=? AND ots=?",
                                        (mid, ots)).fetchone() if self._has_ots() else None
                if dup:
                    send_msg(f, {"ok": False, "msg": "OTS reused"})
                    return True
            if not pq.verify(mid, row[0], pkg, msg, len(code)):
                send_msg(f, {"ok": False, "msg": "bad PQ signature"})
                return True
            with self.lock:
                if self._has_ots():
                    self.conn.execute("INSERT OR IGNORE INTO ots_used VALUES(?,?)", (mid, ots))
                    self.conn.commit()
                else:
                    self.conn.execute("CREATE TABLE ots_used(addr TEXT, ots INTEGER, PRIMARY KEY(addr, ots))")
                    self.conn.execute("INSERT INTO ots_used VALUES(?,?)", (mid, ots))
                    self.conn.commit()
            t = TASKS[req["task"]]
            passed = check(code, t["tests"])
            tokens = max(1, len(code) // 4)
            with self.lock:
                m = self.conn.execute("SELECT price_per_1k FROM miners WHERE id=?", (mid,)).fetchone()
            s, pay = score_job(passed, tokens, (m[0] if m else 0.02), float(req.get("latency_ms", 0)))
            if not passed:
                pay = 0
            job_tx = {"type": "job", "miner": mid, "ph": sha(t["prompt"]), "oh": sha(code),
                      "tokens": tokens, "lat": float(req.get("latency_ms", 0)),
                      "eval": False, "passed": passed, "score": s, "pay": pay}
            if self.block_mode:
                ok, msg = self.mempool_add(job_tx)
                send_msg(f, {"ok": True, "passed": passed, "payout_watts": pay,
                             "confirmed": False, "mempool": msg})
            else:
                with self.lock:
                    log_job(self.conn, mid, sha(t["prompt"]), sha(code), tokens,
                            float(req.get("latency_ms", 0)), False, passed, s, pay)
                send_msg(f, {"ok": True, "passed": passed, "payout_watts": pay})
            return True
        if op == "submit_tx":
            if not self._throttle(ip):
                send_msg(f, {"ok": False, "msg": "rate limited, slow down"})
                return True
            ok, msg = self.mempool_add(req["tx"])
            send_msg(f, {"ok": ok, "msg": msg})
            return True
        return False

    def _has_ots(self):
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='ots_used'").fetchone()

    # -- block mode: mempool -> PoW -> tip -> ledger ----------------------
    def persist(self, block):
        from chain import save_block
        with open(self.chain_path, "ab") as f:
            save_block(f, block)

    def mempool_add(self, tx: dict) -> tuple[bool, str]:
        if len(self.mempool) >= 500:
            return False, "mempool full"
        if tx.get("type") == "transfer":
            import pq
            with self.lock:
                row = self.conn.execute("SELECT root_hex FROM pq_accounts WHERE addr=?",
                                        (tx["from"],)).fetchone()
            if row is None:
                return False, "unknown PQ sender"
            if not pq.verify(tx["from"], row[0], tx["sig"], tx["to"], tx["amount"]):
                return False, "bad PQ signature"
        elif tx.get("type") not in ("job",):
            return False, "only transfer/job relayed (mint/slash are protocol)"
        with self.lock:
            self.mempool.append(tx)
        return True, "queued (unconfirmed)"

    def mine_round(self) -> dict | None:
        """Assemble + mine one block. Slow PoW runs WITHOUT the lock.
        Coinbase (capped protocol mint) is always tx[0] when reward set."""
        from chain import mine_block, retarget, header_hash
        with self.lock:
            txs = self.mempool[:200]
            tip, height = self.chain.tip, self.chain.height
            if not txs and tip:
                # idle: heartbeat empty block only every N rounds, else sleep
                self._idle_rounds += 1
                if self._idle_rounds < self.empty_every:
                    return None
                self._idle_rounds = 0
        n_mem = len(txs)
        if tip and self.reward_addr and self.block_reward:
            txs = [{"type": "mint", "from": "subsidy", "to": self.reward_addr,
                    "amount": self.block_reward}] + txs
        if not tip:
            blk = mine_block("00" * 32, 0, self.bits, txs)
        else:
            with self.lock:
                bits = retarget(self.chain.path_to(tip)) or self.bits
            blk = mine_block(tip, height + 1, bits, txs)
        with self.lock:
            ok, msg = self.chain.add(blk)
            if not ok:
                return None
            self.persist(blk)
            del self.mempool[:n_mem]
            self.apply_tip_locked()
        print(f"mined block {blk['header']['height']} {header_hash(blk['header'])[:12]}.. "
              f"({len(txs)} txs, tip={self.chain.height})", flush=True)
        return blk

    def apply_tip_locked(self):
        """Ledger follows the tip. Forward-apply on extension, full reindex
        on reorg. Metadata (miners, pq keys) snapshotted across reindex."""
        from chain import apply_tx
        new_tip = self.chain.tip
        if new_tip == self.applied_tip:
            return
        fwd, b = [], new_tip
        while b and b != self.applied_tip:
            blk = self.chain.blocks.get(b)
            if blk is None:
                break
            fwd.append(blk)
            b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
        if b == self.applied_tip and self.applied_tip:
            for blk in reversed(fwd):
                for t in blk["txs"]:
                    apply_tx(self.conn, t)
        else:
            miners = list(self.conn.execute("SELECT * FROM miners"))
            accounts = list(self.conn.execute("SELECT * FROM pq_accounts"))
            self.conn.execute("DELETE FROM jobs")
            self.conn.execute("DELETE FROM transfers")
            self.conn.execute("DELETE FROM slashes")
            self.conn.commit()
            for m in miners:
                self.conn.execute("INSERT OR REPLACE INTO miners VALUES(?,?,?,?,?)", tuple(m))
            for a_ in accounts:
                self.conn.execute("INSERT OR REPLACE INTO pq_accounts VALUES(?,?)", tuple(a_))
            self.conn.commit()
            for blk in self.chain.path_to(new_tip):
                for t in blk["txs"]:
                    apply_tx(self.conn, t)
        self.applied_tip = new_tip

    def start_mining(self):
        def loop():
            import time as _t
            while True:
                _t.sleep(self.mine_interval)
                try:
                    self.mine_round()
                except Exception as e:
                    print(f"mine round failed: {e}", flush=True)
        threading.Thread(target=loop, daemon=True).start()


class MinerClient:
    def __init__(self, miner_id, backend, host="127.0.0.1", port=19191,
                 model="?", price=0.0):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("rwb")
        self.miner_id, self.backend = miner_id, backend
        send_msg(self.f, {"op": "register", "miner_id": miner_id, "model": model, "price": price})
        r = recv_msg(self.f)
        assert r.get("ok"), r

    def run_job(self):
        import time
        send_msg(self.f, {"op": "get_job"})
        job = recv_msg(self.f)
        t0 = time.time()
        code, _ = self.backend.generate(job["task"])
        lat = (time.time() - t0) * 1000
        send_msg(self.f, {"op": "submit", "job_id": job["job_id"], "miner_id": self.miner_id,
                          "task": job["task"], "code": code, "latency_ms": lat})
        return job["task"], recv_msg(self.f)

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()


# ---------------------------------------------------------------------------
# WAN layer: TLS with cert pinning (TOFU) + PQ-signed submits.
# Server auth = pinned self-signed cert (no CA needed for testnet).
# Miner auth = PQ signatures (Shor-proof). Payloads encrypted in transit.
# ---------------------------------------------------------------------------

def ensure_cert(cert_path="gateway-cert.pem", key_path="gateway-key.pem") -> str:
    """Create self-signed ECDSA cert on first run. Returns hex fingerprint."""
    import hashlib
    import os as _os
    if _os.path.exists(cert_path) and _os.path.exists(key_path):
        from cryptography import x509 as _x509
        with open(cert_path, "rb") as f:
            der = _x509.load_pem_x509_certificate(f.read()).public_bytes(
                __import__("cryptography.hazmat.primitives.serialization", fromlist=["x"]).Encoding.DER)
        return hashlib.sha256(der).hexdigest()
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import NameOID
    import datetime
    sk = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "infer-testnet-gateway")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(sk.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now).not_valid_after(now + datetime.timedelta(days=825))
            .sign(sk, hashes.SHA256()))
    with open(key_path, "wb") as f:
        f.write(sk.private_bytes(serialization.Encoding.PEM,
                                 serialization.PrivateFormat.TraditionalOpenSSL,
                                 serialization.NoEncryption()))
    _os.chmod(key_path, 0o600)
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    import hashlib as _hl
    return _hl.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()


def tls_connect(host: str, port: int, pin: str, timeout=15):
    """TLS to gateway, verifying the pinned cert fingerprint (TOFU).
    First connect: operator confirms the pin out-of-band (printed by the
    gateway at startup). Wrong pin = abort, no secrets sent."""
    import hashlib
    import socket as _s
    import ssl
    raw = _s.create_connection((host, port), timeout=timeout)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        tls = ctx.wrap_socket(raw, server_hostname=host)
        der = tls.getpeercert(binary_form=True)
    except Exception:
        raw.close()
        raise
    got = hashlib.sha256(der).hexdigest()
    if got != pin.lower():
        tls.close()
        raise ValueError(f"cert pin mismatch (possible MITM): got {got[:16]}.., want {pin[:16]}..")
    return tls


class WANMiner:
    """WAN client: pinned-TLS transport + PQ-signed submits."""

    def __init__(self, key: dict, backend, host, port, pin, model="?", price=0.0):
        import pq
        self.key, self.backend = key, backend
        self.mid = key["addr"]
        self.ots = 0  # local counter; server is source of truth on collisions
        tls = tls_connect(host, port, pin)
        self.sock, self.f = tls, tls.makefile("rwb")
        send_msg(self.f, {"op": "register_pq", "addr": self.mid,
                          "root_hex": pq.root_of(bytes.fromhex(key["seed"])).hex()})
        r = recv_msg(self.f)
        assert r.get("ok"), r
        send_msg(self.f, {"op": "register", "miner_id": self.mid, "model": model, "price": price})
        r = recv_msg(self.f)
        assert r.get("ok"), r

    def _fresh_ots(self):
        send_msg(self.f, {"op": "next_ots", "miner_id": self.mid})
        return recv_msg(self.f)["ots"]

    def run_job(self):
        import time
        import pq
        from miner import sha
        send_msg(self.f, {"op": "get_job"})
        job = recv_msg(self.f)
        t0 = time.time()
        code, _ = self.backend.generate(job["task"])
        lat = (time.time() - t0) * 1000
        ots = self._fresh_ots()
        msg = f"submit:{job['job_id']}:{sha(code)}"
        pkg = pq.sign(self.key["seed"], ots, self.mid, msg, len(code))
        send_msg(self.f, {"op": "submit_signed", "job_id": job["job_id"], "miner_id": self.mid,
                          "task": job["task"], "code": code, "latency_ms": lat, "sig_pkg": pkg})
        return job["task"], recv_msg(self.f)

    def close(self):
        try:
            self.f.close()
        finally:
            self.sock.close()
