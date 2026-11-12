import { expect, test as base } from "@playwright/test"
import type { Skill, SkillUpdate } from "../src/lib/api/types"

const content = `---
name: incident-report
description: Write an incident report from a Slack thread.
---

1. Read the whole thread.
2. List the impact, the cause and the fix.
`

const draft: Skill = {
  id: "s1",
  name: "incident-report",
  description: "Write an incident report from a Slack thread.",
  published: false,
  all_channels: false,
  channels: [],
  source_url: null,
  revision: 1,
  updated_at: "2026-10-14T09:00:00Z",
  content,
}

type Server = {
  skills: Skill[]
  creates: unknown[]
  updates: unknown[]
  deletes: string[]
  createError: string | null
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = {
      skills: [],
      creates: [],
      updates: [],
      deletes: [],
      createError: null,
    }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const request = route.request()
        const method = request.method()
        const path = new URL(request.url()).pathname
        const skillId = path.match(
          /^\/api\/workspaces\/w1\/skills\/([^/]+)$/
        )?.[1]
        const skill = server.skills.find((item) => item.id === skillId)
        if (path === "/api/me") {
          return route.fulfill({
            json: {
              user: {
                id: "u1",
                email: "jane@example.com",
                display_name: "Jane Doe",
                avatar_url: null,
              },
              team: {
                id: "T01",
                name: "Acme team",
                workspace_id: "w1",
                is_admin: true,
                admins: [],
                slack: "ok",
              },
            },
          })
        }
        if (path === "/api/workspaces") {
          return route.fulfill({
            json: {
              workspaces: [
                {
                  id: "w1",
                  slack_team_id: "T01",
                  name: "Acme team",
                  status: "active",
                },
              ],
            },
          })
        }
        if (path === "/api/workspaces/w1/mcp-channels") {
          return route.fulfill({
            json: {
              channels: [
                { id: "C01", name: "engineering" },
                { id: "C02", name: "support" },
              ],
              next_cursor: null,
            },
          })
        }
        if (path === "/api/workspaces/w1/skills" && method === "GET") {
          return route.fulfill({
            json: {
              skills: server.skills.map((item) => ({
                ...item,
                content: undefined,
              })),
            },
          })
        }
        if (path === "/api/workspaces/w1/skills" && method === "POST") {
          server.creates.push(request.postDataJSON())
          if (server.createError) {
            return route.fulfill({
              status: 422,
              json: { detail: server.createError },
            })
          }
          server.skills.push(structuredClone(draft))
          return route.fulfill({ status: 201, json: draft })
        }
        if (skillId && !skill) {
          return route.fulfill({
            status: 404,
            json: { detail: "skill_not_found" },
          })
        }
        if (skill && method === "GET") {
          return route.fulfill({ json: skill })
        }
        if (skill && method === "PUT") {
          const body: SkillUpdate = request.postDataJSON()
          server.updates.push(body)
          if (body.revision !== skill.revision) {
            return route.fulfill({
              status: 409,
              json: { detail: "skill_changed" },
            })
          }
          Object.assign(skill, body, { revision: skill.revision + 1 })
          return route.fulfill({ json: skill })
        }
        if (skill && method === "DELETE") {
          server.deletes.push(skill.id)
          server.skills = server.skills.filter((item) => item !== skill)
          return route.fulfill({ status: 204 })
        }
        unexpected.push(`${method} ${path}`)
        await route.fulfill({ status: 404, json: { detail: "not_found" } })
      }
    )
    await provide(server)
    expect(unexpected).toEqual([])
    expect(pageErrors).toEqual([])
  },
})

test("adds a skill from a pasted SKILL.md file", async ({ page, server }) => {
  await page.goto("/workspaces/w1/skills")
  await expect(page.getByText("No skills yet", { exact: true })).toBeVisible()
  await expect(
    page.getByText(
      "Add a skill to give Hondo the steps for a task. You can also ask Hondo to draft one from a Slack thread."
    )
  ).toBeVisible()
  await page.getByRole("button", { name: "Add skill", exact: true }).click()
  await expect(page).toHaveURL("/workspaces/w1/skills/new")
  await page.getByRole("textbox", { name: "SKILL.md" }).fill(content)
  await page.getByRole("button", { name: "Add skill", exact: true }).click()
  await expect(page).toHaveURL("/workspaces/w1/skills/s1")
  await expect(
    page.getByRole("heading", { name: "incident-report", exact: true })
  ).toBeVisible()
  await expect(page.getByRole("textbox", { name: "SKILL.md" })).toHaveValue(
    content
  )
  await expect(
    page.getByRole("switch", { name: "Publish skill" })
  ).not.toBeChecked()
  expect(server.creates).toEqual([{ content }])
})

test("lists skills with their state and channels", async ({ page, server }) => {
  server.skills = [
    {
      ...structuredClone(draft),
      id: "s2",
      name: "deploy-checklist",
      description: "Check a release before it goes out.",
      published: true,
      all_channels: true,
    },
    { ...structuredClone(draft), channels: ["C01", "C02"] },
  ]
  await page.goto("/workspaces/w1/skills")
  await expect(
    page.getByRole("link", { name: "Skills", exact: true })
  ).toHaveAttribute("aria-current", "page")
  const table = page.getByRole("table", { name: "Skills" })
  const published = table
    .getByRole("row")
    .filter({ hasText: "deploy-checklist" })
  await expect(published).toContainText("Check a release before it goes out.")
  await expect(published).toContainText("Published")
  await expect(published).toContainText("All channels")
  const pending = table.getByRole("row").filter({ hasText: "incident-report" })
  await expect(pending).toContainText("Draft")
  await expect(pending).toContainText("2 channels")
  await pending.getByRole("link").click()
  await expect(page).toHaveURL("/workspaces/w1/skills/s1")
})

