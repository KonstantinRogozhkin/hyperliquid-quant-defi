import { describe, expect, it } from "vitest";
import { ruPlural } from "../i18n";

describe("ruPlural", () => {
  it.each([
    [1, "тик"],
    [2, "тика"],
    [4, "тика"],
    [5, "тиков"],
    [11, "тиков"],
    [12, "тиков"],
    [21, "тик"],
    [22, "тика"],
    [111, "тиков"],
    [0, "тиков"],
  ])("%i → %s", (n, word) => {
    expect(ruPlural(n, "тик", "тика", "тиков")).toBe(word);
  });
});
