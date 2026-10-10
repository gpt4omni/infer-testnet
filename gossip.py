#!/usr/bin/env python3
"""Local broadcast dropbox: wallets publish signed transfers as files,
peers collect + verify + import them. Poor-man's gossip for same-machine
testing — the verify/import path is the same one network sync will use.

Trust: sender root arrives inside the package (TOFU). Production gossip
pins roots via pq_accounts from chain history instead.
"""
import json
import os

from ledger import app_state_dir

OUTBOX = os.path.join(app_state_dir(), "outbox")


def broadcast(from_addr: str, root_hex: str, to_addr: str, amount: int, sig_pkg: dict):
    os.makedirs(OUTBOX, exist_ok=True)
    doc = {"from": from_addr, "root": root_hex, "to": to_addr,
           "amount": amount, "sig": sig_pkg}
    name = f"{sig_pkg['ots']:06d}-{from_addr[:12]}.json"
    with open(os.path.join(OUTBOX, name), "w") as f:
        json.dump(doc, f)


def collect(conn, my_addrs: set[str]) -> int:
    """Import new transfers addressed to me. Returns count imported."""
    from ledger import register_pq, transfer
    if not os.path.isdir(OUTBOX):
        return 0
    n = 0
    for fn in sorted(os.listdir(OUTBOX)):
        if not fn.endswith(".json"):
            continue
        try:
            with open(os.path.join(OUTBOX, fn)) as f:
                doc = json.load(f)
            if doc["to"] not in my_addrs:
                continue
            have = conn.execute("SELECT 1 FROM transfers WHERE from_id=? AND ots=?",
                                (doc["from"], doc["sig"]["ots"])).fetchone()
            if have:
                continue
            register_pq(conn, doc["from"], doc["root"])
            transfer(conn, doc["from"], doc["to"], doc["amount"], doc["sig"],
                     enforce_funds=False)
            n += 1
        except (ValueError, KeyError, OSError):
            continue  # invalid/tampered packages never import
    return n
