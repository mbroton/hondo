import { expect, test as base, type Page } from "@playwright/test"
import type {
  BuiltinConnector,
  MCPConnection,
  MCPPolicy,
} from "../src/lib/api/types"

const initial: MCPConnection = {
  id: "c1",
  name: "Project records",
  url: "https://mcp.example/mcp",
  authorized_by: "u1",
  authorized_by_name: "Jane Doe",
  connected: true,
  enabled: false,
  all_channels: false,
  channels: ["C01"],
  tools: [
    {
      name: "lookup_record",
      description: "Find a record.",
      input_schema: { type: "object" },
      enabled: true,
      read_only: true,
      ask: false,
      request: null,
    },
    {
      name: "update_record",
      description: "Update a record.",
      input_schema: { type: "object" },
      enabled: false,
      read_only: false,
      ask: false,
      request: null,
    },
  ],
  status: "ready",
  review_required: false,
  revision: 1,
  auth: "oauth",
  header_names: [],
  builtin: null,
  kind: "mcp",
}

const github: BuiltinConnector = {
  id: "github",
  title: "GitHub",
  description: "Pull requests, commits and releases on github.com.",
  url: "https://api.githubcopilot.com/mcp/",
  inputs: [
    {
      name: "token",
      label: "Access token",
      secret: true,
      help: "A fine-grained personal access token.",
    },
  ],
  setup: [
    "Use a separate GitHub account for Hondo.",
    "Make a fine-grained personal access token.",
    "Select the repositories that Hondo can use.",
  ],
  skills: [
    {
      name: "github-what-changed",
      description: "Tell what changed in a repository since a time.",
    },
  ],
}

const atlassian: BuiltinConnector = {
  id: "atlassian",
  title: "Atlassian",
  description: "Jira, Confluence and Bitbucket Cloud.",
  url: "https://mcp.atlassian.com/v2/mcp",
  inputs: [{ name: "token", label: "API token", secret: true, help: null }],
  setup: ["Make an API token with scopes."],
  skills: [],
}

const githubConnection: MCPConnection = {
  ...initial,
  id: "c2",
  name: "GitHub",
  url: github.url,
  channels: [],
  tools: initial.tools.map((tool) => ({ ...tool, enabled: false })),
  auth: "key",
  header_names: ["Authorization", "X-MCP-Toolsets"],
  builtin: "github",
}

type Server = {
  connection: MCPConnection
  added: MCPConnection[]
  builtinCreates: unknown[]
  builtinError: string | null
  builtinKeys: unknown[]
  failConnections: boolean
  failWorkspaces: boolean
  failAfterSave: boolean
  saveResult: MCPConnection | null
  updates: MCPPolicy[]
  keyResult: MCPConnection | null
  keys: unknown[]
  authorizeGate: Promise<void> | null
}

