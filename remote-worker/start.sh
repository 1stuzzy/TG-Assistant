#!/bin/sh
cd "$(dirname "$0")"
python3 -m pip install -U pip
python3 -m pip install -r requirements.txt
python3 -m pip install "llama-cpp-python>=0.3.0" --only-binary=llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
  || python3 -m pip install "llama-cpp-python>=0.3.0" --only-binary=llama-cpp-python \
  --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
MODEL="${1:-model.gguf}"
python3 server.py --model "$MODEL" --host 0.0.0.0 --port 8088 --gpu || \
  python3 server.py --model "$MODEL" --host 0.0.0.0 --port 8088
