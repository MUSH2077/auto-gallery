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
  params.set("offset", String(filters.offset ?? 0));
  params.set("limit", String(filters.limit ?? 25));
  return params;
}

export const remoteDiscoveryApi = {
  listRemoteAccounts: (offset = 0, limit = 50) =>
    request<RemoteAccountRead[]>(`/api/v1/remote-accounts?offset=${offset}&limit=${limit}`),

  getRemoteAccount: (id: string) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`),

  createRemoteAccount: (input: RemoteAccountCreateInput) =>
    request<RemoteAccountRead>("/api/v1/remote-accounts", {
      method: "POST",
      body: JSON.stringify(input),
    }),

  updateRemoteAccount: (id: string, input: RemoteAccountUpdateInput) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`, {
      method: "PATCH",
      body: JSON.stringify(input),
    }),

  deleteRemoteAccount: (id: string) =>
    request<void>(`/api/v1/remote-accounts/${encodeURIComponent(id)}`, { method: "DELETE" }),

  testRemoteAccount: (id: string) =>
    request<RemoteAccountRead>(`/api/v1/remote-accounts/${encodeURIComponent(id)}/test`, { method: "POST" }),

  listRemoteCollections: (id: string) =>
    request<RemoteCollection[]>(`/api/v1/remote-accounts/${encodeURIComponent(id)}/collections`),

  authorizeXOAuth: (accountId?: string) => {
    const params = new URLSearchParams();
    if (accountId) params.set("account_id", accountId);
    const suffix = params.size ? `?${params.toString()}` : "";
    return request<XOAuthAuthorizeResponse>(`/api/v1/remote-accounts/x/oauth/authorize${suffix}`);
  },

  completeXOAuth: (state: string, code: string) => {
    const params = new URLSearchParams({ state, code });
    return request<RemoteAccountRead>(`/api/v1/remote-accounts/x/oauth/callback?${params.toString()}`);
  },

  createDiscoveryScan: (remoteAccountId: string) =>
    request<TaskRun>("/api/v1/discovery/scans", {
      method: "POST",
      body: JSON.stringify({ remote_account_id: remoteAccountId }),
    }),

  listDiscoveryScans: (remoteAccountId?: string, offset = 0, limit = 50) => {
    const params = new URLSearchParams({ offset: String(offset), limit: String(limit) });
    if (remoteAccountId) params.set("remote_account_id", remoteAccountId);
    return request<TaskRunListResponse>(`/api/v1/discovery/scans?${params.toString()}`);
  },

  listDiscoveryCandidates: (filters: DiscoveryCandidateFilters = {}) =>
    request<DiscoveryCandidateListResponse>(`/api/v1/discovery/candidates?${candidateParams(filters).toString()}`),

  batchDiscoveryCandidates: (input: DiscoveryCandidateBatchInput) =>
    request<DiscoveryCandidateBatchResponse>("/api/v1/discovery/candidates/batch-actions", {
      method: "POST",
      body: JSON.stringify({
        ids: input.ids,
        action: input.action,
        immediate_sync: input.syncNow ?? false,
      }),
    }),

  resolveDiscoveryCandidate: (id: string, input: DiscoveryCandidateResolveInput) =>
    request<DiscoveryCandidateResolveResponse>(`/api/v1/discovery/candidates/${encodeURIComponent(id)}/resolve`, {
      method: "POST",
      body: JSON.stringify({
        creator_id: input.creatorId,
        creator_name: input.creatorName,
        immediate_sync: input.syncNow ?? false,
      }),
    }),
};
