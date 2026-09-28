# Local OpenBoxes operations

When asked to restart the locally running OpenBoxes server, run `./docker/restart-openboxes-local.sh` from the repository root. The script uses the repository's base and sidecar Compose files, restarts only the `app` service, and waits for `/openboxes/health` to report `UP`. It leaves the MariaDB and nginx containers running.

Use the script's exit status and health message to report the result. If it fails, inspect the Compose service status and app logs. A restart uses the WAR already in the container; building or deploying source changes is a separate task.
