---
name: yolo
description: Publish the current Codex work in a GitHub pull request, squash-merge it, then clean up its local and remote branches without additional approval prompts. Use only when the user explicitly invokes `$yolo` or requests the full publish, merge, and cleanup sequence.
---

# Publish, merge, and clean up

Run each phase in order from the repository root. The implicit pull request is the one for the current Codex work and current branch. Treat the explicit `$yolo` invocation as advance approval to push, create or ready the pull request, squash-merge it, and delete its exact source branch. Do not pause for additional confirmation during a successful, unambiguous run.

## 1. Publish

1. Read all repository instructions and the product specification referenced by `AGENTS.md`.
2. Load and follow `$yeet-github` in full to inspect, validate, commit, push, and open a draft pull request, except do not request its approval before pushing or publishing: the explicit `$yolo` invocation already provides that approval.
3. Retain the exact source and target branches, pushed SHA, pull-request number, and URL.
4. Stop after any failure or ambiguous external result. Never merge or clean up after a partial failure.

## 2. Merge

1. Explicit `$yolo` invocation authorizes merging only the pull request for the current work. It never authorizes bypassing protections or merging another pull request.
2. Verify with `gh pr view` that the pull request exactly matches the captured source branch and SHA. Reject ambiguity or a changed head.
3. Inspect required checks, reviews, conversations, and protections with `gh pr checks` and `gh pr view --json mergeStateStatus,reviewDecision,statusCheckRollup`.
4. Mark a ready draft with `gh pr ready <number>`.
5. Watch pending checks with `gh pr checks <number> --watch --interval 10`; stop and report any failing check.
6. Present the verified final state as a progress update, then continue without requesting approval.
7. Use `gh pr merge <number> --squash --delete-branch --match-head-commit <source-sha>`.
8. If GitHub queues the merge, keep monitoring. Clean up only after `gh pr view <number> --json state,mergedAt,mergeCommit` reports `MERGED`.

## 3. Clean up

1. Reconfirm the merged state and capture the merge commit SHA.
2. Refuse cleanup if new tracked, staged, or untracked work appeared after the source commit.
3. Run `git fetch --prune origin`.
4. Switch to the target branch and update it only by fast-forward with `git switch <target-branch>` and `git pull --ff-only origin <target-branch>`.
5. Delete the exact local source branch if it remains. `git branch -D <source-branch>` is allowed only after verified squash merge.
6. If the exact unprotected remote source branch remains, delete it with `git push origin --delete <source-branch>` without requesting additional approval.
7. Run `git status --short --branch` and report the final state.

## Guardrails

- Never use `git clean`, `git reset --hard`, or recursive deletion.
- Never delete the target branch, a protected branch, or a branch whose exact name was not captured before merge.
- Never clean another worktree.
- Never confuse green checks, approval, or mergeability with an actual merge.
- Never extend this authorization to a pull request unrelated to the current work.

## Result

Report the pull-request URL and state, merged SHA, deleted branches, current branch, and final `git status`.
