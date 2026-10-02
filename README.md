# Orca

Orca is the central dashboard. **Orca Client** is an independent server manager installed on each machine. They share a Python package and image, but run as separate services with separate settings and databases. Clients never communicate with one another.

This first milestone includes:

- Single-admin password login, with server-side authorization and CSRF protection.
- Saved clients, shared/admin-only URL shortcuts, and configurable page headings.
- CPU, RAM, OS, and disk metrics from each client; dashboard disk percentage is the fullest reported filesystem.
- Add/edit/delete client-local command buttons, working directories, and timeouts.
- Live command output, exit status, recent job history, and persistence across page reloads.
- Direct client access when the orchestrator is offline.
- Docker and native startup options.

**Next milestone:** Compose application discovery, automatic update checks, and explicitly requested installation through the original Compose configuration. Those features are not implemented yet. Interactive terminals are deferred. The original `home_server_control/` app is preserved.

## Stack

Python 3.10+, Flask, Waitress, SQLite, psutil, and plain HTML/CSS/JavaScript. No Node server, frontend build step, external database, or message broker. The orchestrator authenticates to clients with distinct bearer tokens; browsers do not receive those tokens. Output is polled once per second while a selected job runs. Metrics refresh every five seconds while the admin page is visible.

## Exporting a local image

The orchestrator and client use the same image; `ORCA_ROLE` selects the service. Build and export it on a machine with Docker access:

```sh
docker build -t orca:0.1.1 .
docker save orca:0.1.1 | gzip > orca-0.1.1.tar.gz
```

Copy `orca-0.1.1.tar.gz`, the appropriate Compose file, and that machine’s `.env` to the server. Load the image there:

```sh
gunzip -c orca-0.1.1.tar.gz | docker load
docker compose up -d
```

The Compose files use `orca:0.1.1` by default and retain `build: .` for development when the repository is present. Set `ORCA_IMAGE` if you use another local tag. No registry or source checkout is needed on the deployment server.

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
| `ORCA_EXECUTION_MODE` | local             | Client command and metrics target; optional ssh                        |

Protect the environment files, databases, and backups: the orchestrator database contains client credentials and clients retain command output. Public shortcuts control visibility in Orca, not authorization at their destination. When using `0.0.0.0`, network access is controlled by your firewall, Tailscale policies, and bind address; Orca does not itself enroll devices into Tailscale. For browser HTTPS, terminate TLS through your existing reverse proxy or Tailscale Serve and enable secure cookies.

Jobs run one at a time on each client. Reloading the browser does not stop a job. Quick Output follows the current request; Recent Actions has its own output panel for browsing the latest 30 runs without replacing the current output. Starting an action switches to Quick Output.

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
