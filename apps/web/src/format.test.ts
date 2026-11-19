import { describe, expect, it } from "vitest";

import { escapeHtml, fmtNumber, formatValue, unitLabel } from "./format";

describe("fmtNumber", () => {
  it("passes integers through with thousands separators", () => {
    expect(fmtNumber(1234567)).toBe((1234567).toLocaleString());
  });

  it("rounds to 0dp when |n| >= 100", () => {
    expect(fmtNumber(123.456)).toBe((123.456).toLocaleString(undefined, {
      minimumFractionDigits: 0,
      maximumFractionDigits: 0,
    }));
  });

  it("uses 2dp for 1 <= |n| < 100", () => {
    expect(fmtNumber(12.345)).toBe((12.345).toLocaleString(undefined, {
      minimumFractionDigits: 2,
      maximumFractionDigits: 2,
    }));
  });

  it("uses 3dp for sub-unit magnitudes", () => {
    expect(fmtNumber(0.12345)).toBe((0.12345).toLocaleString(undefined, {
      minimumFractionDigits: 3,
      maximumFractionDigits: 3,
    }));
  });

  it("treats negatives by absolute magnitude", () => {
    expect(fmtNumber(-0.5)).toBe((-0.5).toLocaleString(undefined, {
      minimumFractionDigits: 3,
      maximumFractionDigits: 3,
    }));
  });
});

describe("escapeHtml", () => {
  it("escapes the five HTML-significant characters", () => {
    expect(escapeHtml(`<a href="x">&'</a>`)).toBe(
      "&lt;a href=&quot;x&quot;&gt;&amp;&#39;&lt;/a&gt;",
    );
  });

  it("escapes & before <, > so existing entities don't double-escape", () => {
    expect(escapeHtml("&amp;")).toBe("&amp;amp;");
  });

  it("returns identity for plain text", () => {
    expect(escapeHtml("hello world")).toBe("hello world");
  });

  it("handles empty input", () => {
    expect(escapeHtml("")).toBe("");
  });
});

describe("unitLabel", () => {
  it("reads the catalog's unit codes as words", () => {
    expect(unitLabel("buildings_per_ha")).toBe("buildings / ha");
    expect(unitLabel("ratio_0_1")).toBe("ratio 0–1");
    expect(unitLabel("score_0_1")).toBe("score 0–1");
    expect(unitLabel("m")).toBe("m");
    expect(unitLabel(null)).toBe("unitless");
  });
});

describe("formatValue", () => {
  const culture = { unit: "m", cap_value: 1500 };
  const livability = { unit: "score_0_1", cap_value: null };

  it("names a value at the cap instead of passing it off as a measurement", () => {
    expect(formatValue(1500, culture)).toBe("1,500 m+ (cap)");
  });

  it("reads a value past the cap as the cap", () => {
    expect(formatValue(1600, culture)).toBe("1,500 m+ (cap)");
  });

  it("reads a value under the cap as a plain distance", () => {
    expect(formatValue(1499, culture)).toBe("1,499 m");
  });

  it("drops the scale of a 0-1 score rather than reading it as a unit", () => {
    expect(formatValue(0.41, livability)).toBe("0.41");
  });

  it("writes unit codes as words", () => {
    expect(formatValue(12.5, { unit: "units_per_ha", cap_value: null })).toBe("12.5 units / ha");
  });

  it("never treats a value as capped when the indicator declares no cap", () => {
    expect(formatValue(1500, { unit: "m", cap_value: null })).toBe("1,500 m");
  });

  it("shows a unitless value bare", () => {
    expect(formatValue(3, { unit: null, cap_value: null })).toBe("3");
  });
});
