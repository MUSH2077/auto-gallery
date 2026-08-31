import { request } from "../client";
import type {
  DiscoveryCandidateBatchInput,
  DiscoveryCandidateBatchResponse,
  DiscoveryCandidateFilters,
  DiscoveryCandidateListResponse,
  DiscoveryCandidateResolveInput,
  DiscoveryCandidateResolveResponse,
  RemoteAccountCreateInput,
  RemoteAccountRead,
  RemoteAccountUpdateInput,
  RemoteCollection,
  RemoteCreatorDetail,
  RemoteWorkFeedType,
  RemoteWorkImportResult,
  RemoteWorkPage,
  TaskRun,
  TaskRunListResponse,
  XOAuthAuthorizeResponse,
} from "../types";

function candidateParams(filters: DiscoveryCandidateFilters = {}) {
  const params = new URLSearchParams();
  if (filters.accountId) params.set("remote_account_id", filters.accountId);
  if (filters.state) params.set("state", filters.state);
  if (filters.confidence) params.set("confidence", filters.confidence);
  if (filters.isFollowing !== undefined) params.set("is_following", String(filters.isFollowing));
  if (filters.localMatch !== undefined) params.set("local_match", String(filters.localMatch));
  params.set("offset", String(filters.offset ?? 0));
  params.set("limit", String(filters.limit ?? 25));
  return params;
}

export const remoteDiscoveryApi = {
  listRemoteAccounts: (offset = 0, limit = 50, signal?: AbortSignal) =>
    request<RemoteAccountRead[]>(`/api/v1/remote-accounts?offset=${offset}&limit=${limit}`, { signal }),

  getRemoteAccount: (id: string, signal?: AbortSignal) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`, { signal }),

  createRemoteAccount: (input: RemoteAccountCreateInput, signal?: AbortSignal) =>
    request<RemoteAccountRead>("/api/v1/remote-accounts", {
      method: "POST",
      body: JSON.stringify(input),
      signal,
    }),

  updateRemoteAccount: (id: string, input: RemoteAccountUpdateInput, signal?: AbortSignal) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(input),
      signal,
    }),

  deleteRemoteAccount: (id: string, signal?: AbortSignal) =>
    request<void>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`, { method: "DELETE", signal }),

  testRemoteAccount: (id: string, signal?: AbortSignal) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}/test`, { method: "POST", signal }),

  listRemoteCollections: (id: string, signal?: AbortSignal) =>
    request<RemoteCollection[]>(`/api/v1/remote-accounts/${encodeURIComponent(id)}/collections`, { signal }),

  authorizeXOAuth: (accountId?: string, signal?: AbortSignal) => {
    const params = new URLSearchParams();
    if (accountId) params.set("account_id", accountId);
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<XOAuthAuthorizeResponse>(`/api/v1/remote-accounts/x/oauth/authorize${suffix}`, { signal });
  },

  completeXOAuth: (state: string, code: string, signal?: AbortSignal) => {
    return request<RemoteAccountRead>("/api/v1/remote-accounts/x/oauth/callback", {
      method: "POST",
      body: JSON.stringify({ state, code }),
      signal,
    });
  },

  createDiscoveryScan: (remoteAccountId: string, signal?: AbortSignal) =>
    request<TaskRun>("/api/v1/discovery/scans", {
      method: "POST",
      body: JSON.stringify({ remote_account_id: remoteAccountId }),
      signal,
    }),

  listDiscoveryScans: (remoteAccountId?: string, offset = 0, limit = 50, signal?: AbortSignal) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (remoteAccountId) params.set("remote_account_id", remoteAccountId);
    return request<TaskRunListResponse>(`/api/v1/discovery/scans?${params.toString()}`, { signal });
  },

  listDiscoveryCandidates: (filters: DiscoveryCandidateFilters = {}, signal?: AbortSignal) =>
    request<DiscoveryCandidateListResponse>(`/api/v1/discovery/candidates?${candidateParams(filters).toString()}`, { signal }),

  batchDiscoveryCandidates: (input: DiscoveryCandidateBatchInput, signal?: AbortSignal) =>
    request<DiscoveryCandidateBatchResponse>("/api/v1/discovery/candidates/batch-actions", {
      method: "POST",
      body: JSON.stringify({
        ids: input.ids,
        action: input.action,
        immediate_sync: input.syncNow ?? false,
      }),
      signal,
    }),

  resolveDiscoveryCandidate: (id: string, input: DiscoveryCandidateResolveInput, signal?: AbortSignal) =>
    request<DiscoveryCandidateResolveResponse>(`/api/v1/discovery/candidates/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        creator_id: input.creatorId,
        creator_name: input.creatorName,
        immediate_sync: input.syncNow ?? false,
      }),
      signal,
    }),

  getDiscoveryCandidateRemoteDetail: (
    id: string,
    options: { workType?: RemoteWorkFeedType; limit?: number; signal?: AbortSignal } = {},
  ) => {
    const params = new URLSearchParams({
      work_type: options.workType || "illust",
      limit: String(options.limit || 20),
    });
    return request<RemoteCreatorDetail>(
      `/api/v1/discovery/candidates/${encodeURIComponent(id)}/remote-detail?${params.toString()}`,
      { signal: options.signal },
    );
  },

  getDiscoveryCandidateRemoteWorks: (
    id: string,
    options: {
      workType?: RemoteWorkFeedType;
      cursor?: string | null;
      limit?: number;
      signal?: AbortSignal;
    } = {},
  ) => {
    const params = new URLSearchParams({
      work_type: options.workType || "illust",
      limit: String(options.limit || 20),
    });
    if (options.cursor) params.set("cursor", options.cursor);
    return request<RemoteWorkPage>(
      `/api/v1/discovery/candidates/${encodeURIComponent(id)}/remote-works?${params.toString()}`,
      { signal: options.signal },
    );
  },

  importDiscoveryCandidateRemoteWork: (
    id: string,
    workToken: string,
    sensitiveContentConfirmed: boolean,
    signal?: AbortSignal,
  ) => request<RemoteWorkImportResult>(
    `/api/v1/discovery/candidates/${encodeURIComponent(id)}/remote-work-imports`,
    {
      method: "POST",
      body: JSON.stringify({
        work_token: workToken,
        sensitive_content_confirmed: sensitiveContentConfirmed,
      }),
      signal,
    },
  ),
};
