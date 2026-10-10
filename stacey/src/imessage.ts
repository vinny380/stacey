import { Spectrum } from "spectrum-ts";
import { imessage } from "spectrum-ts/providers/imessage";
import { SpectrumInterface } from "./spectrum-interface";
export class IMessageInterface extends SpectrumInterface {
  private sendOwner?: (text: string) => Promise<{ message_id: string }>;
  async sendToOwner(text: string): Promise<{ message_id: string }> {
    if (!this.sendOwner) throw new Error("Owner outreach is not configured");
    return this.sendOwner(text);
  }
  static async connect(options: {
    projectId: string; projectSecret: string; allowedSenders: ReadonlySet<string>; owner?: string;
  }): Promise<IMessageInterface> {
    if (!options.allowedSenders.size) throw new Error("Configure allowed iMessage senders");
    const app = await Spectrum({ projectId: options.projectId, projectSecret: options.projectSecret,
      providers: [imessage.config()] });
    const adapter = new IMessageInterface("imessage", app, options.allowedSenders,
      (space) => "type" in space && space.type === "dm");
    const phones = [...options.allowedSenders].filter(id => /^\+[1-9]\d{6,14}$/.test(id));
    const owner = options.owner ?? (phones.length === 1 ? phones[0] :
      options.allowedSenders.size === 1 ? [...options.allowedSenders][0] : undefined);
    if (!owner || !options.allowedSenders.has(owner)) {
      await app.stop();
      throw new Error("Choose STACY_IMESSAGE_OWNER from the configured allowed senders");
    }
    const im = imessage(app);
    adapter.sendOwner = async text => {
      const dm = await im.space.create(await im.user(owner));
      const result = await dm.send(text);
      const messages = Array.isArray(result) ? result : [result];
      const sent = messages.find(message => message?.id);
      if (!sent?.id) throw new Error("iMessage provider did not acknowledge the send");
      return { message_id: sent.id };
    };
    return adapter;
  }
}