const test = base.extend<{ server: Server }>({
  server: async ({ page }, provide) => {
    const server: Server = {
      connection: structuredClone(initial),
      added: [],
      builtinCreates: [],
      builtinError: null,
      builtinKeys: [],
      failConnections: false,
      failWorkspaces: false,
      failAfterSave: false,
      saveResult: null,
      updates: [],
      keyResult: null,
      keys: [],
      authorizeGate: null,
    }
    const unexpected: string[] = []
    const pageErrors: string[] = []
    page.on("pageerror", (error) => pageErrors.push(error.message))
    await page.route(
      (url) => url.pathname.startsWith("/api/"),
      async (route) => {
        const request = route.request()
        const path = new URL(request.url()).pathname
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
          return server.failWorkspaces
            ? route.fulfill({ status: 503, json: { detail: "unavailable" } })
            : route.fulfill({
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
        if (path.endsWith("/providers")) {
          return route.fulfill({
            json: { providers: [], connections: [] },
          })
        }
        if (path.endsWith("/agent/model")) {
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
        if (path === "/api/workspaces/w1/mcp-channels") {
          return route.fulfill({
            json: {
              channels: [{ id: "C01", name: "engineering" }],
              next_cursor: null,
            },
          })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections" &&
          request.method() === "GET"
        ) {
          return server.failConnections
            ? route.fulfill({ status: 503, json: { detail: "unavailable" } })
            : route.fulfill({
                json: {
                  connections: [server.connection, ...server.added],
                },
              })
        }
        if (path === "/api/workspaces/w1/builtin-connectors") {
          return route.fulfill({ json: { connectors: [atlassian, github] } })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections/builtin" &&
          request.method() === "POST"
        ) {
          server.builtinCreates.push(request.postDataJSON())
          if (server.builtinError) {
            return route.fulfill({
              status: 422,
              json: { detail: server.builtinError },
            })
          }
          const created = structuredClone(githubConnection)
          server.added.push(created)
          return route.fulfill({ status: 201, json: created })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections/c1/builtin-key" &&
          request.method() === "PUT"
        ) {
          server.builtinKeys.push(request.postDataJSON())
          if (!server.keyResult) {
            return route.fulfill({
              status: 422,
              json: { detail: "invalid_request" },
            })
          }
          server.connection = structuredClone(server.keyResult)
          return route.fulfill({ json: server.connection })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections/c1" &&
          request.method() === "PUT"
        ) {
          server.updates.push(request.postDataJSON())
          if (!server.saveResult) {
            return route.fulfill({
              status: 409,
              json: { detail: "connection_changed" },
            })
          }
          server.connection = structuredClone(server.saveResult)
          server.failConnections = server.failAfterSave
          return route.fulfill({ json: server.connection })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections/c1/key" &&
          request.method() === "PUT"
        ) {
          server.keys.push(request.postDataJSON())
          if (!server.keyResult) {
            return route.fulfill({
              status: 422,
              json: { detail: "invalid_request" },
            })
          }
          server.connection = structuredClone(server.keyResult)
          return route.fulfill({ json: server.connection })
        }
        if (
          path === "/api/workspaces/w1/mcp-connections/c1/authorize" &&
          server.authorizeGate
        ) {
          await server.authorizeGate
          return route.fulfill({ status: 503, json: { detail: "unavailable" } })
        }
        unexpected.push(`${request.method()} ${path}`)
        await route.fulfill({ status: 404, json: { detail: "not_found" } })
      }
    )
    await provide(server)
    expect(unexpected).toEqual([])
    expect(pageErrors).toEqual([])
  },
})

async function openConnector(page: Page) {
  await page.goto("/workspaces/w1/connectors/c1")
  await expect(
    page.getByRole("heading", { name: "Project records", exact: true })
  ).toBeVisible()
}

async function rename(page: Page, name: string) {
  await page.getByRole("button", { name: "Rename Project records" }).click()
  const input = page.getByRole("textbox", { name: "Connector name" })
  await input.fill(name)
  await input.press("Enter")
}

async function refocus(page: Page) {
  // Workspace data has a five-minute cache lifetime.
  await page.clock.setFixedTime(new Date(Date.now() + 600_000))
  await page.evaluate(() => {
    Object.defineProperty(document, "visibilityState", {
      value: "hidden",
      configurable: true,
    })
    window.dispatchEvent(new Event("visibilitychange"))
    Object.defineProperty(document, "visibilityState", {
      value: "visible",
      configurable: true,
    })
    window.dispatchEvent(new Event("visibilitychange"))
  })
}

async function editPolicy(page: Page) {
  await rename(page, "Unsaved name")
  await page.getByRole("tab", { name: "Tools 1", exact: true }).click()
  await page.getByRole("button", { name: "Select all", exact: true }).click()
  await page.getByRole("tab", { name: "Access", exact: true }).click()
  await page.getByRole("checkbox", { name: "#engineering" }).uncheck()
}

async function expectDraft(page: Page) {
  await expect(
    page.getByRole("heading", { name: "Unsaved name" })
  ).toBeVisible()
  await page.getByRole("tab", { name: "Access", exact: true }).click()
  await expect(
    page.getByRole("checkbox", { name: "#engineering" })
  ).not.toBeChecked()
  await page.getByRole("tab", { name: "Tools 2", exact: true }).click()
  const table = page.getByRole("table", { name: "Connector tools" })
  await expect(
    table.getByRole("checkbox", { name: "lookup_record", exact: true })
  ).toBeChecked()
  await expect(
    table.getByRole("checkbox", { name: "update_record", exact: true })
  ).toBeChecked()
}

test("keeps name, tool and channel edits through failed background reads", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await editPolicy(page)
  server.failConnections = true
  server.failWorkspaces = true
  await refocus(page)
  const notice = page.getByText(
    "Could not refresh connectors. Your unsaved changes are kept."
  )
  await expect(notice).toBeVisible({ timeout: 15_000 })
  await expectDraft(page)
  server.failConnections = false
  server.failWorkspaces = false
  await page.getByRole("button", { name: "Retry", exact: true }).click()
  await expect(notice).toBeHidden()
  await expectDraft(page)
  expect(server.updates).toEqual([])
})

test("updates account health without changing the edit baseline", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await editPolicy(page)
  server.connection.status = "sign_in_required"
  await refocus(page)
  await expect(
    page.getByText("Sign-in required", { exact: true })
  ).toBeVisible()
  await expect(
    page.getByRole("switch", { name: "Enable connector" })
  ).toBeDisabled()
  await expectDraft(page)
  await page.getByRole("tab", { name: "Overview", exact: true }).click()
  await expect(
    page.getByRole("button", { name: "Connect account", exact: true })
  ).toBeVisible()
  server.connection.status = "ready"
  await refocus(page)
  await expect(page.getByText("Connected", { exact: true })).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Connect account", exact: true })
  ).toBeHidden()
  await expect(
    page.getByRole("switch", { name: "Enable connector" })
  ).toBeEnabled()
  await expectDraft(page)
  expect(server.connection.revision).toBe(1)
})

