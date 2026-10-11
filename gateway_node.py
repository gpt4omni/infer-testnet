#!/usr/bin/env python3
"""Infer gateway node: persistent chain, pinned PQ peers, optional mining.

Run on your machine, reachable on LAN:
  python3 gateway_node.py --host 0.0.0.0 --port 19191 --db gateway.db --tls
--tls is a compatibility alias for --pq (ML-KEM, not classical TLS).
First PQ run prints the KEM pin — exchange it out of band before peering.
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
    ap.add_argument("--reset-state-once", default="", help="explicit testnet chain+ledger reset, once per tag")
    ap.add_argument("--pq", "--tls", dest="pq", action="store_true", help="ML-KEM + AES-256-GCM transport")
    ap.add_argument("--kem-key", default="", help="KEM key path (default: next to --db)")
    ap.add_argument("--show-pin", action="store_true", help="create/load KEM key, print pin, exit")
    ap.add_argument("--seed", action="append", default=[], help="HOST:PORT@KEM_PIN (repeat for each seed)")
    ap.add_argument("--seeds", default="", help="comma-separated HOST:PORT@KEM_PIN peers")
    ap.add_argument("--advertise", default="", help="reachable HOST:PORT to publish to peers")
    ap.add_argument("--sync-interval", type=int, default=5, help="seconds between sync attempts")
    ap.add_argument("--sync-max-blocks", type=int, default=10000, help="maximum unknown blocks per sync")
    ap.add_argument("--mine", action="store_true", help="produce PoW blocks from mempool")
    ap.add_argument("--mine-interval", type=int, default=15)
    ap.add_argument("--reward-addr", default="", help="coinbase destination (operator PQ address)")
    ap.add_argument("--block-reward", type=int, default=5_000_000, help="watts per block (cap enforced)")
    ap.add_argument("--checkpoint-root", default="", help="pinned operator PQ root hex")
    ap.add_argument("--checkpoint-file", default="", help="signed checkpoint JSON to enforce+serve")
    ap.add_argument("--bits", default="", help="hex int target (default ~10s CPU blocks)")
    a = ap.parse_args()

    from net import peer_address
    from chain import MAX_TARGET, MAX_BLOCK_REWARD_WATTS
    if bool(a.checkpoint_file) != bool(a.checkpoint_root):
        ap.error("--checkpoint-file and --checkpoint-root must be supplied together")
    if min(a.mine_interval, a.sync_interval, a.sync_max_blocks) < 1:
        ap.error("mining/sync intervals and sync-max-blocks must be positive")
    try:
        bits = int(a.bits, 16) if a.bits else 2 ** 236
        if not 1 <= bits <= MAX_TARGET:
            raise ValueError("target outside allowed range")
    except ValueError as e:
        ap.error(f"invalid --bits: {e}")
    if not 0 <= a.block_reward <= MAX_BLOCK_REWARD_WATTS:
        ap.error("block reward outside protocol cap")
    peers, pins = [], {}
    for seed in a.seed + [s.strip() for s in a.seeds.split(",") if s.strip()]:
        try:
            peer, pin = seed.rsplit("@", 1)
            peer_address(peer)
            if len(pin) != 64 or any(c not in "0123456789abcdefABCDEF" for c in pin):
                raise ValueError("KEM pin must be 64 hex characters")
            if peer in pins and pins[peer] != pin.lower():
                raise ValueError("conflicting pins for one seed")
        except ValueError as e:
            ap.error(f"invalid seed {seed!r}: use HOST:PORT@KEM_PIN ({e})")
        if peer not in pins:
            peers.append(peer)
        pins[peer] = pin.lower()
    if a.advertise:
        try:
            peer_address(a.advertise)
        except ValueError as e:
            ap.error(str(e))
    state_dir = os.path.dirname(os.path.abspath(a.db))
    os.makedirs(state_dir, exist_ok=True)
    chain_path = a.chain or os.path.join(state_dir, "chain.dat")
    kem_path = a.kem_key or os.path.join(state_dir, "gateway-kem.pem")
    if a.show_pin:
        from pqchannel import server_keypair
        _, pin = server_keypair(kem_path)
        print(f"kem pin: {pin}", flush=True)
        return
    from storage import chain_lock, reset_state_once
    os.makedirs(os.path.dirname(os.path.abspath(chain_path)), exist_ok=True)
    with chain_lock(chain_path):
        if a.reset_state_once:
            reset_state_once(a.db, chain_path, a.reset_state_once)
        run_gateway(a, chain_path, kem_path, peers, pins, bits)


def run_gateway(a, chain_path, kem_path, peers, pins, bits):
    from ledger import connect
    from net import GatewayServer
    conn = connect(a.db, check_same_thread=False)
    srv = None
    try:
        srv = GatewayServer(conn, host=a.host, port=a.port, pq=a.pq,
                            kem_path=kem_path, peers=peers,
                            block_mode=True, chain_path=chain_path, recover_tail=True,
                            bits=bits,
                            mine_interval=a.mine_interval,
                            reward_addr=a.reward_addr, block_reward=a.block_reward)
        if a.advertise:
            srv.self_id = a.advertise
        print(f"gateway ledger: {a.db}", flush=True)
        print(f"listening: {a.host}:{srv.port} pq={a.pq} mine={a.mine}", flush=True)
        if a.checkpoint_file:
            ok, msg = srv.load_checkpoint(a.checkpoint_file, a.checkpoint_root)
            if not ok:
                raise ValueError(f"checkpoint: {msg}")
            if srv.chain.height >= srv.chain.checkpoint[0]:
                ok, msg = srv.chain.verify()
                if not ok:
                    raise ValueError(f"checkpoint: {msg}")
            print(f"checkpoint: {msg}", flush=True)
        with srv.lock:
            srv.apply_tip_locked()
        if peers:
            srv.start_peer_sync(peers, a.sync_interval, a.sync_max_blocks, pins)
        if a.mine:
            srv.start_mining()
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if srv:
            srv.close()
        conn.close()


if __name__ == "__main__":
    main()
