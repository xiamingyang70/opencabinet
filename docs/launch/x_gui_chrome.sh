#!/bin/bash
# X posting GUI chrome: Xvfb + Chrome131 headful + CDP 9222
export DISPLAY=:99
pgrep Xvfb >/dev/null || { rm -f /tmp/.X99-lock; Xvfb :99 -screen 0 1440x900x24 & sleep 2; }
pkill -f oc-cdp 2>/dev/null; sleep 1
/tmp/chrome131/chrome-linux64/chrome \
  --no-sandbox --disable-dev-shm-usage \
  --remote-debugging-port=9222 --remote-allow-origins=* \
  --proxy-server=http://127.0.0.1:16492 \
  --user-agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36" \
  --window-size=1440,900 --user-data-dir=/tmp/oc-cdp-fresh "$@" \
  >/tmp/chrome131.log 2>&1 &
sleep 8
curl -s --max-time 5 http://127.0.0.1:9222/json/version | head -3
