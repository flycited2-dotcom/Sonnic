"use client";

import { useEffect, useRef, useState } from "react";
import { VoiceInput } from "../../components/voice-input";
import { api, ApiError, type Locale } from "../../lib/api";
import "./bridge.css";

type Reflection = { title: string; symbols: {label: string; evidence: string}[]; summary: string; key_connection: string; reflection_question: string };
type Result = { reflection: Reflection | null; safe_message: string | null; locale: string; outcome: string; origin_verified: false; data_class: "synthetic" | "personal" };
type Snapshot = { job_id: string; state: string; attempt: number; error: string | null; data_class: "synthetic" | "personal"; dream_text: string | null; candidate: Result | null; result: Result | null; output_sha256: string | null; can_retry: boolean };
type Config = { cases: {id: string; dream_text: string; locale: Locale}[]; personal_input_enabled: boolean; oauth_configured: boolean; mcp_url: string };

const project = "https://chatgpt.com/g/g-p-6a9c47e07510819197bd898c90d1b12b-sonnic-baza-znanii-i-razbor-snov/project";
const states: Record<string,string> = { queued: "Сон сохранён и ждёт разбора", leased: "ChatGPT разбирает сон", awaiting_review: "Разбор готов к вашей проверке", completed: "Готовый разбор", failed: "Разбор отклонён", expired: "Время ожидания истекло", cancelled: "Сон удалён" };
const errors: Record<string,string> = { session_required: "Сессия закончилась. Начните заново.", job_not_found: "Задание недоступно в текущем браузере.", queue_full: "Очередь временно заполнена. Завершите или удалите прежнее задание.", connection_failed: "Не удалось связаться с сервером. Повторите действие.", review_changed: "Ответ изменился. Обновите статус и проверьте его заново.", retry_not_allowed: "Повтор больше недоступен.", not_awaiting_review: "Этот ответ уже проверен или устарел.", review_rejected: "Вы отклонили разбор. Можно запустить повтор.", lease_expired: "ChatGPT не успел вернуть ответ. Можно повторить обработку.", job_expired: "Срок ожидания закончился. Можно повторить обработку.", invalid_input: "Проверьте текст сна: нужно от 20 до 10 000 символов." };

function Card({value}: {value: Result}) {
  const reflection = value.reflection;
  return <article className="result-card bridge-answer" lang={value.locale.split("-")[0]}>
    {reflection ? <><h2>{reflection.title}</h2><div className="symbols">{reflection.symbols.map((symbol,index) => <span className="symbol" key={index}>{symbol.label}<small>«{symbol.evidence}»</small></span>)}</div><p>{reflection.summary}</p><div className="connection"><p>{reflection.key_connection}</p></div><p className="reflection-question">{reflection.reflection_question}</p></> : <p>{value.safe_message}</p>}
  </article>;
}

