#!/usr/bin/env bash
set -e
export AUTH_TOKEN="CHANGE_ME_TO_A_STRONG_TOKEN"
python -m src.agent.agent --discover --auth-token "$AUTH_TOKEN"
