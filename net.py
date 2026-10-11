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

WALLET_HTML = """<!doctype html><html><head><meta charset=utf8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>Infer Web Wallet (testnet)</title>
<style>body{font-family:system-ui,sans-serif;max-width:560px;margin:3em auto;padding:0 1em;color:#222}
input{width:100%;padding:.6em;font-family:monospace;box-sizing:border-box}button{padding:.6em 1.2em;margin:.4em .2em 0 0}
#log{background:#f4f4f4;padding:.8em;white-space:pre-wrap;min-height:3em}
.warn{background:#fff3cd;padding:.8em;border:1px solid #e0c36a}</style></head><body>
<h1>&#9672; Infer Web Wallet</h1>
<p class=warn><b>Testnet toy:</b> keys live on the server (custodial) — fine for worthless
test watts, never for real money. No mining here; miners run <tt>miner_node.py</tt>.</p>
<div id=setup><button id=new>＋ Create wallet</button></div>
<div id=app style="display:none">
<p>Address: <code id=addr></code><br>Balance: <b id=bal></b> <small id=keys></small></p>
<input id=to placeholder="pq1... recipient">
<input id=amt placeholder="watts (e.g. 1000)">
<button id=send>Send</button> <button id=refresh>Refresh</button>
<button id=rot title="Fresh key + sweep balance before keys run out">Rotate key</button>
<h3>Transfers</h3><div id=txs></div>
</div>
<p id=log>idle</p>
<p><a href='/'>faucet</a> &middot; <a href='/blocks'>explorer</a></p>
<script>
const log = m => document.getElementById('log').textContent = m;
async function me(){const r = await fetch('/api/web/me');return r.json();}
async function refresh(){
 const m = await me();
 if(!m.ok){document.getElementById('setup').style.display='';document.getElementById('app').style.display='none';log(m.msg);return;}
 document.getElementById('setup').style.display='none';document.getElementById('app').style.display='';
 document.getElementById('addr').textContent = m.addr;
 document.getElementById('bal').textContent = m.balance_watts >= 1e6
  ? (m.balance_watts/1e6).toFixed(3)+' INFR' : m.balance_watts+' watts';
 document.getElementById('keys').textContent = `(${m.ots_left} sig keys left` +
  (m.ots_left < 32 ? ' — ROTATE SOON' : '') + ')';
 document.getElementById('txs').innerHTML = m.transfers.map(t =>
  `<div>${t[0]===m.addr?'&larr;':'&rarr;'} ${t[2]} watts <small>${(t[0]===m.addr?t[1]:t[0]).slice(0,18)}…</small></div>`).join('') || '(none yet — claim the faucet)';
 log('synced');
}
document.getElementById('new').onclick = async () => {
 const r = await (await fetch('/api/web/new',{method:'POST'})).json();
 log(r.ok ? 'wallet created' : 'denied: '+r.msg); refresh();
};
document.getElementById('send').onclick = async () => {
 const r = await (await fetch('/api/web/send',{method:'POST',headers:{'Content-Type':'application/json'},
  body:JSON.stringify({to:document.getElementById('to').value.trim(),
  amount:parseInt(document.getElementById('amt').value)})})).json();
 log(r.ok ? 'queued — confirms in next block' : 'denied: '+r.msg); refresh();
};
document.getElementById('refresh').onclick = refresh;
document.getElementById('rot').onclick = async () => {
 if(!confirm('Move full balance to a fresh key?')) return;
 const r = await (await fetch('/api/web/rotate',{method:'POST'})).json();
 log(r.ok ? `rotated to ${r.addr.slice(0,18)}… (${r.swept_watts} watts swept)` : 'rotate denied: '+r.msg);
 refresh();
};
refresh();
</script></body></html>"""

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
<p>Got watts? Open the <a href='/wallet'>web wallet</a> to hold and send them &middot; <a href='/blocks'>explorer</a></p>
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
    if len(line) >= RECV_LIMIT or not line.endswith(b"\n"):
        raise ValueError("frame too big or incomplete")
    obj = json.loads(line.decode())
    if not isinstance(obj, dict):
        raise ValueError("RPC frame must be an object")
    return obj


def peer_address(peer):
    host, port = peer.rsplit(":", 1)
    if not host or ":" in host or not 1 <= int(port) <= 65535:
        raise ValueError("peer must be an IPv4 address or hostname with port 1..65535")
    return host, int(port)


def gateway_rpc(peer, obj, pin="", timeout=20):
    """Pinned PQ RPC, or explicit plaintext for the local demos."""
    host, port = peer_address(peer)
    if pin:
        import pqchannel
        pub = pqchannel.fetch_server_key(host, port, timeout)
    raw = socket.create_connection((host, port), timeout=timeout)
    try:
        s = pqchannel.client_connect(raw, pub, pin) if pin else raw
        with s, s.makefile("rwb") as f:
            send_msg(f, obj)
            return recv_msg(f)
    finally:
        raw.close()


