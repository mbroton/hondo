import type { ComponentProps } from "react"
import { Link } from "react-router"
import {
  RiBarChartLine,
  RiBookOpenLine,
  RiBrainLine,
  RiHistoryLine,
  RiHomeLine,
  RiSettings3Line,
  RiSlackFill,
  RiPlugLine,
} from "@remixicon/react"
import { NavMain } from "@/components/nav-main"
import { NavUser } from "@/components/nav-user"
import { Badge } from "@/components/ui/badge"
import { Skeleton } from "@/components/ui/skeleton"
import { useWorkspace } from "@/hooks/use-workspace"
import {
  Sidebar,
  SidebarContent,
  SidebarFooter,
  SidebarHeader,
} from "@/components/ui/sidebar"

export function AppSidebar(props: ComponentProps<typeof Sidebar>) {
  const { workspace, isPending } = useWorkspace()
  const workspaceId = workspace?.id
  const navigation = [
    { title: "Home", url: "/", icon: RiHomeLine },
    { title: "Usage", url: "/usage", icon: RiBarChartLine },
    { title: "Activity", url: "/activity", icon: RiHistoryLine },
    {
      title: "Connectors",
      url: workspaceId
        ? `/workspaces/${workspaceId}/connectors`
        : "/connectors",
      icon: RiPlugLine,
    },
    {
      title: "Skills",
      url: workspaceId ? `/workspaces/${workspaceId}/skills` : "/skills",
      icon: RiBookOpenLine,
    },
    {
      title: "Memory",
      url: workspaceId ? `/workspaces/${workspaceId}/memory` : "/memory",
      icon: RiBrainLine,
    },
    { title: "Settings", url: "/settings", icon: RiSettings3Line },
  ]

  return (
    <Sidebar collapsible="offcanvas" {...props}>
      <SidebarHeader className="gap-6 px-4 pt-6 pb-3">
        <Link
          to="/"
          className="ml-1 flex w-fit items-center gap-3 rounded-md outline-none focus-visible:ring-2 focus-visible:ring-ring"
          aria-label="Hondo home"
        >
          <img src="/logo-sm.png" alt="" className="size-8 object-contain" />
          <span className="text-xl font-semibold tracking-tight">
            Hondo<span className="text-primary">.</span>
          </span>
        </Link>
        {isPending ? (
          <Skeleton className="ml-1 h-5 w-32" />
        ) : workspace ? (
          <div className="ml-1 flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
            <RiSlackFill className="size-4 shrink-0" aria-hidden="true" />
            <span className="min-w-0 truncate">{workspace.name}</span>
            {workspace.status !== "active" && (
              <Badge variant="destructive">{workspace.status}</Badge>
            )}
          </div>
        ) : null}
      </SidebarHeader>
      <SidebarContent className="gap-5 px-2">
        <NavMain items={navigation} />
      </SidebarContent>
      <SidebarFooter className="gap-3 p-3">
        <div className="border-t pt-3">
          <NavUser />
        </div>
      </SidebarFooter>
    </Sidebar>
  )
}
