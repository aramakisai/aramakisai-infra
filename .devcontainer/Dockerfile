FROM mcr.microsoft.com/devcontainers/base:ubuntu

# Avoid prompts
ENV DEBIAN_FRONTEND=noninteractive

# Install core system dependencies
RUN apt-get update && apt-get install -y \
    apt-transport-https \
    ca-certificates \
    curl \
    gnupg \
    software-properties-common \
    wget \
    jq \
    shellcheck \
    unzip \
    python3 \
    python3-pip \
    && rm -rf /var/lib/apt/lists/*

# 1. Install Terraform (matching Makefile preferences)
RUN wget -O- https://apt.releases.hashicorp.com/gpg | gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | tee /etc/apt/sources.list.d/hashicorp.list \
    && apt-get update && apt-get install -y terraform \
    && rm -rf /var/lib/apt/lists/*

# 2. Install kubectl
RUN curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key | gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg \
    && echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /' | tee /etc/apt/sources.list.d/kubernetes.list \
    && apt-get update && apt-get install -y kubectl \
    && rm -rf /var/lib/apt/lists/*

# 3. Install Infisical CLI
RUN curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | bash \
    && apt-get update && apt-get install -y infisical \
    && rm -rf /var/lib/apt/lists/*

# 4. Install Node.js (v22, required for claude-code and codex)
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y nodejs \
    && rm -rf /var/lib/apt/lists/*

# Install global AI Agent CLI tools (claude-code and codex) via npm
RUN npm install -g @anthropic-ai/claude-code @openai/codex

# 5. Install uv (Python Package and Version Manager - strictly enforced)
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
ENV PATH="/root/.local/bin:${PATH}"

# Install linting & formatting tools using uv
RUN uv tool install pre-commit \
    && uv tool install yamllint \
    && uv tool install ansible-lint \
    && uv pip install --system ansible

# 6. Install Antigravity CLI (agy)
RUN curl -fsSL https://antigravity.google/cli/install.sh | bash \
    && if [ -f /root/.local/bin/agy ]; then mv /root/.local/bin/agy /usr/local/bin/agy; fi

# Set working directory inside container
WORKDIR /workspace
