import { readFile } from "node:fs/promises";
import { AssistantClient } from "./client";
import { IMessageInterface } from "./imessage";
import { InterfaceRunner } from "./interface";
import { outboundHandler } from "./outbound";
function required(name: string): string {
  const value = process.env[name]?.trim();
  if (!value) throw new Error(`Set ${name} before starting iMessage`);
  return value;
}
const configuredSenders = process.env.STACY_IMESSAGE_ALLOWED_SENDERS;
const local: { allowedSenders: string[] } = configuredSenders
  ? { allowedSenders: configuredSenders.split(",").map(s => s.trim()).filter(Boolean) }
  : JSON.parse(await readFile(new URL("../config.local.json", import.meta.url), "utf8"));
if (!Array.isArray(local.allowedSenders) ||
    local.allowedSenders.some(s => typeof s !== "string" || !s.trim()))
  throw new Error("allowedSenders must contain nonempty account identifiers");
const allowedSenders = new Set(local.allowedSenders);
const token = process.env.STACY_AGENT_TOKEN ?? (await readFile(
  process.env.STACY_AGENT_TOKEN_FILE ?? new URL("../../.cache/agent-token", import.meta.url),
  "utf8")).trim();
const agent = new AssistantClient(process.env.STACY_AGENT_URL ?? "http://127.0.0.1:8765", token);
const adapter = await IMessageInterface.connect({
  projectId: required("PROJECT_ID"), projectSecret: required("PROJECT_SECRET"), allowedSenders,
  owner: process.env.STACY_IMESSAGE_OWNER });
const outbound = Bun.serve({
  hostname: "127.0.0.1", port: Number(process.env.STACY_OUTBOUND_PORT ?? 8766),
  fetch: outboundHandler(token, text => adapter.sendToOwner(text)),
});
console.info("Voice-to-iMessage bridge is ready on localhost.");
try { await new InterfaceRunner(agent).run(adapter); }
finally { outbound.stop(true); }
