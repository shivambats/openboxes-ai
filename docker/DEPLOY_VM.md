# Deploying on one VM

`docker-compose-vm.yml` runs four containers:

| Service | What it is | Reachable from outside |
|---|---|---|
| `caddy` | HTTPS front door; gets its certificate automatically | ports 80 and 443 |
| `app` | OpenBoxes (built from `openboxes.war`) | through Caddy only |
| `db` | MariaDB | no |
| `sidecar` | vegath-agent workflows, monitor and headless Chromium | chat progress socket through Caddy only |

The sidecar's own console (`/` on port 8091) is not exposed. Operators use the chat in the OpenBoxes dashboard.

## 1. VM

Ubuntu 22.04 or 24.04, 2 vCPU and 4 GB RAM at least (8 GB is more comfortable), 30 GB disk.
Install Docker Engine with the Compose plugin, open ports 22, 80 and 443 only, and point a
DNS A record (for example `openboxes.example.com`) at the VM's IP.

## 2. Build the WAR on your machine

The WAR is not in git (it is 186 MB). From the repository root:

```sh
./gradlew assemble -x generateGitProperties
cp build/libs/openboxes.war docker/openboxes.war
```

## 3. Copy the code and the WAR to the VM

```sh
ssh you@vm 'git clone -b vegath-agents https://github.com/shivambats/openboxes-ai.git'
scp docker/openboxes.war you@vm:openboxes-ai/docker/
```

## 4. Settings and keys (on the VM)

```sh
cd openboxes-ai/docker
cp vm.env.example vm.env
cp sidecar.vm.env.example sidecar.vm.env
chmod 600 vm.env sidecar.vm.env
```

Fill both in. `vm.env` holds the domain, database passwords and the OpenBoxes–sidecar token.
`sidecar.vm.env` holds the model, mail and search keys. Both are git-ignored and stay on the VM.
For the first start set `SIDECAR_SANDBOX_ENABLED=true` in `vm.env`.

## 5. Start

```sh
docker compose -f docker-compose-vm.yml --env-file vm.env up -d --build
docker compose -f docker-compose-vm.yml --env-file vm.env ps
```

The first start runs the OpenBoxes database migrations and takes a few minutes. The sidecar
waits until OpenBoxes reports healthy.

## 6. First-time setup

1. Open `https://<DOMAIN>/openboxes/`, sign in as `admin` / `password` and **change the admin
   password at once**. Put the new one in `sidecar.vm.env` as `OPENBOXES_ADMIN_PASSWORD`.
2. Create the vegath-agent account:

   ```sh
   C="docker compose -f docker-compose-vm.yml --env-file vm.env"
   $C up -d sidecar        # reload sidecar.vm.env
   $C exec sidecar python seed.py
   ```

3. Optional demo data (the CHI-IN-24103 to 24107 delayed shipments):

   ```sh
   $C exec sidecar python -c "import os; from config import Config; from tools.openboxes import OpenBoxesTool; \
   t = OpenBoxesTool(Config(username=os.environ['OPENBOXES_ADMIN_USERNAME'], password=os.environ['OPENBOXES_ADMIN_PASSWORD'], api_key='')); \
   t.login(); print(t.request('POST', 'api/sidecar/sandbox/seed-demo-delays'))"
   ```

4. Set `SIDECAR_SANDBOX_ENABLED=false` in `vm.env` and run `$C up -d app`.

## Updating

```sh
git pull
# copy a new docker/openboxes.war if the Grails or dashboard code changed
docker compose -f docker-compose-vm.yml --env-file vm.env up -d --build
```

## Backups

Database, uploads and sidecar state live in Docker volumes (`db-data`, `uploads`, `sidecar-data`).

```sh
docker compose -f docker-compose-vm.yml --env-file vm.env exec db \
  sh -c 'mysqldump -u root -p"$MYSQL_ROOT_PASSWORD" openboxes' > openboxes-$(date +%F).sql
```

## Before sharing the link

- The agent sends real email through AgentMail. Keep `CARRIER_TEST_RECIPIENTS` to inboxes you own.
- Give demo visitors ordinary OpenBoxes accounts. Chat write access can send email and create shipments.
- Logs: `docker compose -f docker-compose-vm.yml --env-file vm.env logs -f sidecar app`.