test("keeps a confirmed save when the following refresh fails", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await rename(page, "Saved name")
  server.saveResult = {
    ...structuredClone(initial),
    name: "Saved name",
    revision: 2,
  }
  server.failAfterSave = true
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeHidden({
    timeout: 15_000,
  })
  await expect(
    page.getByText(
      "Could not refresh connectors. Your unsaved changes are kept."
    )
  ).toBeVisible()
  await expect(
    page.getByText("This connector changed. Your edits are still here.")
  ).toBeHidden()
  await page.getByRole("button", { name: "Rename Saved name" }).click()
  const input = page.getByRole("textbox", { name: "Connector name" })
  await input.fill("Discard this edit")
  await input.press("Enter")
  await page
    .getByRole("button", { name: "Discard changes", exact: true })
    .click()
  await expect(page.getByRole("heading", { name: "Saved name" })).toBeVisible()
  expect(server.updates).toEqual([
    {
      name: "Saved name",
      revision: 1,
      enabled: false,
      all_channels: false,
      channels: ["C01"],
      allowed_tools: ["lookup_record"],
      ask_tools: [],
      review_tools: false,
    },
  ])
})

test("preserves a draft when another admin saves a newer revision", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await editPolicy(page)
  server.connection = {
    ...structuredClone(initial),
    name: "Other admin's name",
    revision: 2,
  }
  await refocus(page)
  await expect(
    page.getByText("This connector changed. Your edits are still here.")
  ).toBeVisible()
  await expectDraft(page)
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect.poll(() => server.updates.length).toBe(1)
  expect(server.updates[0]).toEqual({
    name: "Unsaved name",
    revision: 1,
    enabled: false,
    all_channels: false,
    channels: [],
    allowed_tools: ["lookup_record", "update_record"],
    ask_tools: ["update_record"],
    review_tools: false,
  })
  await expect(
    page.getByRole("button", { name: "Save changes", exact: true })
  ).toBeEnabled()
  await expectDraft(page)
  await page
    .getByRole("button", { name: "Discard changes", exact: true })
    .first()
    .click()
  await expect(
    page.getByRole("heading", { name: "Other admin's name" })
  ).toBeVisible()
  await expect(
    page.getByText("This connector changed. Your edits are still here.")
  ).toBeHidden()
})

test("retains a saved channel option after it is deselected", async ({
  page,
  server,
}) => {
  server.connection.channels = ["C99"]
  await openConnector(page)
  await page.getByRole("tab", { name: "Access", exact: true }).click()
  const savedChannel = page.getByRole("checkbox", {
    name: "#Saved channel (C99)",
  })
  await expect(savedChannel).toBeChecked()
  await savedChannel.uncheck()
  await expect(savedChannel).toBeVisible()
  await savedChannel.check()
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeHidden()
})

