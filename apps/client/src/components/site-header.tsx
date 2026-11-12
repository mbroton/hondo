import { useLocation, useMatch } from "react-router"
import { SidebarTrigger } from "@/components/ui/sidebar"

const titles: Record<string, string> = {
  "/": "Home",
  "/activity": "Activity",
  "/usage": "Usage",
  "/settings": "Settings",
}

export function SiteHeader() {
  const { pathname } = useLocation()
  const connectorRoute = useMatch("/workspaces/:workspaceId/connectors/*")
  const skillRoute = useMatch("/workspaces/:workspaceId/skills/*")
  const memoryRoute = useMatch("/workspaces/:workspaceId/memory")
  return (
    <header className="flex min-h-(--header-height) shrink-0 items-center gap-3 border-b px-4 text-sm sm:px-6 lg:px-10">
      <SidebarTrigger />
      <span className="shrink-0 font-medium">
        {connectorRoute || pathname === "/connectors"
          ? "Connectors"
          : skillRoute || pathname === "/skills"
            ? "Skills"
            : memoryRoute || pathname === "/memory"
              ? "Memory"
              : (titles[pathname] ?? "Hondo")}
      </span>
    </header>
  )
}
