import { expect, test as base, type Page } from "@playwright/test"
import type { HTTPTool, MCPConnection, MCPPolicy } from "../src/lib/api/types"

const searchLogs = {
  name: "search_logs",
  description: "Search the logs of one service.",
  input_schema: { type: "object" },
  enabled: false,
  read_only: false,
  ask: false,
  request: {
    method: "GET",
    url: "https://logs.example.com/search?service={service}",
    body: null,
    params: [{ name: "service", description: "The service name" }],
  },
}

const empty: MCPConnection = {
  id: "h1",
  name: "Logs API",
  url: "https://logs.example.com",
  authorized_by: "u1",
  authorized_by_name: "Jane Doe",
  connected: true,
  enabled: false,
  all_channels: false,
  channels: [],
  tools: [],
  status: "ready",
  review_required: false,
  revision: 1,
  auth: "key",
  header_names: ["Authorization"],
  builtin: null,
  kind: "http",
}

type Server = {
  connections: MCPConnection[]
  creates: unknown[]
  tools: { method: string; path: string; body: HTTPTool | null }[]
  tests: unknown[]
  updates: MCPPolicy[]
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = {
      connections: [],
      creates: [],
      tools: [],
      tests: [],
      updates: [],
    }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    function connection() {
      return server.connections[0]
    }
    function replace(next: MCPConnection) {
      server.connections = [next]
      return next
    }
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const request = route.request()
        const method = request.method()
        const path = new URL(request.url()).pathname
        const prefix = "/api/workspaces/w1/mcp-connections"
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
        if (path.endsWith("/providers"))
          return route.fulfill({ json: { providers: [], connections: [] } })
        if (path.endsWith("/agent/model"))
          return route.fulfill({
            json: {
              ready: false,
              source: "unconfigured",
              connection_id: null,
              provider: null,
              model_name: null,
            },
          })
        if (path === "/api/workspaces/w1/builtin-connectors")
          return route.fulfill({ json: { connectors: [] } })
        if (path === "/api/workspaces/w1/mcp-channels")
          return route.fulfill({
            json: {
              channels: [{ id: "C01", name: "engineering" }],
              next_cursor: null,
            },
          })
        if (path === prefix && method === "GET")
          return route.fulfill({ json: { connections: server.connections } })
        if (path === `${prefix}/http` && method === "POST") {
          server.creates.push(request.postDataJSON())
          return route.fulfill({
            status: 201,
            json: replace(structuredClone(empty)),
          })
        }
        if (path === `${prefix}/h1/http-tools/search_logs/test`) {
          server.tests.push(request.postDataJSON())
          return route.fulfill({
            json: { status: 200, body: '{\n  "lines": 3\n}' },
          })
        }
        if (path.startsWith(`${prefix}/h1/http-tools`)) {
          const body = method === "DELETE" ? null : request.postDataJSON()
          server.tools.push({ method, path, body })
          const current = connection()
          const tools =
            method === "DELETE"
              ? []
              : [
                  {
                    ...searchLogs,
                    name: body.name,
                    description: body.description,
                    request: {
                      method: body.method,
                      url: body.url,
                      body: body.body,
                      params: body.params,
                    },
                  },
                ]
          return route.fulfill({
            status: method === "POST" ? 201 : 200,
            json: replace({
              ...current,
              tools,
              revision: current.revision + 1,
            }),
          })
        }
        if (path === `${prefix}/h1` && method === "PUT") {
          const policy = request.postDataJSON() as MCPPolicy
          server.updates.push(policy)
          const current = connection()
          return route.fulfill({
            json: replace({
              ...current,
              enabled: policy.enabled,
              channels: policy.channels,
              tools: current.tools.map((tool) => ({
                ...tool,
                enabled: policy.allowed_tools.includes(tool.name),
                ask: policy.ask_tools.includes(tool.name),
              })),
              revision: current.revision + 1,
            }),
          })
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

async function openTool(page: Page) {
  await page.goto("/workspaces/w1/connectors/h1/tools/search_logs")
  await expect(
    page.getByRole("heading", { name: "search_logs", exact: true })
  ).toBeVisible()
}

test("adds an HTTP API and its first tool", async ({ page, server }) => {
  await page.goto("/workspaces/w1/connectors/new")
  await page.getByRole("link", { name: /HTTP API/ }).click()
  await page.getByRole("textbox", { name: "Name" }).fill("Logs API")
  await page
    .getByRole("textbox", { name: "API URL" })
    .fill("https://logs.example.com")
  await page.getByRole("button", { name: "Add header", exact: true }).click()
  await page
    .getByRole("textbox", { name: "Header 1 name" })
    .fill("Authorization")
  await page
    .getByRole("textbox", { name: "Header 1 value" })
    .fill("Bearer logs-key")
  await page.getByRole("button", { name: "Add connector", exact: true }).click()

  await expect(page).toHaveURL("/workspaces/w1/connectors/h1/setup")
  expect(server.creates).toEqual([
    {
      name: "Logs API",
      url: "https://logs.example.com",
      headers: [{ name: "Authorization", value: "Bearer logs-key" }],
    },
  ])
  await page.getByRole("button", { name: "Add tool", exact: true }).click()
  await expect(page.getByRole("heading", { name: "Add tool" })).toBeVisible()
  await expect(page.getByRole("textbox", { name: "URL" })).toHaveValue(
    "https://logs.example.com/"
  )
  await page.getByRole("textbox", { name: "Name" }).fill("search_logs")
  await page
    .getByRole("textbox", { name: "Description" })
    .fill("Search the logs of one service.")
  await page
    .getByRole("textbox", { name: "URL" })
    .fill("https://logs.example.com/search?service={service}")
  const save = page.getByRole("button", { name: "Save tool", exact: true })
  await expect(save).toBeDisabled()
  await page.getByRole("textbox", { name: "service" }).fill("The service name")
  await save.click()

  await expect(page).toHaveURL("/workspaces/w1/connectors/h1/tools/search_logs")
  expect(server.tools).toEqual([
    {
      method: "POST",
      path: "/api/workspaces/w1/mcp-connections/h1/http-tools",
      body: {
        name: "search_logs",
        description: "Search the logs of one service.",
        method: "GET",
        url: "https://logs.example.com/search?service={service}",
        body: null,
        params: [{ name: "service", description: "The service name" }],
      },
    },
  ])
  await expect(
    page.getByText("Hondo can use this tool only after an admin allows it")
  ).toBeVisible()
  await expect(page.locator("input[type=password]")).toHaveCount(0)
})

test("allows a new tool with ask before running on", async ({
  page,
  server,
}) => {
  server.connections = [{ ...empty, tools: [searchLogs] }]
  await page.goto("/workspaces/w1/connectors/h1/setup")
  const table = page.getByRole("table", { name: "Connector tools" })
  await table.getByRole("checkbox", { name: "search_logs" }).check()
  await expect(
    table.getByRole("switch", { name: "Ask before running search_logs" })
  ).toBeChecked()
  await page.getByRole("checkbox", { name: "#engineering" }).check()
  await page.getByRole("button", { name: "Turn on", exact: true }).click()
  await expect(page).toHaveURL("/workspaces/w1/connectors/h1")
  expect(server.updates).toEqual([
    {
      name: "Logs API",
      revision: 1,
      enabled: true,
      all_channels: false,
      channels: ["C01"],
      allowed_tools: ["search_logs"],
      ask_tools: ["search_logs"],
      review_tools: true,
    },
  ])
})

test("sends a test request with sample values", async ({ page, server }) => {
  server.connections = [{ ...empty, tools: [searchLogs] }]
  await openTool(page)
  const card = page.getByText("Send the saved request once").locator("../..")
  const send = card.getByRole("button", { name: "Send test request" })
  await expect(send).toBeDisabled()
  await card.getByRole("textbox", { name: "service" }).fill("payments")
  await send.click()
  const result = page.getByRole("region", { name: "Test result" })
  await expect(result.getByText("Status 200")).toBeVisible()
  await expect(result.getByText('"lines": 3')).toBeVisible()
  expect(server.tests).toEqual([{ arguments: { service: "payments" } }])

  await page.getByRole("textbox", { name: "Description" }).fill("Search logs.")
  await expect(send).toBeDisabled()
  await expect(page.getByText("Save your changes to test them.")).toBeVisible()
})

test("edits and removes a tool", async ({ page, server }) => {
  server.connections = [
    { ...empty, enabled: true, channels: ["C01"], tools: [searchLogs] },
  ]
  await page.goto("/workspaces/w1/connectors/h1?tab=tools")
  await page.getByRole("button", { name: "Edit search_logs" }).click()
  await expect(
    page.getByRole("heading", { name: "search_logs", exact: true })
  ).toBeVisible()
  await page.getByRole("button", { name: "POST", exact: true }).click()
  await page.getByRole("textbox", { name: "Body" }).fill('{"text": "{text}"}')
  await page.getByRole("textbox", { name: "text", exact: true }).fill("Text")
  await page.getByRole("button", { name: "Save tool", exact: true }).click()
  await expect.poll(() => server.tools.length).toBe(1)
  expect(server.tools[0]).toEqual({
    method: "PUT",
    path: "/api/workspaces/w1/mcp-connections/h1/http-tools/search_logs",
    body: {
      name: "search_logs",
      description: "Search the logs of one service.",
      method: "POST",
      url: "https://logs.example.com/search?service={service}",
      body: '{"text": "{text}"}',
      params: [
        { name: "service", description: "The service name" },
        { name: "text", description: "Text" },
      ],
    },
  })

  await page.getByRole("button", { name: "Remove tool", exact: true }).click()
  await page.getByRole("button", { name: "Confirm removal" }).click()
  await expect(page).toHaveURL("/workspaces/w1/connectors/h1?tab=tools")
  expect(server.tools[1]).toEqual({
    method: "DELETE",
    path: "/api/workspaces/w1/mcp-connections/h1/http-tools/search_logs",
    body: null,
  })
  await expect(
    page.getByText(
      "Add a tool for each request that Hondo can make to this API."
    )
  ).toBeVisible()
})

test("shows the saved headers by name only and no sign-in", async ({
  page,
  server,
}) => {
  server.connections = [
    { ...empty, enabled: true, channels: ["C01"], tools: [searchLogs] },
  ]
  await page.goto("/workspaces/w1/connectors/h1")
  await expect(page.getByText("Added by Jane Doe")).toBeVisible()
  await expect(page.getByText("Headers: Authorization")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Disconnect", exact: true })
  ).toHaveCount(0)
  await page.getByRole("button", { name: "Replace headers" }).click()
  await expect(page.getByRole("button", { name: "Sign in" })).toHaveCount(0)
  await expect(
    page.getByRole("textbox", { name: "Header 1 value" })
  ).toHaveValue("")
})
