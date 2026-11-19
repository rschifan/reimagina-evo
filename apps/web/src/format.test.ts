import { describe, expect, it } from "vitest";

import { escapeHtml, fmtNumber, formatValue, grainCount, grainNoun, shareText, unitLabel } from "./format";

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

  it("uses up to 2dp for 1 <= |n| < 100, without padding", () => {
    expect(fmtNumber(12.345)).toBe((12.345).toLocaleString(undefined, { maximumFractionDigits: 2 }));
    expect(fmtNumber(12.5)).toBe("12.5");
  });

  it("uses up to 3dp for sub-unit magnitudes, without padding", () => {
    expect(fmtNumber(0.12345)).toBe("0.123");
    expect(fmtNumber(0.41)).toBe("0.41");
  });

  it("treats negatives by absolute magnitude", () => {
    expect(fmtNumber(-0.5)).toBe("-0.5");
  });

  it("is the digit rule formatValue prints with", () => {
    for (const n of [12.5, 0.41, 1234.5, 3]) {
      expect(formatValue(n, { unit: null })).toBe(fmtNumber(n));
    }
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


describe("shareText", () => {
  it("says 0 and 100 only when they are exact", () => {
    expect(shareText(0, 300)).toBe("0");
    expect(shareText(300, 300)).toBe("100");
  });

  it("never rounds a few to none or almost all to all", () => {
    expect(shareText(1, 300)).toBe("<1");
    expect(shareText(1, 200)).toBe("<1");
    expect(shareText(199, 200)).toBe(">99");
    expect(shareText(19057, 19058)).toBe(">99");
  });

  it("rounds everything in between", () => {
    expect(shareText(3, 300)).toBe("1");
    expect(shareText(124, 200)).toBe("62");
    expect(shareText(297, 300)).toBe("99");
  });
});


describe("grainNoun", () => {
  it("is the grain word in the singular and its lower-cased label in the plural", () => {
    expect(grainNoun("block", false)).toBe("block");
    expect(grainNoun("block", true)).toBe("blocks");
    expect(grainNoun("municipality", true)).toBe("municipalities");
    expect(grainNoun(null, false)).toBe("feature");
    expect(grainNoun(null, true)).toBe("features");
  });
});


describe("grainCount", () => {
  it("counts one in the singular and more in the plural", () => {
    expect(grainCount(1, "block")).toBe("1 block");
    expect(grainCount(2, "block")).toBe("2 blocks");
    expect(grainCount(1, "municipality")).toBe("1 municipality");
    expect(grainCount(19058, "block")).toBe(`${(19058).toLocaleString()} blocks`);
  });

  it("falls back to features for no grain", () => {
    expect(grainCount(1, null)).toBe("1 feature");
    expect(grainCount(2, null)).toBe("2 features");
  });
});
