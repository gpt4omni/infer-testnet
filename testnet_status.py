#!/usr/bin/env python3
"""Testnet status: tip, mempool, rich list. Usage: python3 testnet_status.py [host:port] [db]"""
import os
import socket
import sys
sys.path.insert(0, os.path.dirname(__file__))

from net import send_msg, recv_msg


def rpc(host, port, obj):
    s = socket.create_connection((host, port), timeout=10)
    with s, s.makefile("rwb") as f:
        send_msg(f, obj)
        return recv_msg(f)


def main():
    hp = sys.argv[1] if len(sys.argv) > 1 else "127.0.0.1:19191"
    db = sys.argv[2] if len(sys.argv) > 2 else os.path.join(os.path.dirname(__file__), "livenet", "gateway.db")
    host, port = hp.rsplit(":", 1)
    t = rpc(host, int(port), {"op": "get_tip"})
    m = rpc(host, int(port), {"op": "mempool"})
    print(f"tip height={t['height']} {t['tip'][:16] if t['tip'] else '(none)'}.. queued={m['queued']}")
    if os.path.exists(db):
        from ledger import connect, subsidy_issued_watts, burned_watts
        from money import fmt_watts
        c = connect(db)
        print(f"issued={fmt_watts(subsidy_issued_watts(c))} burned={fmt_watts(burned_watts(c))}")
        rows = c.execute("SELECT miner_id, SUM(payout_watts) FROM jobs GROUP BY 1 ORDER BY 2 DESC LIMIT 5").fetchall()
        for mid, pay in rows:
            bal = c.execute("SELECT COALESCE(SUM(amount_watts),0) FROM transfers WHERE to_id=?", (mid,)).fetchone()[0]
            print(f"  {mid[:20]}.. paid={fmt_watts(pay or 0)} got={fmt_watts(bal)}")
    else:
        print(f"(local db {db} not present — node is remote)")


if __name__ == "__main__":
    main()
