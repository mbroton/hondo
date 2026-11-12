import { expect, test } from "@playwright/test"

test("an admin sets the channels where Hondo answers only when mentioned", async ({
  page,
}) => {
  const pageErrors: string[] = []
  page.on("pageerror", (error) => pageErrors.push(error.message))
  let saved = { channels: ["C02"] }
  const puts: unknown[] = []
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
        if (request.method() === "PUT") {
          saved = request.postDataJSON()
          puts.push(saved)
          return route.fulfill({ status: 204 })
        }
        return route.fulfill({ json: saved })
      }
      return route.fulfill({ status: 404, json: { detail: "not_found" } })
    }
  )

  await page.goto("/settings")
  const form = page.getByRole("form", { name: "Mention-only channels" })
  await expect(form.getByRole("checkbox", { name: "#random" })).toBeChecked()
  const save = form.getByRole("button", { name: "Save channels" })
  await expect(save).toBeDisabled()

  await form.getByRole("checkbox", { name: "#engineering" }).check()
  await save.click()

  await expect(page.getByText("Channels saved.")).toBeVisible()
  expect(puts).toEqual([{ channels: ["C02", "C01"] }])
  await expect(
    form.getByRole("checkbox", { name: "#engineering" })
  ).toBeChecked()
  await expect(save).toBeDisabled()

  await expect(
    form.getByRole("checkbox", { name: "All Slack channels" })
  ).toHaveCount(0)
  await form.getByRole("checkbox", { name: "#engineering" }).uncheck()
  await form.getByRole("checkbox", { name: "#random" }).uncheck()
  await save.click()

  await expect.poll(() => puts.length).toBe(2)
  expect(puts[1]).toEqual({ channels: [] })
  expect(pageErrors).toEqual([])
})
