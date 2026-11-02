# Sandbox for the agentic conditions (1c-run_agent.py --sandbox docker).
# Run with --network none; program.dfy etc. are bind-mounted at /work.
# Dafny is deliberately NOT installed: the driver intercepts `verify` and runs
# Dafny 4.11 on the host, so the verify cap can't be bypassed and every condition
# uses the same verifier binary.
# `resolve` (syntax/type check, unlimited) is intercepted the same way.
#   docker build -t dafny-agent-sandbox:2 iii-evaluation/agent_sandbox
FROM python:3.12-slim-bookworm
COPY verify resolve /usr/local/bin/
RUN chmod 755 /usr/local/bin/verify /usr/local/bin/resolve && mkdir /work && chmod 777 /work
WORKDIR /work
CMD ["sleep", "infinity"]
