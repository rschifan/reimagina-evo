import { describe, expect, it } from "vitest";
import { sheetTop } from "./sheet";

const off = { inspector: false, selection: false };

describe("sheetTop", () => {
  it("shows the card that just opened", () => {
    expect(sheetTop(null, off, { ...off, inspector: true })).toBe("inspector");
    expect(sheetTop("inspector", { ...off, inspector: true }, { inspector: true, selection: true })).toBe("selection");
  });
  it("keeps the top card while both stay open", () => {
    const both = { inspector: true, selection: true };
    expect(sheetTop("selection", both, both)).toBe("selection");
  });
  it("falls back to the other card when the top closes", () => {
    const both = { inspector: true, selection: true };
    expect(sheetTop("selection", both, { inspector: true, selection: false })).toBe("inspector");
    expect(sheetTop("inspector", { ...off, inspector: true }, off)).toBeNull();
  });
});
