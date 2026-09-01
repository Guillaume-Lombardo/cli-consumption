import { describe, expect, it } from "vitest";

import { readBoundedJsonObject } from "./body";

function streamedRequest(chunks: Uint8Array[], contentLength?: string): Request {
  const stream = new ReadableStream<Uint8Array>({
    pull(controller) {
      const chunk = chunks.shift();
      if (chunk) controller.enqueue(chunk);
      else controller.close();
    },
  });
  return new Request("https://dashboard.example/api", {
    body: stream,
    duplex: "half",
    headers: contentLength ? { "Content-Length": contentLength } : undefined,
    method: "POST",
  } as RequestInit & { duplex: "half" });
}

describe("bounded request bodies", () => {
  it("accepts a strict JSON object and preserves its bytes", async () => {
    const bytes = new TextEncoder().encode('{"version":1}');
    const result = await readBoundedJsonObject(streamedRequest([bytes]), 1024);

    expect(result.status).toBe("ok");
    if (result.status === "ok") {
      expect(result.body).toEqual({ version: 1 });
      expect(new TextDecoder().decode(result.bytes)).toBe('{"version":1}');
    }
  });

  it("stops a chunked body as soon as it crosses the bound", async () => {
    const result = await readBoundedJsonObject(
      streamedRequest([new Uint8Array(6), new Uint8Array(6)]),
      8,
    );
    expect(result).toEqual({ status: "too_large" });
  });

  it("rejects invalid lengths, malformed UTF-8, and non-object JSON", async () => {
    expect(
      await readBoundedJsonObject(streamedRequest([new Uint8Array()], "-1"), 8),
    ).toEqual({ status: "invalid" });
    expect(
      await readBoundedJsonObject(streamedRequest([Uint8Array.from([0xff])]), 8),
    ).toEqual({ status: "invalid" });
    expect(
      await readBoundedJsonObject(streamedRequest([new TextEncoder().encode("[]")]), 8),
    ).toEqual({ status: "invalid" });
  });
});
