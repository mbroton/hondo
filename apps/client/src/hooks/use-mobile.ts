import * as React from "react"

const MOBILE_BREAKPOINT = 768

export function useIsMobile() {
  // Lazy initializer instead of a set-state-in-effect: the value is known
  // synchronously from matchMedia, and the effect only subscribes to changes.
  const [isMobile, setIsMobile] = React.useState(
    () => window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`).matches,
  )

  React.useEffect(() => {
    const mql = window.matchMedia(`(max-width: ${MOBILE_BREAKPOINT - 1}px)`)
    const onChange = (event: MediaQueryListEvent) => {
      setIsMobile(event.matches)
    }
    mql.addEventListener("change", onChange)
    return () => mql.removeEventListener("change", onChange)
  }, [])

  return isMobile
}
