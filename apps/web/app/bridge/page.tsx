"use client";

import { useEffect, useRef, useState } from "react";
import { VoiceInput } from "../../components/voice-input";
import { api, ApiError, type Locale } from "../../lib/api";
import en from "../../messages/bridge-en.json";
import ru from "../../messages/bridge-ru.json";
import "./bridge.css";

type Reflection = { title: string; symbols: {label: string; evidence: string}[]; summary: string; key_connection: string; reflection_question: string };
type Result = { reflection: Reflection | null; safe_message: string | null; locale: string; outcome: string; origin_verified: false; data_class: "synthetic" | "personal" };
type Snapshot = { job_id: string; state: string; attempt: number; error: string | null; data_class: "synthetic" | "personal"; dream_text: string | null; candidate: Result | null; result: Result | null; output_sha256: string | null; can_retry: boolean };
type Config = { cases: {id: string; dream_text: string; locale: Locale}[]; personal_input_enabled: boolean; synthetic_only: boolean; automatic_dispatch: boolean; worker_state: string; chatgpt_connected: boolean };
type UiLocale = "ru" | "en";

const messages = { ru, en };
function lookup(values: Record<string,string>, key: string, fallback: string) {
  return values[key] || fallback;
}

function Card({value}: {value: Result}) {
  const reflection = value.reflection;
  return <article className="result-card bridge-answer" lang={value.locale.split("-")[0]}>
    {reflection ? <><h2>{reflection.title}</h2><div className="symbols">{reflection.symbols.map((symbol,index) => <span className="symbol" key={index}>{symbol.label}<small>«{symbol.evidence}»</small></span>)}</div><p>{reflection.summary}</p><div className="connection"><p>{reflection.key_connection}</p></div><p className="reflection-question">{reflection.reflection_question}</p></> : <p>{value.safe_message}</p>}
  </article>;
}

