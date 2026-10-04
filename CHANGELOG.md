# Changelog

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
