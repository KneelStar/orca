# Changelog

## 0.2.6

- Fix false image update warnings with Docker's containerd image store, including `portainer/portainer-ce:lts`. Compare the installed platform manifest with the registry platform manifest; retain configuration-digest comparisons for classic Docker image storage.
- Keep cached update indicators consistent with check output and separate manifest targets from older configuration-digest cache entries.
- Use the container's platform metadata when available, ignore changes to other architectures, and pin registry reads to one release during each check. Missing manifest metadata produces a check failure instead of an update warning.
- Add regression coverage for containerd comparisons and cached indicators. All 31 targeted image-update, Docker, and execution-security tests pass.

## 0.2.5

- Security fixes.

# Orca security review

Reviewed October 4, 2026. Scope: application routes and authentication, browser code, SQLite persistence, local/SSH execution, Docker discovery and updates, host setup, deployment configuration, and Python dependencies. This review identifies reproducible issues and adds regression coverage; it is not a guarantee that every vulnerability has been found.

## Fixed findings

| Finding | Evidence and impact | Fix |
| --- | --- | --- |
| Admin cookie replay after logout or password change | A copied cookie returned HTTP 200 from `/api/clients` after logout and after changing the password. It retained management access, including client command execution. | Random session IDs now require a matching server-side record. Logout and reauthentication revoke the previous record; password/role changes invalidate it. Sessions expire eight hours after login and cannot cross databases. Stored session IDs are hashed. |
| HTML injection from remote client metadata | Remote action, job, and container IDs, plus action timeouts, were interpolated into HTML. The original code created seven injected elements in the browser regression. CSP limited script execution but did not prevent UI injection. | Escape dynamic HTML attributes/text, encode IDs in request paths, and compare IDs directly when restoring focus. |
| Private data remained available after sign-out | Client addresses, action definitions, output, history, and token drafts remained in memory or hidden DOM. Delayed admin requests could also repopulate that state. | Erase private state and DOM on sign-out or a protected request reporting session expiry; discard stale responses using request generations. Remote client authentication failures do not sign out the orchestrator. |
| Large public uploads buffered before Flask rejected them | Flask's 64 KiB limit did not constrain Waitress's default 1 GiB body buffer. A request over 512 KiB was accepted by the server parser under its defaults. | Apply the same 64 KiB limit in the production Waitress configuration. Fixed-length oversized requests are rejected from their headers; chunked bodies are bounded before application dispatch. |
| Slow clients could occupy proxy workers indefinitely | HTTP read timeouts measure inactivity, allowing a configured client to trickle headers or body data indefinitely while remaining below the response size limit. | Isolate each client request in a subprocess with a total deadline of 15 seconds, or 70 seconds for Docker operations. Timeout kills the subprocess. Preserve TLS verification, disabled environment proxies, disabled redirects, and the 1 MiB decoded response limit. Send credentials over private stdin rather than command arguments. |
| Unbounded Docker/registry and SSH query output | `capture_output`/`communicate` collected attacker-controlled registry or daemon output without a size limit. | Bound combined output to 16 MiB for Docker CLI queries, 32 MiB for the bundled worker, and 1 MiB for SSH metrics. Kill the process group on timeout or overflow, including descendants holding pipes open. |
| Windows shell interpretation of generated commands | POSIX `shlex` quoting does not protect shell metacharacters on Windows. Docker metadata influenced generated Compose commands. | Generated lifecycle controls execute argument vectors with `shell=False`. Native Windows Compose updates require a manually configured Windows command. Admin-defined shell actions remain supported. |
| Credential database created with public permissions | With umask 022, SQLite initially created the file as 0644 before a later chmod. Reconnecting after deletion also recreated it as 0644. | Create the file as 0600 before SQLite opens it; secure existing files before reading; reject symlinks, hard links, and files owned by another user on POSIX. Reconnects use `mode=rw` and do not recreate deleted files. Journals inherit private permissions. |
| SQLite connections remained open until garbage collection | 120 reads held 120 extra file descriptors with garbage collection disabled. | Close each connection deterministically after commit or rollback. |
| Ambiguous addresses and invalid bearer headers | URL parsing accepted stripped controls/backslashes and differed from HTTP/browser parsing. Tokens could contain characters unsuitable for HTTP headers. | Reject ambiguous host/address syntax and require saved tokens to contain visible ASCII characters without spaces. Local/private client addresses remain supported. |
| Orca environment values inherited by remote execution | SSH remote actions and metrics could inherit Orca environment variables; filtering was case-sensitive. | Strip `ORCA_*` variables case-insensitively from local/remote command and query environments. This is credential hygiene; arbitrary admin commands are not an isolation boundary against the service account. |

## Dependency hardening

The existing environment already contained the patched versions below, but the requirements did not constrain these transitive dependencies. They are now explicitly pinned so an existing installation cannot retain vulnerable versions while satisfying the direct requirements:

