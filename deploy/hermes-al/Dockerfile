FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates git postgresql-client ripgrep procps jq && \
    rm -rf /var/lib/apt/lists/*
RUN curl -fsSL https://hermes-agent.nousresearch.com/install.sh | bash
ENV PATH="/root/.local/bin:/root/.hermes/bin:${PATH}"
CMD ["hermes","gateway","run"]
