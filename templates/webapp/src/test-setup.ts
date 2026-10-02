// Vitest's setup: the jest-dom matchers, and a cleanup after every test so one
// test's DOM never leaks into the next.
import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
});
