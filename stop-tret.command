#!/bin/bash
# macOS double-click stopper for tret. The real logic lives in stop-tret.sh.
cd "$(dirname "$0")" || exit 1
exec /bin/bash ./stop-tret.sh
