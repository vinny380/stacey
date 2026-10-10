import { expect, test } from "bun:test";
import type { Message, Space } from "spectrum-ts";
import { InterfaceRunner, type IncomingMessage } from "./interface";
import { SpectrumInterface } from "./spectrum-interface";
import { AssistantClient } from "./client";

test("turns run in order and duplicate IDs are ignored", async () => {
  const events: string[] = [];
  const incoming = (id: string): IncomingMessage => ({
    channel: "imessage", conversation_id: "dm", sender_id: "owner", message_id: id, text: id,
    reply: async text => { events.push(`send:${text}`); },
  });
  await new InterfaceRunner({ async respond(m) {
    events.push(`run:${m.message_id}`); await Promise.resolve(); return m.text;
  } }).run({ channel: "imessage", async *messages() {
    yield incoming("1"); yield incoming("1"); yield incoming("2");
  }, async stop() { events.push("stop"); } });
  expect(events).toEqual(["run:1", "send:1", "run:2", "send:2", "stop"]);
});

test("strangers, groups, outbound echoes and reactions are filtered", async () => {
  const space = { id: "dm", type: "dm", async send() {} } as unknown as Space;
  const make = (id: string, extra = {}): Message => ({ id, platform: "imessage",
    direction: "inbound", sender: { id: "owner" }, content: { type: "text", text: "hi" },
    ...extra } as unknown as Message);
  const connection = { async *messages(): AsyncIterable<[Space, Message]> {
    yield [space, make("ok")];
    yield [space, make("echo", { direction: "outbound" })];
    yield [space, make("stranger", { sender: { id: "other" } })];
    yield [space, make("unknown", { sender: undefined })];
    yield [space, make("reaction", { content: { type: "reaction" } })];
    yield [{ ...space, type: "group" } as unknown as Space, make("group")];
  }, async stop() {} };
  const adapter = new SpectrumInterface("imessage", {
    messages: connection.messages(), stop: connection.stop,
  }, new Set(["owner"]),
    s => "type" in s && s.type === "dm");
  const messages = [];
  for await (const m of adapter.messages()) messages.push(m.message_id);
  expect(messages).toEqual(["ok"]);
});

test("failed delivery does not rerun actions or retry the send", async () => {
  let calls = 0, sends = 0;
  await new InterfaceRunner({ async respond() { calls++; return "done"; } }).run({
    channel: "test", async *messages() {
      yield { channel: "test", conversation_id: "dm", sender_id: "owner", message_id: "1",
        text: "do it", async reply() { sends++; throw new Error("lost ack"); } };
    }, async stop() {},
  });
  expect(calls).toBe(1); expect(sends).toBe(1);
});

test("client authenticates and does not retry failed turns", async () => {
  let calls = 0;
  const fetcher = (async (_url: unknown, init: RequestInit) => {
    calls++;
    expect((init.headers as Record<string, string>).authorization).toBe(`Bearer ${"x".repeat(32)}`);
    return new Response("error", { status: 500 });
  }) as typeof fetch;
  const client = new AssistantClient("http://127.0.0.1:8765", "x".repeat(32), fetcher);
  await expect(client.respond({ channel: "imessage", conversation_id: "dm", sender_id: "owner",
    message_id: "1", text: "hi" })).rejects.toThrow("500");
  expect(calls).toBe(1);
});

test("read and typing surround the response, even when delivery fails", async () => {
  const events: string[] = [];
  const space = { id: "dm", type: "dm",
    async startTyping() { events.push("typing:start"); },
    async stopTyping() { events.push("typing:stop"); },
    async send() { events.push("send"); throw new Error("delivery failed"); },
  } as unknown as Space;
  const message = { id: "1", platform: "imessage", direction: "inbound",
    sender: { id: "owner" }, content: { type: "text", text: "hello" },
    async read() { events.push("read"); },
  } as unknown as Message;
  await new InterfaceRunner({ async respond() { events.push("agent"); return "hello"; } })
    .run(new SpectrumInterface("imessage", {
      messages: (async function* (): AsyncIterable<[Space, Message]> { yield [space, message]; })(),
      async stop() {},
    }, new Set(["owner"]), () => true));
  expect(events).toEqual(["read", "typing:start", "agent", "send", "typing:stop"]);
});

test("status failures do not prevent the request or cause another agent call", async () => {
  let calls = 0, sends = 0, stops = 0;
  const space = { id: "dm", type: "dm",
    async startTyping() { throw new Error("offline"); },
    async stopTyping() { stops++; throw new Error("offline"); },
    async send() { sends++; },
  } as unknown as Space;
  const message = { id: "1", platform: "imessage", direction: "inbound",
    sender: { id: "owner" }, content: { type: "text", text: "hello" },
    async read() { throw new Error("offline"); },
  } as unknown as Message;
  await new InterfaceRunner({ async respond() { calls++; return "hello"; } })
    .run(new SpectrumInterface("imessage", {
      messages: (async function* (): AsyncIterable<[Space, Message]> { yield [space, message]; })(),
      async stop() {},
    }, new Set(["owner"]), () => true));
  expect(calls).toBe(1); expect(sends).toBe(1); expect(stops).toBe(1);
});
