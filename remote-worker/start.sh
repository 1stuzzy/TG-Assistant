#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -r requirements.txt
MODEL="${1:-model.gguf}"
exec python3 server.py --model "$MODEL" --host 0.0.0.0 --port 8088 --gpu
