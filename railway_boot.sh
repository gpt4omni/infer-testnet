#!/bin/sh
# Railway boot: persistent operator key + mining gateway on $PORT,
# plus bundled d1 judge (CPU) so inference auto-settles on-site.
# Needs volume at /data (or DATA_DIR) and ~3GB RAM free for d1.
# Uses only python3 + sh (no curl in nixpacks images).
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
  python3 -c "
import sys, urllib.request
url = 'https://huggingface.co/LiquidAI/d1-3B-GGUF/resolve/main/d1-3B-Q4_K_M.gguf'
dst = '$MODEL'
req = urllib.request.Request(url, headers={'User-Agent': 'infer-testnet/1.0'})
with urllib.request.urlopen(req, timeout=60) as r, open(dst, 'wb') as f:
    while True:
        b = r.read(1 << 20)
        if not b:
            break
        f.write(b)
print('model fetched', file=sys.stderr)
"
fi
export LD_LIBRARY_PATH="$PWD/bin-d1:$LD_LIBRARY_PATH"
./bin-d1/llama-server -m "$MODEL" --port 19092 --ctx-size 8192 --no-webui &
i=0
while ! python3 -c "import sys,urllib.request; urllib.request.urlopen('http://127.0.0.1:19092/health', timeout=5).read()" 2>/dev/null; do
  i=$((i + 1))
  if [ "$i" -gt 60 ]; then echo "d1 never came up; gateway starts without judge" >&2; break; fi
  sleep 5
done
exec python3 -u gateway_node.py --host 0.0.0.0 --port "${PORT:-19191}" \
  --db "$DATA/gateway.db" --chain "$DATA/chain.dat" \
  --mine --mine-interval 20 --reward-addr "$OP"
