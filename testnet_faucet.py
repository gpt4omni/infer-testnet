#!/usr/bin/env python3
"""Testnet faucet: free test INFR to an address. Usage: python3 testnet_faucet.py <addr> <infr> [db]"""
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

from ledger import connect, transfer
from money import to_watts, fmt_watts


def main():
    addr, infr = sys.argv[1], float(sys.argv[2])
    db = sys.argv[3] if len(sys.argv) > 3 else os.path.join(os.path.dirname(__file__), "livenet", "gateway.db")
    c = connect(db)
    transfer(c, "faucet", addr, to_watts(infr))
    print(f"fauceted {fmt_watts(to_watts(infr))} to {addr[:24]}..")


if __name__ == "__main__":
    main()
