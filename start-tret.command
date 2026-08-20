#!/bin/bash
# macOS double-click launcher for tret. All the real logic lives in
# start-tret.sh so macOS and Linux stay in step.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start-tret.sh
