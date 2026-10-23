#!/usr/bin/env bash
# Starts the OptiFuzz desktop window (Linux).
cd "$(dirname "$0")/../../.."  # repository root (this script lives in app/scripts/linux)
exec .venv/bin/python app/run_desktop.py "$@"
