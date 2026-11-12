import type { RemixiconComponentType } from "@remixicon/react"
import { Link, useLocation } from "react-router"
import {
  SidebarGroup,
  SidebarGroupContent,
  SidebarGroupLabel,
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"

export function NavMain({
  label,
  items,
}: {
  label?: string
  items: {
    title: string
    url: string
    icon: RemixiconComponentType
  }[]
}) {
  const { pathname } = useLocation()
  const { setOpenMobile } = useSidebar()
  return (
    <SidebarGroup>
      {label && <SidebarGroupLabel>{label}</SidebarGroupLabel>}
      <SidebarGroupContent>
        <SidebarMenu>
          {items.map(({ icon: Icon, ...item }) => (
            <SidebarMenuItem key={item.url}>
              <SidebarMenuButton
                render={
                  <Link
                    to={item.url}
                    aria-current={
                      pathname === item.url ||
                      pathname.startsWith(`${item.url}/`)
                        ? "page"
                        : undefined
                    }
                  />
                }
                isActive={
                  pathname === item.url || pathname.startsWith(`${item.url}/`)
                }
                onClick={() => setOpenMobile(false)}
              >
                <Icon aria-hidden={true} />
                <span>{item.title}</span>
              </SidebarMenuButton>
            </SidebarMenuItem>
          ))}
        </SidebarMenu>
      </SidebarGroupContent>
    </SidebarGroup>
  )
}
