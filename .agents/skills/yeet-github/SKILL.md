---
name: yeet-github
description: Publish the current repository work in a draft GitHub pull request with gh.
---

# Yeet GitHub

Run from the repository root. Publish the current work without merging the pull request. Use `$yolo` when the user explicitly requests publication, squash merge, and cleanup as one workflow.

## 1. Verify context

1. Read all repository instructions, including `AGENTS.md` and `CONTRIBUTING.md` when present.
2. Read `docs/product-specification.md` when present and map the objective to relevant tickets and acceptance criteria without expanding scope.
3. Summarize the objective and identify only the changes that belong to it.
4. Inspect `git status --short --branch`, `git diff --check`, `git diff`, `git diff --stat`, and relevant untracked files.
5. Verify `gh --version`, `gh auth status`, `git remote -v`, and the default branch with `gh repo view --json defaultBranchRef,nameWithOwner`.
6. Require an authenticated `gh` session and an `origin` remote matching the expected GitHub repository. Stop before mutation if a prerequisite is missing.
7. Preserve unrelated changes. Stop before staging if inclusion is ambiguous.

Read the installed command help before relying on version-sensitive options:

```bash
gh pr create --help
gh pr view --help
gh pr merge --help
```

## 2. Select branches

1. Capture the target branch from `defaultBranchRef`.
2. If the current branch is the target, create a short `<type>/<issue>-<subject>` branch using the repository's allowed Conventional Commit types. Never include `codex`, an agent name, or an automation-tool name in the branch name.
3. Keep an existing work branch only when it clearly matches the request.
4. Capture exact source and target names. Reject an empty, identical, protected, or unexpected source branch.
5. Check that proposed local and remote branches do not already exist in an ambiguous state.

## 3. Validate and commit

1. Run the canonical checks in `AGENTS.md` that apply to the changed domain. Never invent substitute commands.
2. Review the final diff and run `git diff --check`.
3. Present the branch, changed files, concise diff summary, checks, and known limitations; obtain explicit approval before any push or pull-request publication.
4. After approval, stage only in-scope files. Run `git diff --cached --check` and inspect `git diff --cached --stat`.
5. Commit using the repository convention, defaulting to an English Conventional Commit. Include a ticket reference only when one exists.
6. Never bypass hooks. Capture the full SHA with `git rev-parse HEAD` and require a clean worktree apart from explicitly ignored local files.

Never use `git clean`, `git reset --hard`, `git checkout --`, or hook bypasses.

## 4. Push

1. Push the captured source branch explicitly with `git push -u origin <source-branch>`.
2. Stop on non-fast-forward. Never rebase, resolve conflicts, or force-push without explicit authorization.
3. Verify that the remote source SHA equals the captured local SHA.

## 5. Open the pull request

1. Use `gh pr list --head <source-branch> --state all` to ensure no pull request already exists.
2. Write an English title and body covering objective, tickets and acceptance criteria, changes, technical decisions, impacts, tests, documentation, limitations, both storage profiles, and references.
3. Create a draft pull request with `gh pr create --draft --head <source-branch> --base <target-branch> --title '<title>' --body-file <temporary-file-outside-repository>`.
4. Remove the temporary file after the command and never track it.
5. Verify number, URL, draft state, branches, SHA, and state with `gh pr view`.
6. If creation has an ambiguous result, look for an existing pull request immediately and do not retry until side effects are known.

## Result

Report the pull-request URL, number, and state; source and target branches; pushed SHA; checks and results; remaining limitations; and final `git status --short --branch`.

Never merge the pull request, delete a branch, or modify GitHub protections in this skill.
