import { StrictMode } from "react"
import { createRoot } from "react-dom/client"
import {
  QueryCache,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query"
import { BrowserRouter } from "react-router"

import "./index.css"
import App from "./App.tsx"
import { ThemeProvider } from "@/components/theme-provider.tsx"
import { TooltipProvider } from "@/components/ui/tooltip"
import { Toaster } from "@/components/ui/sonner"
import { ApiError } from "@/lib/api/client"

// Session expiry: any query hitting a 401 marks the session signed out,
// and RequireAuth redirects to /login on the next render.
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      // A dead session won't come back — retrying a 401 just delays the
      // redirect to /login. Other errors keep the default three retries.
      retry: (failureCount, error) => {
        if (error instanceof ApiError && error.status === 401) {
          return false
        }
        return failureCount < 3
      },
    },
  },
  queryCache: new QueryCache({
    onError: (error) => {
      if (error instanceof ApiError && error.status === 401) {
        queryClient.setQueryData(["me"], null)
      }
    },
  }),
})

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ThemeProvider>
      <TooltipProvider>
        <QueryClientProvider client={queryClient}>
          <BrowserRouter>
            <App />
            <Toaster />
          </BrowserRouter>
        </QueryClientProvider>
      </TooltipProvider>
    </ThemeProvider>
  </StrictMode>
)
