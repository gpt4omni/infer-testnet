# Two-operator gateway onboarding

This pack starts a second gateway with its own database and ML-KEM identity,
then checks that both operators have the same validated chain tip. It needs
Python 3.11+ and the packages in `requirements.txt`. Gateways do not need Tk.

## 1. Install on both machines

```sh
git clone https://github.com/gpt4omni/infer-testnet.git
cd infer-testnet
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
python demo_chain.py
python demo_gossip.py
python demo_pqchannel.py
```

Use the same commit on both hosts (`git rev-parse HEAD`). Choose one existing
gateway as seed A. Start B with a fresh state directory; a separately mined
genesis may be outside the reorg cap and cannot always be merged.

The commands below use `192.168.50.10` for A and `192.168.50.11` for B.
Replace them with the actual LAN IPv4 addresses. Both use TCP port 19191.

## 2. Exchange public pins

On **each** host, run:

```sh
python gateway_node.py --db "$HOME/.local/share/infer-gateway/gateway.db" --show-pin
```

This creates the state directory and a private `gateway-kem.pem` (0600), or
loads its existing key. Send only the printed **64-hex KEM pin** to the other
operator through a separately authenticated channel. Label it with your LAN
address, port, and commit. Each host must have a different pin.

Never copy wallet seeds or `gateway-kem.pem` between operators. Keep the KEM
key across restarts: deleting it rotates the identity and invalidates peers'
pins. A key fetched from the gateway's network port is not an authenticated
pin exchange.

`--pq` enables ML-KEM-768 + AES-256-GCM. `--tls` is a compatibility alias for
the same PQ protocol; it does not enable classical TLS or HTTPS.

## 3. Make TCP reachable

Bind to `0.0.0.0` (or your LAN IP), not the default loopback address. Allow
inbound **TCP 19191** from the other operator, and outbound TCP to that peer.
No UDP port is needed. For a Linux host already managed by UFW, for example:

```sh
# On A; substitute B's actual LAN address.
sudo ufw allow from 192.168.50.11 to any port 19191 proto tcp
# On B; substitute A's actual LAN address.
sudo ufw allow from 192.168.50.10 to any port 19191 proto tcp
```

Use your existing firewall manager; do not enable a second one. Also check
Wi-Fi/client isolation if hosts on the same subnet cannot connect. Across
NAT, arrange a reachable TCP endpoint/port forward and advertise that address.
An HTTP-only reverse proxy cannot carry the PQ peer protocol. HTTP explorer
routes share this port, but pin-verified peer RPC uses the raw TCP listener.

## 4. Start A, then B

On **A**, substitute the pin B sent for `PIN_B` and a real wallet PQ address
for `YOUR_PQ1_ADDRESS`:

```sh
python gateway_node.py --host 0.0.0.0 --port 19191 --pq \
  --db "$HOME/.local/share/infer-gateway/gateway.db" \
  --advertise 192.168.50.10:19191 \
  --seed 192.168.50.11:19191@PIN_B \
  --mine --reward-addr YOUR_PQ1_ADDRESS
```

On **B**, substitute A's independently received `PIN_A`:

```sh
python gateway_node.py --host 0.0.0.0 --port 19191 --pq \
  --db "$HOME/.local/share/infer-gateway/gateway.db" \
  --advertise 192.168.50.11:19191 \
  --seed 192.168.50.10:19191@PIN_A
```

B validates and follows blocks without mining its own genesis. Seed sync
starts immediately and retries every five seconds; connection errors are
printed with the peer address. Repeat `--seed HOST:PORT@PIN` for more peers.
The legacy `--seeds` option also accepts comma-separated pinned entries.
Unpinned seed entries are rejected.

Both runners settle queued jobs through blocks, including standby B. Only
`--mine` enables block production. Idle mining emits a heartbeat every 30
mining rounds (default 15 seconds per round), so allow time for height 1.
Use the existing network difficulty; the runner defaults to `2**236`.

If the network uses an operator checkpoint, independently obtain the
operator **PQ root** and signed file. Supply both
`--checkpoint-root ROOT --checkpoint-file FILE` on each host before syncing.
The checkpoint root is distinct from the transport KEM pin. A failed
checkpoint check stops startup. Do not delete checkpoints or loosen the
reorg cap to join incompatible histories.

## 5. Prove convergence from either host

Once both have at least one block beyond genesis:

```sh
python demo_onboarding.py \
  --peer 192.168.50.10:19191@PIN_A \
  --peer 192.168.50.11:19191@PIN_B \
  --timeout 120 --min-height 1
```

The command authenticates both endpoints, waits for one nonempty tip,
downloads its history in bounded batches, and revalidates PoW, Merkle roots,
links, timestamps, and block reward rules. It checks the tips again after
validation. Add `--balance-addr pq1...` to compare an account on both ledgers.
For a busy chain, pause mining on A by restarting it with the same command
without `--mine`, run the check, then restore mining.

Record the two host identities/IPs, commit, height, full tip hash, and command
output. The script proves endpoint convergence; distinct physical machines
must be confirmed by the operators. Its default run is explicitly a
**same-machine, separate-process regression**, not a two-host acceptance.

## Troubleshooting

- **Connection refused/timeout:** check the advertised LAN address, bind
  address, firewall, NAT, and that the process is still running.
- **KEM pin mismatch:** compare the independently exchanged pin with
  `--show-pin` on that host. Investigate key rotation or the wrong endpoint.
- **No blocks / upgrade both gateways:** the batched sync protocol needs this
  version on both peers; the older runner never started seed sync.
- **Sync budget exceeded:** each attempt permits 10,000 unknown blocks and
  64 MiB of downloaded block data. `--sync-max-blocks` raises the block count
  budget; the byte budget remains fixed. Use a nearer seed for larger gaps.
- **Stored fork, tip does not move:** cumulative work, the six-block reorg
  cap, and checkpoints still govern adoption. Height alone is not enough.
- **Corrupt chain file:** startup fails without replacing history. Preserve
  the original data and investigate/recover from a known-good backup.

## Regression proofs

```sh
python demo_onboarding.py
python demo_chain_fast.py
python demo_ledger_replay.py
python demo_pqchannel.py
python demo_chain.py
python demo_gossip.py
python demo_security.py
python demo_blocks.py
python demo_wan.py
```

`demo_chain_fast.py` compares exact winning nonces/headers against the old
mining algorithm, cached work against the old summation, and bounded retarget
lookups against the full-chain calculation. Timings are machine-dependent;
the difficulty, block format, supply schedule, and signature scheme stay the
same. `demo_onboarding.py` exercises catch-up past 50 blocks, equal-height
heavier forks, signed-transfer root recovery, reorg replay, independent KEM
keys, restart balances, wrong pins, tampering, and slow connection handling.
