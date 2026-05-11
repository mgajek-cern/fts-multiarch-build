# Rucio Storage Testbed — developer commands
#
# All docker compose invocations need TESTBED_HOST_SOURCE to resolve
# bind-mount paths. The Makefile defaults it to the repo root so that
# running `make` outside a devcontainer Just Works; inside a
# devcontainer, set it via remoteEnv (already wired in
# .devcontainer/kind/devcontainer.json).

SHELL       := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

RUNTIME ?= compose

# Anchor for compose bind-mounts. Override if running from an unusual shell.
export TESTBED_HOST_SOURCE ?= $(CURDIR)

COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE      := docker compose -f $(COMPOSE_FILE)

# Helm / Kubernetes
HELM_CHART   := deploy/helm-charts/rucio-storage-testbed
HELM_RELEASE ?= testbed
K8S_NAMESPACE ?= rucio-testbed
KUBECTL      := kubectl -n $(K8S_NAMESPACE)
HELM         := helm

# Execution wrappers based on RUNTIME (Defined after variables they depend on)
ifeq ($(RUNTIME), k8s)
  EXEC_RUCIO := $(KUBECTL) exec deploy/rucio-client --
  EXEC_FTS   := $(KUBECTL) exec deploy/fts --
  EXEC_FTS_OIDC   := $(KUBECTL) exec deploy/fts-oidc --
else
  EXEC_RUCIO := docker exec compose-rucio-client-1
  EXEC_FTS   := docker exec compose-fts-1
  EXEC_FTS_OIDC   := docker exec compose-fts-oidc-1
endif

# ── Help ──────────────────────────────────────────────────────────────────
.PHONY: help
help: ## Show this help (default target)
	@awk 'BEGIN {FS = ":.*?## "} \
	    /^[a-zA-Z0-9_%-]+:.*?## / { printf "  \033[36m%-26s\033[0m %s\n", $$1, $$2 } \
	    /^## / { sub(/^## /, ""); printf "\n\033[1m%s\033[0m\n", $$0 }' $(MAKEFILE_LIST)

## Setup

.PHONY: certs
certs: ## Generate all certificates (CA, hosts, StoRM trust anchors, JVM cacerts)
	./shared/scripts/generate-certs.sh

## Stack lifecycle (compose-*)

.PHONY: compose-up
compose-up: ## Start the full stack in the background
	$(COMPOSE) up -d

.PHONY: compose-down
compose-down: ## Stop the stack and remove volumes
	$(COMPOSE) down -v

.PHONY: compose-restart
compose-restart: compose-down compose-up ## Tear down and restart the stack

.PHONY: compose-rebuild
compose-rebuild: ## Rebuild and restart one or more services: make compose-rebuild SERVICES="teapot fts"
	$(COMPOSE) build $(SERVICES)
	$(COMPOSE) up -d --no-deps --force-recreate $(SERVICES)

.PHONY: compose-ps
compose-ps: ## List running containers
	$(COMPOSE) ps

.PHONY: compose-logs
compose-logs: ## Tail logs from all services (Ctrl-C to exit)
	$(COMPOSE) logs -f --tail=50

.PHONY: compose-logs-%
compose-logs-%: ## Tail logs from a single service, e.g. `make compose-logs-rucio`
	$(COMPOSE) logs -f --tail=100 $*

.PHONY: compose-build
compose-build: ## Build local Docker images (fts, xrd, rucio-client-docker-kubectl)
	$(COMPOSE) build

.PHONY: bootstrap
bootstrap: ## Bootstrap Rucio (uses $RUNTIME — set RUNTIME=k8s for kubernetes)
	./shared/scripts/bootstrap-testbed.sh

## Helm / Kubernetes lifecycle (helm-*, k8s-*)

.PHONY: helm-lint
helm-lint: ## Lint the umbrella chart
	$(HELM) lint $(HELM_CHART)

.PHONY: helm-template
helm-template: ## Render manifests locally (helm template …) without installing
	$(HELM) template $(HELM_RELEASE) $(HELM_CHART) -n $(K8S_NAMESPACE)

.PHONY: helm-install
helm-install: ## Create the namespace and install the umbrella chart
	$(KUBECTL) create namespace $(K8S_NAMESPACE) --dry-run=client -o yaml | kubectl apply -f -
	$(HELM) dependency update $(HELM_CHART)
	$(HELM) install $(HELM_RELEASE) $(HELM_CHART) -n $(K8S_NAMESPACE)

