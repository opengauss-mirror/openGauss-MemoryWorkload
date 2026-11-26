"""Check the native user binding in each dedicated Gateway configuration."""
import json
import os
import sys

from run_lifecycle import _plugin_config, managed_user


def main():
    config = _plugin_config()
    if os.environ.get("OPENVIKING_MANAGED_SAMPLE_USER") == "1":
        users = [managed_user(scope, config) for scope in json.load(sys.stdin)["scopes"]]
        print(json.dumps({"identities": [json.dumps([config["accountId"], user]) for user in users]}))
        return
    user = config.get("userId")
    if not isinstance(user, str) or not user.strip():
        raise ValueError("OpenViking parallel samples require an explicit Gateway userId")
    print(json.dumps({"identities": [json.dumps([config.get("accountId"), user])]}))


if __name__ == "__main__":
    main()
