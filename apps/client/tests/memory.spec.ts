import { expect, test as base } from "@playwright/test"
import type { ThreadSummary } from "../src/lib/api/types"

const outage: ThreadSummary = {
  id: 3,
  private: false,
  channel: "incidents",
  url: "https://slack.com/archives/C01/p1700000000000100",
  text: "The payments API returned 502 errors after the deploy. The team rolled back.",
  updated_at: "2026-11-10T09:00:00Z",
}

const hidden: ThreadSummary = {
  id: 2,
  private: true,
  channel: null,
  url: null,
  text: null,
  updated_at: "2026-11-09T09:00:00Z",
}

const lunch: ThreadSummary = {
  id: 1,
  private: false,
  channel: "random",
  url: "https://slack.com/archives/C02/p1700000100000100",
  text: "People picked a place for the Friday lunch.",
  updated_at: "2026-11-08T09:00:00Z",
}

type Server = {
  summaries: ThreadSummary[]
  pageSize: number
  searches: string[]
  deletes: number[]
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = {
      summaries: [],
      pageSize: 25,
      searches: [],
      deletes: [],
    }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const request = route.request()
        const method = request.method()
        const url = new URL(request.url())
        const path = url.pathname
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
        if (
          path === "/api/workspaces/w1/thread-summaries" &&
          method === "GET"
        ) {
          const words = (url.searchParams.get("q") ?? "")
            .toLowerCase()
            .split(/\s+/)
            .filter(Boolean)
          if (words.length) {
            server.searches.push(url.searchParams.get("q") ?? "")
            return route.fulfill({
              json: {
                summaries: server.summaries.filter(
                  (item) =>
                    item.text !== null &&
                    words.some((word) =>
                      item.text!.toLowerCase().includes(word)
                    )
                ),
                next_offset: null,
              },
            })
          }
          const offset = Number(url.searchParams.get("offset") ?? 0)
          const end = offset + server.pageSize
          return route.fulfill({
            json: {
              summaries: server.summaries.slice(offset, end),
              next_offset: end < server.summaries.length ? end : null,
            },
          })
        }
        const deleted = path.match(
          /^\/api\/workspaces\/w1\/thread-summaries\/(\d+)$/
        )?.[1]
        if (deleted && method === "DELETE") {
          server.deletes.push(Number(deleted))
          server.summaries = server.summaries.filter(
            (item) => item.id !== Number(deleted)
          )
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

test("lists summaries and hides the text of private channels", async ({
  page,
  server,
}) => {
  server.summaries = [outage, hidden]
  await page.goto("/memory")
  await expect(page).toHaveURL("/workspaces/w1/memory")
  await expect(
    page.getByRole("link", { name: "Memory", exact: true })
  ).toHaveAttribute("aria-current", "page")
  const items = page
    .getByRole("list", { name: "Thread summaries" })
    .getByRole("listitem")
  await expect(items).toHaveCount(2)
  await expect(items.nth(0)).toContainText(
    "The payments API returned 502 errors after the deploy. The team rolled back."
  )
  await expect(items.nth(0).getByRole("link")).toHaveAttribute(
    "href",
    "https://slack.com/archives/C01/p1700000000000100"
  )
  await expect(items.nth(0).getByRole("link")).toContainText("#incidents")
  await expect(items.nth(1)).toContainText("Private channel")
  await expect(items.nth(1).getByRole("link")).toHaveCount(0)
  await expect(
    items.nth(1).getByRole("button", {
      name: "Delete summary from Private channel",
    })
  ).toBeVisible()
})

test("shows that there are no summaries yet", async ({ page, server }) => {
  server.summaries = []
  await page.goto("/workspaces/w1/memory")
  await expect(
    page.getByText("No summaries yet", { exact: true })
  ).toBeVisible()
})

test("searches the summaries", async ({ page, server }) => {
  server.summaries = [outage, hidden, lunch]
  await page.goto("/workspaces/w1/memory")
  const items = page
    .getByRole("list", { name: "Thread summaries" })
    .getByRole("listitem")
  await expect(items).toHaveCount(3)
  const search = page.getByRole("searchbox", { name: "Search summaries" })
  await search.fill("  lunch ")
  await search.press("Enter")
  await expect(items).toHaveCount(1)
  await expect(items.first()).toContainText("#random")
  expect(server.searches).toEqual(["lunch"])

  await search.fill("kubernetes")
  await search.press("Enter")
  await expect(
    page.getByText("No summaries match your search", { exact: true })
  ).toBeVisible()
  await page.getByRole("button", { name: "Clear search", exact: true }).click()
  await expect(search).toHaveValue("")
  await expect(search).toBeFocused()
  await expect(items).toHaveCount(3)
})

test("deletes a summary after a confirm step", async ({ page, server }) => {
  server.summaries = [outage, hidden]
  await page.goto("/workspaces/w1/memory")
  await page
    .getByRole("button", { name: "Delete summary from #incidents" })
    .click()
  const dialog = page.getByRole("dialog", { name: "Delete this summary?" })
  await expect(dialog).toContainText(
    "A later run in this thread can write a new summary."
  )
  await dialog.getByRole("button", { name: "Cancel", exact: true }).click()
  await expect(dialog).toBeHidden()
  expect(server.deletes).toEqual([])

  await page
    .getByRole("button", { name: "Delete summary from #incidents" })
    .click()
  await dialog
    .getByRole("button", { name: "Delete summary", exact: true })
    .click()
  await expect(
    page.getByText("Summary deleted.", { exact: true })
  ).toBeVisible()
  await expect(dialog).toBeHidden()
  expect(server.deletes).toEqual([3])
  const items = page
    .getByRole("list", { name: "Thread summaries" })
    .getByRole("listitem")
  await expect(items).toHaveCount(1)
  await expect(items.first()).toContainText("Private channel")
})

test("shows more summaries page by page", async ({ page, server }) => {
  server.summaries = [outage, hidden, lunch]
  server.pageSize = 2
  await page.goto("/workspaces/w1/memory")
  const items = page
    .getByRole("list", { name: "Thread summaries" })
    .getByRole("listitem")
  await expect(items).toHaveCount(2)
  await page.getByRole("button", { name: "Show more", exact: true }).click()
  await expect(items).toHaveCount(3)
  await expect(items.nth(2)).toContainText("#random")
  await expect(
    page.getByRole("button", { name: "Show more", exact: true })
  ).toBeHidden()
})
