import { describe, expect, it } from "vitest";

import { describeApiError } from "./store.svelte";

/** Stand-in for what `http.ts` throws: an Error carrying the Response. */
function apiError(status: number, body: unknown) {
  const err = new Error(`${status} Internal Server Error on /jobs`) as Error & {
    response: Response;
  };
  err.response = new Response(JSON.stringify(body), { status });
  return err;
}

describe("describeApiError", () => {
  it("prefers FastAPI's detail, which is already a whole sentence", async () => {
    const err = apiError(409, { detail: "every indicator in that run succeeded" });
    expect(await describeApiError(err)).toBe(
      "every indicator in that run succeeded",
    );
  });

  it("reads the error envelope and keeps the request id", async () => {
    // `apps.api.errors` withholds the internal message on purpose, so the
    // request id is the only thing tying this toast to the server traceback.
    const err = apiError(500, {
      error: {
        code: "internal_error",
        message: "An unexpected error occurred.",
        request_id: "abc123",
      },
    });
    expect(await describeApiError(err)).toBe(
      "An unexpected error occurred. (request abc123)",
    );
  });

  it("falls back to the status line when the body is not JSON", async () => {
    const err = new Error("503 Service Unavailable on /jobs") as Error & {
      response: Response;
    };
    err.response = new Response("<html>gateway</html>", { status: 503 });
    expect(await describeApiError(err)).toBe("503 Service Unavailable on /jobs");
  });
});
