#!/usr/bin/env sh
# Prepare a local LLM (Ollama) for AI DEV DIRECTOR's "local" provider. Intended for the GPU machine (RTX 5080, 16 GB).
set -e
MODEL="${1:-qwen2.5-coder:14b}"
command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || echo "[WARN] nvidia-smi not found"
if ! command -v ollama >/dev/null 2>&1; then
  echo "[MISSING] ollama - install from https://ollama.com/download (Windows: installer, Linux: curl -fsSL https://ollama.com/install.sh | sh)"; exit 1
fi
ollama pull "$MODEL"
echo "Set in config.yaml:  local_llm.model: \"$MODEL\"  and  reviewer.provider: local   (or head.provider: local)"
echo "Server: http://127.0.0.1:11434/v1  (loopback only)"
