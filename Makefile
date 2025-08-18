# Every check that CI runs is available here under the same name, so a red build
# can be reproduced locally without reading the workflow file.

COMPOSE      ?= docker compose
PROM_IMAGE   ?= prom/prometheus:v3.4.1
AM_IMAGE     ?= prom/alertmanager:v0.28.1
PYTHON       ?= python3
WORKDIR      := $(CURDIR)

# promtool and amtool run from the pinned images rather than from a local
# install: the version that validates the rules must be the version that
# evaluates them.
PROMTOOL = MSYS_NO_PATHCONV=1 docker run --rm -v "$(WORKDIR)":/w -w /w \
	--entrypoint promtool $(PROM_IMAGE)
AMTOOL   = MSYS_NO_PATHCONV=1 docker run --rm -v "$(WORKDIR)":/w -w /w \
	--entrypoint amtool $(AM_IMAGE)

.DEFAULT_GOAL := help

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'

# --- stack -----------------------------------------------------------------

.PHONY: up
up: ## Build and start the whole stack
	$(COMPOSE) up -d --build
	@echo
	@echo "Grafana       http://localhost:3000  (anonymous viewer)"
	@echo "Prometheus    http://localhost:9090"
	@echo "Alertmanager  http://localhost:9093"
	@echo "Sample app    http://localhost:8000/docs"
	@echo "Alert sink    http://localhost:9095/alerts"

.PHONY: down
down: ## Stop the stack, keep the volumes
	$(COMPOSE) down --remove-orphans

.PHONY: clean
clean: ## Stop the stack and delete its data
	$(COMPOSE) down --remove-orphans --volumes

.PHONY: ps
ps: ## Show container and health status
	$(COMPOSE) ps

.PHONY: logs
logs: ## Follow the logs of every service
	$(COMPOSE) logs -f --tail=100

.PHONY: reload
reload: ## Apply changed Prometheus rules without restarting the TSDB
	curl -fsS -XPOST http://localhost:9090/-/reload && echo "prometheus reloaded"
	curl -fsS -XPOST http://localhost:9093/-/reload && echo "alertmanager reloaded"

# --- demos -----------------------------------------------------------------

.PHONY: demo-normal
demo-normal: ## Steady healthy traffic, nothing should fire
	$(COMPOSE) --profile load run --rm loadgen \
		--scenario normal --target http://sample-app:8000

.PHONY: demo-errors
demo-errors: ## 35% of requests start failing; ErrorBudgetBurnFast fires within ~10 min
	$(COMPOSE) --profile load run --rm loadgen \
		--scenario error-burst --target http://sample-app:8000

.PHONY: demo-latency
demo-latency: ## Every request gains 700 ms; LatencyBudgetBurnFast fires within ~16 min
	$(COMPOSE) --profile load run --rm loadgen \
		--scenario latency-spike --target http://sample-app:8000

.PHONY: demo-traffic-drop
demo-traffic-drop: ## Traffic collapses with nothing failing; TrafficDropped fires within ~31 min
	$(COMPOSE) --profile load run --rm loadgen \
		--scenario traffic-drop --target http://sample-app:8000

.PHONY: check-alerts
check-alerts: ## What is firing in Prometheus, Alertmanager and the webhook sink
	PYTHONPATH=alert-sink $(PYTHON) -m alert_sink.check

.PHONY: clear-faults
clear-faults: ## Disarm any fault left behind by an interrupted demo
	curl -fsS -XDELETE http://localhost:8000/faults && echo

# --- checks ----------------------------------------------------------------

.PHONY: lint
lint: lint-yaml lint-prometheus lint-alertmanager lint-dashboards lint-python ## Run every linter

.PHONY: lint-yaml
lint-yaml:
	$(PYTHON) -m yamllint -c .yamllint.yml .

.PHONY: lint-prometheus
lint-prometheus:
	$(PROMTOOL) check config prometheus/prometheus.yml
	$(PROMTOOL) check rules prometheus/rules/recording.yml prometheus/rules/alerting.yml

.PHONY: lint-alertmanager
lint-alertmanager:
	$(AMTOOL) check-config alertmanager/alertmanager.yml

.PHONY: lint-dashboards
lint-dashboards:
	@for file in grafana/dashboards/*.json; do \
		$(PYTHON) -m json.tool "$$file" > /dev/null || exit 1; \
		echo "valid JSON: $$file"; \
	done

.PHONY: lint-python
lint-python:
	$(PYTHON) -m ruff check .
	$(PYTHON) -m mypy sample-app/app loadgen/loadgen alert-sink/alert_sink

.PHONY: test
test: test-rules test-python ## Run the rule unit tests and the Python tests

.PHONY: test-rules
test-rules:
	$(PROMTOOL) test rules prometheus/rules/tests/availability_test.yml \
		prometheus/rules/tests/latency_test.yml \
		prometheus/rules/tests/infrastructure_test.yml \
		prometheus/rules/tests/traffic_test.yml \
		prometheus/rules/tests/recording_test.yml

.PHONY: test-python
test-python:
	$(PYTHON) -m pytest
