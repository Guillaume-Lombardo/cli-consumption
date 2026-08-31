import "server-only";

export type BoundedJsonResult =
  | { body: Record<string, unknown>; bytes: Uint8Array<ArrayBuffer>; status: "ok" }
  | { status: "invalid" }
  | { status: "too_large" };

/** Read an untrusted JSON object without ever buffering more than the declared limit. */
export async function readBoundedJsonObject(
  request: Request,
  limit: number,
): Promise<BoundedJsonResult> {
  const contentLength = request.headers.get("content-length");
  if (contentLength !== null) {
    if (!/^\d+$/.test(contentLength)) return { status: "invalid" };
    const declared = Number(contentLength);
    if (!Number.isSafeInteger(declared)) return { status: "invalid" };
    if (declared > limit) return { status: "too_large" };
  }
  if (!request.body) return { status: "invalid" };

  const reader = request.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      total += value.byteLength;
      if (total > limit) {
        await reader.cancel().catch(() => undefined);
        return { status: "too_large" };
      }
      chunks.push(value);
    }
  } catch {
    return { status: "invalid" };
  }

  const bytes = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    bytes.set(chunk, offset);
    offset += chunk.byteLength;
  }
  try {
    const parsed = JSON.parse(
      new TextDecoder("utf-8", { fatal: true }).decode(bytes),
    ) as unknown;
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
      return { status: "invalid" };
    }
    return { body: parsed as Record<string, unknown>, bytes, status: "ok" };
  } catch {
    return { status: "invalid" };
  }
}