export default function BridgePage() {
  const [config,setConfig]=useState<Config|null>(null);
  const [dreamText,setDreamText]=useState("");
  const [wakeEmotion,setWakeEmotion]=useState("");
  const [locale,setLocale]=useState<Locale>("ru-RU");
  const [recognitionLocale,setRecognitionLocale]=useState<string|null>(null);
  const [consent,setConsent]=useState(false);
  const [voiceBusy,setVoiceBusy]=useState(false);
  const [jobId,setJobId]=useState<string|null>(null), [snapshot,setSnapshot]=useState<Snapshot|null>(null);
  const [claimKey,setClaimKey]=useState(""), [revision,setRevision]=useState(0), [busy,setBusy]=useState(false);
  const [error,setError]=useState(""), [notice,setNotice]=useState(""), [checkedHash,setCheckedHash]=useState<string|null>(null);
  const [unavailable,setUnavailable]=useState(false), [configRevision,setConfigRevision]=useState(0);
  const requestKey=useRef<{fingerprint:string; key:string}|null>(null);
  const generation=useRef(0), operation=useRef(false);

  function fail(value: unknown) {
    const code=value instanceof ApiError?value.code:"unknown";
    setError(errors[code]||"Действие не выполнено. Обновите статус и попробуйте снова.");
    if(code==="session_required"||code==="job_not_found") {setUnavailable(true);setSnapshot(null);}
  }
  useEffect(()=>{ let stopped=false; void api<Config>("/bridge/config").then(value=>{if(stopped)return;setConfig(value);setError("");}).catch(value=>{if(!stopped)fail(value);}); return()=>{stopped=true;}; },[configRevision]);
  useEffect(()=>{ try { const linked=new URLSearchParams(window.location.search).get("job"); const id=linked||sessionStorage.getItem("sonnic.bridge.job"); if(id&&/^[a-f0-9-]{36}$/i.test(id)){const key=sessionStorage.getItem(`sonnic.bridge.claim.${id}`)||crypto.randomUUID();setJobId(id);setClaimKey(key);sessionStorage.setItem(`sonnic.bridge.claim.${id}`,key);window.history.replaceState(null,"",`/bridge?job=${id}`);} }catch{} },[]);
  useEffect(()=>{
    if(!jobId||busy||unavailable) return;
    const version=generation.current;
    let stopped=false; let timer:ReturnType<typeof setTimeout>;
    async function poll(){try { const next=await api<Snapshot>(`/bridge/jobs/${jobId}`); if(stopped||version!==generation.current)return; setSnapshot(next); setCheckedHash(current=>current===next.output_sha256?current:null); if(["queued","leased","awaiting_review"].includes(next.state))timer=setTimeout(poll,1500); }catch(value){if(!stopped&&version===generation.current)fail(value);}}
    void poll(); return()=>{stopped=true;clearTimeout(timer);};
  },[jobId,revision,busy,unavailable]);

  function insertExample(){const sample=config?.cases.find(item=>item.id==="blue-parrot-ru")||config?.cases[0];if(sample){setDreamText(sample.dream_text);setLocale(sample.locale);setRecognitionLocale(null);requestKey.current=null;}}
  async function create(){
    const clean=dreamText.trim();
    if(operation.current||voiceBusy||clean.length<20||clean.length>10000||!consent)return;
    operation.current=true;generation.current++;setBusy(true);setError("");setNotice("");
    try{
      await api("/sessions/guest",{method:"POST",body:JSON.stringify({locale})});
      const fingerprint=JSON.stringify([clean,wakeEmotion.trim(),locale,recognitionLocale]);
      if(requestKey.current?.fingerprint!==fingerprint) requestKey.current={fingerprint,key:crypto.randomUUID()};
      const job=await api<{job_id:string}>("/bridge/jobs",{method:"POST",headers:{"Idempotency-Key":requestKey.current.key},body:JSON.stringify({dream:{dream_text:clean,wake_emotion:wakeEmotion.trim(),locale,content_language:locale==="ru-RU"?"ru":"en",recognition_locale:recognitionLocale}})});
      const key=crypto.randomUUID();setClaimKey(key);setJobId(job.job_id);setSnapshot(null);setCheckedHash(null);setUnavailable(false);
      try{sessionStorage.setItem("sonnic.bridge.job",job.job_id);sessionStorage.setItem(`sonnic.bridge.claim.${job.job_id}`,key);window.history.replaceState(null,"",`/bridge?job=${job.job_id}`);}catch{}
    }catch(value){fail(value);}finally{operation.current=false;setBusy(false);}
  }
  async function action(name:string){
    if(operation.current||!jobId)return;
    if((name==="review"||name==="reject")&&(!snapshot?.output_sha256||snapshot.state!=="awaiting_review"))return;
    if(name==="review"&&checkedHash!==snapshot?.output_sha256)return;
    operation.current=true;generation.current++;setBusy(true);setError("");setNotice("");
    try{
      const body=name==="review"||name==="reject"?JSON.stringify({approve:name==="review",output_sha256:snapshot?.output_sha256}):undefined;
      const next=await api<Snapshot>(`/bridge/jobs/${jobId}/${name==="reject"?"review":name}`,{method:"POST",body});
      setSnapshot(next);setCheckedHash(null);setRevision(value=>value+1);
      if(name==="cancel"){setNotice("Текст сна и результат удалены с сервера.");}
    }catch(value){setCheckedHash(null);fail(value);}finally{operation.current=false;setBusy(false);}
  }
  function refresh(){generation.current++;setError("");setRevision(value=>value+1);}
  function reset(){generation.current++;setJobId(null);setSnapshot(null);setClaimKey("");setCheckedHash(null);setUnavailable(false);requestKey.current=null;setError("");setNotice("");setConsent(false);try{sessionStorage.removeItem("sonnic.bridge.job");window.history.replaceState(null,"","/bridge");}catch{}}
  const task=`Задание Sonnic. Через подключённый мост вызови claim_job с job_id "${jobId}" и claim_key "${claimKey}". Обработай только это задание по инструкциям проекта. Затем передай полный JSON строго по output.schema.json через submit_result с неизменными идентификаторами, версиями, хешем и attempt. Не повторяй личный текст сна в чате сверх необходимого и не сохраняй его в память. Не утверждай публикацию: ответ сначала ждёт проверки на сайте. Если инструмента нет, сообщи «мост не подключён»; не выдумывай выполнение.`;

  return <div className="site-shell bridge-shell"><header className="header"><a className="brand" href="/">sonnic.</a><span>Закрытое бета-тестирование</span></header><main>
    <div className="mode-banner"><div><strong>Рабочий разбор через ваш проект ChatGPT</strong><span>Сайт передаёт сон в подключённый MCP-мост. Из-за ограничений обычной подписки ChatGPT запуск пока требует одного действия в проекте; API-ключ OpenAI не используется.</span></div></div>
    {error&&<p className="error" role="alert">{error}</p>}{notice&&<p className="notice" role="status">{notice}</p>}
    {!config&&error&&<button className="secondary" onClick={()=>{setError("");setConfigRevision(value=>value+1);}}>Повторить подключение</button>}
    {!jobId?<section className="input-card"><p className="eyebrow">ШАГ 1 ИЗ 3</p><h1>Расскажите свой сон</h1><p className="muted">Можно напечатать или надиктовать. Перед отправкой текст всегда можно исправить.</p>
      <div className="bridge-field-head"><label htmlFor="bridge-dream">Ваш сон</label><button type="button" className="text-button" onClick={insertExample}>Вставить пример</button></div>
      <textarea id="bridge-dream" value={dreamText} rows={8} maxLength={10000} placeholder="Например: мне приснился синий попугай…" disabled={busy} onChange={event=>{setDreamText(event.target.value);requestKey.current=null;}}/>
      <div className="field-help bridge-counter">От 20 до 10 000 символов <span>{dreamText.length} / 10 000</span></div>
      <VoiceInput locale={locale} text={dreamText} disabled={busy} onBusy={setVoiceBusy} onText={(text,language)=>{setDreamText(text);setRecognitionLocale(language);requestKey.current=null;}}/>
      <label htmlFor="bridge-emotion">Чувство после пробуждения <small>необязательно</small></label><input id="bridge-emotion" value={wakeEmotion} maxLength={120} placeholder="Например, удивление или спокойствие" disabled={busy} onChange={event=>{setWakeEmotion(event.target.value);requestKey.current=null;}}/>
      <label htmlFor="bridge-locale">Язык результата</label><select id="bridge-locale" value={locale} disabled={busy} onChange={event=>{setLocale(event.target.value as Locale);requestKey.current=null;}}><option value="ru-RU">Русский</option><option value="en-US">English</option></select>
      <label className="check-label privacy-check"><input type="checkbox" checked={consent} disabled={busy} onChange={event=>setConsent(event.target.checked)}/><span>Я согласен отправить этот текст в мой проект ChatGPT для разбора. Аудио Sonnic не сохраняет; текст хранится на тестовом сервере до удаления.</span></label>
      <button className="primary" disabled={busy||voiceBusy||dreamText.trim().length<20||dreamText.trim().length>10000||!consent} onClick={()=>void create()}>{busy?"Сохраняем сон…":"Передать сон на разбор"}</button>
    </section>:<>
      <section className="input-card"><p className="eyebrow">{snapshot?.data_class==="personal"?"ШАГ 2 ИЗ 3 · ВАШ СОН":"ТЕСТОВЫЙ ПРИМЕР"}</p><h1 aria-live="polite">{unavailable?"Задание недоступно":snapshot?states[snapshot.state]||snapshot.state:error?"Не удалось загрузить статус":"Загружаем статус…"}</h1>{snapshot?.dream_text&&<p className="bridge-dream">{snapshot.dream_text}</p>}{snapshot&&<p className="muted">Попытка: {snapshot.attempt} из 3</p>}
      {snapshot?.error&&<p className="error" role="status">{errors[snapshot.error]||"Обработка не завершилась. Обновите статус или запустите повтор."}</p>}
      {snapshot?.state==="queued"&&<><ol className="bridge-steps"><li>Нажмите «Скопировать задание».</li><li>Откройте проект ChatGPT и вставьте текст в новый чат.</li><li>Вернитесь сюда: статус обновится автоматически.</li></ol><div className="bridge-actions"><button className="primary" onClick={()=>{void navigator.clipboard.writeText(task).then(()=>setNotice("Задание скопировано — теперь откройте ChatGPT и отправьте его.")).catch(()=>setError("Не удалось скопировать. Выделите текст ниже вручную."));}}>1. Скопировать задание</button><a className="secondary" href={project} target="_blank" rel="noreferrer">2. Открыть проект ChatGPT</a></div><details><summary>Показать текст задания</summary><textarea aria-label="Текст задания для ChatGPT" readOnly value={task} rows={7}/></details></>}
      {snapshot?.state==="leased"&&<p>Проект ChatGPT получил задание. Оставьте чат открытым до сообщения об отправке результата, затем вернитесь на эту страницу.</p>}
      <div className="bridge-actions">{!unavailable&&<button className="secondary" disabled={busy} onClick={refresh}>Обновить статус</button>}{snapshot&&["queued","leased","awaiting_review","completed"].includes(snapshot.state)&&<button className="secondary danger" disabled={busy} onClick={()=>void action("cancel")}>Удалить сон с сервера</button>}{snapshot?.can_retry&&<button className="secondary" disabled={busy} onClick={()=>void action("retry")}>Повторить обработку</button>}{(unavailable||snapshot&&["completed","cancelled","failed","expired"].includes(snapshot.state))&&<button className="secondary" disabled={busy} onClick={reset}>Записать другой сон</button>}</div></section>
      {snapshot?.state==="awaiting_review"&&snapshot.candidate&&<section aria-label="Проверка разбора"><p className="eyebrow">ШАГ 3 ИЗ 3</p><h2>Проверьте разбор</h2><p>Сверьте детали с исходным сном. Формулировки являются возможной интерпретацией для размышления.</p><Card value={snapshot.candidate}/><label className="check-label"><input type="checkbox" disabled={busy} checked={!!snapshot.output_sha256&&checkedHash===snapshot.output_sha256} onChange={event=>setCheckedHash(event.target.checked?snapshot.output_sha256:null)}/>Я проверил, что разбор опирается на мой рассказ и не добавляет вымышленные события</label><div className="bridge-actions"><button className="primary" disabled={busy||!snapshot.output_sha256||checkedHash!==snapshot.output_sha256} onClick={()=>void action("review")}>Принять разбор</button><button className="secondary" disabled={busy} onClick={()=>void action("reject")}>Отклонить и повторить</button></div></section>}
      {snapshot?.result&&<section aria-label="Готовый разбор"><h2>Ваш разбор</h2><Card value={snapshot.result}/><p className="field-help">Это возможная интерпретация для саморазмышления, а не прогноз событий и не диагноз.</p></section>}
    </>}
    <details className="bridge-setup"><summary>Техническая информация</summary><p>Адрес MCP: <code>{config?.mcp_url}</code></p><p>{config?.oauth_configured?"Плагин Sonnic подключён. Если ChatGPT попросит авторизацию, разрешите её в этом же браузере.":"OAuth-мост ещё не настроен."}</p><button className="secondary" onClick={()=>void api("/bridge/connections/revoke",{method:"POST"}).then(()=>setNotice("Доступ ChatGPT к этой сессии отозван")).catch(fail)}>Отозвать доступ ChatGPT</button></details>
  </main></div>;
}
