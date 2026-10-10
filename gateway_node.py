#!/usr/bin/env python3
"""Infer gateway node: persistent ledger, optional TLS, peer seeds.

Run on your machine, reachable on LAN:
  python3 gateway_node.py --host 0.0.0.0 --port 19191 --db gateway.db --tls
First TLS run prints the cert pin — miners need it (--pin).
Miners on other machines: python3 miner_node.py --gateway YOUR_LAN_IP:19191 ...
"""
import argparse
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=19191)
    ap.add_argument("--db", default="gateway.db")
    ap.add_argument("--chain", default="", help="chain.dat path (default: next to --db)")
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--seeds", default="", help="comma-separated host:port peers")
    ap.add_argument("--mine", action="store_true", help="produce PoW blocks from mempool")
    ap.add_argument("--mine-interval", type=int, default=15)
    ap.add_argument("--reward-addr", default="", help="coinbase destination (operator PQ address)")
    ap.add_argument("--block-reward", type=int, default=5_000_000, help="watts per block (cap enforced)")
    ap.add_argument("--bits", default="", help="hex int target (default ~10s CPU blocks)")
    a = ap.parse_args()

    from ledger import connect
    from net import GatewayServer
    import os as _os
    chain_path = a.chain or _os.path.join(_os.path.dirname(_os.path.abspath(a.db)), "chain.dat")
    conn = connect(a.db, check_same_thread=False)
    srv = GatewayServer(conn, host=a.host, port=a.port, tls=a.tls,
                        cert_path="gateway-cert.pem", key_path="gateway-key.pem",
                        peers=[s for s in a.seeds.split(",") if s],
                        block_mode=a.mine, chain_path=chain_path,
                        bits=int(a.bits, 16) if a.bits else 2 ** 236,
                        mine_interval=a.mine_interval,
                        reward_addr=a.reward_addr, block_reward=a.block_reward)
    print(f"gateway ledger: {a.db}", flush=True)
    print(f"listening: {a.host}:{srv.port} tls={a.tls} mine={a.mine}", flush=True)
    if a.mine:
        srv.start_mining()
    srv.serve_forever()


if __name__ == "__main__":
    main()
