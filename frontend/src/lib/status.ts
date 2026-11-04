// Small pure helper, unit-tested to show the FE test setup works.
export function statusLabel(status: string): string {
  switch (status) {
    case "PROVISIONING":
      return "Provisioning…";
    case "ACTIVE":
      return "Ready to chat";
    case "FAILED":
      return "Provisioning failed";
    default:
      return "Unknown";
  }
}

export function canChat(status: string): boolean {
  return status === "ACTIVE";
}
