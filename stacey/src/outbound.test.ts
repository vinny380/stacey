import { expect, test } from "bun:test";
import { outboundHandler } from "./outbound";
const token = "x".repeat(32);
function request(id = "one", text = "Connect calendar: https://auth.example/link", auth = true) {
  return new Request("http://127.0.0.1:8766/send-text", { method: "POST",
    headers: { authorization: auth ? `Bearer ${token}` : "wrong" },
    body: JSON.stringify({ request_id: id, text }) });
}
test("outbound authenticates, coalesces redelivery and checks payload identity", async () => {
  let sends = 0;
  const handler = outboundHandler(token, async text => {
    sends++; expect(text).toContain("https://auth.example/link");
    return { message_id: "provider-id" };
  });
  expect((await handler(request("one", "ignored", false))).status).toBe(401);
  const responses = await Promise.all([handler(request()), handler(request())]);
  for (const response of responses)
    expect(await response.json()).toEqual({ status: "sent", message_id: "provider-id" });
  expect((await handler(request("one", "different"))).status).toBe(409);
  expect(sends).toBe(1);
});
test("an uncertain delivery never gets retried on HTTP redelivery", async () => {
  let sends = 0;
  const handler = outboundHandler(token, async () => { sends++; throw new Error("lost ack"); });
  expect((await handler(request())).status).toBe(502);
  expect((await handler(request())).status).toBe(502);
  expect(sends).toBe(1);
});