test("selects and clears tools without saving through the search field", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await page.getByRole("tab", { name: "Tools 1", exact: true }).click()
  const table = page.getByRole("table", { name: "Connector tools" })
  await page.getByRole("button", { name: "Select all", exact: true }).click()
  await expect(table.getByRole("checkbox", { checked: true })).toHaveCount(2)
  await page.getByRole("button", { name: "Deselect all", exact: true }).click()
  await expect(table.getByRole("checkbox", { checked: true })).toHaveCount(0)
  const search = page.getByRole("textbox", { name: "Search tools" })
  await search.fill("lookup")
  await search.press("Enter")
  await expect(table.getByRole("row")).toHaveCount(2)
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeVisible()
  expect(server.updates).toEqual([])
})

test("lets another admin enable the connector and expand its tools and channels", async ({
  page,
  server,
}) => {
  server.connection.authorized_by = "u2"
  server.connection.authorized_by_name = "Bob"
  server.saveResult = {
    ...structuredClone(server.connection),
    enabled: true,
    all_channels: true,
    channels: [],
    tools: [
      { ...initial.tools[0], enabled: true },
      { ...initial.tools[1], enabled: true },
    ],
    revision: 2,
  }
  await openConnector(page)
  await expect(
    page.getByText("Connected by Bob", { exact: true })
  ).toBeVisible()
  await page.getByRole("switch", { name: "Enable connector" }).check()
  await page.getByRole("tab", { name: "Tools 1", exact: true }).click()
  await page.getByRole("button", { name: "Select all", exact: true }).click()
  await page.getByRole("tab", { name: "Access", exact: true }).click()
  await page.getByRole("checkbox", { name: "All Slack channels" }).check()
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect(page.getByText("Unsaved changes", { exact: true })).toBeHidden()
  expect(server.updates).toEqual([
    {
      revision: 1,
      name: "Project records",
      enabled: true,
      all_channels: true,
      channels: [],
      allowed_tools: ["lookup_record", "update_record"],
      ask_tools: ["update_record"],
      review_tools: false,
    },
  ])
})

const notConnected: MCPConnection = {
  ...initial,
  authorized_by: null,
  authorized_by_name: null,
  connected: false,
  channels: [],
  tools: [],
  status: "sign_in_required",
  auth: null,
}

test("connects with a key and never shows its value again", async ({
  page,
  server,
}) => {
  server.connection = structuredClone(notConnected)
  server.keyResult = {
    ...initial,
    revision: 2,
    auth: "key",
    header_names: ["Authorization", "X-Team"],
  }
  await openConnector(page)
  await expect(page.getByText("Not connected", { exact: true })).toBeVisible()
  await page.getByRole("button", { name: "Key", exact: true }).click()
  const value = page.getByRole("textbox", { name: "Header 1 value" })
  await expect(value).toHaveAttribute("type", "password")
  await value.fill("Bearer bot-key")
  await page.getByRole("button", { name: "Add header", exact: true }).click()
  await page.getByRole("textbox", { name: "Header 2 name" }).fill("X-Team")
  await page.getByRole("textbox", { name: "Header 2 value" }).fill("ops")
  const save = page.getByRole("button", { name: "Save key", exact: true })
  await expect(save).toBeDisabled()
  await page
    .getByRole("checkbox", {
      name: "I authorize this key's access for the workspace agent.",
    })
    .check()
  await save.click()
  await expect(page).toHaveURL("/workspaces/w1/connectors/c1/setup")
  await expect(
    page.getByRole("heading", { name: "Set up Project records" })
  ).toBeVisible()
  await expect(
    page.getByRole("table", { name: "Connector tools" })
  ).toBeVisible()
  expect(server.keys).toEqual([
    {
      headers: [
        { name: "Authorization", value: "Bearer bot-key" },
        { name: "X-Team", value: "ops" },
      ],
    },
  ])
  await expect(page.locator("input[type=password]")).toHaveCount(0)
  await openConnector(page)
  await expect(page.getByText("Key added by Jane Doe")).toBeVisible()
  await expect(page.getByText("Headers: Authorization, X-Team")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Replace key", exact: true })
  ).toBeVisible()
  await expect(page.locator("input[type=password]")).toHaveCount(0)
})

