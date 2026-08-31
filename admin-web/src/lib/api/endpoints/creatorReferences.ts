import { request } from "../client";
import type { CreatorReferences } from "../types";

export const creatorReferencesApi = {
  getCreatorReferences: (id: string, signal?: AbortSignal) =>
    request<CreatorReferences>(
      `/api/v1/creators/${id}/references`,
      { signal },
    ),
};