.PHONY: helm-upgrade
helm-upgrade: ## Apply local chart changes to the running release
	$(HELM) upgrade $(HELM_RELEASE) $(HELM_CHART) -n $(K8S_NAMESPACE)

.PHONY: helm-uninstall
helm-uninstall: ## Uninstall the release and delete its PVCs
	$(HELM) uninstall $(HELM_RELEASE) -n $(K8S_NAMESPACE) || true
	$(KUBECTL) delete pvc --all --ignore-not-found

.PHONY: helm-reinstall
helm-reinstall: helm-uninstall helm-install ## Uninstall + install (full reset)

.PHONY: k8s-pods
k8s-pods: ## List pods in the testbed namespace
	$(KUBECTL) get pods

## Tests
.PHONY: test-xrootd-gsi
test-xrootd-gsi: ## XRootD TPC test with X.509 GSI
	$(EXEC_FTS) bash -c "pytest /tests/test-fts-with-xrootd.py"

.PHONY: test-s3
test-s3: ## S3/MinIO test with signed URLs
	$(EXEC_FTS) bash -c "pytest /tests/test-fts-with-s3.py"

.PHONY: test-webdav
test-webdav: ## WebDAV TPC test with X.509 GSI
	$(EXEC_FTS) bash -c "RUNTIME=$(RUNTIME) K8S_NAMESPACE=$(K8S_NAMESPACE) pytest /tests/test-fts-with-webdav.py"

.PHONY: test-storm
test-storm: ## StoRM WebDAV TPC test with OIDC tokens
	$(EXEC_FTS_OIDC) bash -c "RUNTIME=$(RUNTIME) K8S_NAMESPACE=$(K8S_NAMESPACE) pytest /tests/test-fts-with-storm-webdav.py"

.PHONY: test-xrootd-oidc
test-xrootd-oidc: ## XRootD TPC test with OIDC tokens (SciTokens)
	$(EXEC_FTS_OIDC) bash -c "pytest /tests/test-fts-with-xrootd-scitokens.py"

.PHONY: test-rucio
test-rucio: ## Rucio E2E transfer test
	$(EXEC_RUCIO) bash -c "RUNTIME=$(RUNTIME) K8S_NAMESPACE=$(K8S_NAMESPACE) pytest /tests/test-rucio-transfers.py"

.PHONY: test-happy-pathes
test-happy-pathes: ## Run all happy-path tests (in series)
	$(MAKE) test-xrootd-gsi
	$(MAKE) test-s3
	$(MAKE) test-webdav
	$(MAKE) test-storm
	$(MAKE) test-xrootd-oidc
	$(MAKE) test-rucio

.PHONY: test-failure-modes
test-failure-modes: ## Run fast failure mode tests
	$(EXEC_RUCIO) bash -c "RUNTIME=$(RUNTIME) pytest /tests/test-rucio-checksum-mismatch.py"
	$(EXEC_RUCIO) bash -c "RUNTIME=$(RUNTIME) pytest /tests/test-rucio-replica-unavailable.py"

.PHONY: test-failure-modes-slow
test-failure-modes-slow: ## Run slow failure mode tests (token expiry, etc.)
	$(EXEC_RUCIO) bash -c "RUNTIME=$(RUNTIME) pytest /tests/test-rucio-token-expiry.py"

.PHONY: test-teapot
test-teapot: ## Teapot WebDAV test with OIDC tokens
	$(EXEC_RUCIO) bash -c "RUNTIME=$(RUNTIME) K8S_NAMESPACE=$(K8S_NAMESPACE) pytest /tests/test-teapot.py -v"

## Development

.PHONY: lint
lint: ## Run pre-commit hooks on all files
	pre-commit run --all-files

## Cleanup

.PHONY: clean
clean: ## Remove generated certs and volumes; keep CA (rucio_ca.pem + key)
	$(COMPOSE) down -v --remove-orphans 2>/dev/null || true
	@# Preserve the CA so we don't have to re-trust it on every iteration.
	@# We group the patterns with -o (OR) and \( \) to apply the ! -name (NOT) logic to all of them.
	find certs \
	! -name 'rucio_ca.pem' \
	! -name 'rucio_ca.key.pem' \
	\( -name '*.pem' -o -name '*.namespaces' -o -name '*.signing_policy' -o -name '*.csr' -o -name '*.r0' -o -name '*.0' \) \
	-delete 2>/dev/null || true
	rm -rf certs/storm-cacerts
	@echo "Cleaned certs (preserved rucio_ca.pem and rucio_ca.key.pem) and volumes"