test("asks for a new key when the server refuses it", async ({
  page,
  server,
}) => {
  server.connection = {
    ...initial,
    status: "sign_in_required",
    auth: "key",
    header_names: ["Authorization"],
  }
  await openConnector(page)
  await expect(page.getByText("Key refused", { exact: true })).toBeVisible()
  await expect(
    page.getByText("The server refused this key.", { exact: false })
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Key", exact: true })
  ).toHaveAttribute("aria-pressed", "true")
  await expect(
    page.getByRole("button", { name: "Save key", exact: true })
  ).toBeVisible()
})

test("explains refused headers on the key form", async ({ page, server }) => {
  server.connection = structuredClone(notConnected)
  await openConnector(page)
  await page.getByRole("button", { name: "Key", exact: true }).click()
  await page
    .getByRole("textbox", { name: "Header 1 value" })
    .fill("Bearer bot-key")
  await page
    .getByRole("checkbox", {
      name: "I authorize this key's access for the workspace agent.",
    })
    .check()
  await page.getByRole("button", { name: "Save key", exact: true }).click()
  await expect(
    page.getByText("Check the header names and values.", { exact: false })
  ).toBeVisible()
})

test("keeps the connection method while sign-in starts", async ({
  page,
  server,
}) => {
  server.connection = structuredClone(notConnected)
  let release = () => {}
  server.authorizeGate = new Promise((resolve) => {
    release = resolve
  })
  await openConnector(page)
  await page
    .getByRole("checkbox", {
      name: "I authorize this account's access for the workspace agent.",
    })
    .check()
  await page
    .getByRole("button", { name: "Connect account", exact: true })
    .click()
  const key = page.getByRole("button", { name: "Key", exact: true })
  await expect(key).toBeDisabled()
  release()
  await expect(
    page.getByText("The connector could not be updated. Try again.")
  ).toBeVisible()
  await expect(key).toBeEnabled()
})

test("adds GitHub from its card and opens its setup", async ({
  page,
  server,
}) => {
  await page.goto("/workspaces/w1/connectors/new")
  await expect(
    page.getByRole("link", { name: "Atlassian", exact: true })
  ).toBeVisible()
  await expect(
    page.getByRole("link", { name: "Custom MCP server", exact: true })
  ).toBeVisible()
  await page.getByRole("link", { name: "GitHub", exact: true }).click()
  await expect(
    page.getByRole("list", { name: "Before you connect" }).getByRole("listitem")
  ).toHaveText(github.setup)
  await expect(page.getByRole("textbox", { name: "Name" })).toHaveValue(
    "GitHub"
  )
  const token = page.getByLabel("Access token")
  await expect(token).toHaveAttribute("type", "password")
  await expect(
    page.getByText("A fine-grained personal access token.", { exact: true })
  ).toBeVisible()
  await token.fill("github_pat_abc")
  const connect = page.getByRole("button", { name: "Connect", exact: true })
  await expect(connect).toBeDisabled()
  await page
    .getByRole("checkbox", {
      name: "I authorize this key's access for the workspace agent.",
    })
    .check()
  await connect.click()
  await expect(page).toHaveURL("/workspaces/w1/connectors/c2/setup")
  await expect(
    page.getByRole("heading", { name: "Set up GitHub" })
  ).toBeVisible()
  const table = page.getByRole("table", { name: "Connector tools" })
  await expect(
    table.getByRole("checkbox", { name: "lookup_record" })
  ).toBeChecked()
  await expect(
    table.getByRole("checkbox", { name: "update_record" })
  ).not.toBeChecked()
  await expect(page.getByText("Select at least one channel.")).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Turn on", exact: true })
  ).toBeDisabled()
  expect(server.builtinCreates).toEqual([
    { builtin: "github", name: "GitHub", inputs: { token: "github_pat_abc" } },
  ])
})

