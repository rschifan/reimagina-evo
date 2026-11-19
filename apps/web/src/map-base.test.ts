import { describe, expect, it, vi } from "vitest";

import { mapErrorBanner } from "./map-base";

describe("mapErrorBanner", () => {
  it("keeps the error through the idle that ends the failing cycle", () => {
    const show = vi.fn(), hide = vi.fn();
    const banner = mapErrorBanner(show, hide);
    banner.error("tile timed out");
    banner.idle();
    expect(show).toHaveBeenCalledWith("tile timed out");
    expect(hide).not.toHaveBeenCalled();
  });

  it("clears it on the first idle after a cycle with no error", () => {
    const show = vi.fn(), hide = vi.fn();
    const banner = mapErrorBanner(show, hide);
    banner.error("tile timed out");
    banner.idle();
    banner.idle();
    expect(hide).toHaveBeenCalledExactlyOnceWith("tile timed out");
  });

  it("keeps it while errors keep coming", () => {
    const show = vi.fn(), hide = vi.fn();
    const banner = mapErrorBanner(show, hide);
    banner.error("a");
    banner.idle();
    banner.error("b");
    banner.idle();
    expect(hide).not.toHaveBeenCalled();
    banner.idle();
    expect(hide).toHaveBeenCalledExactlyOnceWith("b");
  });

  it("hides nothing when nothing was shown", () => {
    const hide = vi.fn();
    const banner = mapErrorBanner(vi.fn(), hide);
    banner.idle();
    banner.idle();
    expect(hide).not.toHaveBeenCalled();
  });
});
