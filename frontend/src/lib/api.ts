// Typed client for the gateway REST API.
// All server data flows through here; React Query wraps these calls for
// caching, polling, and request state.

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8001";

export interface Agent {
  id: string;
  name: string;
  description: string;
  status: "PROVISIONING" | "ACTIVE" | "FAILED";
  created_at: string;
  updated_at: string;
}

/** A document attached to an agent. The text itself is never sent back. */
export interface DocumentInfo {
  id: string;
  filename: string;
  characters: number;
  ingested: boolean;
  created_at: string;
}

/** One agent with what the edit screen needs. */
export interface AgentDetail extends Agent {
  documents: DocumentInfo[];
  tool_names: string[];
  /** Outcome of the most recent saga. FAILED on an ACTIVE agent = last edit rolled back. */
  last_saga_status: "RUNNING" | "COMPENSATING" | "COMPLETED" | "FAILED" | null;
  last_saga_error: string;
}

/** Partial update: anything left out stays unchanged. `tool_names` is the full new list. */
export interface AgentUpdate {
  name?: string;
  description?: string;
  tool_names?: string[];
  add_documents?: { filename: string; content: string }[];
  remove_document_ids?: string[];
}

export interface Page<T> {
  items: T[];
  total: number;
  limit: number;
  offset: number;
}

/** A document passage an answer was grounded in. */
export interface Source {
  filename: string;
  chunk_index: number;
  snippet: string;
  /** Cosine similarity, 0-1. Higher is more relevant. */
  score: number | null;
}

export interface QueryResult {
  answer: string;
  sources: Source[];
}

/** One stored message of an agent's conversation. */
export interface ChatMessage {
  id: string;
  role: "user" | "agent";
  content: string;
  /** Retrieved passages (agent messages only; empty for the user's). */
  sources: Source[];
  created_at: string;
}

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

type UnauthorizedHandler = () => void;
let onUnauthorized: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(fn: UnauthorizedHandler | null): void {
  onUnauthorized = fn;
}

/**
 * FastAPI sends `detail` as a string for our own errors, but as a list of
 * `{msg, loc}` objects for request-validation errors (422). Show both readably.
 */
export function describeDetail(detail: unknown): string | undefined {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    const msgs = detail
      .map((d) => (d && typeof d.msg === "string" ? d.msg.replace(/^Value error, /, "") : null))
      .filter((m): m is string => !!m);
    if (msgs.length) return msgs.join("; ");
  }
  return undefined;
}

async function request<T>(
  path: string,
  options: RequestInit & { token?: string } = {}
): Promise<T> {
  const { token, headers, ...rest } = options;
  const res = await fetch(`${API_URL}${path}`, {
    ...rest,
    headers: {
      "Content-Type": "application/json",
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
      ...headers,
    },
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = describeDetail(body.detail) ?? detail;
    } catch {
      /* ignore */
    }
    // An expired/invalid token on an authenticated call: notify the global
    // handler so the app logs out + redirects, instead of surfacing a
    // misleading "failed to load" error to the user.
    if (res.status === 401 && token && onUnauthorized) {
      onUnauthorized();
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return res.json();
}

export const api = {
  async register(email: string, password: string) {
    return request("/v1/auth/register", {
      method: "POST",
      body: JSON.stringify({ email, password }),
    });
  },

  async login(email: string, password: string): Promise<string> {
    // OAuth2 password flow expects form-encoded data.
    const form = new URLSearchParams({ username: email, password });
    const res = await fetch(`${API_URL}/v1/auth/login`, {
      method: "POST",
      headers: { "Content-Type": "application/x-www-form-urlencoded" },
      body: form,
    });
    if (!res.ok) throw new ApiError(res.status, "Login failed");
    const data = await res.json();
    return data.access_token as string;
  },

  async listAgents(token: string, limit = 20, offset = 0): Promise<Page<Agent>> {
    return request(`/v1/agents?limit=${limit}&offset=${offset}`, { token });
  },

  async getAgent(token: string, id: string): Promise<AgentDetail> {
    return request(`/v1/agents/${id}`, { token });
  },

  async updateAgent(
    token: string,
    id: string,
    body: AgentUpdate
  ): Promise<AgentDetail> {
    return request(`/v1/agents/${id}`, {
      method: "PATCH",
      token,
      body: JSON.stringify(body),
    });
  },

  async createAgent(
    token: string,
    body: {
      name: string;
      description: string;
      documents: { filename: string; content: string }[];
      tool_names: string[];
    }
  ): Promise<Agent> {
    return request("/v1/agents", {
      method: "POST",
      token,
      body: JSON.stringify(body),
    });
  },

  async deleteAgent(token: string, id: string): Promise<void> {
    return request(`/v1/agents/${id}`, { method: "DELETE", token });
  },

  /** The saved conversation, oldest first. */
  async getMessages(token: string, id: string): Promise<ChatMessage[]> {
    return request(`/v1/agents/${id}/messages`, { token });
  },

  /** Start a fresh conversation (the agent forgets the old one). */
  async clearMessages(token: string, id: string): Promise<void> {
    return request(`/v1/agents/${id}/messages`, { method: "DELETE", token });
  },

  async queryAgent(
    token: string,
    id: string,
    question: string
  ): Promise<QueryResult> {
    return request(`/v1/agents/${id}/query`, {
      method: "POST",
      token,
      body: JSON.stringify({ question }),
    });
  },
};