test("keeps the form when the service refuses the token", async ({
  page,
  server,
}) => {
  server.builtinError = "key_refused"
  await page.goto("/workspaces/w1/connectors/new?type=github")
  await page.getByLabel("Access token").fill("github_pat_bad")
  await page
    .getByRole("checkbox", {
      name: "I authorize this key's access for the workspace agent.",
    })
    .check()
  await page.getByRole("button", { name: "Connect", exact: true }).click()
  await expect(
    page.getByText(
      "The service refused this token. Check the token and its permissions."
    )
  ).toBeVisible()
  await expect(page).toHaveURL("/workspaces/w1/connectors/new?type=github")
  await expect(page.getByLabel("Access token")).toHaveValue("github_pat_bad")
  expect(server.builtinCreates).toHaveLength(1)
  expect(server.added).toEqual([])
})

test("opens the custom server form from its card", async ({ page, server }) => {
  await page.goto("/workspaces/w1/connectors/new")
  await page
    .getByRole("link", { name: "Custom MCP server", exact: true })
    .click()
  await expect(page.getByRole("textbox", { name: "Server URL" })).toBeVisible()
  await page.getByRole("button", { name: "Back", exact: true }).click()
  await expect(
    page.getByRole("link", { name: "GitHub", exact: true })
  ).toBeVisible()
  expect(server.added).toEqual([])
})

test("replaces a built-in token with the built-in form", async ({
  page,
  server,
}) => {
  server.connection = {
    ...structuredClone(githubConnection),
    id: "c1",
    enabled: true,
  }
  server.keyResult = { ...structuredClone(server.connection), revision: 2 }
  await page.goto("/workspaces/w1/connectors/c1")
  await expect(
    page.getByText(`GitHub: ${github.description}`, { exact: true })
  ).toBeVisible()
  await expect(
    page.getByRole("list", { name: "Skills" }).getByRole("listitem")
  ).toContainText(["github-what-changed"])
  await expect(page.getByText("Key added by Jane Doe")).toBeVisible()
  await expect(page.getByText("Shared with this workspace")).toBeVisible()
  await page.getByRole("button", { name: "Replace key", exact: true }).click()
  await expect(
    page.getByRole("list", { name: "Before you connect" })
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Sign in", exact: true })
  ).toHaveCount(0)
  await expect(
    page.getByRole("textbox", { name: "Header 1 value" })
  ).toHaveCount(0)
  await page.getByLabel("Access token").fill("github_pat_new")
  await page
    .getByRole("checkbox", {
      name: "I authorize this key's access for the workspace agent.",
    })
    .check()
  await page.getByRole("button", { name: "Save key", exact: true }).click()
  await expect(
    page.getByText("Key saved. Select the tools that Hondo can use.")
  ).toBeVisible()
  await expect(
    page.getByRole("tab", { name: "Tools 0", exact: true })
  ).toHaveAttribute("aria-selected", "true")
  expect(server.builtinKeys).toEqual([{ inputs: { token: "github_pat_new" } }])
  expect(server.keys).toEqual([])
})

test("says that a removed built-in is no longer available", async ({
  page,
  server,
}) => {
  server.connection = {
    ...structuredClone(githubConnection),
    id: "c1",
    builtin: "gitlab",
    status: "sign_in_required",
  }
  await page.goto("/workspaces/w1/connectors/c1")
  await expect(
    page.getByText("This built-in connector is no longer available")
  ).toBeVisible()
  await expect(page.getByRole("button", { name: "Retry" })).toHaveCount(0)
  await expect(
    page.getByRole("list", { name: "Before you connect" })
  ).toHaveCount(0)
})

