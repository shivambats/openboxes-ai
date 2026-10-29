#!/usr/bin/env python3
"""One-time local sandbox accounts for exercising operator chat permissions.

Credentials are written only to a mode-0600 file outside the repository. This
script refuses to alter a pre-existing account with one of the fixture names.
"""

import base64
import hashlib
import json
import os
import secrets
import subprocess
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ["docker", "compose", "-f", "docker/docker-compose.yml", "-f",
           "docker/docker-compose-sidecar.yml", "exec", "-T", "db", "sh", "-c",
           'MYSQL_PWD="$MYSQL_PASSWORD" mariadb -u "$MYSQL_USER" openboxes']
ACCOUNTS = (
    ("chat_reader", ("Morgan", "Reyes"), "ROLE_BROWSER"),
    ("chat_no_access", ("Taylor", "Brooks"), None),
    ("chat_operator", ("Jordan", "Ellis"), "ROLE_ASSISTANT"),
)
OUTPUT = Path("/private/tmp/openboxes-chat-test-users.json")
WAREHOUSE = "2c928083a0cf1b8801a0cf1c388d0002"


def sql(statement):
    result = subprocess.run(COMPOSE, input=statement, text=True, cwd=ROOT,
                            capture_output=True, check=True)
    return result.stdout.strip()


def main():
    if OUTPUT.exists():
        raise SystemExit("Credential file already exists; no accounts changed: " + str(OUTPUT))
    names = ",".join("'" + name + "'" for name, _, _ in ACCOUNTS)
    existing = sql(f"SELECT username FROM `user` WHERE username IN ({names});")
    if existing:
        raise SystemExit("Fixture username already exists; no accounts changed: " + existing)
    credentials = {}
    statements = ["START TRANSACTION;"]
    for username, (first_name, last_name), role in ACCOUNTS:
        password = secrets.token_urlsafe(24)
        digest = base64.b64encode(hashlib.sha1(password.encode()).digest()).decode()
        user_id = str(uuid.uuid4())
        credentials[username] = {"password": password, "chat_access":
                                 "write" if role == "ROLE_ASSISTANT" else
                                 "read" if role == "ROLE_BROWSER" else "none"}
        statements.append(
            "INSERT INTO person (id,version,date_created,last_updated,first_name,last_name,active) "
            f"VALUES ('{user_id}',0,NOW(),NOW(),'{first_name}','{last_name}',b'1');")
        statements.append(
            "INSERT INTO `user` (id,username,password,warehouse_id,timezone,remember_last_location) "
            f"VALUES ('{user_id}','{username}','{digest}','{WAREHOUSE}','UTC',b'1');")
        if role:
            statements.append(
                "INSERT INTO user_role (user_id,role_id) "
                f"SELECT '{user_id}',id FROM role WHERE role_type='{role}';")
    statements.append("COMMIT;")
    sql("\n".join(statements))
    descriptor = os.open(OUTPUT, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as target:
        json.dump(credentials, target, indent=2)
        target.write("\n")
    print("Created chat_reader (read), chat_no_access (none), chat_operator (read/write).")
    print("Credentials: " + str(OUTPUT))


if __name__ == "__main__":
    main()
