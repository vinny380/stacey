import { timingSafeEqual } from "node:crypto";

export type SendText = (text: string) => Promise<{ message_id: string }>;

/** Local authenticated bridge. A request is sent once even if its HTTP ack is lost. */
export function outboundHandler(token: string, send: SendText) {
  if (token.length < 32) throw new Error("Outbound token must be at least 32 characters");
  const expected = Buffer.from(`Bearer ${token}`);
  const requests = new Map<string, { text: string; result: Promise<{ status: number; body: object }> }>();
  return async (request: Request): Promise<Response> => {
    const supplied = Buffer.from(request.headers.get("authorization") ?? "");
    if (supplied.length !== expected.length || !timingSafeEqual(supplied, expected))
      return Response.json({ error: "Unauthorized" }, { status: 401 });
    const path = new URL(request.url).pathname;
    if (path === "/health" && request.method === "GET")
      return Response.json({ ready: true });
    if (path !== "/send-text" || request.method !== "POST")
      return new Response("Not found", { status: 404 });
    let body: { request_id: string; text: string };
    try {
      const raw = await request.text();
      if (raw.length > 64000) throw new Error("Too large");
      body = JSON.parse(raw);
      if (!body || typeof body.request_id !== "string" || !/^[a-zA-Z0-9-]{1,100}$/.test(body.request_id) ||
          typeof body.text !== "string" || !body.text.trim() || body.text.length > 12000)
        throw new Error("Invalid message");
    } catch { return Response.json({ error: "Invalid message" }, { status: 400 }); }
    const previous = requests.get(body.request_id);
    if (previous) {
      if (previous.text !== body.text)
        return Response.json({ error: "Request ID already used" }, { status: 409 });
      const cached = await previous.result;
      return Response.json(cached.body, { status: cached.status });
    }
    // Keep failures as well as successes: an uncertain result must not repeat a send.
    const result = (async () => {
      try {
        const sent = await send(body.text);
        if (!sent.message_id) throw new Error("No provider acknowledgement");
        return { status: 200, body: { status: "sent", message_id: sent.message_id } };
      } catch {
        return { status: 502, body: { error: "Send uncertain; do not retry automatically" } };
      }
    })();
    requests.set(body.request_id, { text: body.text, result });
    const response = await result;
    return Response.json(response.body, { status: response.status });
  };
}
