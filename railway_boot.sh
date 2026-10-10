#!/bin/sh
# Railway boot: persistent operator key + mining gateway on $PORT.
# Attach a Railway volume mounted at /data (or set DATA_DIR).
set -e
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA"
if [ ! -f "$DATA/operator.json" ]; then
  python3 -c "import sys,pq,json; sys.path.insert(0,'.'); k=pq.generate(); json.dump(k, open('$DATA/operator.json','w'))"
  chmod 600 "$DATA/operator.json"
fi
OP=$(python3 -c "import json; print(json.load(open('$DATA/operator.json'))['addr'])")
echo "operator (coinbase): $OP" >&2
exec python3 -u gateway_node.py --host 0.0.0.0 --port "${PORT:-19191}" \
  --db "$DATA/gateway.db" --chain "$DATA/chain.dat" \
  --mine --mine-interval 20 --reward-addr "$OP"