class GatewayServer:
    def __init__(self, conn, host="127.0.0.1", port=19191, tls=False,
                 cert_path="gateway-cert.pem", key_path="gateway-key.pem",
                 peers=(), block_mode=False, chain_path="chain.dat",
                 bits=2 ** 236, mine_interval=15, reward_addr="",
                 block_reward=5_000_000, empty_every=30, pq=False,
                 kem_path="gateway-kem.pem"):
        from eval import TASKS
        from chain import load_chain
        self.conn, self.tasks = conn, list(TASKS)
        self.lock = threading.Lock()
        self.seq = 0
        self.peers = list(peers)
        self.self_id = f"{host}:{port}"
        self.tls, self.ctx = tls, None
        # PQ transport (ML-KEM-768 + AES-256-GCM). --tls maps here too:
        # the old ECDSA-TLS path is retired (Shor-vulnerable), same UX.
        self.pq = pq or tls
        self.kem_sk, self.kem_pin = None, ""
        if self.pq:
            import pqchannel as _pq
            self.kem_sk, self.kem_pin = _pq.server_keypair(kem_path)
            print(f"PQ channel on, kem pin: {self.kem_pin}", flush=True)
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
        self._hits_lock = threading.Lock()
        self.challenges = {}  # challenge_id -> (challenge, expiry)
        self.checkpoint_doc = None
        import collections as _c
        self.infer_q = _c.deque()  # open-prompt jobs awaiting a miner
        self.infer_results = {}  # job_id -> {status, output, miner, ms, customer}
        self.infer_prompts = {}  # job_id -> prompt (scoring context after serve)
        self.infer_terms = {}  # job_id -> (customer, quoted_watts) for auto-settle
        self.D1_PAY_THRESHOLD = 0.3  # d1 score at/above this settles automatically
        self.infer_seq = 0
        self.miner_stats = {}  # miner_id -> [accepted, total] (customer feedback)
        self.d1_hist = {}  # miner_id -> deque(maxlen=20) of recent d1 scores
        self.miner_seen = {}  # miner_id -> last-seen epoch seconds
        self.served_outputs = _c.deque(maxlen=500)  # (prompt_hash, output_hash, ts): replay radar
        self.REPLAY_WINDOW_SEC = 24 * 3600
        self.D1_LOW = 0.3  # below: deprioritized for paid infer work
        self.D1_GOOD = 0.5  # at/above: preferred
        self.ACTIVE_TTL = 120  # seconds a miner counts as connected
        self.MIN_INFER_STAKE_WATTS = 1_000_000  # 1 INFR locked to serve paid infer (Sybil toll)
        self.chain = load_chain(chain_path)  # corrupt history must stop startup
        if self.chain.tip:
            print(f"loaded chain: height={self.chain.height} tip={self.chain.tip[:16]}..", flush=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((host, port))
        self.sock.listen(16)
        self.port = self.sock.getsockname()[1]
        self.self_id = f"{host}:{self.port}"
        self._connections = threading.BoundedSemaphore(128)
        self._stop = threading.Event()

    def _recv_or_http(self, f, ip="local"):
        """Read one frame; serve HTTP routes (healthcheck, faucet site/API),
        else return the parsed JSON protocol message (None = HTTP handled)."""
        line = f.readline(RECV_LIMIT)
        if not line:
            raise ConnectionError("peer closed")
        if line[:3] not in (b"GET", b"HEA", b"POS", b"OPT"):
            if len(line) >= RECV_LIMIT or not line.endswith(b"\n"):
                raise ValueError("frame too big or incomplete")
            obj = json.loads(line.decode())
            if not isinstance(obj, dict):
                raise ValueError("RPC frame must be an object")
            return obj
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
        if path == "/blocks" and method == "GET":
            self._http(f, 200, self._explorer_blocks().encode(), "text/html; charset=utf-8")
            return None
        if path.startswith("/block/") and method == "GET":
            self._http(f, 200, self._explorer_block(path[len("/block/"):]).encode(),
                       "text/html; charset=utf-8")
            return None
        if path == "/wallet" and method == "GET":
            self._http(f, 200, WALLET_HTML.encode(), "text/html; charset=utf-8")
            return None
        if path == "/api/checkpoint" and method == "GET":
            doc = self.checkpoint_doc
            if doc is None:
                self._http(f, 404, b'{"ok":false,"msg":"no checkpoint"}', "application/json")
            else:
                self._http(f, 200, json.dumps(doc).encode(), "application/json")
            return None
        if path == "/api/web/new" and method == "POST":
            self._web_new(f, ip)
            return None
        if path == "/api/web/me" and method == "GET":
            self._web_me(f, headers)
            return None
        if path == "/api/web/send" and method == "POST":
            self._web_send(f, headers, body)
            return None
        if path == "/api/web/rotate" and method == "POST":
            self._web_rotate(f, headers)
            return None
        self._http(f, 200, b'{"ok":true,"service":"infer-gateway","protocol":"tcp+json"}',
                   "application/json")
        return None

    @staticmethod
    def _http(f, code, body: bytes, ctype: str):
        f.write(f"HTTP/1.1 {code} {'OK' if code == 200 else 'ERR'}\r\nContent-Type: {ctype}\r\n"
                f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        f.flush()

    # -- custodial web wallet (TESTNET ONLY) -------------------------------
    # Keys live server-side so browsers need no PQ-in-JS. That makes the
    # gateway a custodian: fine for worthless test watts, NEVER for real
    # money. Sessions = random 256-bit tokens in HttpOnly cookies.
    WEB_SESSION_DAYS = 30
    WEB_NEW_PER_IP_PER_DAY = 10

    @staticmethod
    def _cookies(headers: dict) -> dict:
        out = {}
        for part in headers.get("cookie", "").split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                out[k.strip()] = v.strip()
        return out

    def _web_tables(self):
        self.conn.execute("CREATE TABLE IF NOT EXISTS web_wallets(addr TEXT PRIMARY KEY, seed_hex TEXT)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS web_sessions(token TEXT PRIMARY KEY, addr TEXT, expiry REAL)")
        self.conn.execute("CREATE TABLE IF NOT EXISTS web_creates(ip TEXT, day INTEGER)")

    def _web_session_addr(self, headers: dict):
        import hashlib as _hl
        import time as _t
        tok = self._cookies(headers).get("infer_session", "")
        if not tok or len(tok) > 128:
            return None
        with self.lock:
            self._web_tables()
            row = self.conn.execute("SELECT addr, expiry FROM web_sessions WHERE token=?",
                                    (_hl.sha256(tok.encode()).hexdigest(),)).fetchone()
        return row[0] if row and row[1] > _t.time() else None

    def _web_new(self, f, ip):
        import hashlib as _hl
        import secrets as _s
        import time as _t
        import pq as _pq
        with self.lock:
            self._web_tables()
            day = int(_t.time() // 86400)
            n = self.conn.execute("SELECT COUNT(*) FROM web_creates WHERE ip=? AND day=?",
                                  (ip, day)).fetchone()[0]
            if n >= self.WEB_NEW_PER_IP_PER_DAY:
                self.conn.commit()
                self._http(f, 429, b'{"ok":false,"msg":"too many wallets today"}', "application/json")
                return
            k = _pq.generate()
            root = _pq.root_of(bytes.fromhex(k["seed"])).hex()
            from ledger import register_pq
            register_pq(self.conn, k["addr"], root)
            self.conn.execute("INSERT INTO web_wallets VALUES(?,?)", (k["addr"], k["seed"]))
            tok = _s.token_hex(32)
            self.conn.execute("INSERT INTO web_sessions VALUES(?,?,?)",
                              (_hl.sha256(tok.encode()).hexdigest(), k["addr"],
                               _t.time() + self.WEB_SESSION_DAYS * 86400))
            self.conn.execute("INSERT INTO web_creates VALUES(?,?)", (ip, day))
            self.conn.commit()
        f.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n"
                f"Set-Cookie: infer_session={tok}; HttpOnly; SameSite=Lax; Path=/; "
                f"Max-Age={self.WEB_SESSION_DAYS * 86400}\r\n".encode())
        body = ('{"ok":true,"addr":"%s"}' % k["addr"]).encode()
        f.write(f"Content-Length: {len(body)}\r\nConnection: close\r\n\r\n".encode() + body)
        f.flush()

    def _web_me(self, f, headers):
        addr = self._web_session_addr(headers)
        if addr is None:
            self._http(f, 401, b'{"ok":false,"msg":"no session (create a wallet first)"}',
                       "application/json")
            return
        from ledger import balance_watts, next_ots
        with self.lock:
            bal = balance_watts(self.conn, addr)
            left = 256 - next_ots(self.conn, addr)
            txs = self.conn.execute(
                "SELECT from_id, to_id, amount_watts FROM transfers "
                "WHERE from_id=? OR to_id=? ORDER BY id DESC LIMIT 20", (addr, addr)).fetchall()
        self._http(f, 200, json.dumps(
            {"ok": True, "addr": addr, "balance_watts": bal, "ots_left": left,
             "transfers": txs}).encode(),
            "application/json")

    def _web_rotate(self, f, headers):
        """Fresh key + sweep full balance before OTS exhaustion bricks the wallet."""
        addr = self._web_session_addr(headers)
        if addr is None:
            self._http(f, 401, b'{"ok":false,"msg":"no session"}', "application/json")
            return
        import pq as _pq
        from ledger import balance_watts, next_ots
        with self.lock:
            row = self.conn.execute("SELECT seed_hex FROM web_wallets WHERE addr=?", (addr,)).fetchone()
            bal = balance_watts(self.conn, addr)
            ots = next_ots(self.conn, addr)
        if row is None:
            self._http(f, 400, b'{"ok":false,"msg":"unknown wallet"}', "application/json")
            return
        import hashlib as _hl
        new = _pq.generate()
        new_root = _pq.root_of(bytes.fromhex(new["seed"])).hex()
        pkg = None
        if bal > 0:
            pkg = _pq.sign(row[0], ots, addr, new["addr"], bal)
            ok, msg = self.mempool_add({"type": "transfer", "from": addr, "to": new["addr"],
                                        "amount": bal, "sig": pkg})
            if not ok:
                self._http(f, 400, json.dumps({"ok": False, "msg": msg}).encode(),
                           "application/json")
                return
        with self.lock:
            from ledger import register_pq
            register_pq(self.conn, new["addr"], new_root)
            self.conn.execute("INSERT INTO web_wallets VALUES(?,?)", (new["addr"], new["seed"]))
            tok = self._cookies(headers).get("infer_session", "")
            self.conn.execute("UPDATE web_sessions SET addr=? WHERE token=?",
                              (new["addr"], _hl.sha256(tok.encode()).hexdigest()))
            self.conn.commit()
        self._http(f, 200, json.dumps({"ok": True, "addr": new["addr"], "swept_watts": bal}).encode(),
                   "application/json")

    def _web_send(self, f, headers, body: bytes):
        addr = self._web_session_addr(headers)
        if addr is None:
            self._http(f, 401, b'{"ok":false,"msg":"no session"}', "application/json")
            return
        try:
            req = json.loads(body.decode() or "{}")
            to, amount = req["to"], int(req["amount"])
        except (ValueError, KeyError, TypeError):
            self._http(f, 400, b'{"ok":false,"msg":"need {to, amount}"}', "application/json")
            return
        if not isinstance(to, str) or not to.startswith("pq1") or len(to) > 64 or amount <= 0:
            self._http(f, 400, b'{"ok":false,"msg":"bad to/amount"}', "application/json")
            return
        import pq as _pq
        from ledger import next_ots
        with self.lock:
            row = self.conn.execute("SELECT seed_hex FROM web_wallets WHERE addr=?", (addr,)).fetchone()
            ots = next_ots(self.conn, addr)
        if row is None:
            self._http(f, 400, b'{"ok":false,"msg":"unknown wallet"}', "application/json")
            return
        pkg = _pq.sign(row[0], ots, addr, to, amount)
        ok, msg = self.mempool_add({"type": "transfer", "from": addr, "to": to,
                                    "amount": amount, "sig": pkg})
        self._http(f, 200 if ok else 400, json.dumps({"ok": ok, "msg": msg}).encode(),
                   "application/json")

    def _explorer_blocks(self) -> str:
        from chain import header_hash
        with self.lock:
            tip, height = self.chain.tip, self.chain.height
            rows = []
            b = tip
            for _ in range(50):
                blk = self.chain.blocks.get(b)
                if blk is None:
                    break
                h = blk["header"]
                rows.append((h["height"], header_hash(h)[:16], len(blk["txs"])))
                b = h["prev_hash"] if h["height"] else ""
        items = "".join(f'<tr><td><a href="/block/{hh}">{hh}</a></td><td>{n}</td><td>{c}</td></tr>'
                        for hh, n, c in sorted(rows, reverse=True))
        return (f"<!doctype html><html><head><meta charset=utf8><title>Infer Explorer</title>"
                f"<style>body{{font-family:system-ui,sans-serif;max-width:640px;margin:2em auto;padding:0 1em}}"
                f"table{{border-collapse:collapse;width:100%}}td,th{{border:1px solid #ccc;padding:.4em}}</style>"
                f"</head><body><h1>&#9672; Infer Explorer</h1>"
                f"<p>tip height {height} &middot; <a href='/'>faucet</a></p>"
                f"<table><tr><th>height</th><th>hash</th><th>txs</th></tr>{items}</table></body></html>")

    def _explorer_block(self, which: str) -> str:
        from chain import header_hash
        with self.lock:
            blk = self.chain.blocks.get(which)
            if blk is None:
                for b in self.chain.blocks.values():
                    if str(b["header"]["height"]) == which:
                        blk = b
                        break
            if blk is None:
                return "<h1>unknown block</h1><p><a href='/blocks'>back</a></p>"
            h = blk["header"]
            txrows = "".join(
                f"<tr><td>{t.get('type')}</td><td>{(t.get('from') or t.get('miner') or '')[:20]}</td>"
                f"<td>{(t.get('to') or '')[:20]}</td><td>{t.get('amount', t.get('pay', ''))}</td></tr>"
                for t in blk["txs"])
        return (f"<!doctype html><html><head><meta charset=utf8><title>block {h['height']}</title></head><body>"
                f"<h1>block {h['height']}</h1><p>hash {header_hash(h)}<br>prev {h['prev_hash'][:16]}.."
                f"<br>nonce {h['nonce']} &middot; <a href='/blocks'>all blocks</a></p>"
                f"<table border=1 cellpadding=4><tr><th>type</th><th>from/miner</th><th>to</th><th>amount/pay</th></tr>"
                f"{txrows}</table></body></html>")

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
        with self._hits_lock:
            now = _t.monotonic()
            dq = self.hits.setdefault(ip, collections.deque())
            while dq and now - dq[0] > window:
                dq.popleft()
            if len(dq) >= limit:
                return False
            dq.append(now)
            return True

    def serve_forever(self):
        while not self._stop.is_set():
            try:
                cs, addr = self.sock.accept()
            except OSError:
                if self._stop.is_set():
                    return
                raise
            if not self._connections.acquire(blocking=False):
                cs.close()
                continue
            threading.Thread(target=self._connection, args=(cs, addr[0]), daemon=True).start()

    def close(self):
        self._stop.set()
        try:
            self.sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        self.sock.close()

    def _connection(self, cs, ip):
        import struct as _st
        import time as _pt
        try:
            cs.settimeout(10)
            deadline = _pt.monotonic() + 10
            while True:
                peek = cs.recv(6, socket.MSG_PEEK)  # never append: PEEK repeats the same bytes
                if not peek:
                    return
                if len(peek) >= 6:
                    break
                if _pt.monotonic() >= deadline:
                    raise TimeoutError("protocol prefix timeout")
                _pt.sleep(0.01)
            if peek.startswith(b"PQKEY"):
                import pqchannel as _pqk
                if _pqk._recvn_raw(cs, 6) != b"PQKEY\n":
                    return
                pub = _pqk._pub_bytes(self.kem_sk) if self.kem_sk else b""
                cs.sendall(_st.pack(">H", len(pub)) + pub)
                return
            if peek[:1] == b"\x16":
                return  # legacy TLS hello: retired
            if self.pq and peek.startswith(b"PQ1\n"):
                import pqchannel as _pqh
                _pqh._recvn_raw(cs, 4)
                cs = _pqh.server_accept(cs, self.kem_sk)
            raw = cs.sock if hasattr(cs, "sock") else cs
            raw.settimeout(120)
            self.handle(cs, ip)
        except (ConnectionError, OSError, ValueError, TypeError):
            pass
        finally:
            cs.close()
            self._connections.release()

    def handle(self, cs, ip="local"):
        from miner import sha
        from eval import TASKS, check, score_job
        from ledger import log_job, register_miner
        try:
            with cs, cs.makefile("rwb") as f:
                hello = self._recv_or_http(f, ip)
                if hello is None:  # was an HTTP healthcheck; answered already
                    return
                if hello.get("op") in ("get_tip", "get_block", "get_blocks", "peers", "submit_block", "mempool",
                                        "balance", "history", "next_ots", "submit_tx",
                                        "template", "get_mempool", "infer_submit", "infer_result",
                                        "feedback"):
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
                infer_ok = bool(hello.get("infer"))  # new clients also take open prompts
                self.note_seen(hello.get("miner_id", ""))
                while True:
                    req = recv_msg(f)
                    if req.get("op") == "get_job":
                        with self.lock:
                            ij = None
                            if infer_ok and self.infer_q:
                                # route to this poller only if no better miner is active;
                                # low-quality miners keep task flow (still paid by subsidy)
                                import time as _gt
                                self.note_seen(req.get("miner_id", "") or hello.get("miner_id", ""))
                                me = req.get("miner_id", "") or hello.get("miner_id", "")
                                if self.infer_eligible(me):
                                    ij = self.infer_q.popleft()
                            if ij is None:
                                self.seq += 1
                                task = self.tasks[self.seq % len(self.tasks)]
                                jid = f"p2p-{self.seq}"
                        if ij is not None:
                            send_msg(f, {"kind": "infer", "job_id": ij["job_id"],
                                         "prompt": ij["prompt"], "max_tokens": ij["max_tokens"],
                                         "customer": ij["customer"]})
                            continue
                        send_msg(f, {"kind": "task", "job_id": jid, "task": task, "is_eval": False})
                    elif req.get("op") == "submit":
                        if not self._throttle(ip):
                            send_msg(f, {"ok": False, "msg": "rate limited, slow down"})
                        else:
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
        except (ConnectionError, OSError, ValueError, KeyError, TypeError):
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
                for m in self.mempool:  # unconfirmed spends reserve their index too
                    if m.get("from") == req["miner_id"]:
                        try:
                            n = max(n, int(m["sig"]["ots"]) + 1)
                        except (KeyError, TypeError, ValueError):
                            pass
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
        if op == "get_mempool":
            from chain import MAX_BLOCK_REWARD_WATTS
            with self.lock:
                send_msg(f, {"queued": len(self.mempool), "tip": self.chain.tip,
                             "height": self.chain.height,
                             "txs": self.mempool[:200], "cap_watts": MAX_BLOCK_REWARD_WATTS})
            return True
        if op == "infer_submit":
            prompt = str(req.get("prompt", ""))[:4000]
            max_tok = max(16, min(int(req.get("max_tokens", 256)), 1024))
            if not prompt.strip():
                send_msg(f, {"ok": False, "msg": "empty prompt"})
                return True
            with self.lock:
                rows = self.conn.execute("SELECT MIN(price_per_1k) FROM miners").fetchone()
                floor = rows[0] if rows and rows[0] else 0.005
                quote = max(100, int((len(prompt) // 4 + max_tok) / 1000 * floor * 1000000))
                self.infer_seq += 1
                jid = f"inf-{self.infer_seq}"
                self.infer_q.append({"job_id": jid, "prompt": prompt, "max_tokens": max_tok,
                                     "customer": req.get("customer", ""), "quoted_watts": quote})
                self.infer_prompts[jid] = prompt
                self.infer_terms[jid] = (req.get("customer", ""), quote)
            send_msg(f, {"ok": True, "job_id": jid, "quoted_watts": quote})
            return True
        if op == "infer_result":
            with self.lock:
                r = self.infer_results.get(req.get("job_id", ""))
            if r is not None and r.get("status") != "done":
                send_msg(f, {"ok": False, "status": r.get("status", "pending")})
            else:
                send_msg(f, dict({"ok": r is not None}, **(r or {})))
            return True
        if op == "submit_infer":
            from miner import sha
            import time as _rt0
            with self.lock:
                ctx0 = self.infer_prompts.get(req["job_id"], "")
            ph = sha(ctx0)
            oh = sha(str(req.get("output", "")))
            with self.lock:
                replay = any(p == ph and o == oh and _rt0.time() - ts < self.REPLAY_WINDOW_SEC
                             for p, o, ts in self.served_outputs)
                if not replay:
                    self.served_outputs.append((ph, oh, _rt0.time()))
                self.infer_results[req["job_id"]] = {
                    "status": "scoring", "output": str(req.get("output", ""))[:8000],
                    "miner": req.get("miner_id", ""), "ms": float(req.get("latency_ms", 0))}
                st = self.miner_stats.setdefault(req.get("miner_id", ""), [0, 0])
                st[1] += 1
                self.note_seen(req.get("miner_id", ""))
            # Replay radar: identical output already served for this prompt
            # recently. No d1 call wasted, score forced 0, no pay. Deterministic
            # (temp-0) miners should add nonces; exact repeats look like theft.
            # d1 quality score gates payment automatically: at/above threshold
            # the quoted watts settle with no customer action, so answers
            # can't be taken for free and good work can't be stiffed.
            # Ground truth (unit tests) still rules code tasks; feedback stays
            # as a pure rating signal.
            # Scored BEFORE ack: rows are complete at first read, so pollers
            # never see unstamped results (that race showed phantom "unscored").
            if replay:
                q = 0.0
            else:
                # Jury panel: local d1 + hosted Jev (if key), median wins.
                # One judge down = the other decides alone (recorded). Both down = abstain.
                try:
                    from judge import DJudge
                    with self.lock:
                        ctx = self.infer_prompts.get(req["job_id"], "general inference")
                    panel = DJudge(timeout=45).panel(ctx, str(req.get("output", ""))[:2000])
                    q = panel["score"]
                    with self.lock:
                        if req["job_id"] in self.infer_results:
                            self.infer_results[req["job_id"]]["judges"] = panel["sources"]
                            if panel["disagree"]:
                                self.infer_results[req["job_id"]]["judges_disagree"] = True
                except Exception:
                    q = None
            paid = 0
            note = "judge abstained — no score, no pay"
            if q is not None and q < self.D1_PAY_THRESHOLD:
                note = f"d1={q:.2f} below pay line — miner unpaid"
            if replay:
                note = "replay detected — identical output already served (no pay)"
            if q is not None:
                with self.lock:
                    if req["job_id"] in self.infer_results:
                        self.infer_results[req["job_id"]]["d1_quality"] = round(q, 3)
                    import collections as _dq
                    self.d1_hist.setdefault(req.get("miner_id", ""), _dq.deque(maxlen=20)).append(q)
            # AUTO-SETTLE: score at/above threshold pays quoted watts immediately.
            # No accept button anywhere: customers can't take answers for free,
            # miners can't be stiffed. Below threshold: no pay, stats dinged.
            if q is not None and q >= self.D1_PAY_THRESHOLD:
                from ledger import balance_watts
                from money import split_customer_fee
                with self.lock:
                    terms = self.infer_terms.pop(req["job_id"], ("", 0))
                cust, quote = terms
                miner = req.get("miner_id", "")
                if cust and quote > 0 and balance_watts(self.conn, cust) >= quote:
                    m_amt, b_amt = split_customer_fee(quote)
                    import time as _st
                    with self.lock:
                        self.conn.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts)"
                                          " VALUES(?,?,?,?)", (cust, miner, m_amt, _st.time()))
                        if b_amt:
                            self.conn.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts)"
                                              " VALUES(?,?,?,?)", (cust, "burn", b_amt, _st.time()))
                        self.conn.commit()
                        if req["job_id"] in self.infer_results:
                            self.infer_results[req["job_id"]]["paid_watts"] = m_amt
                    paid = m_amt
                    note = f"auto-paid {m_amt} watts"
                else:
                    note = "customer insolvent — fund wallet first (faucet/mining)"
            if paid == 0:
                with self.lock:
                    self.infer_terms.pop(req["job_id"], None)
                    if req["job_id"] in self.infer_results:
                        self.infer_results[req["job_id"]]["pay_note"] = note
            with self.lock:
                if req["job_id"] in self.infer_results:
                    self.infer_results[req["job_id"]]["status"] = "done"
            send_msg(f, {"ok": True})
            return True
        if op == "feedback":
            with self.lock:
                st = self.miner_stats.setdefault(req.get("miner_id", ""), [0, 0])
                if req.get("accept"):
                    st[0] += 1
                st[1] = max(st[1], 1)
                acc = st[0] / st[1]
            send_msg(f, {"ok": True, "accept_rate": acc})
            return True
        if op in ("stake", "unstake"):
            from ledger import stake_lock, stake_unlock, staked_watts
            try:
                addr, amt, pkg = req["addr"], int(req["amount"]), req["sig"]
                (stake_lock if op == "stake" else stake_unlock)(self.conn, addr, amt, pkg)
                send_msg(f, {"ok": True, "staked_watts": staked_watts(self.conn, addr)})
            except (ValueError, KeyError, TypeError) as e:
                send_msg(f, {"ok": False, "msg": str(e)[:120]})
            return True
        if op == "template":
            with self.lock:
                tip, height = self.chain.tip, self.chain.height
                bits = self.chain.next_bits(tip) if tip else self.bits
            send_msg(f, {"prev_hash": tip or "00" * 32, "height": (height + 1) if tip else 0,
                         "bits": bits})
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
            with self.lock:
                send_msg(f, {"tip": self.chain.tip, "height": self.chain.height})
            return True
        if op == "get_blocks":
            b, blocks, size = req.get("hash", ""), [], 128
            limit = max(1, min(int(req.get("limit", 50)), 50))
            with self.lock:
                for _ in range(limit):
                    blk = self.chain.blocks.get(b)
                    if blk is None:
                        break
                    n = len(json.dumps(blk).encode()) + 2
                    if size + n >= RECV_LIMIT - 256:
                        break
                    blocks.append(blk)
                    size += n
                    b = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
            send_msg(f, {"ok": bool(blocks), "blocks": blocks, "next": b})
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
                if ok and msg != "already known":
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

    def load_checkpoint(self, path: str, pinned_root: str) -> tuple[bool, str]:
        """Verify operator PQ signature + pin root, then enforce on chain."""
        import json as _json
        import pq as _pq
        try:
            with open(path) as f:
                doc = _json.load(f)
        except (OSError, ValueError) as e:
            return False, f"unreadable: {e}"
        if not isinstance(doc, dict):
            return False, "checkpoint must be an object"
        try:
            msg = f"checkpoint|{doc['height']}|{doc['hash']}"
            ok = (doc["root"] == pinned_root and
                  _pq.verify(doc["addr"], doc["root"], doc["sig"], msg, int(doc["height"])))
        except (KeyError, ValueError, TypeError):
            ok = False
        if not ok:
            return False, "bad checkpoint signature or wrong operator root"
        if (int(doc["height"]) < 0 or not isinstance(doc["hash"], str) or len(doc["hash"]) != 64 or
                any(c not in "0123456789abcdef" for c in doc["hash"])):
            return False, "bad checkpoint height/hash"
        with self.lock:
            self.checkpoint_doc = doc
            self.chain.set_checkpoint(int(doc["height"]), doc["hash"])
        return True, f"checkpoint height={doc['height']} enforced"

    def d1_avg(self, miner_id: str):
        """Rolling mean quality, None for unmeasured miners (probation: eligible)."""
        import collections as _c
        h = self.d1_hist.get(miner_id)
        if not h:
            return None
        return sum(h) / len(h)

    def note_seen(self, miner_id: str):
        import time as _t
        if miner_id:
            self.miner_seen[miner_id] = _t.time()

    def infer_eligible(self, miner_id: str) -> bool:
        """Paid infer work goes to the capable first. Below-threshold miners
        get tasks (still paid by subsidy) unless nobody better is around —
        coverage beats purity, but quality earns the premium flow.
        No stake locked, no paid infer at all: spawning identities is free,
        losing stake is not (Sybil toll)."""
        import time as _t
        from ledger import staked_watts
        if staked_watts(self.conn, miner_id) < self.MIN_INFER_STAKE_WATTS:
            return False
        avg = self.d1_avg(miner_id)
        if avg is None or avg >= self.D1_LOW:
            return True
        now = _t.time()
        for m, seen in self.miner_seen.items():
            if m != miner_id and now - seen < self.ACTIVE_TTL and (self.d1_avg(m) or 0) >= self.D1_GOOD:
                return False
        return True

    def _has_ots(self):
        return self.conn.execute("SELECT 1 FROM sqlite_master WHERE name='ots_used'").fetchone()

    # -- block mode: mempool -> PoW -> tip -> ledger ----------------------
    def persist(self, block):
        from chain import save_block
        with open(self.chain_path, "ab") as f:
            save_block(f, block)

    def mempool_add(self, tx: dict) -> tuple[bool, str]:
        with self.lock:
            return self._mempool_add_locked(tx)

    def _mempool_add_locked(self, tx):
        if len(self.mempool) >= 500:
            return False, "mempool full"
        if tx.get("type") == "transfer":
            import pq
            row = self.conn.execute("SELECT root_hex FROM pq_accounts WHERE addr=?",
                                    (tx["from"],)).fetchone()
            if row is None:
                return False, "unknown PQ sender"
            if not pq.verify(tx["from"], row[0], tx["sig"], tx["to"], tx["amount"]):
                return False, "bad PQ signature"
            try:
                ots = int(tx["sig"]["ots"])
            except (KeyError, TypeError, ValueError):
                return False, "bad OTS index"
            used = self.conn.execute(
                "SELECT 1 FROM transfers WHERE from_id=? AND ots=?", (tx["from"], ots)).fetchone()
            if not used and self._has_ots():
                used = self.conn.execute(
                    "SELECT 1 FROM ots_used WHERE addr=? AND ots=?", (tx["from"], ots)).fetchone()
            if not used:
                used = any(m.get("from") == tx["from"] and
                           int(m.get("sig", {}).get("ots", -1)) == ots for m in self.mempool)
            if used:
                return False, f"OTS #{ots} already spent or queued — wallet must use a fresh index"
        elif tx.get("type") not in ("job",):
            return False, "only transfer/job relayed (mint/slash are protocol)"
        self.mempool.append(tx)
        return True, "queued (unconfirmed)"

    def mine_round(self) -> dict | None:
        """Assemble + mine one block. Slow PoW runs WITHOUT the lock.
        Coinbase (capped protocol mint) is always tx[0] when reward set."""
        from chain import mine_block, header_hash
        with self.lock:
            txs = self.mempool[:200]
            tip, height = self.chain.tip, self.chain.height
            if not txs and tip:
                # idle: heartbeat empty block only every N rounds, else sleep
                self._idle_rounds += 1
                if self._idle_rounds < self.empty_every:
                    return None
                self._idle_rounds = 0
        if tip and self.reward_addr and self.block_reward:
            from chain import split_coinbase
            txs = split_coinbase(self.block_reward, self.reward_addr, txs,
                                 height=height + 1) + txs
        if not tip:
            txs = []  # genesis carries no user txs (consensus rule)
            blk = mine_block("00" * 32, 0, self.bits, txs)
        else:
            with self.lock:
                bits = self.chain.next_bits(tip)
            blk = mine_block(tip, height + 1, bits, txs)
        with self.lock:
            if self.chain.tip != tip:
                return None  # stale work must not evict transactions from the new tip
            ok, msg = self.chain.add(blk)
            if not ok:
                return None
            self.persist(blk)
            self.apply_tip_locked()
        print(f"mined block {blk['header']['height']} {header_hash(blk['header'])[:12]}.. "
              f"({len(txs)} txs, tip={self.chain.height})", flush=True)
        return blk

    def apply_tip_locked(self):
        """Ledger follows the tip. Forward-apply on extension, full reindex
        on reorg. INVALID txs are SKIPPED (logged), never fatal: one bad
        transfer must not freeze the chain (lesson learned on testnet)."""
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
                self._apply_block(blk)
        else:
            self.conn.execute("UPDATE miners SET slashed_watts=slashed_watts-COALESCE("
                              "(SELECT SUM(amount) FROM chain_slashes WHERE entity=miners.id AND kind='miner'),0)")
            self.conn.execute("DELETE FROM chain_slashes")
            miners = list(self.conn.execute("SELECT * FROM miners"))
            accounts = list(self.conn.execute("SELECT * FROM pq_accounts"))
            self.conn.execute("CREATE TABLE IF NOT EXISTS chain_mints(txid TEXT PRIMARY KEY, transfer_id INTEGER)")
            issued = list(self.conn.execute("SELECT * FROM transfers WHERE from_id IN ('faucet','subsidy') "
                                            "AND id NOT IN (SELECT transfer_id FROM chain_mints)"))
            self.conn.execute("CREATE TABLE IF NOT EXISTS applied_txid(txid TEXT PRIMARY KEY)")
            self.conn.execute("DELETE FROM jobs")
            self.conn.execute("DELETE FROM transfers")
            self.conn.execute("DELETE FROM slashes")
            self.conn.execute("DELETE FROM applied_txid")
            self.conn.execute("DELETE FROM chain_mints")
            self.conn.commit()
            for m in miners:
                self.conn.execute("INSERT OR REPLACE INTO miners VALUES(?,?,?,?,?)", tuple(m))
            for a_ in accounts:
                self.conn.execute("INSERT OR REPLACE INTO pq_accounts VALUES(?,?)", tuple(a_))
            for row in issued:  # only out-of-chain issuance survives; orphaned rewards do not
                self.conn.execute("INSERT OR IGNORE INTO transfers VALUES(?,?,?,?,?,?,?)", tuple(row))
            self.conn.commit()
            for blk in self.chain.path_to(new_tip):
                self._apply_block(blk)
        self.applied_tip = new_tip
        from chain import txid
        self.conn.execute("CREATE TABLE IF NOT EXISTS applied_txid(txid TEXT PRIMARY KEY)")
        self.mempool = [t for t in self.mempool if not self.conn.execute(
            "SELECT 1 FROM applied_txid WHERE txid=?", (txid(t),)).fetchone()]

    def _apply_block(self, blk):
        from chain import apply_tx
        h = blk["header"]["height"]
        for t in blk["txs"]:
            try:
                apply_tx(self.conn, t)
            except Exception as e:
                print(f"block {h}: skipping invalid tx ({t.get('type')}/{t.get('from','-')[:12]}): {e}",
                      flush=True)

    def start_mining(self):
        def loop():
            while not self._stop.wait(self.mine_interval):
                try:
                    self.mine_round()
                except Exception as e:
                    print(f"mine round failed: {e}", flush=True)
        threading.Thread(target=loop, daemon=True).start()

    def sync_peer(self, peer, pin="", max_depth=10000):
        """Fetch bounded batches backwards, then validate forward. Height is not work."""
        from chain import header_hash
        t = gateway_rpc(peer, {"op": "get_tip"}, pin)
        h, blocks, size = t["tip"], [], 0
        with self.lock:
            if not h or h == self.chain.tip:
                return 0
        while h:
            with self.lock:
                if h in self.chain.blocks:
                    break
            if len(blocks) >= max_depth or size >= 64 * RECV_LIMIT:
                raise ValueError("sync budget exceeded; increase max_depth or use a nearer seed")
            r = gateway_rpc(peer, {"op": "get_blocks", "hash": h,
                                  "limit": min(50, max_depth - len(blocks))}, pin)
            if not r.get("ok") or not r.get("blocks"):
                raise ValueError("seed returned no blocks (upgrade both gateways)")
            for blk in r["blocks"]:
                if header_hash(blk["header"]) != h:
                    raise ValueError("seed returned the wrong block hash")
                with self.lock:
                    if h in self.chain.blocks:
                        break
                blocks.append(blk)
                size += len(json.dumps(blk).encode())
                h = blk["header"]["prev_hash"] if blk["header"]["height"] else ""
                if len(blocks) > max_depth or size > 64 * RECV_LIMIT:
                    raise ValueError("sync budget exceeded")
        added = 0
        with self.lock:
            for blk in reversed(blocks):
                ok, msg = self.chain.add(blk)
                if not ok:
                    self.apply_tip_locked()
                    raise ValueError(f"invalid seed block: {msg}")
                if msg != "already known":
                    self.persist(blk)
                    added += 1
            self.apply_tip_locked()
        return added

    def start_peer_sync(self, peers: list[str], interval: int = 30, max_depth: int = 10000,
                        pins=None):
        pins = pins or {}

        def loop():
            while not self._stop.is_set():
                for peer in peers:
                    try:
                        added = self.sync_peer(peer, pins.get(peer, ""), max_depth)
                        if added:
                            print(f"synced {added} blocks from {peer} (tip={self.chain.height})", flush=True)
                    except (ConnectionError, OSError, ValueError, KeyError, TypeError) as e:
                        print(f"peer sync {peer}: {e}", flush=True)
                if self._stop.wait(interval):
                    return
        threading.Thread(target=loop, daemon=True).start()


class MinerClient:
    def __init__(self, miner_id, backend, host="127.0.0.1", port=19191,
                 model="?", price=0.0, infer=False):
        self.sock = socket.create_connection((host, port), timeout=10)
        self.f = self.sock.makefile("rwb")
        self.miner_id, self.backend = miner_id, backend
        reg = {"op": "register", "miner_id": miner_id, "model": model, "price": price}
        if infer:
            reg["infer"] = True
        send_msg(self.f, reg)
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

    def run_mixed_once(self):
        """One unit of work: open-prompt inference if offered, else a task."""
        import time
        send_msg(self.f, {"op": "get_job"})
        job = recv_msg(self.f)
        if job.get("kind") == "infer":
            t0 = time.time()
            out, _ = self.backend.generate_raw(job["prompt"], job.get("max_tokens", 256))
            lat = (time.time() - t0) * 1000
            send_msg(self.f, {"op": "submit_infer", "job_id": job["job_id"],
                              "miner_id": self.miner_id, "output": out, "latency_ms": lat})
            return ("infer", job["job_id"], recv_msg(self.f))
        if "task" not in job:
            raise ConnectionError(job.get("msg", "get_job rejected"))
        t0 = time.time()
        code, _ = self.backend.generate(job["task"])
        lat = (time.time() - t0) * 1000
        send_msg(self.f, {"op": "submit", "job_id": job["job_id"], "miner_id": self.miner_id,
                          "task": job["task"], "code": code, "latency_ms": lat})
        return ("task", job["task"], recv_msg(self.f))

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


class WANMiner:
    """WAN client: ML-KEM-768 pinned channel + PQ-signed submits. No ECDSA/TLS anywhere."""

    def __init__(self, key: dict, backend, host, port, pin, model="?", price=0.0, infer=False):
        import pq
        import socket as _s
        import pqchannel as _pq
        self.key, self.backend = key, backend
        self.mid = key["addr"]
        self.ots = 0  # local counter; server is source of truth on collisions
        pub = _pq.fetch_server_key(host, port)
        raw = _s.create_connection((host, port), timeout=15)
        ch = _pq.client_connect(raw, pub, pin)
        self.sock, self.f = ch, ch.makefile("rwb")
        send_msg(self.f, {"op": "register_pq", "addr": self.mid,
                          "root_hex": pq.root_of(bytes.fromhex(key["seed"])).hex()})
        r = recv_msg(self.f)
        assert r.get("ok"), r
        reg = {"op": "register", "miner_id": self.mid, "model": model, "price": price}
        if infer:
            reg["infer"] = True
        send_msg(self.f, reg)
        r = recv_msg(self.f)
        assert r.get("ok"), r

    def run_mixed_once(self):
        """Same dual protocol as MinerClient: infer jobs or signed tasks."""
        import time
        import pq
        from miner import sha
        send_msg(self.f, {"op": "get_job"})
        job = recv_msg(self.f)
        if job.get("kind") == "infer":
            t0 = time.time()
            out, _ = self.backend.generate_raw(job["prompt"], job.get("max_tokens", 256))
            lat = (time.time() - t0) * 1000
            send_msg(self.f, {"op": "submit_infer", "job_id": job["job_id"],
                              "miner_id": self.mid, "output": out, "latency_ms": lat})
            return ("infer", job["job_id"], recv_msg(self.f))
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
        return ("task", job["task"], recv_msg(self.f))

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
