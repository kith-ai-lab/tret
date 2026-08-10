#!/bin/bash
# macOS double-click stopper for bench. The real logic lives in stop-bench.sh.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./stop-bench.sh
