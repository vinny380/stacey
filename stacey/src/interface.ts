import { createHash } from "node:crypto";

export interface AgentMessage {
  channel: string;
  conversation_id: string;
  sender_id: string;
  message_id: string;
  text: string;
}
export interface Agent { respond(message: AgentMessage): Promise<string>; }
export interface IncomingMessage extends AgentMessage {
  reply(text: string): Promise<void>;
  /** Optional native read/typing lifecycle, entered when this queued turn starts. */
  withActivity?(work: () => Promise<void>): Promise<void>;
}
export interface AgentInterface {
  readonly channel: string;
  messages(): AsyncIterable<IncomingMessage>;
  stop(): Promise<void>;
}
/** Transport-independent turn ordering and process-local duplicate protection. */
export class InterfaceRunner {
  private readonly queues = new Map<string, Promise<void>>();
  private readonly seen = new Set<string>();
  constructor(private readonly agent: Agent) {}
  async run(adapter: AgentInterface): Promise<void> {
    try {
      for await (const message of adapter.messages()) {
        const thread = JSON.stringify([message.channel, message.conversation_id]);
        const id = JSON.stringify([thread, message.sender_id, message.message_id]);
        if (this.seen.has(id)) continue;
        this.seen.add(id);
        if (this.seen.size > 10000) this.seen.delete(this.seen.values().next().value!);
        const receivedAt = performance.now();
        const messageRef = createHash("sha256").update(message.message_id).digest("hex").slice(0, 16);
        const trace = (event: string, fields: Record<string, unknown> = {}) => {
          console.info(JSON.stringify({ time: new Date().toISOString(), event,
            message_ref: messageRef, channel: message.channel, ...fields }));
        };
        trace("channel_received");
        const previous = this.queues.get(thread) ?? Promise.resolve();
        const current = previous.then(async () => {
          trace("channel_queue_wait", { seconds: (performance.now() - receivedAt) / 1000 });
          const respond = async () => {
          let reply: string;
          const backendAt = performance.now();
          try {
            reply = await this.agent.respond(message);
            trace("channel_backend", { seconds: (performance.now() - backendAt) / 1000,
              status: "complete" });
          }
          catch {
            trace("channel_backend", { seconds: (performance.now() - backendAt) / 1000,
              status: "failed" });
            reply = "I couldn’t finish that request. Some actions may have completed—check their state before trying again.";
          }
          // Delivery failure must not repeat an agent turn or retry a send blindly.
          const deliveryAt = performance.now();
          try {
            await message.reply(reply);
            trace("channel_send", { seconds: (performance.now() - deliveryAt) / 1000,
              status: "acknowledged" });
          } catch (error) {
            trace("channel_send", { seconds: (performance.now() - deliveryAt) / 1000,
              status: "failed" });
            throw error;
          }
          };
          if (message.withActivity) await message.withActivity(respond);
          else await respond();
        }).catch(() => {
          console.error("Reply delivery failed; not automatically retrying.");
        }).finally(() => {
          trace("channel_turn", { seconds: (performance.now() - receivedAt) / 1000 });
          if (this.queues.get(thread) === current) this.queues.delete(thread);
        });
        this.queues.set(thread, current);
      }
    } finally {
      await Promise.all(this.queues.values());
      await adapter.stop();
    }
  }
}
