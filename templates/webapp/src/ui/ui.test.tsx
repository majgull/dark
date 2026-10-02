// The shared pieces panels must use and not rebuild: a Button with a tooltip
// and a pending state, a Notice for a refusal, and the Field shape.
import { act, fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import type { Result } from "../api";
import { Button } from "./Button";
import { Field } from "./Field";
import { Notice } from "./Notice";

describe("Button", () => {
  it("is busy while its action runs and carries the tooltip it was given", async () => {
    let release: (result: Result<never>) => void = () => {};
    const run = () =>
      new Promise<Result<never>>((resolve) => {
        release = resolve;
      });
    render(<Button label="Add note" title="Adds the note to the list" run={run} />);
    const button = screen.getByRole("button", { name: "Add note" });
    expect(button).toHaveAttribute("title", "Adds the note to the list");
    fireEvent.click(button);
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("aria-busy", "true");
    await act(async () => {
      release({ ok: false, status: 0, message: "no" });
    });
    expect(button).toBeEnabled();
  });
});

describe("Notice", () => {
  it("shows a refusal's message and nothing when the answer was fine", () => {
    const { rerender } = render(
      <Notice result={{ ok: false, status: 403, message: "Not allowed" }} />,
    );
    expect(screen.getByRole("status")).toHaveTextContent("Not allowed");
    rerender(<Notice result={{ ok: true, value: 1 }} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    rerender(<Notice result={null} />);
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });
});

describe("Field", () => {
  it("labels a control and carries its hint as a tooltip", () => {
    render(
      <Field label="New note" hint="What to remember">
        <input type="text" defaultValue="" />
      </Field>,
    );
    expect(screen.getByLabelText("New note")).toBeInTheDocument();
    expect(screen.getByTitle("What to remember")).toBeInTheDocument();
  });
});
