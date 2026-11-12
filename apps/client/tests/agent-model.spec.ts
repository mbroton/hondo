import { expect, test } from "@playwright/test"

test("the model picker shows which models read images", async ({ page }) => {
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
                status: "active",
              },
            ],
          },
        })
      }
      if (path === "/api/workspaces/w1/providers") {
        return route.fulfill({
          json: {
            providers: [
              {
                id: "vercel",
                name: "Vercel AI Gateway",
                description: "Access models from multiple providers.",
                api_key_url: "https://vercel.com",
              },
            ],
            connections: [
              {
                id: "c1",
                name: "Production",
                provider: "vercel",
                check_status: "valid",
                checked_at: "2026-11-01T09:00:00Z",
                updated_at: "2026-11-01T09:00:00Z",
              },
            ],
          },
        })
      }
      if (path === "/api/workspaces/w1/provider-connections/c1/models") {
        return route.fulfill({
          json: {
            models: [
              {
                id: "deepseek/deepseek-r1",
                name: "DeepSeek R1",
                reads_images: false,
              },
              { id: "xai/grok-4.5", name: "Grok 4.5", reads_images: true },
            ],
          },
        })
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
      return route.fulfill({ status: 404, json: { detail: "not_found" } })
    }
  )

  await page.goto("/settings")
  await page.getByRole("button", { name: "Select model" }).click()
  await page.getByPlaceholder("Search models…").click()

  await expect(page.getByRole("option", { name: /Grok 4\.5/ })).toContainText(
    "Reads images"
  )
  await expect(
    page.getByRole("option", { name: /DeepSeek R1/ })
  ).not.toContainText("Reads images")
  expect(pageErrors).toEqual([])
})
