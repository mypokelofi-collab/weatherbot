# Convenience targets. Everything works without them; these just save typing.

PY ?= python3
VENV ?= .venv
HOST ?=
PORT ?= 8033

.PHONY: help venv test run sim backtest record docker deploy logs clean

help:
	@echo "make venv           create .venv and install dependencies"
	@echo "make test           run the test suite"
	@echo "make sim            run the bot offline on the simulator (dashboard :$(PORT))"
	@echo "make run            run the bot on live Binance data (paper money)"
	@echo "make record MIN=60  record the live feed for later backtests"
	@echo "make backtest F=... replay a recording through the bot"
	@echo "make docker         build the container image locally"
	@echo "make deploy HOST=user@vps   deploy to a VPS over SSH"
	@echo "make logs HOST=user@vps     tail the remote logs"

venv:
	$(PY) -m venv $(VENV)
	$(VENV)/bin/pip install -U pip
	$(VENV)/bin/pip install -r requirements-dev.txt

test:
	$(VENV)/bin/python -m pytest -q

run:
	$(VENV)/bin/python -m flowbot run -c config/flowbot.yml --port $(PORT)

sim:
	$(VENV)/bin/python -m flowbot run -c config/sim.yml --port $(PORT)

record:
	$(VENV)/bin/python -m flowbot record -c config/flowbot.yml --minutes $(or $(MIN),60)

backtest:
	$(VENV)/bin/python -m flowbot backtest $(F) -c config/flowbot.yml

docker:
	docker build -t flowbot:latest .

deploy:
	@test -n "$(HOST)" || (echo "usage: make deploy HOST=user@your-vps"; exit 1)
	./scripts/deploy.sh $(HOST) --port $(PORT)

logs:
	@test -n "$(HOST)" || (echo "usage: make logs HOST=user@your-vps"; exit 1)
	ssh $(HOST) 'cd /opt/flowbot && docker compose logs -f --tail 100'

clean:
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
	rm -rf .pytest_cache
