"""Test adapter exercising actual subprocess environment and HTTP routing."""
import json
import os
import sys
import urllib.request

request = json.load(sys.stdin)
http = urllib.request.Request(os.environ["TEST_MEMORY_URL"], json.dumps(request).encode(),
                              {"Content-Type": "application/json"})
with urllib.request.urlopen(http, timeout=2) as response:
    print(response.read().decode())
