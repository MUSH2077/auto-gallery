"use client";

import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { LoginView } from "@/features/gallery-ui/LoginView";
import { useAuth } from "@/lib/auth";
import { AuthLoginError, AuthUserLookupError, type AuthUser } from "@/lib/authFlow";
import { loginDestination } from "@/lib/loginDestination";
import { useI18n } from "@/lib/i18n";
import loginCopy from "@/lib/locales/login.json";
import { defaultGalleryRoutes, galleryLandingRoute } from "./routes";

export default function LoginPage() {
  const { login, user, isLoading, authUnavailable } = useAuth();
  const { lang } = useI18n();
  const router = useRouter();
  const sp = useSearchParams();
  const copy = lang === "en" ? loginCopy.en : loginCopy.zh;
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [pending, setPending] = useState(false);
  const [error, setError] = useState("");
  const submitting = useRef(false);
  const generation = useRef(0);
  const returnTarget = sp.get("redirect");

  const destination = useCallback((principal: AuthUser) => {
    const requestedDestination = returnTarget || galleryLandingRoute(principal);
    const fragment = requestedDestination.includes("#") ? "" : window.location.hash;
    return loginDestination(requestedDestination + fragment, principal.must_change_password);
  }, [returnTarget]);

  useEffect(() => {
    if (!isLoading && user && !submitting.current) router.replace(destination(user));
  }, [isLoading, user, router, destination]);

  useEffect(() => () => { generation.current += 1; }, []);

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (submitting.current) return;
    submitting.current = true;
    const attempt = ++generation.current;
    setPending(true);
    setError("");

    try {
      const authenticated = await login(username, password);
      if (attempt === generation.current) router.replace(destination(authenticated));
    } catch (failure) {
      if (attempt !== generation.current) return;
      if (failure instanceof AuthLoginError) {
        if (failure.status >= 500) setError(copy.sessionUnavailable);
        else if (failure.status === 401) setError(copy.invalidCredentials);
        else setError(failure.message);
      } else if (failure instanceof AuthUserLookupError || failure instanceof TypeError) {
        setError(copy.sessionUnavailable);
      } else {
        setError(failure instanceof Error ? failure.message : copy.invalidCredentials);
      }
    } finally {
      submitting.current = false;
      if (attempt === generation.current) setPending(false);
    }
  }

  return <LoginView
    brandHref={defaultGalleryRoutes.login + (sp.toString() ? "?" + sp.toString() : "")}
    copy={copy}
    username={username}
    password={password}
    onUsernameChange={setUsername}
    onPasswordChange={setPassword}
    onSubmit={submit}
    usernameName="username"
    passwordName="password"
    note={copy.secureAccess}
    pending={pending}
    pendingLabel={copy.loggingIn}
    error={error || (!pending && authUnavailable ? copy.sessionUnavailable : "")}
    credentialAutocomplete
  />;
}
