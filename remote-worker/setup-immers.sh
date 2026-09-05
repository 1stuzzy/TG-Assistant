#!/bin/bash
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
export PATH="/usr/local/cuda/bin:$PATH"
ROOT=/home/ubuntu/tg-worker
MODEL_URL="https://huggingface.co/LessThanThreeAI/Qwen3.8-27B-Humanlike-Chat-GGUF/resolve/main/Qwen3.8-27B-Humanlike-Chat-Q6_K.gguf"
MODEL_NAME="Qwen3.8-27B-Humanlike-Chat-Q6_K.gguf"
mkdir -p "$ROOT/models" "$ROOT/src" "$ROOT/logs"
cd "$ROOT"

echo "=== download model (background) ==="
if [[ ! -f "models/$MODEL_NAME" ]]; then
  curl -L --fail --retry 5 --retry-all-errors \
    -o "models/$MODEL_NAME.part" "$MODEL_URL" \
    > "$ROOT/logs/download.log" 2>&1 &
  DL_PID=$!
else
  DL_PID=""
fi

echo "=== apt ==="
sudo apt-get update -y
sudo apt-get install -y build-essential cmake git ninja-build curl pciutils

echo "=== llama.cpp ==="
if [[ ! -d src/llama.cpp/.git ]]; then
  git clone --depth 1 https://github.com/ggml-org/llama.cpp src/llama.cpp
else
  git -C src/llama.cpp pull --ff-only || true
fi
cd src/llama.cpp
cmake -B build -DGGML_CUDA=ON -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_SERVER=ON
cmake --build build --config Release -j"$(nproc)" --target llama-server
install -m 0755 build/bin/llama-server "$ROOT/llama-server"
echo "=== built ==="
"$ROOT/llama-server" --version || true
nvidia-smi -L

if [[ -n "${DL_PID}" ]]; then
  echo "=== wait download pid $DL_PID ==="
  wait "$DL_PID"
  mv "$ROOT/models/$MODEL_NAME.part" "$ROOT/models/$MODEL_NAME"
fi
ls -lh "$ROOT/models/$MODEL_NAME"
echo SETUP_DONE