test("publishes a skill in the selected channels", async ({ page, server }) => {
  server.skills = [structuredClone(draft)]
  await page.goto("/workspaces/w1/skills/s1")
  await expect(
    page.getByRole("link", { name: "Drafted from this thread" })
  ).toHaveCount(0)
  await page.getByRole("checkbox", { name: "#support" }).check()
  await page.getByRole("switch", { name: "Publish skill" }).check()
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeHidden()
  await expect(page.getByText("Skill saved.", { exact: true })).toBeVisible()
  expect(server.updates).toEqual([
    {
      revision: 1,
      content,
      published: true,
      all_channels: false,
      channels: ["C02"],
    },
  ])
  await expect(
    page.getByRole("switch", { name: "Publish skill" })
  ).toBeChecked()
  await expect(page.getByRole("checkbox", { name: "#support" })).toBeChecked()
  await expect(
    page.getByText("This skill changed. Your edits are still here.")
  ).toBeHidden()
})

test("does not publish a skill without channels", async ({ page, server }) => {
  server.skills = [structuredClone(draft)]
  await page.goto("/workspaces/w1/skills/s1")
  await page.getByRole("switch", { name: "Publish skill" }).check()
  await expect(
    page.getByText("Select at least one channel to publish this skill.")
  ).toBeVisible()
  const save = page.getByRole("button", { name: "Save changes", exact: true })
  await expect(save).toBeDisabled()
  await page.getByRole("checkbox", { name: "All Slack channels" }).check()
  await expect(save).toBeEnabled()
  await save.click()
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeHidden()
  expect(server.updates).toEqual([
    {
      revision: 1,
      content,
      published: true,
      all_channels: true,
      channels: [],
    },
  ])
})

test("explains the name rules when the server refuses the name", async ({
  page,
  server,
}) => {
  server.createError = "invalid_skill_name"
  const file = `---
name: Incident Report
description: Write an incident report from a Slack thread.
---

1. Read the whole thread.
`
  await page.goto("/workspaces/w1/skills/new")
  const editor = page.getByRole("textbox", { name: "SKILL.md" })
  await editor.fill(file)
  await page.getByRole("button", { name: "Add skill", exact: true }).click()
  await expect(page.getByRole("alert")).toHaveText(
    "Add a name to the frontmatter. Use only lowercase letters, numbers and single hyphens, with no hyphen at the start or end. Use at most 64 characters."
  )
  await expect(editor).toHaveValue(file)
  await expect(page).toHaveURL("/workspaces/w1/skills/new")
  expect(server.creates).toEqual([{ content: file }])
})

test("keeps the edited text when another admin saved first", async ({
  page,
  server,
}) => {
  server.skills = [structuredClone(draft)]
  await page.goto("/workspaces/w1/skills/s1")
  const editor = page.getByRole("textbox", { name: "SKILL.md" })
  const edited = `${content}3. Post the report in the thread.\n`
  await editor.fill(edited)
  const theirs = `---
name: incident-report
description: Write an incident report from a Slack thread.
---

1. Ask the on-call engineer for a summary.
`
  server.skills = [{ ...structuredClone(draft), content: theirs, revision: 2 }]
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect(
    page
      .getByRole("alert")
      .filter({ hasText: "This skill changed after you opened it." })
  ).toHaveText(
    "This skill changed after you opened it. Your changes are still here. Copy them, then discard them to load the latest version."
  )
  await expect(
    page.getByText("This skill changed. Your edits are still here.")
  ).toBeVisible()
  await expect(editor).toHaveValue(edited)
  expect(server.updates).toEqual([
    {
      revision: 1,
      content: edited,
      published: false,
      all_channels: false,
      channels: [],
    },
  ])
  await page
    .getByRole("button", { name: "Discard changes", exact: true })
    .first()
    .click()
  await expect(editor).toHaveValue(theirs)
  await expect(
    page.getByText("This skill changed. Your edits are still here.")
  ).toBeHidden()
})

test("deletes a skill after confirmation", async ({ page, server }) => {
  server.skills = [structuredClone(draft)]
  await page.goto("/workspaces/w1/skills/s1")
  const question = page.getByText(
    "Delete this skill? Hondo stops using it in all channels."
  )
  await page.getByRole("button", { name: "Delete skill" }).click()
  await expect(question).toBeVisible()
  await page.getByRole("button", { name: "Cancel", exact: true }).click()
  await expect(question).toBeHidden()
  await page.getByRole("button", { name: "Delete skill" }).click()
  await page.getByRole("button", { name: "Confirm deletion" }).click()
  await expect(page).toHaveURL("/workspaces/w1/skills")
  await expect(page.getByText("No skills yet", { exact: true })).toBeVisible()
  await expect(page.getByText("Skill deleted.", { exact: true })).toBeVisible()
  expect(server.deletes).toEqual(["s1"])
})

test("links a draft to the thread it came from", async ({ page, server }) => {
  server.skills = [
    {
      ...structuredClone(draft),
      source_url: "https://slack.com/archives/C01/p1760432400000100",
    },
  ]
  await page.goto("/workspaces/w1/skills/s1")
  const link = page.getByRole("link", { name: "Drafted from this thread" })
  await expect(link).toHaveAttribute(
    "href",
    "https://slack.com/archives/C01/p1760432400000100"
  )
  await expect(link).toHaveAttribute("target", "_blank")
})

test("shows a missing skill as not found", async ({ page, server }) => {
  server.skills = []
  await page.goto("/workspaces/w1/skills/s9")
  await expect(page.getByText("Skill not found", { exact: true })).toBeVisible()
  await expect(
    page.getByText("This skill no longer exists.", { exact: true })
  ).toBeVisible()
})
