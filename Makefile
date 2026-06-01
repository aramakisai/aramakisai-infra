.PHONY: lint lint-staged install-hooks setup

lint: ## Run all linters on all files
	pre-commit run --all-files

lint-staged: ## Run linters on staged files only
	pre-commit run

install-hooks: ## Install pre-commit git hooks
	pre-commit install

setup: ## Install linting dependencies via uv
	uv tool install pre-commit
	uv tool install yamllint
	uv tool install ansible-lint
	@echo ""
	@echo "Also install system tools:"
	@echo "  sudo pacman -S shellcheck"
	@echo "  # tflint:      https://github.com/terraform-linters/tflint/releases"
	@echo "  # kubeconform: https://github.com/yannh/kubeconform/releases"
