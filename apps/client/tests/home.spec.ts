import { expect, test as base } from "@playwright/test"
import type { Setup } from "../src/lib/api/types"

const nothingDone: Setup = {
  provider_connected: false,
  model_selected: false,
  tool_connected: false,
  answered_in_channel: false,
  complete: false,
}

const allDone: Setup = {
  provider_connected: true,
  model_selected: true,
  tool_connected: true,
  answered_in_channel: true,
  complete: true,
}

type Server = {
  setup: Setup
  setupCalls: number
  status: string
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = {
      setup: nothingDone,
      setupCalls: 0,
      status: "active",
    }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const path = new URL(route.request().url()).pathname
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
                  status: server.status,
                },
              ],
            },
          })
        }
        if (path === "/api/workspaces/w1/setup") {
          server.setupCalls += 1
          return route.fulfill({ json: server.setup })
        }
        unexpected.push(path)
        await route.fulfill({ status: 404, json: { detail: "not_found" } })
      }
    )
    await provide(server)
    expect(unexpected).toEqual([])
    expect(pageErrors).toEqual([])
  },
})

test("shows what is done and offers the next step first", async ({
  page,
  server,
}) => {
  server.setup = { ...nothingDone, provider_connected: true }
  await page.goto("/")

  await expect(
    page.getByRole("heading", { name: "Set up Hondo" })
  ).toBeVisible()
  await expect(page.getByText("2 of 5 done")).toBeVisible()
  await expect(
    page.getByText(
      "Hondo answers in Acme team after you connect a provider and choose a model."
    )
  ).toBeVisible()
  const steps = page.getByRole("list", { name: "Setup steps" })
  await expect(steps.getByRole("listitem")).toHaveCount(5)
  await expect(
    steps.getByText("Connect an AI provider, done", { exact: true })
  ).toBeAttached()
  await expect(steps.getByRole("button", { name: "Add a key" })).toHaveCount(0)
  await expect(
    steps.getByRole("button", { name: "Choose a model" })
  ).toHaveAttribute("href", "/settings")
  await expect(
    steps.getByRole("button", { name: "Connect a tool" })
  ).toHaveAttribute("href", "/workspaces/w1/connectors")
  const slack = steps.getByRole("button", { name: "Open Slack" })
  await expect(slack).toHaveAttribute(
    "href",
    "https://app.slack.com/client/T01"
  )
  await expect(slack).toHaveAttribute("target", "_blank")
})

test("checks steps off on its own as Hondo records them", async ({
  page,
  server,
}) => {
  await page.clock.install()
  await page.goto("/")
  await expect(page.getByText("1 of 5 done")).toBeVisible()

  server.setup = allDone
  await page.clock.runFor(15_000)

  await expect(
    page.getByRole("heading", { name: "Hondo is ready" })
  ).toBeVisible()
  await expect(page.getByRole("list", { name: "Tips" })).toBeVisible()
  const calls = server.setupCalls
  // A complete setup is not asked for again.
  await page.clock.runFor(60_000)
  expect(server.setupCalls).toBe(calls)
})

test("reports a finished install", async ({ page, server }) => {
  await page.goto("/?installed=1")
  await expect(page.getByText("Hondo is installed in Slack.")).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Home", exact: true })
  ).toHaveAttribute("aria-current", "page")
  expect(server.setupCalls).toBeGreaterThan(0)
})

test("shows the workspace once, as context", async ({ page, server }) => {
  await page.goto("/")

  const header = page.locator("header")
  await expect(header.getByText("Home", { exact: true })).toBeVisible()
  await expect(header.getByText("Acme team")).toHaveCount(0)
  await expect(page.getByText("Acme team", { exact: true })).toBeVisible()
  await expect(page.getByText("active", { exact: true })).toHaveCount(0)

  server.status = "paused"
  await page.reload()
  await expect(page.getByText("paused", { exact: true })).toBeVisible()
})

test("explains that Hondo already works in another workspace", async ({
  page,
  server,
}) => {
  await page.goto("/?slack_error=other_workspace")
  await expect(
    page.getByText(
      "Hondo already works in a different Slack workspace. It can work in only one."
    )
  ).toBeVisible()
  expect(server.setupCalls).toBeGreaterThan(0)
})

test("asks for a reinstall first when the workspace is paused", async ({
  page,
  server,
}) => {
  server.status = "paused"
  server.setup = allDone
  await page.goto("/")

  await expect(
    page.getByRole("heading", { name: "Set up Hondo" })
  ).toBeVisible()
  await expect(page.getByText("4 of 5 done")).toBeVisible()
  const steps = page.getByRole("list", { name: "Setup steps" })
  await expect(
    steps.getByText("Add Hondo to Slack, to do", { exact: true })
  ).toBeAttached()
  await expect(
    steps.getByRole("button", { name: "Reinstall in Slack" })
  ).toBeVisible()
  await expect(page.getByText("Hondo is ready")).toHaveCount(0)
  await expect(page.getByRole("list", { name: "Tips" })).toHaveCount(0)
})

test("says Hondo can answer once it has a model", async ({ page, server }) => {
  server.setup = {
    ...nothingDone,
    provider_connected: true,
    model_selected: true,
  }
  await page.goto("/")

  await expect(page.getByText("3 of 5 done")).toBeVisible()
  await expect(
    page.getByText(/^Hondo can answer in Acme team now\./)
  ).toBeVisible()
})
