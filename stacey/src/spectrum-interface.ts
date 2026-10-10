import type { Message, Space } from "spectrum-ts";
import type { AgentInterface, IncomingMessage } from "./interface";
export interface SpectrumConnection {
  messages: AsyncIterable<[Space, Message]>;
  stop(): Promise<void>;
}
/** Extend for another Spectrum provider and its access policy. */
export class SpectrumInterface implements AgentInterface {
  constructor(readonly channel: string, protected readonly connection: SpectrumConnection,
              private readonly allowedSenders: ReadonlySet<string>,
              private readonly allowedSpace: (space: Space) => boolean) {
    if (!allowedSenders.size) throw new Error("Configure at least one allowed sender");
  }
  async *messages(): AsyncIterable<IncomingMessage> {
    for await (const [space, message] of this.connection.messages) {
      if (message.platform !== this.channel || message.direction !== "inbound" ||
          !message.sender || !this.allowedSenders.has(message.sender.id) ||
          !this.allowedSpace(space)) continue;
      if (message.content.type !== "text") {
        if (["attachment", "voice"].includes(message.content.type))
          await space.send("I can handle text here for now. Please send your request as a message.");
        continue;
      }
      if (!message.content.text.trim()) continue;
      yield {
        channel: this.channel,
        conversation_id: JSON.stringify([space.id, "phone" in space ? space.phone : null]),
        sender_id: message.sender.id, message_id: message.id, text: message.content.text,
        reply: async (text: string) => { await space.send(text); },
        withActivity: async (work: () => Promise<void>) => {
          const signal = async (operation: () => Promise<unknown>, label: string) => {
            try { await operation(); }
            catch { console.warn(`Could not send ${label}; continuing the conversation.`); }
          };
          await signal(() => message.read(), "read receipt");
          try {
            await signal(() => space.startTyping(), "typing indicator");
            await work();
          } finally {
            await signal(() => space.stopTyping(), "typing stop");
          }
        },
      };
    }
  }
  async stop(): Promise<void> { await this.connection.stop(); }
}
