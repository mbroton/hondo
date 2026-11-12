import { lazy, Suspense } from "react"
import { Navigate, Route, Routes } from "react-router"

import {
  RedirectIfAuthed,
  RequireAdmin,
  RequireAuth,
} from "@/components/route-guards"
import { AppLayout } from "@/layouts/app-layout"

const ActivityPage = lazy(() => import("@/app/activity/page"))
const HomePage = lazy(() => import("@/app/home/page"))
const LoginPage = lazy(() => import("@/app/login/page"))
const SettingsPage = lazy(() => import("@/app/settings/page"))
const StartPage = lazy(() => import("@/app/start/page"))
const LegacySettingsRedirect = lazy(() =>
  import("@/app/settings/page").then((module) => ({
    default: module.LegacySettingsRedirect,
  }))
)
const UsagePage = lazy(() => import("@/app/usage/page"))
const MCPLayout = lazy(() => import("@/app/mcp/layout"))
const MCPRedirect = lazy(() =>
  import("@/app/mcp/layout").then((module) => ({ default: module.MCPRedirect }))
)
const MCPListPage = lazy(() => import("@/app/mcp/page"))
const NewMCPPage = lazy(() => import("@/app/mcp/new"))
const MCPDetailsPage = lazy(() => import("@/app/mcp/details"))
const MCPSetupPage = lazy(() => import("@/app/mcp/setup"))
const HTTPToolPage = lazy(() => import("@/app/mcp/tool"))
const SkillsLayout = lazy(() => import("@/app/skills/layout"))
const SkillsListPage = lazy(() => import("@/app/skills/page"))
const NewSkillPage = lazy(() => import("@/app/skills/new"))
const SkillDetailsPage = lazy(() => import("@/app/skills/details"))
const MemoryPage = lazy(() => import("@/app/memory/page"))

export function App() {
  return (
    <Suspense
      fallback={
        <div
          role="status"
          className="flex min-h-svh items-center justify-center text-sm text-muted-foreground"
        >
          Loading Hondo…
        </div>
      }
    >
      <Routes>
        <Route element={<RedirectIfAuthed />}>
          <Route path="/login" element={<LoginPage />} />
        </Route>
        <Route element={<RequireAuth />}>
          <Route element={<RequireAdmin fallback={<StartPage />} />}>
            <Route path="/" element={<AppLayout />}>
              <Route index element={<HomePage />} />
              <Route path="usage" element={<UsagePage />} />
              <Route path="activity" element={<ActivityPage />} />
              <Route path="mcp" element={<MCPRedirect />} />
              <Route
                path="workspaces/:workspaceId/mcp/:connectionId?"
                element={<MCPRedirect />}
              />
              <Route path="connectors" element={<MCPLayout />} />
              <Route
                path="workspaces/:workspaceId/connectors"
                element={<MCPLayout />}
              >
                <Route index element={<MCPListPage />} />
                <Route path="new" element={<NewMCPPage />} />
                <Route path=":connectionId" element={<MCPDetailsPage />} />
                <Route path=":connectionId/setup" element={<MCPSetupPage />} />
                <Route
                  path=":connectionId/tools/new"
                  element={<HTTPToolPage />}
                />
                <Route
                  path=":connectionId/tools/:toolName"
                  element={<HTTPToolPage />}
                />
              </Route>
              <Route path="skills" element={<SkillsLayout />} />
              <Route
                path="workspaces/:workspaceId/skills"
                element={<SkillsLayout />}
              >
                <Route index element={<SkillsListPage />} />
                <Route path="new" element={<NewSkillPage />} />
                <Route path=":skillId" element={<SkillDetailsPage />} />
              </Route>
              <Route path="memory" element={<MemoryPage />} />
              <Route
                path="workspaces/:workspaceId/memory"
                element={<MemoryPage />}
              />
              <Route path="settings" element={<SettingsPage />} />
              <Route
                path="settings/providers"
                element={<LegacySettingsRedirect />}
              />
              <Route
                path="settings/agent"
                element={<LegacySettingsRedirect />}
              />
            </Route>
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Suspense>
  )
}

export default App
