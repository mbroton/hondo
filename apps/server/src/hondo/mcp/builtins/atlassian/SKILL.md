---
name: atlassian-what-changed
description: Find what changed in Bitbucket Cloud repositories in a time window (merged pull requests, commits, pipelines, deployments), for example before an incident started.
---

# What changed in Bitbucket

Use this when someone asks what changed, what was merged or deployed, or what
could have caused a problem that started at a known time. Use the tools of the
Atlassian connector.

Bitbucket operations are not separate tools: run each one with the
`executeRead` tool, as `{"name": "<operation>", "inputs": {...}}`. Bitbucket
operations need no `cloudId`. If an operation fails with an input error, call
`discover` with what you want to do (for example "list bitbucket pipelines") to
get its current inputs.

In every operation below, `workspaceId` is the workspace slug and `repoId` is
the repository slug, as in `bitbucket.org/WORKSPACE/REPO`.

## 1. Set the window and the repositories

- Take the start time from the request or the thread, for example the time an
  alert fired. Convert it to UTC. If nobody gives a time, use the last 24 hours
  and say so.
- Use the repositories that the request or the thread names. If none is named,
  ask which one. Do not read every repository.

## 2. Merged pull requests

- `listBitbucketRepoPullRequests` with `state: "MERGED"` and the filter
  `q: "updated_on >= 2026-10-19T14:00:00+00:00"` (your window start).
- `updated_on` is the last change, not the merge time, so a later comment can
  also match. If the exact time matters, read the merge commit with
  `getBitbucketRepoCommit`.

## 3. Commits

- `listBitbucketRepoCommits`. It has no time filter. The newest commits come
  first, so read pages until a commit's `date` is before the window. Pass a
  branch name as `include` for a branch other than the main branch.

## 4. Pipelines and deployments

- `listBitbucketRepoPipelines` with `sort: "-created_on"` (newest first). Read
  until `created_on` is before the window. Narrow it with `targetBranch` or
  `status`.
- For a failed pipeline: `getBitbucketRepoPipeline`, then
  `listBitbucketRepoPipelineSteps`. To read a step's log, call `discover` with
  "bitbucket pipeline step log".
- `listBitbucketRepoDeployments`, and check each deployment's time.
  `listBitbucketRepoEnvironments` gives the environment UUIDs for its
  `environment` filter.

## 5. Answer

- Give a short timeline in UTC, newest first: the time, what changed (pull
  request, commit, pipeline or deployment), who did it, and the link.
- Say which change most likely relates to the problem, and why.
- Say what you could not check, for example a repository that you cannot read.
