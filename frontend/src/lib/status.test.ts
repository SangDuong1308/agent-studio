import { describe, expect, it } from "vitest";
import { canChat, statusLabel } from "./status";

describe("status helpers", () => {
  it("labels each status", () => {
    expect(statusLabel("PROVISIONING")).toBe("Provisioning…");
    expect(statusLabel("ACTIVE")).toBe("Ready to chat");
    expect(statusLabel("FAILED")).toBe("Provisioning failed");
    expect(statusLabel("WEIRD")).toBe("Unknown");
  });

  it("only allows chat when active", () => {
    expect(canChat("ACTIVE")).toBe(true);
    expect(canChat("PROVISIONING")).toBe(false);
    expect(canChat("FAILED")).toBe(false);
  });
});
