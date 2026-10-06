FROM python:3.13-slim-bookworm
ARG E2E_RUN_ID
LABEL loopyard.e2e.run=$E2E_RUN_ID
# No Loopyard, application dependencies, or release keys in the clean image.
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl openssh-client tmux git procps \
    && rm -rf /var/lib/apt/lists/*
ENV HOME=/root
CMD ["sleep", "infinity"]
