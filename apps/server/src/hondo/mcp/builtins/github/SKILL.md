---
name: github-what-changed
description: Find what changed in GitHub repositories in a time window (merged pull requests, commits, Actions runs, releases), for example before an incident started.
---

# What changed in GitHub

Use this when someone asks what changed, what was merged or deployed, or what
could have caused a problem that started at a known time. Use the tools of the
GitHub connector.

## 1. Set the window and the repositories

- Take the start time from the request or the thread, for example the time an
  alert fired. Convert it to UTC. If nobody gives a time, use the last 24 hours
  and say so.
- Use the repositories that the request or the thread names, as `owner/repo`.
  If none is named, ask which one. Do not read every repository.

## 2. Merged pull requests

- `search_pull_requests` with the query
  `repo:OWNER/REPO is:merged merged:>=2026-10-19T14:00:00+00:00`
  (your window start). Add `merged:<=…` for an end time. Use
  `fields: ["number", "title", "user", "closed_at", "html_url"]`.
- For a pull request that looks related, `pull_request_read` with method `get`
  or `get_files`.

## 3. Commits

- `list_commits` with `since` (and `until`) in ISO 8601 UTC, and
  `fields: ["sha", "html_url", "commit"]`. It lists the default branch; pass a
  branch name as `sha` for another branch.
- Commits that were pushed without a pull request show only here.

## 4. Actions runs and deploys

- `actions_list` with method `list_workflow_runs`. It has no time filter. The
  newest runs come first, so read pages (`perPage` up to 100) until a run's
  `created_at` is before the window.
- Narrow it with `workflow_runs_filter`: `event` (for example `push` or
  `deployment`), `branch` or `status`. If the repository has a deploy workflow,
  pass its file name as `resource_id`; method `list_workflows` shows the names.
- For a failed run: `actions_get` with method `get_workflow_run`, then
  `get_job_logs` with `failed_only: true` and `tail_lines`.
- GitHub has no tool that lists deployments. A deploy shows as a workflow run
  or as a release: `list_releases`, `get_latest_release`, `list_tags`.

## 5. Answer

- Give a short timeline in UTC, newest first: the time, what changed (pull
  request, commit, run or release), who did it, and the link.
- Say which change most likely relates to the problem, and why.
- Say what you could not check, for example a repository that you cannot read.
