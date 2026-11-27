import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

afterEach(() => {
  cleanup();
  // Language preference is persisted; keep every test starting from the browser default (en in jsdom).
  try {
    window.localStorage.clear();
  } catch {
    // ignore
  }
});
