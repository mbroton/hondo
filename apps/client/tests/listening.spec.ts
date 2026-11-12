import { expect, test, type Page } from "@playwright/test"

type Listening = { channels: { channel: string; goal: string }[] }

async function mockApi(
  page: Page,
  {
    saved,
    putStatus = 204,
    failLoads = false,
    mentionOnly = [],
  }: {
    saved: Listening
    putStatus?: number
    failLoads?: boolean
    mentionOnly?: string[]
  }
) {
  const state = { saved, puts: [] as unknown[], failLoads }
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
              { id: "C02", name: "random" },
            ],
            next_cursor: null,
          },
        })
      }
      if (path === "/api/workspaces/w1/agent/mention-only") {
        return route.fulfill({ json: { channels: mentionOnly } })
      }
      if (path === "/api/workspaces/w1/agent/listening") {
        if (request.method() === "PUT") {
          state.puts.push(request.postDataJSON())
          if (putStatus !== 204) {
            return route.fulfill({
              status: putStatus,
              json: {
                detail:
                  putStatus === 409 ? "channel_conflict" : "internal_error",
              },
            })
          }
          state.saved = request.postDataJSON()
          return route.fulfill({ status: 204 })
        }
        if (state.failLoads) {
          return route.fulfill({
            status: 500,
            json: { detail: "internal_error" },
          })
        }
        return route.fulfill({ json: state.saved })
      }
      return route.fulfill({ status: 404, json: { detail: "not_found" } })
    }
  )
  return state
}

function trackPageErrors(page: Page) {
  const errors: string[] = []
  page.on("pageerror", (error) => errors.push(error.message))
  return errors
}

test("an admin sets a goal for each channel where Hondo listens", async ({
  page,
}) => {
  const pageErrors = trackPageErrors(page)
  const api = await mockApi(page, {
    saved: {
      channels: [{ channel: "C02", goal: "Help with on-call alerts." }],
    },
  })

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Listening channels" })
  await expect(form.getByRole("textbox", { name: "#random" })).toHaveValue(
    "Help with on-call alerts."
  )
  const save = form.getByRole("button", { name: "Save listening channels" })
  await expect(save).toBeDisabled()

  await form.getByLabel("Add a channel").fill("eng")
  await page.getByRole("option", { name: "#engineering" }).click()
  await expect(save).toBeDisabled()
  await form
    .getByRole("textbox", { name: "#engineering" })
    .fill("  Answer deploy questions.  ")
  await save.click()

  await expect(page.getByText("Listening channels saved.")).toBeVisible()
  expect(api.puts).toEqual([
    {
      channels: [
        { channel: "C02", goal: "Help with on-call alerts." },
        { channel: "C01", goal: "Answer deploy questions." },
      ],
    },
  ])
  await expect(save).toBeDisabled()

  await form.getByRole("button", { name: "Stop listening in #random" }).click()
  await save.click()

  await expect.poll(() => api.puts.length).toBe(2)
  expect(api.puts[1]).toEqual({
    channels: [{ channel: "C01", goal: "Answer deploy questions." }],
  })
  expect(pageErrors).toEqual([])
})

test("a channel needs a goal before it can be saved", async ({ page }) => {
  const pageErrors = trackPageErrors(page)
  const api = await mockApi(page, { saved: { channels: [] } })

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Listening channels" })
  const save = form.getByRole("button", { name: "Save listening channels" })
  await form.getByLabel("Add a channel").fill("random")
  await page.getByRole("option", { name: "#random" }).click()
  const goal = form.getByRole("textbox", { name: "#random" })

  await goal.fill("    ")
  await expect(save).toBeDisabled()
  await goal.fill("Help with on-call alerts.")
  await expect(save).toBeEnabled()
  await goal.fill("")
  await expect(save).toBeDisabled()

  await expect(goal).toHaveAttribute("maxlength", "1000")
  await form.getByLabel("Add a channel").fill("random")
  await expect(page.getByRole("option", { name: "#random" })).toHaveCount(0)
  expect(api.puts).toEqual([])
  expect(pageErrors).toEqual([])
})

