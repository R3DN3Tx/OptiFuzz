#!/usr/bin/env bash
# Starts the OptiFuzz desktop window (Linux).
cd "$(dirname "$0")"
exec .venv/bin/python run_desktop.py "$@"
