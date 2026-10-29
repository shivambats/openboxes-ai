"""Seed contact-only sandbox cases using an admin-scoped OpenBoxes API."""
import json
import os

from config import Config
from email_orchestrator import load_local_env
from tools.openboxes import OpenBoxesTool


def main():
    load_local_env()
    credential_file = os.getenv('SHIPMENT_EMAIL_ENV_FILE')
    if credential_file:
        load_local_env(credential_file, overwrite=True)
    username = os.getenv('OPENBOXES_ADMIN_USERNAME')
    password = os.getenv('OPENBOXES_ADMIN_PASSWORD')
    if not username or not password:
        raise ValueError('OPENBOXES_ADMIN_USERNAME and OPENBOXES_ADMIN_PASSWORD are required')
    config = Config(username=username, password=password, api_key='')
    wms = OpenBoxesTool(config)
    try:
        wms.login()
        print(json.dumps(wms.request('POST', 'api/sidecar/sandbox/seed-outreach'), indent=2))
    finally:
        wms.close()


if __name__ == '__main__':
    main()
