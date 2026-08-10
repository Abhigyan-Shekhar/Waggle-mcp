# Security policy

## Supported versions

Security fixes are provided for the latest released major version. Pin consumers to a reviewed full commit SHA and update deliberately.

## Threat model

Issue, pull-request, discussion, release, push, and generic event text is attacker-controlled data. The Action parses the event file with a bounded JSON parser and passes only paths and trusted metadata in subprocess argument arrays. It never evaluates event text, constructs a shell command from it, executes pull-request code, dumps the environment, or sends context to an external LLM or hosted Waggle service.

Do not use `pull_request_target` to execute or check out untrusted pull-request code. That trigger can expose a privileged base-repository context to attacker-controlled changes. Use ordinary read-only event workflows, keep `permissions: contents: read`, and pass generated context to later steps by file path.

The Action creates a unique temporary database, validates that outputs remain under `GITHUB_WORKSPACE`, uses deterministic local embeddings, and cleans up temporary state. Generated context and checkpoints can still contain non-secret repository content; treat uploaded artifacts according to your repository's data policy.

## Reporting a vulnerability

Privately report a suspected vulnerability through the security advisory feature of the eventual public Action repository. Include the affected commit, reproduction, impact, and whether any secret or artifact was exposed. Do not open a public issue for an undisclosed vulnerability. The owner should acknowledge the report, coordinate a fix and advisory, and rotate any credentials shown in a reproduction.

