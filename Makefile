KUBECTL_CONF := /tmp/kubeconfig-aramakisai

.PHONY: lint lint-staged install-hooks setup kubectl

lint: ## Run all linters on all files
	pre-commit run --all-files

lint-staged: ## Run linters on staged files only
	pre-commit run

install-hooks: ## Install pre-commit git hooks
	pre-commit install

setup: ## Check prerequisites and install project dependencies
	@echo "=== Checking & Installing Tools ==="
	@if ! command -v uv >/dev/null 2>&1; then \
		echo "❌ uv is not installed."; \
		echo "👉 Please install uv first via: curl -LsSf https://astral.sh/uv/install.sh | sh"; \
		exit 1; \
	fi
	@echo "✅ uv found."
	@if ! command -v gitleaks >/dev/null 2>&1; then \
		echo "⚠️  gitleaks is not installed. (Recommended for pre-commit secret scanning)"; \
	else \
		echo "✅ gitleaks found."; \
	fi
	uv tool install pre-commit || true
	uv tool install yamllint || true
	uv tool install ansible-lint || true
	@echo ""
	@echo "====================================================================="
	@echo "📢  Please install other required CLI tools for your OS:"
	@echo "====================================================================="
	@echo ""
	@echo "💻  Option A: Debian / WSL2 (Recommended for Freshmen)"
	@echo "---------------------------------------------------------------------"
	@echo "  # 1. Install Basic Tools & Ansible & Shellcheck"
	@echo "  sudo apt-get update && sudo apt-get install -y curl jq shellcheck ansible apt-transport-https ca-certificates"
	@echo ""
	@echo "  # 2. Install Infisical CLI"
	@echo "  curl -1sLf 'https://dl.cloudsmith.io/public/infisical/infisical-cli/setup.deb.sh' | sudo -E bash"
	@echo "  sudo apt-get update && sudo apt-get install -y infisical"
	@echo ""
	@echo "  # 3. Install Terraform"
	@echo "  wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg"
	@echo "  echo \"deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com \$$(lsb_release -cs) main\" | sudo tee /etc/apt/sources.list.d/hashicorp.list"
	@echo "  sudo apt-get update && sudo apt-get install -y terraform"
	@echo ""
	@echo "  # 4. Install kubectl"
	@echo "  curl -fsSL https://pkgs.k8s.io/core:/stable:/v1.32/deb/Release.key | sudo gpg --dearmor -o /etc/apt/keyrings/kubernetes-apt-keyring.gpg"
	@echo "  echo 'deb [signed-by=/etc/apt/keyrings/kubernetes-apt-keyring.gpg] https://pkgs.k8s.io/core:/stable:/v1.32/deb/ /' | sudo tee /etc/apt/sources.list.d/kubernetes.list"
	@echo "  sudo apt-get update && sudo apt-get install -y kubectl"
	@echo ""
	@echo "  # 5. Install Gitleaks"
	@echo "  wget https://github.com/gitleaks/gitleaks/releases/download/v8.23.0/gitleaks_8.23.0_linux_x64.tar.gz"
	@echo "  tar -xzf gitleaks_8.23.0_linux_x64.tar.gz gitleaks && sudo mv gitleaks /usr/local/bin/ && rm gitleaks_8.23.0_linux_x64.tar.gz"
	@echo ""
	@echo "💻  Option B: EndeavourOS (pacman/yay)"
	@echo "---------------------------------------------------------------------"
	@echo "  sudo pacman -S terraform ansible kubectl jq shellcheck gitleaks"
	@echo "  yay -S infisical-cli"
	@echo ""
	@echo "🔧  Other Utility Tools (All OS):"
	@echo "---------------------------------------------------------------------"
	@echo "  - tflint:      https://github.com/terraform-linters/tflint/releases"
	@echo "  - kubeconform: https://github.com/yannh/kubeconform/releases"
	@echo "====================================================================="

deploy: ## Run terraform apply and ansible bootstrapping sequentially (use ARGS="-y" for auto-approve)
	./scripts/deploy.sh $(ARGS)

kubectl: ## kubectl を Infisical 経由で実行 (例: make kubectl ARGS="get pods -A")
	@infisical run -- bash -c \
		'echo "$$KUBECONFIG" > $(KUBECTL_CONF) && chmod 600 $(KUBECTL_CONF) && kubectl --kubeconfig=$(KUBECTL_CONF) $(ARGS)'
