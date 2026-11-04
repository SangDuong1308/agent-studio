// "What did the user actually change?" for the edit form.
//
// The edit page keeps the form in local state. On save we compare it with the
// agent as it was loaded and send ONLY the differences (a PATCH). Keeping this
// as a pure function means the rule is easy to read and easy to unit-test.

import type { AgentDetail, AgentUpdate } from "./api";

export interface EditForm {
  name: string;
  description: string;
  tools: string[];
  /** Ids of existing documents the user marked for removal. */
  removedDocIds: string[];
  /** Optional new document to add. Ignored while the text is blank. */
  newDocName: string;
  newDocContent: string;
}

export const DEFAULT_DOC_NAME = "knowledge.txt";

export function formFromAgent(agent: AgentDetail): EditForm {
  return {
    name: agent.name,
    description: agent.description,
    tools: [...agent.tool_names],
    removedDocIds: [],
    newDocName: DEFAULT_DOC_NAME,
    newDocContent: "",
  };
}

function sameSet(a: string[], b: string[]): boolean {
  return a.length === b.length && a.every((x) => b.includes(x));
}

/** The PATCH body for this form, or null when nothing changed. */
export function buildUpdate(
  original: AgentDetail,
  form: EditForm
): AgentUpdate | null {
  const update: AgentUpdate = {};

  const name = form.name.trim();
  if (name !== original.name) update.name = name;

  const description = form.description.trim();
  if (description !== original.description) update.description = description;

  // Tools are sent as the complete new list, and only if the set changed.
  if (!sameSet(form.tools, original.tool_names)) update.tool_names = form.tools;

  if (form.removedDocIds.length > 0) {
    update.remove_document_ids = form.removedDocIds;
  }

  if (form.newDocContent.trim()) {
    update.add_documents = [
      {
        filename: form.newDocName.trim() || DEFAULT_DOC_NAME,
        content: form.newDocContent,
      },
    ];
  }

  return Object.keys(update).length > 0 ? update : null;
}

/** Adding a document means re-indexing, so the agent is briefly unavailable. */
export function needsReindex(update: AgentUpdate | null): boolean {
  return !!update?.add_documents?.length;
}
