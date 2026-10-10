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

FAUCET_DIFFICULTY = 5  # leading hex zeros (~1M hashes, a few seconds in-browser)
FAUCET_AMOUNT = 50_000  # watts = 0.05 INFR per claim, 1 claim/IP/day

FAUCET_HTML = """<!doctype html><html><head><meta charset=utf8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Infer Testnet Faucet</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:3em auto;padding:0 1em;color:#222}
input{width:100%;padding:.6em;font-family:monospace}button{padding:.6em 1.2em;margin-top:.6em}
#log{background:#f4f4f4;padding:.8em;white-space:pre-wrap;min-height:4em}</style></head><body>
<h1>&#9672; Infer Testnet Faucet</h1>
<p>Free test INFR. The captcha is proof-of-work: your browser grinds SHA-256
until the hash has <b>5 leading zeros</b> — bots pay CPU per claim, just like miners.</p>
<p>Amount: <b>50,000 watts (0.05 INFR)</b> &middot; 1 claim per IP per day &middot; test funds only.</p>
<input id=addr placeholder="pq1... your wallet address">
<button id=go>Solve captcha &amp; claim</button>
<p id=log>idle</p>
<script>
const log = m => document.getElementById('log').textContent = m;
async function sha(s){const b=await crypto.subtle.digest('SHA-256',new TextEncoder().encode(s));
return [...new Uint8Array(b)].map(x=>x.toString(16).padStart(2,'0')).join('');}
document.getElementById('go').onclick = async () => {
 const addr = document.getElementById('addr').value.trim();
 if(!addr.startsWith('pq1')){log('enter a pq1... address from your wallet');return;}
 log('fetching challenge…');
 const c = await (await fetch('/api/challenge')).json();
 log(`grinding ${c.difficulty} zeros (a few seconds)…`);
 let nonce = 0;
 for(;;nonce++){
  if(nonce % 20000 === 0){log(`grinding… ${nonce} hashes`);await new Promise(r=>setTimeout(r));}
  if((await sha(c.challenge + nonce)).startsWith('0'.repeat(c.difficulty))) break;
 }
 log('solved, claiming…');
 const r = await (await fetch('/api/faucet',{method:'POST',
  headers:{'Content-Type':'application/json'},
  body:JSON.stringify({addr,challenge_id:c.challenge_id,nonce})})).json();
 log(r.ok ? `paid ${r.amount_watts} watts — mine or send it!` : 'denied: ' + r.msg);
};
fetch('/api/status').then(r=>r.json()).then(s=>
 log(`idle — chain height ${s.height}, ${s.queued} queued`)).catch(()=>{});
</script></body></html>"""


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
        self.challenges = {}  # challenge_id -> (challenge, expiry)
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

    def _recv_or_http(self, f, ip="local"):
        """Read one frame; serve HTTP routes (healthcheck, faucet site/API),
        else return the parsed JSON protocol message (None = HTTP handled)."""
        line = f.readline(RECV_LIMIT)
        if not line:
            raise ConnectionError("peer closed")
        if line[:3] not in (b"GET", b"HEA", b"POS", b"OPT"):
            return json.loads(line.decode())
        parts = line.decode(errors="replace").split()
        method, path = (parts + ["", "/"])[:2]
        headers, body = {}, b""
        while True:
            hl = f.readline(65536)
            if not hl or hl in (b"\r\n", b"\n"):
                break
            if b":" in hl:
                k, v = hl.decode(errors="replace").split(":", 1)
                headers[k.strip().lower()] = v.strip()
        if method == "POST":
            try:
                body = f.read(int(headers.get("content-length", "0")))
            except ValueError:
                pass
        if path == "/" and method == "GET":
            self._http(f, 200, FAUCET_HTML.encode(), "text/html; charset=utf-8")
            return None
        if path == "/api/challenge" and method == "GET":
            import secrets as _s
            cid = _s.token_hex(8)
            chal = _s.token_hex(16)
            with self.lock:
                self.challenges[cid] = (chal, __import__("time").time() + 600)
            self._http(f, 200, json.dumps(
                {"challenge_id": cid, "challenge": chal,
                 "difficulty": FAUCET_DIFFICULTY, "amount_watts": FAUCET_AMOUNT}).encode(),
                "application/json")
            return None
        if path == "/api/faucet" and method == "POST":
            xff = headers.get("x-forwarded-for", "")
            real_ip = xff.split(",")[0].strip() if xff else ip
            self._faucet_claim(f, real_ip, body)
            return None
        if path == "/api/status" and method == "GET":
            from ledger import subsidy_issued_watts, burned_watts
            with self.lock:
                st = {"tip": self.chain.tip, "height": self.chain.height,
                      "queued": len(self.mempool),
                      "issued_watts": subsidy_issued_watts(self.conn),
                      "burned_watts": burned_watts(self.conn)}
            self._http(f, 200, json.dumps(st).encode(), "application/json")
            return None
        self._http(f, 200, b'{"ok":true,"service":"infer-gateway","protocol":"tcp+json"}',
                   "application/json")
        return None

    @staticmethod
    def _http(f, code, body: bytes, ctype: str):
        f.write(f"HTTP/1.1 {code} {'OK' if code == 200 else 'ERR'}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        f.flush()

    def _faucet_claim(self, f, ip, body: bytes):
        """PoW-captcha drip: 1 claim/IP/day, small amount, all auditable."""
        import hashlib as _hl
        import time as _t
        try:
            req = json.loads(body.decode() or "{}")
            addr, cid, nonce = req["addr"], req["challenge_id"], str(req["nonce"])
        except (ValueError, KeyError):
            self._http(f, 400, b'{"ok":false,"msg":"bad request"}', "application/json")
            return
        if not isinstance(addr, str) or not addr.startswith("pq1") or len(addr) > 64:
            self._http(f, 400, b'{"ok":false,"msg":"bad address"}', "application/json")
            return
        with self.lock:
            chal = self.challenges.pop(cid, None)
        if chal is None or _t.time() > chal[1]:
            self._http(f, 400, b'{"ok":false,"msg":"stale/unknown challenge"}', "application/json")
            return
        digest = _hl.sha256((chal[0] + nonce).encode()).hexdigest()
        if not digest.startswith("0" * FAUCET_DIFFICULTY):
            self._http(f, 400, b'{"ok":false,"msg":"insufficient work"}', "application/json")
            return
        day = int(_t.time() // 86400)
        with self.lock:
            self.conn.execute("CREATE TABLE IF NOT EXISTS faucet_claims(ip TEXT, day INTEGER, PRIMARY KEY(ip, day))")
            if self.conn.execute("SELECT 1 FROM faucet_claims WHERE ip=? AND day=?", (ip, day)).fetchone():
                self.conn.commit()
                self._http(f, 429, b'{"ok":false,"msg":"already claimed today"}', "application/json")
                return
            from ledger import transfer
            try:
                transfer(self.conn, "faucet", addr, FAUCET_AMOUNT)
            except ValueError as e:
                self._http(f, 400, json.dumps({"ok": False, "msg": str(e)}).encode(), "application/json")
                return
            self.conn.execute("INSERT INTO faucet_claims VALUES(?,?)", (ip, day))
            self.conn.commit()
        self._http(f, 200, json.dumps({"ok": True, "amount_watts": FAUCET_AMOUNT}).encode(),
                   "application/json")

    def _throttle(self, ip: str, limit=300, window=60) -> bool:
        """Sliding-window per-IP cap. 300/min lets honest miners burst;
        garbage still earns nothing (scoring pays 0) and blocks cap space."""
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
                hello = self._recv_or_http(f, ip)
                if hello is None:  # was an HTTP healthcheck; answered already
                    return
                if hello.get("op") in ("get_tip", "get_block", "peers", "submit_block", "mempool",
                                        "balance", "history", "next_ots", "register_pq", "submit_tx"):
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
        if op == "balance":
            from ledger import balance_watts
            with self.lock:
                send_msg(f, {"balance_watts": balance_watts(self.conn, req.get("addr", ""))})
            return True
        if op == "history":
            addr, lim = req.get("addr", ""), min(int(req.get("limit", 30)), 100)
            with self.lock:
                jobs = self.conn.execute(
                    "SELECT id, passed, payout_watts FROM jobs WHERE miner_id=? "
                    "ORDER BY id DESC LIMIT ?", (addr, lim)).fetchall()
                txs = self.conn.execute(
                    "SELECT from_id, to_id, amount_watts FROM transfers "
                    "WHERE from_id=? OR to_id=? ORDER BY id DESC LIMIT ?", (addr, addr, lim)).fetchall()
            send_msg(f, {"jobs": jobs, "transfers": txs})
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
            txs, n_mem = [], 0  # genesis carries no user txs (consensus rule)
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
        while b and b != self.applied_tip and set(b) != {"0"}:
            blk = self.chain.blocks.get(b)
            if blk is None:
                break
            fwd.append(blk)
            ph = blk["header"]["prev_hash"]
            b = "" if set(ph) == {"0"} else ph
        if b == self.applied_tip:
            for blk in reversed(fwd):
                for t in blk["txs"]:
                    apply_tx(self.conn, t)
        else:
            miners = list(self.conn.execute("SELECT * FROM miners"))
            accounts = list(self.conn.execute("SELECT * FROM pq_accounts"))
            issued = list(self.conn.execute("SELECT * FROM transfers WHERE from_id IN ('faucet','subsidy')"))
            self.conn.execute("DELETE FROM jobs")
            self.conn.execute("DELETE FROM transfers")
            self.conn.execute("DELETE FROM slashes")
            self.conn.commit()
            for m in miners:
                self.conn.execute("INSERT OR REPLACE INTO miners VALUES(?,?,?,?,?)", tuple(m))
            for a_ in accounts:
                self.conn.execute("INSERT OR REPLACE INTO pq_accounts VALUES(?,?)", tuple(a_))
            for row in issued:  # protocol issuance survives reorgs (faucet/subsidy aren't user spends)
                self.conn.execute("INSERT OR IGNORE INTO transfers VALUES(?,?,?,?,?,?,?)", tuple(row))
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
        if "task" not in job:
            raise ConnectionError(job.get("msg", "get_job rejected"))
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
        if "task" not in job:
            raise ConnectionError(job.get("msg", "get_job rejected"))
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
