import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider } from "@tanstack/react-router";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ApiError } from "./api/client";
import { router } from "./router";
import { installSharedModules } from "./shell/plugins";

installSharedModules();
import { applyTheme, useTheme } from "./lib/theme";
import "./styles/tokens.css";
import "./styles/base.css";
import "./styles/components.css";
import "./styles/shell.css";
import "./styles/evidence.css";
import "./styles/pages.css";
import "./styles/viewers.css";
import "./styles/workbench.css";
import "./styles/sheet.css";
import "./styles/studio.css";

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 10_000,
      refetchOnWindowFocus: false,
      retry: (count, error) => {
        if (error instanceof ApiError && error.status >= 400 && error.status < 500) return false;
        return count < 2;
      },
    },
  },
});

function ThemeSync() {
  const theme = useTheme();
  applyTheme(theme);
  return null;
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <QueryClientProvider client={queryClient}>
      <ThemeSync />
      <RouterProvider router={router} />
    </QueryClientProvider>
  </StrictMode>,
);
