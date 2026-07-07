import { render, screen } from "@testing-library/react";

import { App } from "./App";

test("renders the React island scaffold", () => {
  render(<App />);

  expect(
    screen.getByRole("heading", { name: "React island scaffold" }),
  ).toBeInTheDocument();
  expect(screen.getByText("Recap")).toBeInTheDocument();
});
