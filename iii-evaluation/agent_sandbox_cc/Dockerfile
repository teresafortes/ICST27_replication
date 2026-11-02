# Sandbox for ag_main_cc (1d-run_claude_code.py): Claude Code with Dafny installed.
# Unlike agent_sandbox/ (ag_main), Dafny 4.11 and Z3 4.15.4 (the versions used on the host
# for scoring) are inside, and the agent runs them as often as it likes.
# The container runs on an internal Docker network with no route out; its only exit is the
# egress proxy (egress_proxy.py), which allows api.anthropic.com and nothing else.
#   docker build -t dafny-agent-sandbox-cc:1 iii-evaluation/agent_sandbox_cc
FROM python:3.12-slim-bookworm

ARG NODE_VERSION=22.12.0
ARG CLAUDE_CODE_VERSION=2.1.306
ARG DAFNY_VERSION=4.11.0
ARG Z3_VERSION=4.15.4

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl unzip xz-utils libicu72 procps git ripgrep \
    && rm -rf /var/lib/apt/lists/*

# Node and Claude Code (pinned)
RUN arch=$(dpkg --print-architecture | sed 's/amd64/x64/') \
    && curl -fsSL https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${arch}.tar.xz \
       | tar -xJ -C /usr/local --strip-components=1 \
    && npm install -g @anthropic-ai/claude-code@${CLAUDE_CODE_VERSION} && npm cache clean --force

# .NET 8 and Dafny 4.11 (as a dotnet tool, like the host)
ENV DOTNET_ROOT=/opt/dotnet DOTNET_CLI_TELEMETRY_OPTOUT=1 DOTNET_NOLOGO=1 \
    DOTNET_SKIP_FIRST_TIME_EXPERIENCE=1 PATH=/opt/dafny:/opt/dotnet:$PATH
RUN curl -fsSL https://dot.net/v1/dotnet-install.sh -o /tmp/dotnet-install.sh \
    && bash /tmp/dotnet-install.sh --channel 8.0 --install-dir /opt/dotnet && rm /tmp/dotnet-install.sh \
    && dotnet tool install dafny --version ${DAFNY_VERSION} --tool-path /opt/dafny \
    && chmod -R a+rX /opt/dafny /opt/dotnet

# Z3 4.15.4, found by Dafny on PATH (as on the host). arm64 build needs glibc 2.34 (bookworm: 2.36)
RUN arch=$(dpkg --print-architecture | sed 's/amd64/x64/') \
    && glibc=$([ "$arch" = arm64 ] && echo 2.34 || echo 2.39) \
    && curl -fsSL -o /tmp/z3.zip https://github.com/Z3Prover/z3/releases/download/z3-${Z3_VERSION}/z3-${Z3_VERSION}-${arch}-glibc-${glibc}.zip \
    && unzip -q /tmp/z3.zip -d /opt && mv /opt/z3-${Z3_VERSION}-* /opt/z3 && rm /tmp/z3.zip \
    && ln -s /opt/z3/bin/z3 /usr/local/bin/z3

# Claude Code managed settings (cannot be overridden by the agent): no web tools, and a
# PostToolUse hook that snapshots program.dfy / diagnosis.json after every tool call.
COPY managed-settings.json /etc/claude-code/managed-settings.json
COPY snapshot.py egress_proxy.py /opt/cc/
RUN mkdir -p /work /snap /home/agent && chmod 777 /work /snap /home/agent

ENV HOME=/home/agent
WORKDIR /work
CMD ["sleep", "infinity"]
