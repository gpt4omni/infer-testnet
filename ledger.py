#!/usr/bin/env python3
"""SQLite ledger: jobs, miners, payouts. Auditable, no chain needed for v0."""
import os
import sqlite3
import sys
import time


def app_state_dir() -> str:
    """Writable per-user state dir. Frozen bundles (AppImage/exe) run from
    read-only mounts, so state NEVER lives next to the code there."""
    if getattr(sys, "frozen", False):
        d = os.path.join(os.path.expanduser("~"), ".local", "share", "infer-wallet")
    else:
        d = os.path.dirname(os.path.abspath(__file__))
    os.makedirs(d, exist_ok=True)
    return d

SCHEMA = """
CREATE TABLE IF NOT EXISTS miners(
  id TEXT PRIMARY KEY, model TEXT, price_per_1k REAL, stake_watts INTEGER,
  slashed_watts INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, miner_id TEXT, prompt_hash TEXT,
  output_hash TEXT, tokens INTEGER, latency_ms REAL, is_eval INTEGER,
  passed INTEGER, score REAL, payout_watts INTEGER, ts REAL,
  gateway_id TEXT DEFAULT '', evaluator_id TEXT DEFAULT '', epoch INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS routes(
  job_id INTEGER PRIMARY KEY, gateway_id TEXT, miner_id TEXT,
  expected_miner TEXT, rule_hash TEXT, honest INTEGER);
CREATE TABLE IF NOT EXISTS eval_commits(
  evaluator_id TEXT, epoch INTEGER, commit_hash TEXT,
  PRIMARY KEY(evaluator_id, epoch));
CREATE TABLE IF NOT EXISTS slashes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, entity TEXT, kind TEXT,
  amount_watts INTEGER, reason TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS transfers(
  id INTEGER PRIMARY KEY AUTOINCREMENT, from_id TEXT, to_id TEXT,
  amount_watts INTEGER, ts REAL, sig_json TEXT, ots INTEGER,
  UNIQUE(from_id, ots));
CREATE TABLE IF NOT EXISTS pq_accounts(addr TEXT PRIMARY KEY, root_hex TEXT);
"""


def connect(path="ledger.db", check_same_thread=True):
    c = sqlite3.connect(path, check_same_thread=check_same_thread, timeout=30)
    c.execute("PRAGMA journal_mode=WAL")
    c.execute("PRAGMA synchronous=NORMAL")
    c.executescript(SCHEMA)
    cols = {r[1] for r in c.execute("PRAGMA table_info(transfers)")}
    for col in ("sig_json TEXT", "ots INTEGER"):
        if col.split()[0] not in cols:
            c.execute(f"ALTER TABLE transfers ADD COLUMN {col}")
    mcols = {r[1] for r in c.execute("PRAGMA table_info(miners)")}
    if "slashed_watts" not in mcols:
        c.execute("ALTER TABLE miners ADD COLUMN slashed_watts INTEGER DEFAULT 0")
    c.commit()
    return c


def register_pq(c, addr: str, root_hex: str):
    c.execute("INSERT OR REPLACE INTO pq_accounts VALUES(?,?)", (addr, root_hex))
    c.commit()


def next_ots(c, addr: str) -> int:
    hi = c.execute("SELECT COALESCE(MAX(ots),-1) FROM transfers WHERE from_id=?", (addr,)).fetchone()[0]
    if c.execute("SELECT 1 FROM sqlite_master WHERE name='ots_used'").fetchone():
        h2 = c.execute("SELECT COALESCE(MAX(ots),-1) FROM ots_used WHERE addr=?", (addr,)).fetchone()[0]
        hi = max(hi, h2)
    return hi + 1


def register_miner(c, mid, model, price_per_1k, stake_watts=0):
    c.execute("INSERT OR REPLACE INTO miners(id,model,price_per_1k,stake_watts,slashed_watts)"
              " VALUES(?,?,?,?,COALESCE((SELECT slashed_watts FROM miners WHERE id=?),0))",
              (mid, model, price_per_1k, stake_watts, mid))
    c.commit()


