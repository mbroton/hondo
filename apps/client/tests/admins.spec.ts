import { expect, test as base } from "@playwright/test"
import type { Admin, SlackMember } from "../src/lib/api/types"

const jane: Admin = {
  slack_user_id: "U1",
  name: "Jane Doe",
  avatar_url: null,
  status: "active",
  added_by: null,
  created_at: "2026-10-01T09:00:00Z",
}

const kim: Admin = {
  slack_user_id: "U2",
  name: "Kim Lee",
  avatar_url: null,
  status: "inactive",
  added_by: "Jane Doe",
  created_at: "2026-10-03T09:00:00Z",
}

const members: SlackMember[] = [
  {
    slack_user_id: "U1",
    name: "Jane Doe",
    real_name: "Jane Doe",
    handle: "jane",
    avatar_url: null,
  },
  {
    slack_user_id: "U3",
    name: "Sam",
    real_name: "Sam Rivera",
    handle: "srivera",
    avatar_url: null,
  },
]

type Server = {
  admins: Admin[]
  added: string[]
  removed: string[]
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = { admins: [jane, kim], added: [], removed: [] }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const request = route.request()
        const path = new URL(request.url()).pathname
        const method = request.method()
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
        if (path === "/api/workspaces/w1/providers") {
          return route.fulfill({ json: { providers: [], connections: [] } })
        }
        if (path === "/api/workspaces/w1/agent/model") {
          return route.fulfill({
            json: {
              ready: false,
              source: "unconfigured",
              connection_id: null,
              provider: null,
              model_name: null,
            },
          })
        }
        if (
          path === "/api/workspaces/w1/agent/mention-only" ||
          path === "/api/workspaces/w1/agent/listening"
        ) {
          return route.fulfill({ json: { channels: [] } })
        }
        if (path === "/api/workspaces/w1/mcp-channels") {
          return route.fulfill({ json: { channels: [], next_cursor: null } })
        }
        if (path === "/api/workspaces/w1/admins" && method === "GET") {
          return route.fulfill({ json: { admins: server.admins } })
        }
        if (path === "/api/workspaces/w1/admins" && method === "POST") {
          const { slack_user_id: id } = request.postDataJSON() as {
            slack_user_id: string
          }
          server.added.push(id)
          const member = members.find((item) => item.slack_user_id === id)
          if (member) {
            server.admins = [
              ...server.admins,
              {
                slack_user_id: member.slack_user_id,
                name: member.name,
                avatar_url: member.avatar_url,
                status: "active",
                added_by: "Jane Doe",
                created_at: "2026-10-08T09:00:00Z",
              },
            ]
          }
          return route.fulfill({ status: 204 })
        }
        if (
          path.startsWith("/api/workspaces/w1/admins/") &&
          method === "DELETE"
        ) {
          const id = path.split("/").pop() ?? ""
          server.removed.push(id)
          server.admins = server.admins.filter(
            (admin) => admin.slack_user_id !== id
          )
          return route.fulfill({ status: 204 })
        }
        if (path === "/api/workspaces/w1/slack-members") {
          return route.fulfill({ json: { members, next_cursor: null } })
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

test("lists the admins and shows who has no access", async ({
  page,
  server,
}) => {
  await page.goto("/settings")
  const list = page.getByRole("list", { name: "Admins" })
  await expect(list.getByText("Jane Doe", { exact: true })).toBeVisible()
  await expect(list.getByText("Kim Lee", { exact: true })).toBeVisible()
  await expect(list.getByText("No access")).toBeVisible()
  await expect(list.getByText(/^Added by Jane Doe/)).toBeVisible()
  expect(server.admins).toHaveLength(2)
})

test("adds a person from the Slack workspace", async ({ page, server }) => {
  await page.goto("/settings")
  await page.getByRole("button", { name: "Add admin" }).click()
  const person = page.getByRole("combobox", { name: "Person" })
  await person.fill("Jane")
  // People who are already admins are not offered again.
  await expect(page.getByText("No matching people.")).toBeVisible()
  // The search also matches the real name and the Slack username.
  await person.fill("srivera")
  await expect(page.getByRole("option", { name: /Sam Rivera/ })).toBeVisible()
  await person.fill("Rivera")
  await page.getByRole("option", { name: /Sam Rivera/ }).click()

  await expect(page.getByRole("dialog")).toBeHidden()
  expect(server.added).toEqual(["U3"])
  await expect(
    page.getByRole("list", { name: "Admins" }).getByText("Sam", { exact: true })
  ).toBeVisible()
})

test("asks before it removes an admin", async ({ page, server }) => {
  await page.goto("/settings")
  await page.getByRole("button", { name: "Remove Kim Lee" }).click()
  const dialog = page.getByRole("dialog")
  await expect(dialog.getByText("Remove Kim Lee as an admin?")).toBeVisible()
  expect(server.removed).toEqual([])

  await dialog.getByRole("button", { name: "Remove admin" }).click()

  await expect(dialog).toBeHidden()
  expect(server.removed).toEqual(["U2"])
  await expect(
    page.getByRole("list", { name: "Admins" }).getByText("Kim Lee")
  ).toBeHidden()
})

test("keeps the last admin", async ({ page, server }) => {
  server.admins = [jane]
  await page.goto("/settings")
  await expect(
    page.getByRole("button", { name: "Remove Jane Doe" })
  ).toBeDisabled()
  await expect(
    page.getByText("Add another admin before you remove this one.")
  ).toBeVisible()
})
