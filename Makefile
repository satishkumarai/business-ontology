.PHONY: help install unit deploy deploy-prod seed test test-agent test-all render teardown-data

ENV ?= dev
PY  ?= python3

help:
	@echo "make install        install local dev dependencies"
	@echo "make unit           run unit tests (no Snowflake)"
	@echo "make deploy         deploy to ENV (default dev)"
	@echo "make seed           seed demo domains/glossaries through the governed path"
	@echo "make test           integration tests against ENV"
	@echo "make test-agent     agent behavioural evaluation (calls Cortex)"
	@echo "make render         print rendered SQL for ENV (no connection)"
	@echo "make teardown-data  wipe ontology data in a non-prod ENV"

install:
	$(PY) -m pip install -r requirements.txt

unit:
	$(PY) -m pytest tests/unit -q

render:
	$(PY) scripts/deploy.py --env $(ENV) --dry-run

deploy:
	$(PY) scripts/deploy.py --env $(ENV)

deploy-prod:
	$(PY) scripts/deploy.py --env prod --connection $${SNOWFLAKE_CONNECTION:?set SNOWFLAKE_CONNECTION}

seed:
	$(PY) scripts/seed_demo.py --env $(ENV)

test:
	ONTOLOGY_ENV=$(ENV) $(PY) -m pytest tests/integration -v

test-agent:
	ONTOLOGY_ENV=$(ENV) $(PY) -m pytest tests/agent -v

test-all: unit test test-agent

teardown-data:
	$(PY) scripts/teardown.py --env $(ENV) --data