def log_job(c, miner_id, prompt_hash, output_hash, tokens, latency_ms,
            is_eval, passed, score, payout_watts,
            gateway_id="", evaluator_id="", epoch=0):
    cur = c.execute(
        "INSERT INTO jobs(miner_id,prompt_hash,output_hash,tokens,latency_ms,"
        "is_eval,passed,score,payout_watts,ts,gateway_id,evaluator_id,epoch)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (miner_id, prompt_hash, output_hash, tokens, latency_ms,
         int(is_eval), int(passed), score, payout_watts, time.time(),
         gateway_id, evaluator_id, epoch))
    c.commit()
    return cur.lastrowid


def log_route(c, job_id, gateway_id, miner_id, expected_miner, rule_hash):
    c.execute("INSERT OR REPLACE INTO routes VALUES(?,?,?,?,?,?)",
              (job_id, gateway_id, miner_id, expected_miner, rule_hash,
               int(miner_id == expected_miner)))
    c.commit()


def commit_eval(c, evaluator_id, epoch, commit_hash):
    c.execute("INSERT OR REPLACE INTO eval_commits VALUES(?,?,?)",
              (evaluator_id, epoch, commit_hash))
    c.commit()


def slash(c, entity, kind, amount_watts, reason):
    c.execute("INSERT INTO slashes(entity,kind,amount_watts,reason,ts) VALUES(?,?,?,?,?)",
              (entity, kind, amount_watts, reason, time.time()))
    if kind == "miner":
        c.execute("UPDATE miners SET slashed_watts=slashed_watts+? WHERE id=?",
                  (amount_watts, entity))
        # burn locked stake first: slashing costs real collateral, not rows
        take = min(staked_watts(c, entity), amount_watts)
        if take > 0:
            c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
                      (stake_sink(entity), "burn", take, time.time()))
    c.commit()


def audit_steering(c):
    """Gateways whose actual routing deviates from the deterministic rule."""
    return list(c.execute(
        "SELECT gateway_id, COUNT(*), SUM(honest) FROM routes "
        "GROUP BY gateway_id"))


def net_stake(c, mid):
    r = c.execute("SELECT stake_watts, slashed_watts FROM miners WHERE id=?", (mid,)).fetchone()
    return r[0] - r[1] if r else 0


def leaderboard(c):
    return list(c.execute(
        "SELECT miner_id, COUNT(*), SUM(passed), SUM(payout_watts), AVG(score) "
        "FROM jobs GROUP BY miner_id ORDER BY SUM(payout_watts) DESC"))


def transfer(c, from_id: str, to_id: str, amount_watts: int, sig_pkg: dict | None = None,
             enforce_funds: bool = True):
    """Move watts. Protocol senders ('faucet', 'subsidy') issue funds without
    sigs. Light-client receipt imports (gossip.collect) pass
    enforce_funds=False: signature + OTS rules still enforced, sender
    balance checked on full chain sync."""
    import json as _json
    PROTOCOL = ("faucet", "subsidy")
    if amount_watts <= 0:
        raise ValueError("amount must be positive")
    if from_id not in PROTOCOL and enforce_funds and balance_watts(c, from_id) < amount_watts:
        raise ValueError("insufficient balance")
    if from_id in PROTOCOL:
        c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
                  (from_id, to_id, amount_watts, time.time()))
    else:
        if sig_pkg is None:
            raise ValueError("PQ signature required")
        row = c.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (from_id,)).fetchone()
        if row is None:
            raise ValueError("unknown PQ sender — register key first")
        import pq as _pq
        if not _pq.verify(from_id, row[0], sig_pkg, to_id, amount_watts):
            raise ValueError("PQ signature invalid (tampered amount/recipient/index?)")
        ots = int(sig_pkg["ots"])
        if c.execute("SELECT 1 FROM transfers WHERE from_id=? AND ots=?", (from_id, ots)).fetchone():
            raise ValueError("OTS index reused — signature replay rejected")
        _ots_mark(c, from_id, ots)
        try:
            c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts,sig_json,ots)"
                      " VALUES(?,?,?,?,?,?)",
                      (from_id, to_id, amount_watts, time.time(), _json.dumps(sig_pkg), ots))
            if c.execute("SELECT 1 FROM sqlite_master WHERE name='ots_used'").fetchone():
                c.execute("INSERT OR IGNORE INTO ots_used VALUES(?,?)", (from_id, ots))
        except sqlite3.IntegrityError:
            raise ValueError("OTS index reused — signature replay rejected")
    c.commit()


