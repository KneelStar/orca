# Orca

Orca is the central dashboard. **Orca Client** is an independent server manager installed on each machine. They share a Python package and image, but run as separate services with separate settings and databases. Clients never communicate with one another.

Version 0.2.0 includes:

- Single-admin password login, with server-side authorization and CSRF protection.
- Saved clients, shared/admin-only URL shortcuts, and configurable page headings.
- Drag handles for rearranging clients, Orca actions, Defined actions, and shortcuts in admin mode; orders are saved across reloads. Handles also support touch and arrow keys.
- CPU, RAM, OS, and disk metrics from each client; dashboard disk percentage is the fullest reported filesystem.
- Add/edit/delete client-local command buttons, working directories, and timeouts.
- Live command output, exit status, recent job history, and persistence across page reloads.
- Direct client access when the orchestrator is offline.
- Docker and native startup options.
- **Actions** and **Docker** client tabs; immutable Orca actions and editable Defined actions share Action output and Recent actions.
- All existing Docker containers with state/health, CPU, memory, and lifecycle controls.
- Manual image update checks, per-container indicators, and editable project-wide Compose updates with exact-command confirmation.

Interactive terminals and automatic scheduling are deferred.

## Stack

Python 3.10+, Flask, Waitress, SQLite, psutil, and plain HTML/CSS/JavaScript. No Node server, frontend build step, external database, or message broker. The orchestrator authenticates to clients with distinct bearer tokens; browsers do not receive those tokens. Output is polled once per second while a selected job runs. Metrics refresh every five seconds while the admin page is visible.

## Exporting a local image

The orchestrator and client use the same image; `ORCA_ROLE` selects the service. Build and export it on a machine with Docker access:

```sh
docker build -t orca:0.2.0 .
docker save orca:0.2.0 | gzip > orca-0.2.0.tar.gz
```

Copy `orca-0.2.0.tar.gz`, the appropriate Compose file, and that machine’s `.env` to the server. Load the image there:

```sh
gunzip -c orca-0.2.0.tar.gz | docker load
docker compose up -d
```

The Compose files use `orca:0.2.0` by default and retain `build: .` for development when the repository is present. Set `ORCA_IMAGE` if you use another local tag. No registry or source checkout is needed on the deployment server.

## Docker: Orca orchestrator

Copy `.env.orchestrator.example` to `.env`, then set:

- `ORCA_ADMIN_PASSWORD`: your administrator password, at least 12 characters.
- `ORCA_SESSION_SECRET`: an independent random session-signing secret, at least 32 characters.
- `ORCA_ADMIN_TITLE` and `ORCA_PUBLIC_TITLE`: the two page headings.
- `ORCA_BIND_IP`: `0.0.0.0` by default so network and Tailscale devices can connect. Set `127.0.0.1` for localhost-only access.
- `ORCA_PORT`: the external port, normally 8000 for Orca.

Generate random secrets with `python3 -c "import secrets; print(secrets.token_urlsafe(48))"`.

```sh
docker compose up -d --build
```

Visit the bound address and port. Shared visitors see only shared shortcuts. Use **Admin login** to add clients, private shortcuts, and actions.

## Docker: Orca Client

On each server, copy `.env.client.example` to `.env`, then set its own admin password, session secret, and random `ORCA_CLIENT_TOKEN`. Set `ORCA_CLIENT_NAME`, `ORCA_BIND_IP`, and `ORCA_PORT=8001`.

```sh
docker compose -f compose.client.yaml up -d --build
```

Register the client in Orca with its name, full URL including port, and matching client token. The main server can have an Orca Client too, using a different port.

### Container execution versus host execution

A Docker client in default `local` mode executes commands **inside its container**. Its metrics describe what that container can see, not a guaranteed representation of the physical host. The interface labels this scope explicitly. The base image deliberately has no Docker socket, host filesystem mount, or elevated host privileges.

For host actions from a Docker client, use the optional SSH configuration. This first version supports a **POSIX host (Linux/macOS)** with an SSH server and Python plus psutil installed in the host Python environment. For example, provision a small host Python environment with `python3 -m venv ~/.orca-host`, then install psutil there and set `ORCA_SSH_PYTHON` to its absolute Python path. Native Windows clients use local execution; SSH execution against Windows is not implemented.

