#!/bin/sh
# Railway boot: persistent operator key + mining gateway on $PORT,
# plus bundled d1 judge (CPU) so inference auto-settles on-site.
# Needs volume at /data (or DATA_DIR) and ~3GB RAM free for d1.
set -e
DATA="${DATA_DIR:-/data}"
mkdir -p "$DATA"
if [ ! -f "$DATA/operator.json" ]; then
  python3 -c "import sys,pq,json; sys.path.insert(0,'.'); k=pq.generate(); json.dump(k, open('$DATA/operator.json','w'))"
  chmod 600 "$DATA/operator.json"
fi
OP=$(python3 -c "import json; print(json.load(open('$DATA/operator.json'))['addr'])")
echo "operator (coinbase): $OP" >&2
MODEL="$DATA/d1-3B-Q4_K_M.gguf"
if [ ! -f "$MODEL" ]; then
  echo "fetching d1 model (~1.6GB, one-time)…" >&2
  curl -L -o "$MODEL" "https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/main/d1-3B-Q4_K_M.gguf"
fi
export LD_LIBRARY_PATH="$PWD/bin-d1:$LD_LIBRARY_PATH"
./bin-d1/llama-server -m "$MODEL" --port 19092 --ctx-size 8192 --no-webui &
i=0
while ! curl -sf -m 5 http://127.0.0.1:19092/health > /dev/null 2>&1; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo "d1 never came up; gateway starts without judge" >&2; break; fi
  sleep 5
done
exec python3 -u gateway_node.py --host 0.0.0.0 --port "${PORT:-19191}" \
  --db "$DATA/gateway.db" --chain "$DATA/chain.dat" \
  --mine --mine-interval 20 --reward-addr "$OP"
