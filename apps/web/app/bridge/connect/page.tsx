"use client";
import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../../lib/api";
import "../bridge.css";

type Consent = {handle:string; client_id:string; scope:string; redirect_uri:string; synthetic_only:true};
export default function ConnectBridge(){
  const [consent,setConsent]=useState<Consent|null>(null),[error,setError]=useState(""),[busy,setBusy]=useState(false);
  const operation=useRef(false);
  useEffect(()=>{
    let stopped=false;
    const params=Object.fromEntries(new URLSearchParams(window.location.search));
    void api<Consent>("/bridge/oauth/prepare",{method:"POST",body:JSON.stringify({params})}).then(value=>{if(!stopped)setConsent(value);}).catch(e=>{if(!stopped)setError(e instanceof ApiError&&e.code==="session_required"?"Откройте страницу тестового моста и создайте задание в этом браузере, затем снова начните подключение.":"Запрос подключения недействителен, устарел или клиент OAuth ещё не настроен.");});
    return()=>{stopped=true;};
  },[]);
  async function decide(allow:boolean){if(operation.current||!consent)return;operation.current=true;setBusy(true);setError("");try{const response=await api<{redirect:string}>("/bridge/oauth/approve",{method:"POST",body:JSON.stringify({handle:consent.handle,allow})});window.location.assign(response.redirect);}catch{setConsent(null);setError("Не удалось подтвердить подключение. Начните авторизацию заново.");operation.current=false;setBusy(false);}}
  return <div className="site-shell bridge-shell"><header className="header"><a className="brand" href="/bridge">sonnic.</a></header><main className="input-card"><h1>Разрешить обработку тестовых заданий?</h1><p>Подключение получит доступ к вымышленным заданиям вашей текущей сессии и сможет присылать кандидаты ответов. Оно не сможет одобрять результат, читать личные сны основного сайта или чужие задания.</p><p>Доступ действует до 15 минут. Его можно отозвать на странице моста. Это локальная тестовая авторизация, а не постоянная учётная запись.</p>{error&&<p role="alert" className="error">{error}</p>}{consent&&<><p>Клиент: <strong>{consent.client_id}</strong></p><p>После решения вы вернётесь на: <code>{consent.redirect_uri}</code></p><div className="bridge-actions"><button className="primary" disabled={busy} onClick={()=>void decide(true)}>Разрешить тестовый доступ</button><button className="secondary" disabled={busy} onClick={()=>void decide(false)}>Отказать</button></div></>}<p><a href="/bridge">К тестовым заданиям</a></p></main></div>;
}