1. Provision an SSH account on the host with the permissions needed for your actions. Configure its public key and verify its host fingerprint independently.
2. Set `ORCA_SSH_TARGET=user@host.docker.internal` (or another explicit host address), `ORCA_SSH_KEY_FILE` to the private key path, and `ORCA_SSH_KNOWN_HOSTS_FILE` to your verified known-hosts file. A non-default SSH port requires the corresponding known-hosts entry.
3. Ensure the private key is readable by the container user (UID 10001) while retaining restrictive permissions. Both files are mounted read-only. Host verification and noninteractive key authentication are mandatory.
4. Start the client with the override:

```sh
docker compose -f compose.client.yaml -f compose.client-ssh.yaml up -d --build
```

Commands then run on that host, and metrics come from its Python/psutil. The target account's permissions apply. Commands cannot prompt for a sudo password; configure narrowly scoped noninteractive permissions for the specific actions you want. Long-running commands have a timeout on the host as well as in Orca. This is not an interactive terminal. Desktop wake actions may also need the correct user's display/session environment.

### Automated host setup on Ubuntu/Debian

For an Orca Client container running on the same Ubuntu/Debian host it manages, use [scripts/setup_host.py](scripts/setup_host.py). This requires systemd. Run it **on the host**, from the repository directory:

```sh
sudo python3 scripts/setup_host.py
```

The script installs and enables the SSH server, installs Python/venv and sudo, creates the `orca-run` account, and installs psutil in `/home/orca-run/.orca-host`. It creates an SSH key under `/etc/orca-ssh`, authorizes it for `orca-run`, and records the host's local SSH public key for `host.docker.internal`. The private key is readable only by the container user, UID/GID `10001` by default. Existing authorized keys are preserved and the generated private key is reused on repeat runs. For a custom container user, pass `--container-uid` and `--container-gid`.

**This script grants `orca-run` unrestricted passwordless sudo: `NOPASSWD: ALL`. Anyone able to run Orca actions can therefore execute commands as root on this host.** The script validates the sudoers rule and checks passwordless root access. Use the manual setup above if you want to grant only specific privileged commands.

After successful setup, add or update these values in your existing client `.env`, preserving its passwords, session secret, client token, and port:

```dotenv
ORCA_IMAGE=orca:0.2.0
ORCA_EXECUTION_MODE=ssh
ORCA_SSH_TARGET=orca-run@host.docker.internal
ORCA_SSH_PORT=22
ORCA_SSH_PYTHON=/home/orca-run/.orca-host/bin/python
ORCA_SSH_KEY_FILE=/etc/orca-ssh/id_ed25519
ORCA_SSH_KNOWN_HOSTS_FILE=/etc/orca-ssh/known_hosts
```

The script prints the Python path for the account's actual home directory if it differs from `/home/orca-run`. With the image already loaded, restart the client from the directory containing `.env` and both Compose files:

```sh
sudo docker compose -f compose.client.yaml -f compose.client-ssh.yaml up -d --no-build
```

If UFW is active, the script prints firewall setup instructions unless you supply `--docker-network`. Find the client's existing network with `sudo docker network ls`, then allow its subnet to reach host SSH (replace `orca-client_default` with your actual network name):

```sh
sudo python3 scripts/setup_host.py --firewall-only --docker-network orca-client_default
```

For a full setup with firewall configuration, use `sudo python3 scripts/setup_host.py --docker-network orca-client_default`. The network must already exist; otherwise, start the client first and run the firewall-only command afterward. The script reads the network's IPv4 subnet and adds a UFW rule for TCP port 22 to the default Docker bridge gateway. It does not enable UFW or open other ports. If `host.docker.internal` resolves to a custom address, add `--ssh-host-address ADDRESS`. Repeating the same command is safe; if Docker recreates the network with a different subnet, rerun it for the new subnet and remove the obsolete UFW rule with `sudo ufw status numbered` and `sudo ufw delete RULE_NUMBER`.

Keep bridge networking for these Compose files. Host networking ignores published port mappings and requires separate changes to the service's listening port and SSH target. Test a quick action in Orca:

```sh
whoami; hostname; sudo -n id
```

The output should show `orca-run`, the host's hostname, and root identity for the last command. Actions use host filesystem paths for their working directory. Use `sudo -n` for privileged actions; it fails immediately if passwordless access is unavailable instead of attempting a password prompt. If setup stops with an error, fix the reported issue and rerun the script.

## Docker management

The Docker tab lists existing containers, including created, stopped, paused, restarting, and dead containers. Unused images are not rows. CPU and memory follow `docker stats` output and show `—` when unavailable. Table data refreshes every five seconds while Docker is visible. State dots distinguish lifecycle state from health-check status; their descriptions are accessible by hover, keyboard focus, or tap.

