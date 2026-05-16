# Repository Instructions

## Git Policy

- If the user explicitly asks for `commit and push`, `提交并推送`, or an equivalent publish request, the agent may commit the scoped project changes and push the current branch without asking for another confirmation.
- Before committing, run `git status --short`, keep unrelated or pre-existing dirty files out of the commit, and include new files that belong to the requested change.
- Do not push for ordinary code changes unless the user explicitly requested commit-and-push or publish.
- Do not force push, delete branches, delete worktrees, or rewrite shared history unless the user explicitly confirms that exact operation.

## Installed Command Copy

- The live root-level command is `/usr/local/bin/clash-proxy`, which executes payloads under `/usr/local/lib/clash-proxy`.
- When changing `proxy.sh` or helper scripts used by `probe-stable-node`, `ai-use`, `guard`, `ai-connections`, or `incident`, verify whether the installed payload also needs to be refreshed.
- Prefer `scripts/install-system-commands.sh` for refreshing the installed payload so helper files stay in sync with the wrapper layout.