export default function BridgePage() {
  const [uiLocale,setUiLocale]=useState<UiLocale>("ru");
  const t=messages[uiLocale];
  const [config,setConfig]=useState<Config|null>(null);
  const [dreamText,setDreamText]=useState("");
  const [wakeEmotion,setWakeEmotion]=useState("");
  const [locale,setLocale]=useState<Locale>("ru-RU");
  const [recognitionLocale,setRecognitionLocale]=useState<string|null>(null);
  const [consent,setConsent]=useState(false);
  const [voiceBusy,setVoiceBusy]=useState(false);
  const [jobId,setJobId]=useState<string|null>(null), [snapshot,setSnapshot]=useState<Snapshot|null>(null);
  const [revision,setRevision]=useState(0), [busy,setBusy]=useState(false);
  const [errorCode,setErrorCode]=useState(""), [deletedNotice,setDeletedNotice]=useState(false);
  const [unavailable,setUnavailable]=useState(false), [configRevision,setConfigRevision]=useState(0);
  const [configError,setConfigError]=useState(false);
  const resultRef=useRef<HTMLElement>(null);
  const requestKey=useRef<{fingerprint:string; key:string}|null>(null);
  const generation=useRef(0), operation=useRef(false);

  function fail(value: unknown) {
    const code=value instanceof ApiError?value.code:"unknown";
    setErrorCode(code);
    if(code==="session_required"||code==="job_not_found") {setUnavailable(true);setSnapshot(null);}
  }
  useEffect(()=>{try{const saved=localStorage.getItem("sonnic.bridge.uiLocale");if(saved==="ru"||saved==="en"){setUiLocale(saved);setLocale(saved==="ru"?"ru-RU":"en-US");}}catch{}},[]);
  useEffect(()=>{
    document.documentElement.lang=uiLocale;
    const updateTitle=()=>{if(document.title!==t.documentTitle)document.title=t.documentTitle;};
    updateTitle();
    // Next applies static layout metadata during hydration. Keep the visible
    // tab title aligned with the client-side language after those mutations.
    const timer=setTimeout(updateTitle,0);
    const observer=new MutationObserver(updateTitle);
    observer.observe(document.head,{childList:true,subtree:true,characterData:true});
    try{localStorage.setItem("sonnic.bridge.uiLocale",uiLocale);}catch{}
    return()=>{clearTimeout(timer);observer.disconnect();};
  },[uiLocale,t.documentTitle]);
  useEffect(()=>{ let stopped=false; let timer:ReturnType<typeof setTimeout>; async function poll(){try{const value=await api<Config>("/bridge/config");if(!stopped){setConfig(value);setConfigError(false);}}catch{if(!stopped){setConfigError(true);setConfig(value=>value?{...value,chatgpt_connected:false}:null);}}finally{if(!stopped)timer=setTimeout(poll,10000);}} void poll(); return()=>{stopped=true;clearTimeout(timer);}; },[configRevision]);
  useEffect(()=>{if(snapshot?.result)resultRef.current?.focus();},[snapshot?.state]);
  useEffect(()=>{ try { const linked=new URLSearchParams(window.location.search).get("job"); const id=linked||sessionStorage.getItem("sonnic.bridge.job"); if(id&&/^[a-f0-9-]{36}$/i.test(id)){setJobId(id);window.history.replaceState(null,"",`/bridge?job=${id}`);} }catch{} },[]);
  useEffect(()=>{
    if(!jobId||busy||unavailable||snapshot?.state==="cancelled") return;
    const version=generation.current;
    let stopped=false; let timer:ReturnType<typeof setTimeout>;
    let failures=0;
    async function poll(){
      try {
        const next=await api<Snapshot>(`/bridge/jobs/${jobId}`);
        if(stopped||version!==generation.current)return;
        failures=0;setErrorCode("");setSnapshot(next);
        if(["queued","leased","awaiting_review"].includes(next.state))timer=setTimeout(poll,1500);
      }catch(value){
        if(stopped||version!==generation.current)return;
        fail(value);
        // Recover reads only; never recreate or retry the model job after a network error.
        if(value instanceof ApiError&&value.code==="connection_failed"&&failures<3){
          timer=setTimeout(poll,2000*2**failures++);
        }
      }
    }
    void poll(); return()=>{stopped=true;clearTimeout(timer);};
  },[jobId,revision,busy,unavailable]);

  function insertExample(){const wanted=uiLocale==="ru"?"ru-RU":"en-US";const sample=config?.cases.find(item=>item.locale===wanted)||config?.cases[0];if(sample){setDreamText(sample.dream_text);setLocale(sample.locale);setRecognitionLocale(null);requestKey.current=null;}}
  async function create(){
    const clean=dreamText.trim();
    if(operation.current||voiceBusy||clean.length<20||clean.length>10000||!consent||!config?.chatgpt_connected)return;
    operation.current=true;generation.current++;setBusy(true);setErrorCode("");setDeletedNotice(false);
    try{
      await api("/sessions/guest",{method:"POST",body:JSON.stringify({locale})});
      const fingerprint=JSON.stringify([clean,wakeEmotion.trim(),locale,recognitionLocale]);
      if(requestKey.current?.fingerprint!==fingerprint) requestKey.current={fingerprint,key:crypto.randomUUID()};
      const job=await api<{job_id:string}>("/bridge/jobs",{method:"POST",headers:{"Idempotency-Key":requestKey.current.key},body:JSON.stringify({synthetic:config?.synthetic_only===true,dream:{dream_text:clean,wake_emotion:wakeEmotion.trim(),locale,content_language:locale==="ru-RU"?"ru":"en",recognition_locale:recognitionLocale}})});
      setJobId(job.job_id);setSnapshot(null);setUnavailable(false);
      try{sessionStorage.setItem("sonnic.bridge.job",job.job_id);window.history.replaceState(null,"",`/bridge?job=${job.job_id}`);}catch{}
    }catch(value){fail(value);}finally{operation.current=false;setBusy(false);}
  }
  async function action(name:"retry"|"delete"){
    if(operation.current||!jobId)return;
    if(name==="retry"&&(!config?.chatgpt_connected||snapshot?.error==="submission_uncertain"))return;
    operation.current=true;generation.current++;setBusy(true);setErrorCode("");setDeletedNotice(false);
    try{
      const path=name==="delete"?`/bridge/jobs/${jobId}`:`/bridge/jobs/${jobId}/retry`;
      const next=await api<Snapshot>(path,{method:name==="delete"?"DELETE":"POST"});
      setSnapshot(next);
      if(name==="delete"){
        setDeletedNotice(true);
        try{sessionStorage.removeItem("sonnic.bridge.job");sessionStorage.removeItem(`sonnic.bridge.claim.${jobId}`);window.history.replaceState(null,"","/bridge");}catch{}
      }else setRevision(value=>value+1);
    }catch(value){fail(value);}finally{operation.current=false;setBusy(false);}
  }
  function refresh(){generation.current++;setErrorCode("");setRevision(value=>value+1);}
  function reset(){generation.current++;setJobId(null);setSnapshot(null);setUnavailable(false);requestKey.current=null;setErrorCode("");setDeletedNotice(false);setConsent(false);setDreamText("");setWakeEmotion("");setRecognitionLocale(null);try{sessionStorage.removeItem("sonnic.bridge.job");window.history.replaceState(null,"","/bridge");}catch{}}

  const uiVoiceLocale:Locale=uiLocale==="ru"?"ru-RU":"en-US";
  const cleanLength=dreamText.trim().length;
  const submitBlocked=busy||voiceBusy||!config?.chatgpt_connected||cleanLength<20||cleanLength>10000||!consent;
  const submitHelp=busy?t.saving:voiceBusy?t.submitRecording:
    configError?t.configError:!config?t.submitChecking:!config.chatgpt_connected?t.submitUnavailable:
    cleanLength<20?t.submitTooShort.replace("{remaining}",String(20-cleanLength)):
    cleanLength>10000?t.submitTooLong:!consent?t.submitConsent:t.submitReady;
  return <div className="site-shell bridge-shell">
    <a className="bridge-skip" href="#bridge-main">{t.skip}</a>
    <header className="header">
      <a className="brand" href="/bridge" aria-label={t.homeAria}>☾ sonnic.</a>
      <nav aria-label={t.navigationAria}>
        <a href="#how-it-works">{t.howLink}</a><span>{t.beta}</span>
        <button type="button" className="bridge-language" disabled={voiceBusy} aria-label={t.switchLanguage} onClick={()=>{const next=uiLocale==="ru"?"en":"ru";setUiLocale(next);setLocale(next==="ru"?"ru-RU":"en-US");requestKey.current=null;}}>{uiLocale==="ru"?"EN":"RU"}</button>
      </nav>
    </header>
    <main id="bridge-main">
      <div className="mode-banner"><div><strong>{t.bannerTitle}</strong><span>{config?.personal_input_enabled===true?t.bannerPersonal:t.bannerSynthetic}</span>{config&&!config.chatgpt_connected&&<span role="status">{config.automatic_dispatch?t.bannerUnavailable:t.bannerDisconnected}</span>}</div></div>
      {errorCode&&jobId&&<p className="error" role="alert">{lookup(t.errors,errorCode,t.errorFallback)}</p>}
      {deletedNotice&&<p className="notice" role="status">{t.deletedNotice}</p>}
      {configError&&<div className="error" role="alert">{t.configError} <button className="text-button" onClick={()=>setConfigRevision(value=>value+1)}>{t.retryConnection}</button></div>}
      {!jobId?<div className="bridge-entry-grid">
        <aside className="bridge-intro"><p className="eyebrow">{t.introEyebrow}</p><h2>{t.introTitle}<br/><em>{t.introTitleEmphasis}</em></h2><p>{t.introBody}</p><div className="bridge-moon" aria-hidden="true">☾<span>✦</span></div><p className="field-help">{t.introHelp}</p></aside>
        <section className="input-card"><p className="eyebrow">{t.storyEyebrow}</p><h1>{t.formTitle}</h1><p className="muted">{t.formDescription}</p>
          <div className="bridge-field-head"><label htmlFor="bridge-dream">{t.dreamLabel}</label><button type="button" className="text-button" disabled={busy||voiceBusy||!config} onClick={insertExample}>{t.example}</button></div>
          <textarea id="bridge-dream" value={dreamText} rows={7} maxLength={10000} aria-describedby="bridge-limit" placeholder={t.dreamPlaceholder} readOnly={busy||voiceBusy} onChange={event=>{setDreamText(event.target.value);requestKey.current=null;}}/>
          <div id="bridge-limit" className="field-help bridge-counter">{t.limit} <span>{cleanLength} / 10 000</span></div>
          <VoiceInput locale={locale} uiLocale={uiVoiceLocale} text={dreamText} disabled={busy} onBusy={setVoiceBusy} onText={(text,language)=>{setDreamText(text);setRecognitionLocale(language);requestKey.current=null;}}/>
          <label htmlFor="bridge-emotion">{t.emotionLabel} <small>{t.optional}</small></label><input id="bridge-emotion" value={wakeEmotion} maxLength={120} placeholder={t.emotionPlaceholder} disabled={busy} onChange={event=>{setWakeEmotion(event.target.value);requestKey.current=null;}}/>
          <label htmlFor="bridge-locale">{t.resultLanguage}</label><select id="bridge-locale" value={locale} disabled={busy||voiceBusy} onChange={event=>{setLocale(event.target.value as Locale);requestKey.current=null;}}><option value="ru-RU">{t.russian}</option><option value="en-US">{t.english}</option></select>
          <label className="check-label privacy-check"><input type="checkbox" checked={consent} disabled={busy} onChange={event=>setConsent(event.target.checked)}/><span>{config?.synthetic_only?t.syntheticConsent:""}{t.consent}</span></label>
          <p id="bridge-submit-help" className="field-help" role="status">{submitHelp}</p>
          {errorCode&&<p id="bridge-submit-error" className="error" role="alert">{lookup(t.errors,errorCode,t.errorFallback)}</p>}
          <button className="primary" aria-describedby={`bridge-submit-help${errorCode?" bridge-submit-error":""}`} disabled={submitBlocked} onClick={()=>void create()}>{busy?t.saving:t.submit}</button>
        </section>
      </div>:<>
        <section className="input-card">
          <p className="eyebrow">{snapshot?.data_class==="personal"?t.personalEyebrow:t.testEyebrow}</p>
          <h1 aria-live="polite">{unavailable?t.unavailableTitle:snapshot?lookup(t.states,snapshot.state,snapshot.state):errorCode?t.statusLoadFailed:t.statusLoading}</h1>
          {snapshot?.dream_text&&<p className="bridge-dream">{snapshot.dream_text}</p>}
          {snapshot&&<p className="muted">{t.attempt}: {snapshot.attempt} {t.attemptOf}</p>}
          {snapshot?.error&&<p className="error" role="status">{lookup(t.errors,snapshot.error,t.snapshotErrorFallback)}</p>}
          {snapshot&&["queued","leased","completed"].includes(snapshot.state)&&<ol className="bridge-progress" aria-label={t.progressAria}>{t.progressLabels.map((label,index)=><li key={label} aria-current={index===(snapshot.state==="queued"?0:snapshot.state==="leased"?1:2)?"step":undefined}><span aria-hidden="true">{index+1}</span>{label}</li>)}</ol>}
          {snapshot?.state==="queued"&&<p>{t.queuedBody}</p>}
          {snapshot?.state==="leased"&&<p>{t.leasedBody}</p>}
          <div className="bridge-actions">
            {!unavailable&&snapshot?.state!=="cancelled"&&<button className="secondary" disabled={busy} onClick={refresh}>{t.refresh}</button>}
            {snapshot&&["queued","leased","awaiting_review","completed","failed","expired"].includes(snapshot.state)&&<button className="secondary danger" disabled={busy} onClick={()=>void action("delete")}>{t.delete}</button>}
            {snapshot?.can_retry&&snapshot.error!=="submission_uncertain"&&<button className="secondary" disabled={busy||!config?.chatgpt_connected} onClick={()=>void action("retry")}>{t.retry}</button>}
            {(unavailable||snapshot&&["completed","cancelled","failed","expired"].includes(snapshot.state))&&<button className="secondary" disabled={busy} onClick={reset}>{t.newDream}</button>}
          </div>
        </section>
        {snapshot?.result&&<section ref={resultRef} tabIndex={-1} className="bridge-result" aria-label={t.resultRegion}><h2>{t.resultTitle}</h2><Card value={snapshot.result}/><p className="field-help">{t.resultDisclaimer}</p></section>}
      </>}
      <section id="how-it-works" className="bridge-about" aria-labelledby="bridge-about-title"><p className="eyebrow">{t.aboutEyebrow}</p><h2 id="bridge-about-title">{t.aboutTitle}</h2><div className="bridge-explainer">{t.aboutSteps.map((step,index)=><article key={step.title}><span>{String(index+1).padStart(2,"0")}</span><h3>{step.title}</h3><p>{step.body}</p></article>)}</div></section>
      <section className="bridge-faq" aria-label={t.faqAria}><h2>{t.faqTitle}</h2>{t.faqs.map(item=><details key={item.question}><summary>{item.question}</summary><p>{item.answer}</p></details>)}</section>
    </main>
    <footer><span>{t.footerPrimary}</span><span>{t.footerDisclaimer}</span></footer>
  </div>;
}
