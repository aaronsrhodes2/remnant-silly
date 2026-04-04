# Ollama with auto-pull entrypoint.
# Wraps the official ollama/ollama image so that on first boot the
# configured OLLAMA_MODEL is pulled automatically, then the server
# stays in the foreground as PID 1.

FROM ollama/ollama:latest

COPY docker/ollama/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV OLLAMA_HOST=0.0.0.0
ENV OLLAMA_MODEL=mistral

EXPOSE 11434

ENTRYPOINT ["/entrypoint.sh"]
