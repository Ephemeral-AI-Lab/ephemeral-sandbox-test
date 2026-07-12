import { MantineProvider } from "@mantine/core";
import axe from "axe-core";
import { render, screen } from "@testing-library/react";
import { expect, it } from "vitest";
import { AsyncStateNotice, asyncStateCopy } from "../src/state-copy";

it("renders every documented asynchronous state with stable operator copy", async () => {
  render(<MantineProvider defaultColorScheme="dark"><>{Object.entries(asyncStateCopy).map(([state]) => <AsyncStateNotice key={state} state={state as keyof typeof asyncStateCopy} />)}</></MantineProvider>);
  for (const [headline, explanation] of Object.values(asyncStateCopy)) {
    expect(screen.getByRole("heading", { name: headline })).toBeTruthy();
    expect(screen.getByText(explanation)).toBeTruthy();
  }
  const results = await axe.run(document, { rules: { "color-contrast": { enabled: false } } });
  expect(results.violations.filter((item) => ["critical", "serious"].includes(item.impact ?? ""))).toEqual([]);
});