Install Docker CLI on the execution target, Compose v2 for generated updates, and Buildx for registry checks. In SSH mode these tools and registry credentials belong to the SSH account on the host; Orca sends its read-only Python worker to that host, so a separate Orca installation there is unnecessary. The base Orca image does not include Docker CLI or mount the Docker socket. Use SSH host execution or a native client with Docker access for this feature.

`ORCA_DOCKER_COMMAND` defaults to `docker`. If the execution account requires configured passwordless sudo, explicitly set `ORCA_DOCKER_COMMAND="sudo -n docker"` in the client environment. `ORCA_DOCKER_CONTEXT` optionally selects a Docker context. The same command prefix/context applies to discovery, registry checks, generated updates, and lifecycle controls. Registry authentication must be available to that effective Docker user. Orca never silently switches users to gain Docker access. Existing host setup does not need to be rerun for this release.

**Check image updates** is an immutable Orca action. It queries registries without pulling images or changing containers, selecting the image configuration digest for each installed image's platform. It checks the currently configured tag, including fixed version tags; it does not search for newer tag names. Digest-pinned and image-ID-only references cannot discover another release automatically. Check results and failures appear in Action output and Recent actions. There is no automatic check schedule.

The client database stores registry targets, check times, errors, update commands, and execution history. Green update icons mean the row's installed image differs from its last successfully checked registry target. Normal icons can mean no known difference, no successful check, or a pinned image. A failed registry refresh retains the previous successful target and records the failure in output. Reloading or recreating a container compares its actual image with that saved target, so installed updates clear naturally. The orchestrator proxies these requests and does not store this Docker state.

For Compose containers, Orca uses project, working-directory, ordered Compose-file, and environment-file labels to suggest a whole-project `pull && up -d` command. Files must exist on the execution target. The Update/Edit button group shares one saved command across the project's rows, and manual edits survive recreation. Missing metadata, standalone containers, and Swarm services need a manually supplied deployment command. Swarm task lifecycle controls are disabled because the deployment manager owns those tasks.

Review generated commands for configurations whose original launch environment cannot be reconstructed from Docker labels: exported variables, selected Compose profiles, launcher-specific options, and Portainer/Git-managed deployments. Use the owning manager's deployment command where appropriate. Orca does not recover an arbitrary original `docker run` command. Paths containing ambiguous comma separators may require manual configuration. For named profiles, add the original `--profile` options in Edit before updating. File existence does not guarantee the execution account can read a file; errors remain visible in command output.

Every update confirmation shows the exact command and working directory. Standard project-wide Compose updates may recreate several containers and start stopped services. They preserve tags and digest pins and do not perform application-specific version migrations. A changed command invalidates an open confirmation. Progress remains in the Docker popup, and full output is also saved in action history. Command completion does not imply that all containers are healthy; refreshed table dots show their states separately.

All Orca actions, Defined actions, Docker updates, and lifecycle commands share one running job per client. Discovery/statistics reads remain available while a job runs. Start is available for created/stopped containers; running containers expose Stop, Pause, and Restart; paused containers must first Resume; restarting containers expose Stop; removing/dead containers have no lifecycle controls. Update/Edit are disabled during another job. Container IDs are rechecked before execution rather than silently retargeting a replaced container.

The client detects its own container using its configured client token or container hostname/ID and disables stopping, pausing, restarting, or updating that project from its Docker controls. Update Orca from a separate host terminal or external executor. If the token is supplied only through a file and the container has a custom hostname, automatic self-identification may be unavailable; avoid commands that replace the executing client itself. Saved manual commands run exactly as confirmed, so their actual scope may exceed the selected project.

The standalone equivalent remains available with `python3 scripts/check_app_updates.py` (`--context`, `--app`, and `--json` are supported). It prints results without writing Orca's database; use the built-in action to populate UI indicators.

## Native startup

Native execution is the simplest way for Orca Client to manage its host directly, and is supported by the code on Linux, macOS, and Windows. This milestone was exercised on Linux only.

```sh
python3 -m venv .venv-orca
.venv-orca/bin/python -m pip install -r requirements.txt
.venv-orca/bin/python -m pip install -e .
```

On Windows, use `.venv-orca\Scripts\python.exe` and the entry points in `.venv-orca\Scripts`.

With the appropriate environment configured:

```sh
.venv-orca/bin/orca
# On each client, with its own environment:
.venv-orca/bin/orca-client
```

