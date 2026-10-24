'use client';
import {useEffect,useRef,useState,type FormEvent} from 'react';
import {useRouter,useSearchParams} from 'next/navigation';
import {LoginView} from '@/features/gallery-ui/LoginView';
import {useAuth} from '@/lib/auth';
import {AuthLoginError,AuthUserLookupError} from '@/lib/authFlow';
import {loginDestination} from '@/lib/loginDestination';
import {useI18n} from '@/lib/i18n';
import loginCopy from '@/lib/locales/login.json';
import {defaultGalleryRoutes,type GalleryRoutes} from './routes';
export default function LoginPage({routes=defaultGalleryRoutes}:{routes?:GalleryRoutes}={}) {
  const {login,user,isLoading,authUnavailable}=useAuth(),{lang}=useI18n(),router=useRouter(),sp=useSearchParams();
  const copy=lang==='en'?loginCopy.en:loginCopy.zh;
  const [username,setUsername]=useState(''),[password,setPassword]=useState(''),[pending,setPending]=useState(false),[error,setError]=useState('');
  const submitting=useRef(false),generation=useRef(0);
  function destination(mustChange=false){return loginDestination(sp.get('redirect')||routes.works,mustChange);}
  useEffect(()=>{if(!isLoading&&user&&!submitting.current)router.replace(destination(user.must_change_password));},[isLoading,user,router,sp]);
  useEffect(()=>()=>{generation.current+=1;},[]);
  async function submit(event:FormEvent<HTMLFormElement>) {
    event.preventDefault();if(submitting.current)return;submitting.current=true;
    const attempt=++generation.current;setPending(true);setError('');
    try {const authenticated=await login(username,password);if(attempt===generation.current)router.replace(destination(authenticated.must_change_password));}
    catch(failure){
      if(attempt!==generation.current)return;
      setError(failure instanceof AuthLoginError?(failure.status>=500?copy.sessionUnavailable:failure.status===401?copy.invalidCredentials:failure.message):failure instanceof AuthUserLookupError||failure instanceof TypeError?copy.sessionUnavailable:failure instanceof Error?failure.message:copy.invalidCredentials);
    }finally{submitting.current=false;if(attempt===generation.current)setPending(false);}
  }
  return <LoginView brandHref={routes.login+(sp.toString()?'?'+sp.toString():'')} copy={copy}
    username={username} password={password} onUsernameChange={setUsername} onPasswordChange={setPassword} onSubmit={submit}
    usernameName="username" passwordName="password" note={copy.secureAccess} pending={pending} pendingLabel={copy.loggingIn}
    error={error||(!pending&&authUnavailable?copy.sessionUnavailable:'')} credentialAutocomplete/>;
}
