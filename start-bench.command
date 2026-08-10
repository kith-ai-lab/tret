#!/bin/bash
# macOS double-click launcher for bench. All the real logic lives in
# start-bench.sh so macOS and Linux stay in step.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./start-bench.sh
