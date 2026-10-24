'use client';
import { useEffect, useRef, useState, type FormEventHandler } from 'react';
import { BrandWordmark } from './Shell';

export interface LoginCopy {
  login: string; formDesc: string; username: string; password: string;
  hidePassword: string; showPassword: string; loginButton: string; privateArchive: string; sourceCode: string;
}
export function LoginView({copy,username,password,onUsernameChange,onPasswordChange,onSubmit,usernameName,passwordName,note,brandHref,pending=false,pendingLabel,error,credentialAutocomplete=false}: {
  copy: LoginCopy; username: string; password: string; onUsernameChange: (value: string) => void;
  onPasswordChange: (value: string) => void; onSubmit: FormEventHandler<HTMLFormElement>;
  usernameName: string; passwordName: string; note: string; brandHref: string; pending?:boolean;pendingLabel?:string;error?:string;credentialAutocomplete?:boolean;
}) {
  const [passwordVisible,setPasswordVisible]=useState(false);
  const errorRef=useRef<HTMLParagraphElement>(null);
  useEffect(()=>{if(error)errorRef.current?.focus();},[error]);
  return (
    <main className="layout-login" id="main-content" tabIndex={-1}>
      <div className="layout-login-content">
      <section className="layout-login-column" aria-labelledby="layout-login-title">
        <div className="layout-login-identity">
          <BrandWordmark href={brandHref} className="layout-login-brand"/>
        </div>

        <div className="layout-login-intro">
          <h1 id="layout-login-title">{copy.login}</h1>
          <p>{copy.formDesc}</p>
        </div>

        <form className="layout-login-form" onSubmit={onSubmit} aria-busy={pending || undefined} autoComplete={credentialAutocomplete?"on":"off"}>
          <div className="layout-login-field">
            <label htmlFor="layout-login-username">{copy.username}</label>
            <input
              id="layout-login-username"
              name={usernameName}
              type="text"
              value={username}
              autoComplete={credentialAutocomplete?"username":"off"}
              autoCapitalize="none"
              spellCheck={false}
              required
              onChange={event => onUsernameChange(event.target.value)}
            />
          </div>

          <div className="layout-login-field">
            <label htmlFor="layout-login-password">{copy.password}</label>
            <div className="layout-login-password">
              <input
                id="layout-login-password"
                name={passwordName}
                type={passwordVisible ? 'text' : 'password'}
                value={password}
                autoComplete={credentialAutocomplete?"current-password":"off"}
                required
                onChange={event => onPasswordChange(event.target.value)}
              />
              <button
                className="layout-login-password-toggle"
                type="button"
                aria-label={passwordVisible ? copy.hidePassword : copy.showPassword}
                aria-controls="layout-login-password"
                aria-pressed={passwordVisible}
                onClick={() => setPasswordVisible(value => !value)}
              >
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
                  <path d="M2 12s4-7 10-7 10 7 10 7-4 7-10 7S2 12 2 12Z" />
                  <circle cx="12" cy="12" r="3" />
                  {!passwordVisible ? <path d="m3 3 18 18" /> : null}
                </svg>
              </button>
            </div>
          </div>

          {error?<p ref={errorRef} className="layout-login-error" role="alert" tabIndex={-1}>{error}</p>:null}
          <button className="button primary layout-login-submit" type="submit" disabled={pending}>
            {pending?(pendingLabel||copy.loginButton):copy.loginButton}
          </button>
        </form>

        <p className="layout-login-note" role="note">{note}</p>
      </section>

      <footer className="layout-login-footer">
        <span>{copy.privateArchive}</span>
        <a href="https://github.com/MUSH2077/auto-gallery" target="_blank" rel="noreferrer">{copy.sourceCode}</a>
      </footer>
      </div>
    </main>
  );
}
