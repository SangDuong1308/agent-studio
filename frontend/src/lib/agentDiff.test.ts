import { describe, expect, it } from "vitest";
import type { AgentDetail } from "./api";
import { buildUpdate, formFromAgent, needsReindex } from "./agentDiff";
import { describeDetail } from "./api";

const agent: AgentDetail = {
  id: "a1",
  name: "Support Bot",
  description: "Answers questions",
  status: "ACTIVE",
  created_at: "2026-11-01T00:00:00Z",
  updated_at: "2026-11-01T00:00:00Z",
  documents: [
    { id: "d1", filename: "faq.txt", characters: 120, ingested: true, created_at: "" },
  ],
  tool_names: ["calculator"],
  last_saga_status: "COMPLETED",
  last_saga_error: "",
};

describe("buildUpdate", () => {
  it("returns null when nothing changed", () => {
    expect(buildUpdate(agent, formFromAgent(agent))).toBeNull();
  });

  it("sends only the fields that changed", () => {
    const form = { ...formFromAgent(agent), name: "Renamed" };
    expect(buildUpdate(agent, form)).toEqual({ name: "Renamed" });
  });

  it("ignores surrounding whitespace", () => {
    const form = { ...formFromAgent(agent), name: "  Support Bot  " };
    expect(buildUpdate(agent, form)).toBeNull();
  });

  it("lets a description be cleared", () => {
    const form = { ...formFromAgent(agent), description: "" };
    expect(buildUpdate(agent, form)).toEqual({ description: "" });
  });

  it("sends the full tool list when the set changes", () => {
    const form = { ...formFromAgent(agent), tools: ["calculator", "word_count"] };
    expect(buildUpdate(agent, form)).toEqual({
      tool_names: ["calculator", "word_count"],
    });
  });

  it("does not resend tools that were only reordered", () => {
    const two = { ...agent, tool_names: ["calculator", "word_count"] };
    const form = { ...formFromAgent(two), tools: ["word_count", "calculator"] };
    expect(buildUpdate(two, form)).toBeNull();
  });

  it("sends an empty list when every tool is unchecked", () => {
    const form = { ...formFromAgent(agent), tools: [] };
    expect(buildUpdate(agent, form)).toEqual({ tool_names: [] });
  });

  it("sends removed document ids", () => {
    const form = { ...formFromAgent(agent), removedDocIds: ["d1"] };
    expect(buildUpdate(agent, form)).toEqual({ remove_document_ids: ["d1"] });
  });

  it("adds a document only when there is text, defaulting the file name", () => {
    const blank = { ...formFromAgent(agent), newDocContent: "   " };
    expect(buildUpdate(agent, blank)).toBeNull();

    const form = { ...formFromAgent(agent), newDocName: " ", newDocContent: "Ships in 2 days." };
    expect(buildUpdate(agent, form)).toEqual({
      add_documents: [{ filename: "knowledge.txt", content: "Ships in 2 days." }],
    });
  });
});

describe("needsReindex", () => {
  it("is true only when a document is being added", () => {
    expect(needsReindex(null)).toBe(false);
    expect(needsReindex({ name: "x" })).toBe(false);
    expect(needsReindex({ remove_document_ids: ["d1"] })).toBe(false);
    expect(
      needsReindex({ add_documents: [{ filename: "a.txt", content: "x" }] })
    ).toBe(true);
  });
});

describe("describeDetail", () => {
  it("passes plain string errors through", () => {
    expect(describeDetail("Agent is PROVISIONING")).toBe("Agent is PROVISIONING");
  });

  it("flattens FastAPI validation errors", () => {
    expect(
      describeDetail([{ msg: "Value error, unknown tool(s): rm_rf. Available: calculator" }])
    ).toBe("unknown tool(s): rm_rf. Available: calculator");
  });

  it("returns undefined for anything else", () => {
    expect(describeDetail(undefined)).toBeUndefined();
    expect(describeDetail({})).toBeUndefined();
  });
});