test("a failed save keeps the edits", async ({ page }) => {
  const pageErrors = trackPageErrors(page)
  const api = await mockApi(page, {
    saved: { channels: [] },
    putStatus: 500,
  })

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Listening channels" })
  await form.getByLabel("Add a channel").fill("eng")
  await page.getByRole("option", { name: "#engineering" }).click()
  await form
    .getByRole("textbox", { name: "#engineering" })
    .fill("Answer deploy questions.")
  await form.getByRole("button", { name: "Save listening channels" }).click()

  await expect(
    page.getByText("The listening channels could not be saved. Try again.")
  ).toBeVisible()
  await expect(form.getByRole("textbox", { name: "#engineering" })).toHaveValue(
    "Answer deploy questions."
  )
  await expect(
    form.getByRole("button", { name: "Save listening channels" })
  ).toBeEnabled()
  expect(api.puts).toHaveLength(1)
  expect(pageErrors).toEqual([])
})

test("the section recovers when the channels fail to load", async ({
  page,
}) => {
  const pageErrors = trackPageErrors(page)
  const api = await mockApi(page, {
    saved: {
      channels: [{ channel: "C02", goal: "Help with on-call alerts." }],
    },
    failLoads: true,
  })

  await page.goto("/settings")
  const listening = page.getByRole("region", { name: "Listening" })
  // The query retries three times with backoff before it shows the error.
  await expect(
    listening.getByText("Could not load the listening channels")
  ).toBeVisible({ timeout: 15_000 })
  api.failLoads = false
  await listening.getByRole("button", { name: /try again|retry/i }).click()

  await expect(
    page
      .getByRole("form", { name: "Listening channels" })
      .getByRole("textbox", { name: "#random" })
  ).toHaveValue("Help with on-call alerts.")
  expect(pageErrors).toEqual([])
})

test("a saved channel that Hondo cannot see keeps its goal", async ({
  page,
}) => {
  const pageErrors = trackPageErrors(page)
  const api = await mockApi(page, {
    saved: {
      channels: [{ channel: "C09", goal: "Answer billing questions." }],
    },
  })

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Listening channels" })
  const goal = form.getByRole("textbox", { name: "Saved channel (C09)" })
  await expect(goal).toHaveValue("Answer billing questions.")
  await goal.fill("Answer billing and invoice questions.")
  await form.getByRole("button", { name: "Save listening channels" }).click()

  await expect.poll(() => api.puts.length).toBe(1)
  expect(api.puts[0]).toEqual({
    channels: [
      { channel: "C09", goal: "Answer billing and invoice questions." },
    ],
  })
  expect(pageErrors).toEqual([])
})

test("a mention-only channel and a listening channel stay apart", async ({
  page,
}) => {
  const pageErrors = trackPageErrors(page)
  await mockApi(page, {
    saved: {
      channels: [{ channel: "C02", goal: "Help with on-call alerts." }],
    },
    mentionOnly: ["C01"],
  })

  await page.goto("/settings")
  const listening = page.getByRole("form", { name: "Listening channels" })
  await expect(
    listening.getByRole("textbox", { name: "#random" })
  ).toBeVisible()
  await listening.getByLabel("Add a channel").fill("eng")
  await expect(page.getByRole("option", { name: "#engineering" })).toHaveCount(
    0
  )
  await page.keyboard.press("Escape")

  const mentionOnly = page.getByRole("form", { name: "Mention-only channels" })
  await expect(
    mentionOnly.getByRole("checkbox", { name: "#engineering" })
  ).toBeChecked()
  await expect(
    mentionOnly.getByRole("checkbox", { name: "#random" })
  ).toHaveCount(0)
  expect(pageErrors).toEqual([])
})

test("a save that conflicts with mention-only says why", async ({ page }) => {
  const pageErrors = trackPageErrors(page)
  await mockApi(page, { saved: { channels: [] }, putStatus: 409 })

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Listening channels" })
  await form.getByLabel("Add a channel").fill("eng")
  await page.getByRole("option", { name: "#engineering" }).click()
  await form
    .getByRole("textbox", { name: "#engineering" })
    .fill("Answer deploy questions.")
  await form.getByRole("button", { name: "Save listening channels" }).click()

  await expect(
    page.getByText("A mention-only channel cannot be a listening channel.")
  ).toBeVisible()
  expect(pageErrors).toEqual([])
})
