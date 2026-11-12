import { Avatar, AvatarFallback, AvatarImage } from "@/components/ui/avatar"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import {
  SidebarMenu,
  SidebarMenuButton,
  SidebarMenuItem,
  useSidebar,
} from "@/components/ui/sidebar"
import { useTheme } from "@/components/theme-provider"
import { useLogout, useMe } from "@/lib/api/queries"
import { loginUrl } from "@/lib/auth"
import {
  RiArrowLeftRightLine,
  RiComputerLine,
  RiLogoutBoxLine,
  RiMoonLine,
  RiMore2Line,
  RiSunLine,
} from "@remixicon/react"

function initials(name: string): string {
  const parts = name.trim().split(/\s+/)
  const first = parts[0]?.charAt(0) ?? ""
  const last = parts.length > 1 ? parts[parts.length - 1].charAt(0) : ""
  return (first + last).toUpperCase()
}

export function NavUser() {
  const { isMobile } = useSidebar()
  const { theme, setTheme } = useTheme()
  const { data: me } = useMe()
  const logout = useLogout()

  // NavUser renders inside RequireAuth, so the session is always present.
  if (!me) {
    return null
  }

  const user = me.user
  const name = user.display_name || user.email

  return (
    <SidebarMenu>
      <SidebarMenuItem>
        <DropdownMenu>
          <DropdownMenuTrigger
            render={
              <SidebarMenuButton
                size="lg"
                className="h-14 aria-expanded:bg-muted"
              />
            }
          >
            <Avatar className="size-9 rounded-lg">
              <AvatarImage src={user.avatar_url ?? undefined} alt={name} />
              <AvatarFallback className="rounded-lg">
                {initials(name)}
              </AvatarFallback>
            </Avatar>
            <div className="grid flex-1 text-left text-sm leading-tight">
              <span className="truncate font-medium">{name}</span>
              <span className="truncate text-xs text-foreground/70">
                {user.email}
              </span>
            </div>
            <RiMore2Line className="ml-auto size-4" />
          </DropdownMenuTrigger>
          <DropdownMenuContent
            className="min-w-56"
            side={isMobile ? "bottom" : "right"}
            align="end"
            sideOffset={4}
          >
            <DropdownMenuGroup>
              <DropdownMenuLabel className="p-0 font-normal">
                <div className="flex items-center gap-2 px-1 py-1.5 text-left text-sm">
                  <Avatar className="size-8">
                    <AvatarImage
                      src={user.avatar_url ?? undefined}
                      alt={name}
                    />
                    <AvatarFallback className="rounded-lg">
                      {initials(name)}
                    </AvatarFallback>
                  </Avatar>
                  <div className="grid flex-1 text-left text-sm leading-tight">
                    <span className="truncate font-medium">{name}</span>
                    <span className="truncate text-xs text-muted-foreground">
                      {user.email}
                    </span>
                  </div>
                </div>
              </DropdownMenuLabel>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuLabel className="text-xs text-muted-foreground">
                Theme
              </DropdownMenuLabel>
              <DropdownMenuRadioGroup
                value={theme}
                onValueChange={(value) =>
                  setTheme(value as "light" | "dark" | "system")
                }
              >
                <DropdownMenuRadioItem value="light">
                  <RiSunLine />
                  Light
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="dark">
                  <RiMoonLine />
                  Dark
                </DropdownMenuRadioItem>
                <DropdownMenuRadioItem value="system">
                  <RiComputerLine />
                  System
                </DropdownMenuRadioItem>
              </DropdownMenuRadioGroup>
            </DropdownMenuGroup>
            <DropdownMenuSeparator />
            <DropdownMenuGroup>
              <DropdownMenuItem
                onClick={() => window.location.assign(loginUrl())}
              >
                <RiArrowLeftRightLine />
                Switch account
              </DropdownMenuItem>
              <DropdownMenuItem
                onClick={() => logout.mutate()}
                disabled={logout.isPending}
              >
                <RiLogoutBoxLine />
                Log out
              </DropdownMenuItem>
            </DropdownMenuGroup>
          </DropdownMenuContent>
        </DropdownMenu>
      </SidebarMenuItem>
    </SidebarMenu>
  )
}
