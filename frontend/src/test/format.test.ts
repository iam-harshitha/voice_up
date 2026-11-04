import { describe, expect, it } from "vitest";
import { eventLabel, eventTone } from "../lib/format";

describe("audit event labels for text approvals", () => {
  it("labels the text and channel events", () => {
    expect(eventLabel("text.prompt_sent")).toBe("Telegram message sent");
    expect(eventLabel("text.decision_accepted")).toBe("Telegram decision accepted");
    expect(eventLabel("text.decision_rejected")).toBe("Telegram tap not accepted");
    expect(eventLabel("channel.link_created")).toBe("Telegram account linked");
    expect(eventLabel("voice.fallback_expedited")).toBe("Text not delivered; fallback call started now");
  });

  it("marks refusals as warnings and the rest as information", () => {
    expect(eventTone("text.decision_rejected")).toBe("warn");
    expect(eventTone("text.prompt_failed")).toBe("warn");
    expect(eventTone("channel.link_rejected")).toBe("warn");
    expect(eventTone("text.prompt_sent")).toBe("info");
    expect(eventTone("channel.link_created")).toBe("info");
  });

  it("still falls back to a readable label for unknown events", () => {
    expect(eventLabel("future.thing_happened")).toBe("future thing happened");
  });
});