test("turns on a new connector with its tools and channels in one save", async ({
  page,
  server,
}) => {
  server.connection = {
    ...structuredClone(initial),
    channels: [],
    tools: initial.tools.map((tool) => ({ ...tool, enabled: false })),
    review_required: true,
  }
  server.saveResult = {
    ...structuredClone(initial),
    enabled: true,
    revision: 2,
  }
  await page.goto("/workspaces/w1/connectors/c1/setup")
  const table = page.getByRole("table", { name: "Connector tools" })
  await expect(
    table.getByRole("checkbox", { name: "lookup_record" })
  ).toBeChecked()
  await expect(
    table.getByRole("checkbox", { name: "update_record" })
  ).not.toBeChecked()
  const turnOn = page.getByRole("button", { name: "Turn on", exact: true })
  await page.getByRole("button", { name: "Deselect all", exact: true }).click()
  await expect(page.getByText("Select at least one tool.")).toBeVisible()
  await expect(turnOn).toBeDisabled()
  await table.getByRole("checkbox", { name: "lookup_record" }).check()
  await expect(page.getByText("Select at least one channel.")).toBeVisible()
  await expect(turnOn).toBeDisabled()
  await page.getByRole("textbox", { name: "Search channels" }).fill("random")
  await expect(
    page.getByText(
      "No channels match your search. The list shows only channels where Hondo is a member."
    )
  ).toBeVisible()
  await page.getByRole("textbox", { name: "Search channels" }).fill("")
  await page.getByRole("checkbox", { name: "#engineering" }).check()
  await expect(page.getByText("1 tool in 1 channel")).toBeVisible()
  await turnOn.click()
  await expect(page).toHaveURL("/workspaces/w1/connectors/c1")
  await expect(page.getByText("Project records is turned on.")).toBeVisible()
  await expect(page.getByText("Enabled", { exact: true })).toBeVisible()
  expect(server.updates).toEqual([
    {
      name: "Project records",
      revision: 1,
      enabled: true,
      all_channels: false,
      channels: ["C01"],
      allowed_tools: ["lookup_record"],
      ask_tools: [],
      review_tools: true,
    },
  ])
})

test("continues setup after sign-in for a connector that is off", async ({
  page,
  server,
}) => {
  await page.goto("/workspaces/w1/connectors/c1?mcp=connected")
  await expect(page).toHaveURL("/workspaces/w1/connectors/c1/setup")
  await expect(
    page.getByRole("heading", { name: "Set up Project records" })
  ).toBeVisible()
  await expect(page.getByText("Account connected.")).toBeHidden()
  expect(server.updates).toEqual([])
})

test("keeps a reconnected connector that is on in its details", async ({
  page,
  server,
}) => {
  server.connection.enabled = true
  await page.goto("/workspaces/w1/connectors/c1?mcp=connected")
  await expect(
    page.getByText("Account connected. Review the tools and channel access.")
  ).toBeVisible()
  await expect(page).toHaveURL("/workspaces/w1/connectors/c1?mcp=connected")
})

test("says what is missing before a connector can be enabled", async ({
  page,
  server,
}) => {
  server.connection.channels = []
  await openConnector(page)
  await page.getByRole("switch", { name: "Enable connector" }).check()
  await expect(
    page.getByText("Select at least one channel to enable the connector.")
  ).toBeVisible()
  await expect(
    page.getByRole("button", { name: "Save changes", exact: true })
  ).toBeDisabled()
  expect(server.updates).toEqual([])
})

test("asks before running tools that can change data, unless the admin turns it off", async ({
  page,
  server,
}) => {
  await openConnector(page)
  await page.getByRole("tab", { name: "Tools 1", exact: true }).click()
  const table = page.getByRole("table", { name: "Connector tools" })
  await expect(
    table.getByRole("switch", { name: "Ask before running update_record" })
  ).toHaveCount(0)
  await table.getByRole("checkbox", { name: "update_record" }).check()
  const askUpdate = table.getByRole("switch", {
    name: "Ask before running update_record",
  })
  const askLookup = table.getByRole("switch", {
    name: "Ask before running lookup_record",
  })
  await expect(askUpdate).toBeChecked()
  await expect(askLookup).not.toBeChecked()
  await askLookup.check()
  await askUpdate.uncheck()
  await page.getByRole("button", { name: "Save changes", exact: true }).click()
  await expect.poll(() => server.updates.length).toBe(1)
  expect(server.updates[0]).toEqual({
    revision: 1,
    name: "Project records",
    enabled: false,
    all_channels: false,
    channels: ["C01"],
    allowed_tools: ["lookup_record", "update_record"],
    ask_tools: ["lookup_record"],
    review_tools: false,
  })
})