- Werkzeug 3.1.9 addresses the Windows special-device filename issue described in [the maintainer advisory](https://github.com/pallets/werkzeug/security/advisories/GHSA-g6x2-hccm-hh4m).
- urllib3 2.8.0 addresses unbounded HTTP chunk-size buffering, described in [the maintainer advisory](https://github.com/urllib3/urllib3/security/advisories/GHSA-vxq7-64xx-v4gw), and earlier [compressed-content resource exhaustion](https://github.com/urllib3/urllib3/security/advisories/GHSA-gh4c-6fx4-qh6g).
- Jinja2 3.1.6 addresses an `attr` filter sandbox escape described in [the maintainer advisory](https://github.com/pallets/jinja/security/advisories/GHSA-cpwx-vrp4-4pq7). Orca uses trusted templates; this pin also prevents an older environment from persisting.

Direct dependency pins were checked against public maintainer advisories. `pip check` passes in the existing environment. An automated OSV batch audit was rejected by automatic approval review because sending the package/version inventory to a public third party was considered potential data exposure. Public maintainer advisories were used instead; the third-party automated audit was not completed.

## Deployment and operational limits

- Source changes do not update running services or the existing `orca-0.2.4.tar.gz` Docker image export. The local Docker socket is inaccessible to this user, and noninteractive sudo requires authentication, so that archive could not be regenerated here. Rebuild from the updated source before deploying these fixes; the old export still contains the old code.
- Existing browser sessions require a new login after installing the fixes. The database schema remains compatible and session records are stored separately from client/action/job records.
- Private client addresses and HTTP remain supported for the documented LAN/Tailscale setup. Use HTTPS plus `ORCA_COOKIE_SECURE=true` for browser access over an untrusted network; client bearer tokens also need HTTPS or a trusted encrypted network path.
- `scripts/setup_host.py` intentionally grants unrestricted passwordless sudo to `orca-run`, as documented. Anyone authorized to run its actions has root-level capabilities. Provision a restricted account manually when that scope is unsuitable.
- Windows behavior was exercised with simulated platform conditions; native Windows and a real SSH/Docker deployment were not available for integration testing. POSIX database mode checks do not configure Windows ACLs.
- The packaged image's operating-system dependencies were not scanned or rebuilt. No recognizable secrets were found in 38 current tracked files or 133 historical source/configuration blobs by the credential-pattern scan. Large historical Docker image blobs and recursively decompressed image layers were omitted; this does not establish the absence of every possible secret.

To rebuild the deployment image and regenerate its export on a host with Docker access:

```bash
set -euo pipefail
sudo docker build --pull -t orca:0.2.4 .
sudo docker save orca:0.2.4 | gzip > orca-0.2.4.tar.gz.new
mv orca-0.2.4.tar.gz.new orca-0.2.4.tar.gz
```

Restart the relevant service after rebuilding, or load the rebuilt export on each deployment host. For native installations, reinstall `requirements.txt` and restart the service.

## Validation

Regression coverage is in `tests/test_security_app.py`, `tests/test_execution_security.py`, `tests/test_proxy_security.py`, `tests/test_store_security.py`, and `tests/frontend-security.cjs`. The Python suite includes the real localhost orchestrator/client proxy test. The browser security test uses hostile simulated client responses without executing host commands. Existing browser and reorder suites exercise the disposable preview.

- Complete Python suite: 73 tests passed, including the real orchestrator/client proxy and slow-response fixtures.
- `pip check`, Python compilation, JavaScript syntax checks, and `git diff --check` passed.
- The expanded `tests/frontend-security.cjs` passed, including protected HTTP 401 cleanup, an expired session attempting sign-out, remote HTTP 502 authentication isolation, wrong-password handling, malicious metadata, and stale responses. `tests/browser.cjs` passed again after the final UI changes and subprocess proxy integration. `tests/reorder.cjs` passed before the final expiry UI refinement.
- The unmodified Docker browser suite fails an existing layout assertion at `tests/docker-browser.cjs:28`: the heading and Add action button tops differ by 9 pixels under both original and patched JavaScript. All remaining assertions passed using a disposable copy that omitted only that assertion. No CSS change or test weakening was committed.

## 0.2.4

- Improved update popup loading time.

## 0.2.3

- Styling changes in defined actions, and docker buttons

## 0.2.2

- Fix Docker updates and lifecycle controls over SSH failing before execution because job metadata and the SSH transport both supplied `kind`.

## 0.2.1

- Fix Docker discovery failing with `Permission denied` when the execution user cannot inspect an original Compose directory, Compose file, or environment file.
- Keep the Docker table and image checks available. Disable only automatic update-command generation for the affected project, with an explanation in Edit; saved manual commands remain available.
- Add regression coverage for inaccessible deployment paths.

Upgrade the client image to `orca:0.2.1` and preserve the named volume mounted at `/data`. No database migration is required. The 0.2.0 orchestrator is compatible with this client patch. Ensure the deployed client Compose file forwards `ORCA_DOCKER_COMMAND` and `ORCA_DOCKER_CONTEXT`; changing `.env` alone does not add missing environment entries to a running container.

## 0.2.0

- Add Actions and Docker tabs to client details, with Orca actions, Defined actions, and shared Action output/history.
- List all existing containers with accessible lifecycle/health indicators, CPU, memory, and state-aware controls.
- Add manual registry image checks and client-owned cached results with per-container update indicators.
- Generate editable project-wide Compose update commands from discovery metadata and confirm the exact command before execution.
- Keep update progress on the Docker tab and refresh discovery after jobs complete.
- Apply the existing one-job-per-client limit to Docker operations and built-in checks.
- Support Docker commands on native or SSH execution targets with explicit command/context settings.

Update both orchestrator and clients to 0.2.0 for the new UI and API. Database additions are created automatically; existing actions and history are retained. Docker CLI/Compose/Buildx must be available on the execution target. No changes to host sudo privileges or host setup are performed by this release.
