#!/bin/bash
# Opens the settings GUI.
cd "$(dirname "$(readlink -f "$0")")"
exec python3 topspin4_launcher.py
