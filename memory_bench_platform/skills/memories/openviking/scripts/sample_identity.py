"""Resolve the same user identities used by direct HTTP and CLI operations."""
import json
import os
import sys

from run_operation import _scoped_environment


def main():
    if not os.environ.get("OPENVIKING_SCOPE_USERS_FILE"):
        raise ValueError("Concurrent OpenViking samples require OPENVIKING_SCOPE_USERS_FILE")
    identities = []
    for scope in json.load(sys.stdin)["scopes"]:
        env = _scoped_environment(os.environ, {"scope_id": scope})
        identities.append(json.dumps([env.get("OPENVIKING_ACCOUNT_ID"), env["OPENVIKING_USER_ID"]]))
    print(json.dumps({"identities": identities}))


if __name__ == "__main__":
    main()
