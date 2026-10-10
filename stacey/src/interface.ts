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
        const previous = this.queues.get(thread) ?? Promise.resolve();
        const current = previous.then(async () => {
          const respond = async () => {
          let reply: string;
          try { reply = await this.agent.respond(message); }
          catch {
            reply = "I couldn’t finish that request. Some actions may have completed—check their state before trying again.";
          }
          // Delivery failure must not repeat an agent turn or retry a send blindly.
          await message.reply(reply);
          };
          if (message.withActivity) await message.withActivity(respond);
          else await respond();
        }).catch(() => {
          console.error("Reply delivery failed; not automatically retrying.");
        }).finally(() => {
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
