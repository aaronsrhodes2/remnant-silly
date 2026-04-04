#!/bin/sh
# Start ollama serve in the background, wait for it, pull the model,
# then bring serve into the foreground as PID 1.

set -e

MODEL="${OLLAMA_MODEL:-mistral}"

echo "[ollama-init] Starting ollama server..."
ollama serve &
SERVE_PID=$!

# Wait for the API to come up
echo "[ollama-init] Waiting for Ollama API to become ready..."
i=0
until ollama list >/dev/null 2>&1; do
    i=$((i+1))
    if [ "$i" -gt 60 ]; then
        echo "[ollama-init] ERROR: ollama server never came up"
        kill $SERVE_PID 2>/dev/null || true
        exit 1
    fi
    sleep 1
done
echo "[ollama-init] Ollama API is ready."

# Pull the model if not already present
if ollama list | awk '{print $1}' | grep -q "^${MODEL}"; then
    echo "[ollama-init] Model '${MODEL}' already present, skipping pull."
else
    echo "[ollama-init] Pulling model: ${MODEL} (this may take several minutes on first run)..."
    ollama pull "${MODEL}"
    echo "[ollama-init] Model '${MODEL}' pulled successfully."
fi

# Hand off: wait on the serve process so signals propagate
echo "[ollama-init] Handoff complete. Ollama serving on :11434"
wait $SERVE_PID
