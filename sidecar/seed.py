"""Use a separate admin session for seed setup; never elevate the runtime account."""
import getpass
import json
import os
from config import Config
from email_orchestrator import load_local_env
from tools.openboxes import OpenBoxesTool


def main():
    load_local_env()
    config = Config(username=os.environ.get('OPENBOXES_ADMIN_USERNAME', 'admin'),
                    password=os.environ.get('OPENBOXES_ADMIN_PASSWORD') or getpass.getpass('OpenBoxes admin password: '),
                    api_key='')
    password = os.environ.get('OPENBOXES_PASSWORD') or getpass.getpass('New sidecar_bot password (16+ characters): ')
    if len(password) < 16:
        raise ValueError('sidecar_bot password must contain at least 16 characters')
    tool = OpenBoxesTool(config)
    try:
        tool.login()
        print(json.dumps(tool.request('POST', 'api/sidecar/sandbox/seed', json={'password': password}), indent=2))
    finally:
        tool.close()


if __name__ == '__main__':
    main()
