# Security Policy

## Supported versions

The latest stable major/minor release receives security fixes.

## Reporting a vulnerability

Do not open a public issue for credentials, command-injection bugs, sandbox escapes, or destructive-operation bypasses. Use GitHub's private vulnerability reporting feature when enabled for the repository.

## Security boundaries

OrchBridge launches local provider CLIs and may allow them to modify the current repository. The public default is `guarded`. `trusted` is an explicit opt-in for broader reversible local development actions.

The project intentionally keeps destructive Git actions, force-push, credential mutation, automatic merge/push, and high-risk external side effects behind explicit gates.
