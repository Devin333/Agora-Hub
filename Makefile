PYTHON ?= python

.PHONY: install compile test test-workflows test-services smoke smoke-test-no-llm smoke-test-agent-loop smoke-live-offline smoke-live diagnose sources-validate tool-stability

install:
	$(PYTHON) -m pip install -U pip
	$(PYTHON) -m pip install -e ".[dev]"

compile:
	$(PYTHON) -m scripts.dev compile

test:
	$(PYTHON) -m scripts.dev test

test-workflows:
	$(PYTHON) -m scripts.dev test-workflows

test-services:
	$(PYTHON) -m scripts.dev test-services

smoke:
	$(PYTHON) -m scripts.dev smoke

smoke-test-no-llm:
	$(PYTHON) -m scripts.dev smoke-test-no-llm

smoke-test-agent-loop:
	$(PYTHON) -m scripts.dev smoke-test-agent-loop

smoke-live-offline:
	$(PYTHON) -m scripts.dev smoke-live-offline

smoke-live:
	$(PYTHON) -m scripts.dev smoke-live

diagnose:
	$(PYTHON) -m scripts.dev diagnose

sources-validate:
	$(PYTHON) -m scripts.dev sources-validate

tool-stability:
	$(PYTHON) -m scripts.dev tool-stability --layer $(or $(LAYER),fault)
