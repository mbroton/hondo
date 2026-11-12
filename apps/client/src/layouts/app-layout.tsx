import { Suspense, type CSSProperties } from "react"
import { Skeleton } from "@/components/ui/skeleton"
import { Outlet } from "react-router"

import { AppSidebar } from "@/components/app-sidebar"
import { SiteHeader } from "@/components/site-header"
import { SidebarInset, SidebarProvider } from "@/components/ui/sidebar"

export function AppLayout() {
  return (
    <SidebarProvider
      style={
        {
          "--sidebar-width": "15rem",
          "--header-height": "4rem",
        } as CSSProperties
      }
    >
      <a href="#main-content" className="skip-link">
        Skip to content
      </a>
      <AppSidebar />
      <SidebarInset className="min-w-0">
        <SiteHeader />
        <div
          id="main-content"
          tabIndex={-1}
          className="@container/main mx-auto flex w-full max-w-7xl flex-1 flex-col px-4 py-6 outline-none sm:px-6 lg:px-10 lg:py-9"
        >
          <Suspense
            fallback={
              <div role="status" className="flex flex-col gap-7">
                <span className="sr-only">Loading page</span>
                <Skeleton className="h-9 w-40" />
                <Skeleton className="h-44 w-full" />
              </div>
            }
          >
            <Outlet />
          </Suspense>
        </div>
      </SidebarInset>
    </SidebarProvider>
  )
}
