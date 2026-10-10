import type { Agent, AgentMessage } from "./interface";
/** Adapters delegate model, memory and tools to the shared assistant service. */
export class AssistantClient implements Agent {
  constructor(private readonly url: string, private readonly token: string,
              private readonly fetcher: typeof fetch = fetch) {
    const parsed = new URL(url);
    if (!["127.0.0.1", "localhost", "[::1]"].includes(parsed.hostname))
      throw new Error("Assistant bridge must use localhost");
    if (token.length < 32) throw new Error("Assistant token must be at least 32 characters");
  }
  async respond(message: AgentMessage): Promise<string> {
    const response = await this.fetcher(new URL("/agent/message", this.url), {
      method: "POST",
      headers: { "content-type": "application/json", authorization: `Bearer ${this.token}` },
      body: JSON.stringify(message), signal: AbortSignal.timeout(600000), redirect: "error",
    });
    if (!response.ok) throw new Error(`Assistant turn failed (${response.status})`);
    const result: unknown = await response.json();
    if (typeof result !== "object" || result === null || !("text" in result) ||
        typeof result.text !== "string" || !result.text.trim())
      throw new Error("Assistant returned an invalid reply");
    return result.text;
  }
}
