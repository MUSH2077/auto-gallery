export type AuthState = "healthy" | "unhealthy" | "unknown";
export type CredentialState = "ready" | "missing" | "not_required" | "unknown";

export type AuthHealthSource = {
  auth_healthy?: boolean | null;
  auth_state?: AuthState | null;
  credential_state?: CredentialState | null;
};

export type AuthHealthPresentation = {
  state: AuthState | "credential_missing";
  tone: "neutral" | "good" | "bad";
  labelKey: "repo.auth_unknown" | "repo.auth_healthy" | "repo.auth_issue" | "repo.credential_missing";
  dotClass: "bg-placeholder" | "bg-success" | "bg-danger";
};

export function authStateForSource(source: AuthHealthSource): AuthState {
  if (source.auth_state === "healthy" || source.auth_state === "unhealthy" || source.auth_state === "unknown") {
    return source.auth_state;
  }
  // Old servers did not expose attempt evidence. A legacy false therefore
  // cannot safely distinguish a failed check from a never-checked source.
  return source.auth_healthy === true ? "healthy" : "unknown";
}

export function hasActionableAuthFailure(source: AuthHealthSource): boolean {
  return authStateForSource(source) === "unhealthy";
}

export function authHealthPresentation(source: AuthHealthSource): AuthHealthPresentation {
  if (source.credential_state === "missing") {
    return {
      state: "credential_missing",
      tone: "bad",
      labelKey: "repo.credential_missing",
      dotClass: "bg-danger",
    };
  }

  const state = authStateForSource(source);
  if (state === "healthy") {
    return { state, tone: "good", labelKey: "repo.auth_healthy", dotClass: "bg-success" };
  }
  if (state === "unhealthy") {
    return { state, tone: "bad", labelKey: "repo.auth_issue", dotClass: "bg-danger" };
  }
  return { state, tone: "neutral", labelKey: "repo.auth_unknown", dotClass: "bg-placeholder" };
}
