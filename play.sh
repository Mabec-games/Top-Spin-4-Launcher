#!/bin/bash
# Direct launch with saved settings (use this for Steam / Steam Deck Game Mode).
cd "$(dirname "$(readlink -f "$0")")"
exec python3 topspin4_launcher.py --play --wait