Alternatively, set `ORCA_ROLE=orchestrator` or `ORCA_ROLE=client` and run `python -m orca_app`. Set `ORCA_ENV_FILE` to select a different environment file. `ORCA_DATABASE` selects the database path. Native services bind to `0.0.0.0` by default; set `ORCA_HOST=127.0.0.1` for localhost-only access. Native clients execute commands using the service user's shell and permissions, with the service's working directory unless the action specifies one.

## Configuration

| Variable              | Default           | Purpose                                                                |
| --------------------- | ----------------- | ---------------------------------------------------------------------- |
| `ORCA_ADMIN_TITLE`    | Your fleet        | Admin page heading on the orchestrator                                 |
| `ORCA_PUBLIC_TITLE`   | Your home base    | Shared page heading                                                    |
| `ORCA_CLIENT_NAME`    | Hostname          | Client identity                                                        |
| `ORCA_COOKIE_SECURE`  | false             | Set true when serving through HTTPS                                    |
| `ORCA_HOST`           | 0.0.0.0           | Native bind address; set `127.0.0.1` for localhost-only access         |
| `ORCA_PORT`           | 8000/8001         | Native service or Compose published port                               |
| `ORCA_BIND_IP`        | 0.0.0.0           | Compose published interface; use `127.0.0.1` for localhost-only access |
| `ORCA_DATABASE`       | data/ROLE.sqlite3 | Native database path                                                   |
| `ORCA_DOCKER_COMMAND` | docker            | Docker CLI prefix on the execution target, e.g. `sudo -n docker`       |
| `ORCA_DOCKER_CONTEXT` | current context   | Optional Docker context for discovery and generated commands          |
| `ORCA_EXECUTION_MODE` | local             | Client command and metrics target; optional ssh                        |

Protect the environment files, databases, and backups: the orchestrator database contains client credentials and clients retain command output. Public shortcuts control visibility in Orca, not authorization at their destination. When using `0.0.0.0`, network access is controlled by your firewall, Tailscale policies, and bind address; Orca does not itself enroll devices into Tailscale. For browser HTTPS, terminate TLS through your existing reverse proxy or Tailscale Serve and enable secure cookies.

Jobs run one at a time on each client. Reloading the browser does not stop a job. Action output follows the current request; Recent actions has its own output panel for browsing the latest 30 runs without replacing the current output. Starting an action switches to Action output.

Finished job records and their output are automatically deleted six calendar months after completion. Cleanup runs on client startup and at most once per hour during authenticated client requests; an idle client cleans up when next used. Running jobs and action definitions are never removed by retention. Deleting records frees database pages for reuse; it does not necessarily shrink the SQLite file on disk. Each run retains the latest 262,144 decoded characters of output (approximately 256 KiB for ASCII).

An interrupted service marks unfinished jobs as interrupted on restart; it does not automatically retry a possibly destructive action. Inspect host state before rerunning. Commands should run in the foreground; detached processes cannot be reliably tracked as jobs. Output buffering by the command itself can delay displayed output.

## Verification

```sh
.venv-orca/bin/python -m unittest discover -s tests -v
```

The tests cover authentication, CSRF, public visibility, persistence, host metrics, real client proxying, streamed output, concurrent action rejection, failures, timeouts, recovery, and SSH command construction. They open a localhost test server.

A disposable local preview is available with:

```sh
.venv-orca/bin/python tests/preview.py
```

It runs Orca at `http://127.0.0.1:8765` and an independent client at `http://127.0.0.1:8766`. Its **test-only password is `orca-preview-local`**, all databases are temporary, and it binds only to localhost. Do not expose this preview through a proxy or tailnet. The preview uses real metrics and real actions on the local machine; add only commands you intend to execute.

`tests/browser.cjs` checks the preview using Playwright, including mobile layout, adding actions/shortcuts, running a harmless command, direct client access, and private shortcut visibility. Supply `ORCA_PLAYWRIGHT` if the module isn't on Node's module path, and optionally `ORCA_BROWSER` for an existing Chromium executable.

`tests/reorder.cjs` checks dragging all three lists, persistence after reloads and edits, rollback on save errors, keyboard movement, and mobile touch dragging against the same disposable preview.

`tests/docker-browser.cjs` covers the Docker tab, accessible state descriptions, per-image indicators, shared command edits, exact confirmation, update progress, container recreation, lifecycle controls, history, and mobile layout. Run the preview with `ORCA_PREVIEW_DOCKER_FIXTURE=true` and ports 8875/8876 for this suite. Its Docker data is simulated and its update test runs only a harmless `printf`/`sleep` command. Backend tests also execute the bundled worker locally and through the SSH payload using a disposable fake Docker CLI.
