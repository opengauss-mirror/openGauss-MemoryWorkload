"""Check the native user binding in each dedicated Gateway configuration."""
import json

from run_lifecycle import _plugin_config


def main():
    config = _plugin_config()
    user = config.get("userId")
    if not isinstance(user, str) or not user.strip():
        raise ValueError("OpenViking parallel samples require an explicit Gateway userId")
    print(json.dumps({"identities": [json.dumps([config.get("accountId"), user])]}))


if __name__ == "__main__":
    main()
