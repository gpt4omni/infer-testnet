#!/usr/bin/env python3
"""Operator checkpoint signer: PQ-sign (height|hash) with the operator key.
Usage: python3 checkpoint_tool.py <keyfile.json> <height> <hash> [--ots N]
OTS index auto-tracks in <keyfile>.ots next to the key. Publish the output
JSON where gateways load it (--checkpoint-file); they verify against the
pinned operator root (--checkpoint-root) and refuse conflicting history.
"""
import json
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))

import pq


def main():
    keyf, height, hash_ = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    with open(keyf) as f:
        key = json.load(f)
    if "enc" in key:
        pw = os.environ.get("INFER_WALLET_PW")
        if not pw:
            raise SystemExit("encrypted keyfile needs INFER_WALLET_PW")
        from wallet import _fernet
        key = json.loads(_fernet(pw, bytes.fromhex(key["salt"])).decrypt(key["enc"].encode()).decode())
    otsf = keyf + ".ots"
    try:
        ots = int(open(otsf).read().strip())
    except (OSError, ValueError):
        ots = 0
    if "--ots" in sys.argv:
        ots = int(sys.argv[sys.argv.index("--ots") + 1])
    msg = f"checkpoint|{height}|{hash_}"
    pkg = pq.sign(key["seed"], ots, key["addr"], msg, height)
    with open(otsf, "w") as f:
        f.write(str(ots + 1))
    os.chmod(otsf, 0o600)
    print(json.dumps({"addr": key["addr"],
                      "root": pq.root_of(bytes.fromhex(key["seed"])).hex(),
                      "height": height, "hash": hash_, "sig": pkg}))


if __name__ == "__main__":
    main()
