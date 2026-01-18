@echo off
set AUTH_TOKEN=CHANGE_ME_TO_A_STRONG_TOKEN
python -m src.server.server --auth-token %AUTH_TOKEN%
