import { describe, expect, it } from "vitest";

import { escapeHtml, fmtNumber } from "./format";

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
