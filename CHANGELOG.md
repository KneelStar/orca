# Changelog

## 0.2.0

- Add Actions and Docker tabs to client details, with Orca actions, Defined actions, and shared Action output/history.
- List all existing containers with accessible lifecycle/health indicators, CPU, memory, and state-aware controls.
- Add manual registry image checks and client-owned cached results with per-container update indicators.
- Generate editable project-wide Compose update commands from discovery metadata and confirm the exact command before execution.
- Keep update progress on the Docker tab and refresh discovery after jobs complete.
- Apply the existing one-job-per-client limit to Docker operations and built-in checks.
- Support Docker commands on native or SSH execution targets with explicit command/context settings.

Update both orchestrator and clients to 0.2.0 for the new UI and API. Database additions are created automatically; existing actions and history are retained. Docker CLI/Compose/Buildx must be available on the execution target. No changes to host sudo privileges or host setup are performed by this release.
