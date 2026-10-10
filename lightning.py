#!/usr/bin/env python3
"""Lightning-style watts-per-job (testnet HTLC core).

Real Lightning moves this onto LN invoices/channels later; the contract
semantics here are identical and enforceable TODAY against our ledger:
no delivery of VALID output => no payment, automatically.

Lifecycle per job:
  offer   customer PQ-signs HTLC terms (miner, amount, job, hash, expiry).
          Funds do NOT move yet — like a real commitment tx.
  fulfill miner reveals preimage AND gateway confirms output validity.
          sha256(preimage)==hash => customer->miner settles. Both gates.
  fail    output invalid => contract void, nothing moves.
  expire  past expiry epoch => void, nothing moves.

Hash locks are quantum-safe (Grover only); authorization sigs are our
PQ WOTS (Shor-proof). Double-fulfill rejected via status gate.
"""
import hashlib
import secrets
import time

SCHEMA_EXTRA = """
CREATE TABLE IF NOT EXISTS htlcs(
  job_id TEXT PRIMARY KEY, customer TEXT, miner TEXT, amount_watts INTEGER,
  hash_hex TEXT, expiry INTEGER, status TEXT, ts REAL,
  terms TEXT DEFAULT '', terms_sig_json TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS ots_used(addr TEXT, ots INTEGER, PRIMARY KEY(addr, ots));
"""


def ensure(c):
    c.executescript(SCHEMA_EXTRA)
    c.commit()


def new_preimage() -> bytes:
    return secrets.token_bytes(32)


def hash_of(preimage: bytes) -> str:
    return hashlib.sha256(preimage).hexdigest()


def offer(c, customer_key: dict, miner_addr: str, amount: int, job_id: str,
          hash_hex: str, expiry: int) -> dict:
    """Customer authorizes payment terms with PQ signatures (TWO fresh OTS
    indices — never reuse one index for two messages). Funds do NOT move
    yet. Returns audit package."""
    import json as _json
    import pq
    from ledger import next_ots, balance_watts
    if balance_watts(c, customer_key["addr"]) < amount:
        raise ValueError("customer cannot cover HTLC amount")
    terms = f"htlc|{customer_key['addr']}|{miner_addr}|{amount}|{job_id}|{hash_hex}|{expiry}"
    ots = next_ots(c, customer_key["addr"])
    terms_sig = pq.sign(customer_key["seed"], ots, customer_key["addr"], terms, amount)
    if not _terms_ok(customer_key["addr"], _root_hex(c, customer_key["addr"]),
                     {"terms": terms, "terms_sig": terms_sig}):
        raise ValueError("terms self-check failed")
    ensure(c)
    c.execute("INSERT OR IGNORE INTO ots_used VALUES(?,?)", (customer_key["addr"], ots))
    c.execute("INSERT INTO htlcs VALUES(?,?,?,?,?,?,?,?,?,?)",
              (job_id, customer_key["addr"], miner_addr, amount, hash_hex, expiry,
               "PENDING", time.time(), terms, _json.dumps(terms_sig)))
    c.commit()
    return {"ots": ots, "terms": terms}


def _root_hex(c, addr: str) -> str:
    row = c.execute("SELECT root_hex FROM pq_accounts WHERE addr=?", (addr,)).fetchone()
    if row is None:
        raise ValueError("customer PQ account unregistered")
    return row[0]


def _terms_ok(customer_addr: str, root_hex: str, pkg: dict) -> bool:
    import pq
    terms = pkg.get("terms", "")
    try:
        parts = terms.split("|")
        if len(parts) != 7 or parts[0] != "htlc" or parts[1] != customer_addr:
            return False
        tsig = pkg.get("terms_sig")
        return pq.verify(customer_addr, root_hex, tsig, terms, int(parts[3]))
    except (ValueError, IndexError):
        return False


def fulfill(c, job_id: str, preimage: bytes, output_valid: bool, now_epoch: int) -> tuple[bool, str]:
    """Settle ONLY if: PENDING + unexpired + hash matches + output valid.
    Executes the pre-authorized terms (verified at offer); funds move here,
    never before. Customer must still cover the amount at settle time."""
    from ledger import balance_watts
    row = c.execute("SELECT customer,miner,amount_watts,hash_hex,expiry,status FROM htlcs WHERE job_id=?",
                    (job_id,)).fetchone()
    if row is None:
        return False, "unknown HTLC"
    customer, miner, amount, want, expiry, status = row
    if status != "PENDING":
        return False, f"already {status} (double-fulfill rejected)"
    if now_epoch > expiry:
        c.execute("UPDATE htlcs SET status='EXPIRED' WHERE job_id=?", (job_id,))
        c.commit()
        return False, "expired — refunded (nothing moved)"
    if hash_of(preimage) != want:
        return False, "preimage mismatch"
    if not output_valid:
        c.execute("UPDATE htlcs SET status='FAILED' WHERE job_id=?", (job_id,))
        c.commit()
        return False, "output invalid — void, customer keeps funds"
    if c.execute("SELECT 1 FROM pq_accounts WHERE addr=?", (miner,)).fetchone() is None:
        return False, "miner PQ account unregistered"
    if balance_watts(c, customer) < amount:
        c.execute("UPDATE htlcs SET status='FAILED' WHERE job_id=?", (job_id,))
        c.commit()
        return False, "customer drained funds before settle — void"
    from money import split_customer_fee
    miner_amt, burn_amt = split_customer_fee(amount)
    c.execute("UPDATE htlcs SET status='FULFILLED' WHERE job_id=?", (job_id,))
    c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
              (customer, miner, miner_amt, time.time()))
    if burn_amt:
        c.execute("INSERT INTO transfers(from_id,to_id,amount_watts,ts) VALUES(?,?,?,?)",
                  (customer, "burn", burn_amt, time.time()))
    c.commit()
    return True, f"settled {miner_amt} watts customer->miner, {burn_amt} burned"


def expire_sweep(c, now_epoch: int) -> int:
    rows = c.execute("SELECT job_id FROM htlcs WHERE status='PENDING' AND expiry<?",
                     (now_epoch,)).fetchall()
    for (jid,) in rows:
        c.execute("UPDATE htlcs SET status='EXPIRED' WHERE job_id=?", (jid,))
    c.commit()
    return len(rows)