def balance_watts(c, mid: str) -> int:
    earned = c.execute("SELECT COALESCE(SUM(payout_watts),0) FROM jobs WHERE miner_id=?",
                       (mid,)).fetchone()[0]
    got = c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id=?",
                    (mid,)).fetchone()[0]
    sent = c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE from_id=?",
                     (mid,)).fetchone()[0]
    cut = c.execute("SELECT COALESCE(slashed_watts,0) FROM miners WHERE id=?",
                    (mid,)).fetchone()
    return earned + got - sent - (cut[0] if cut else 0)


def mint(c, to_id: str, amount_watts: int):
    """Epoch subsidy issuance (money.py schedule). Protocol sender, no sig."""
    transfer(c, "subsidy", to_id, amount_watts)


def subsidy_issued_watts(c) -> int:
    return c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE from_id='subsidy'").fetchone()[0]


def burned_watts(c) -> int:
    return c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id='burn'").fetchone()[0]


def stake_sink(addr: str) -> str:
    return f"stake:{addr}"


def staked_watts(c, addr: str) -> int:
    """Locked collateral = in(sink) - out(sink). Comes out of the owner's
    balance automatically (transfers move real money into the sink)."""
    sink = stake_sink(addr)
    inn = c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id=?",
                    (sink,)).fetchone()[0]
    out = c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE from_id=?",
                    (sink,)).fetchone()[0]
    return inn - out


def _ots_mark(c, addr: str, n: int):
    """Claim an OTS index atomically; losers of a race get a clean rejection."""
    c.execute("CREATE TABLE IF NOT EXISTS ots_used(addr TEXT, ots INTEGER, PRIMARY KEY(addr, ots))")
    try:
        c.execute("INSERT INTO ots_used VALUES(?,?)", (addr, n))
    except sqlite3.IntegrityError:
        raise ValueError("OTS index reused — signature replay rejected")


def stake_lock(c, addr: str, amount_watts: int, sig_pkg: dict):
    """Lock own funds as serving collateral. One PQ signature over
    stake|<addr>|<amount>|<next-ots>; funds move addr -> sink via protocol
    execution (same pattern as HTLC fulfill)."""
    import pq as _pq
    import time as _t
    if amount_watts <= 0:
        raise ValueError("amount must be positive")
    row = c.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (addr,)).fetchone()
    if row is None:
        raise ValueError("unknown PQ account")
    if balance_watts(c, addr) < amount_watts:
        raise ValueError("insufficient balance to lock")
    n = next_ots(c, addr)
    if not _pq.verify(addr, row[0], sig_pkg, f"stake|{addr}|{amount_watts}|{n}", amount_watts):
        raise ValueError("bad stake signature")
    _ots_mark(c, addr, n)
    c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
              (addr, stake_sink(addr), amount_watts, _t.time()))
    c.commit()


def stake_unlock(c, addr: str, amount_watts: int, sig_pkg: dict):
    """Release collateral back to liquid (signed; capped at locked amount)."""
    import pq as _pq
    import time as _t
    row = c.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (addr,)).fetchone()
    if row is None:
        raise ValueError("unknown PQ account")
    if staked_watts(c, addr) < amount_watts:
        raise ValueError("unlock exceeds locked stake")
    n = next_ots(c, addr)
    if not _pq.verify(addr, row[0], sig_pkg, f"unstake|{addr}|{amount_watts}|{n}", amount_watts):
        raise ValueError("bad unstake signature")
    _ots_mark(c, addr, n)
    c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
              (stake_sink(addr), addr, amount_watts, _t.time()))
    c.commit()
