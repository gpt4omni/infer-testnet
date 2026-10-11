#!/bin/sh
# Railway boot: persistent operator key + mining gateway on $PORT.
# Judging via hosted Jev API (set JEV_API_KEY service variable) — no local
# model download (volume too small, image lacks libgomp).
set -e
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA"
if [ ! -f "$DATA/operator.json" ]; then
  python3 -c "import sys,pq,json; sys.path.insert(0,'.'); k=pq.generate(); json.dump(k, open('$DATA/operator.json','w'))"
  chmod 600 "$DATA/operator.json"
fi
OP=$(python3 -c "import json; print(json.load(open('$DATA/operator.json'))['addr'])")
echo "operator (coinbase): $OP" >&2
if [ -z "$JEV_API_KEY" ]; then
  echo "WARNING: JEV_API_KEY unset — inference pays nothing until judging works" >&2
fi
exec python3 -u gateway_node.py --host 0.0.0.0 --port "${PORT:-19191}" \
  --db "$DATA/gateway.db" --chain "$DATA/chain.dat" \
  --reset-state-once railway-chain-store-v2 \
  --mine --mine-interval 20 --reward-addr "$OP"
