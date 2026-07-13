import "@mantine/core/styles.css";
import "./styles.css";
import { createTheme, MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router";
import { App } from "./App";

const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 15_000 }, mutations: { retry: false } } });

const theme = createTheme({
  fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif",
  fontFamilyMonospace: "\"SFMono-Regular\", Consolas, \"Liberation Mono\", Menlo, monospace",
  primaryColor: "slate",
  primaryShade: 6,
  colors: {
    slate: ["#f0f5f8", "#e4eef3", "#d6e5ec", "#b9cfda", "#8eafc0", "#638ba1", "#395f76", "#29495d", "#203c4d", "#18313f"],
  },
  defaultRadius: "sm",
  headings: { fontFamily: "Inter, ui-sans-serif, -apple-system, BlinkMacSystemFont, \"Segoe UI\", sans-serif", fontWeight: "680" },
});

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <MantineProvider theme={theme} defaultColorScheme="light" forceColorScheme="light">
      <QueryClientProvider client={queryClient}>
        <BrowserRouter>
          <App />
        </BrowserRouter>
      </QueryClientProvider>
    </MantineProvider>
  </StrictMode>,
);
