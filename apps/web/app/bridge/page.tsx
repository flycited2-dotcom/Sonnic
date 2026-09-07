"use client";

import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../../lib/api";
import "./bridge.css";

type Reflection = { title: string; symbols: {label: string; evidence: string}[]; summary: string; key_connection: string; reflection_question: string };
type Result = { reflection: Reflection | null; safe_message: string | null; locale: string; outcome: string; origin_verified: false };
type Snapshot = { job_id: string; state: string; attempt: number; error: string | null; dream_text: string | null; candidate: Result | null; result: Result | null; output_sha256: string | null; can_retry: boolean };
type Config = { cases: {id: string; dream_text: string; locale: string}[]; oauth_configured: boolean; mcp_url: string };
const project = "https://chatgpt.com/g/g-p-6a9c47e07510819197bd898c90d1b12b-sonnic-baza-znanii-i-razbor-snov/project";
const states: Record<string,string> = { queued: "Ожидает запроса из ChatGPT", leased: "Задание получено обработчиком", awaiting_review: "Ответ ожидает вашей проверки", completed: "Проверенный результат", failed: "Ответ отклонён", expired: "Время ожидания истекло", cancelled: "Задание отменено" };
const errors: Record<string,string> = { session_required: "Сессия закончилась. Начните новое тестовое задание.", job_not_found: "Задание недоступно в текущей сессии.", queue_full: "Очередь заполнена. Завершите или отмените прежние задания.", connection_failed: "Не удалось связаться с сервером. Повторите действие.", review_changed: "Ответ изменился. Обновите статус и проверьте его заново.", retry_not_allowed: "Повтор больше недоступен.", not_awaiting_review: "Этот ответ уже проверен или устарел. Обновите статус.", review_rejected: "Вы отклонили ответ. Можно повторить обработку или выбрать другой тест.", lease_expired: "Обработчик не успел вернуть ответ. Можно повторить обработку.", job_expired: "Срок ожидания задания закончился. Можно повторить обработку." };

function Card({value}: {value: Result}) {
  const reflection = value.reflection;
  return <article className="result-card bridge-answer" lang={value.locale.split("-")[0]}>
    {reflection ? <><h2>{reflection.title}</h2><div className="symbols">{reflection.symbols.map((s,i) => <span className="symbol" key={i}>{s.label}<small>«{s.evidence}»</small></span>)}</div><p>{reflection.summary}</p><div className="connection"><p>{reflection.key_connection}</p></div><p className="reflection-question">{reflection.reflection_question}</p></> : <p>{value.safe_message}</p>}
  </article>;
}

export default function BridgePage() {
  const [config,setConfig]=useState<Config|null>(null), [selected,setSelected]=useState("blue-parrot-ru");
  const [jobId,setJobId]=useState<string|null>(null), [snapshot,setSnapshot]=useState<Snapshot|null>(null);
  const [claimKey,setClaimKey]=useState(""), [revision,setRevision]=useState(0), [busy,setBusy]=useState(false);
  const [error,setError]=useState(""), [notice,setNotice]=useState(""), [checkedHash,setCheckedHash]=useState<string|null>(null);
  const [unavailable,setUnavailable]=useState(false), [configRevision,setConfigRevision]=useState(0);
  const requestKey=useRef<{caseId:string; key:string}|null>(null);
  const generation=useRef(0), operation=useRef(false);
  function fail(e:unknown) { const code=e instanceof ApiError?e.code:"unknown"; setError(errors[code]||"Действие не выполнено. Обновите статус и попробуйте снова."); if(code==="session_required"||code==="job_not_found") {setUnavailable(true);setSnapshot(null);} }
  useEffect(()=>{ let stopped=false; void api<Config>("/bridge/config").then(value=>{if(stopped)return;setConfig(value);setSelected(current=>value.cases.some(c=>c.id===current)?current:value.cases[0]?.id||"");setError("");}).catch(e=>{if(!stopped)fail(e);}); return()=>{stopped=true;}; },[configRevision]);
  useEffect(()=>{ try { const linked=new URLSearchParams(window.location.search).get("job"); const id=linked||sessionStorage.getItem("sonnic.bridge.job"); if(id&&/^[a-f0-9-]{36}$/i.test(id)){const key=sessionStorage.getItem(`sonnic.bridge.claim.${id}`)||crypto.randomUUID();setJobId(id);setClaimKey(key);sessionStorage.setItem(`sonnic.bridge.claim.${id}`,key);window.history.replaceState(null,"",`/bridge?job=${id}`);} }catch{} },[]);
  useEffect(()=>{
    if(!jobId||busy||unavailable) return;
    const version=generation.current;
    let stopped=false; let timer:ReturnType<typeof setTimeout>;
    async function poll(){try { const next=await api<Snapshot>(`/bridge/jobs/${jobId}`); if(stopped||version!==generation.current)return; setSnapshot(next); setCheckedHash(current=>current===next.output_sha256?current:null); if(["queued","leased","awaiting_review"].includes(next.state))timer=setTimeout(poll,1500); }catch(e){if(!stopped&&version===generation.current)fail(e);}}
    void poll(); return()=>{stopped=true;clearTimeout(timer);};
  },[jobId,revision,busy,unavailable]);
  async function create(){
    if(operation.current||!config?.cases.some(c=>c.id===selected))return;
    operation.current=true;generation.current++;
    setBusy(true);setError("");setNotice("");
    try{
      await api("/sessions/guest",{method:"POST",body:JSON.stringify({locale:"ru-RU"})});
      if(requestKey.current?.caseId!==selected) requestKey.current={caseId:selected,key:crypto.randomUUID()};
      const job=await api<{job_id:string}>("/bridge/jobs",{method:"POST",headers:{"Idempotency-Key":requestKey.current.key},body:JSON.stringify({case_id:selected})});
      const key=crypto.randomUUID();setClaimKey(key);setJobId(job.job_id);setSnapshot(null);setCheckedHash(null);setUnavailable(false);
      try{sessionStorage.setItem("sonnic.bridge.job",job.job_id);sessionStorage.setItem(`sonnic.bridge.claim.${job.job_id}`,key);window.history.replaceState(null,"",`/bridge?job=${job.job_id}`);}catch{}
    }catch(e){fail(e);}finally{operation.current=false;setBusy(false);}
  }
  async function action(name:string){
    if(operation.current||!jobId)return;
    if((name==="review"||name==="reject")&&(!snapshot?.output_sha256||snapshot.state!=="awaiting_review"))return;
    if(name==="review"&&checkedHash!==snapshot?.output_sha256)return;
    operation.current=true;generation.current++;
    setBusy(true);setError("");setNotice("");
    try{
      const body=name==="review"||name==="reject"?JSON.stringify({approve:name==="review",output_sha256:snapshot?.output_sha256}):undefined;
      const next=await api<Snapshot>(`/bridge/jobs/${jobId}/${name==="reject"?"review":name}`,{method:"POST",body});
      setSnapshot(next);setCheckedHash(null);setRevision(v=>v+1);
    }catch(e){setCheckedHash(null);fail(e);}finally{operation.current=false;setBusy(false);}
  }
  function refresh(){generation.current++;setError("");setRevision(v=>v+1);}
  function reset(){generation.current++;setJobId(null);setSnapshot(null);setClaimKey("");setCheckedHash(null);setUnavailable(false);requestKey.current=null;setError("");setNotice("");try{sessionStorage.removeItem("sonnic.bridge.job");window.history.replaceState(null,"","/bridge");}catch{}}
  const task=`Синтетическое задание Sonnic. Через подключённый мост вызови claim_job с job_id "${jobId}" и claim_key "${claimKey}". Используй полученные данные и инструкции проекта. Верни JSON через submit_result с неизменными идентификаторами, версиями, хешем и attempt. Не утверждай публикацию: ответ ждёт проверки на сайте. Если инструмента нет, сообщи «мост не подключён»; не выдумывай выполнение.`;
  return <div className="site-shell bridge-shell"><header className="header"><a className="brand" href="/">sonnic.</a><span>Проверка связи с ChatGPT</span></header><main>
    <div className="mode-banner"><div><strong>Тестовый мост · только вымышленные сны</strong><span>Создайте задание и передайте его в проект ChatGPT с подключённым плагином Sonnic. Ответ вернётся сюда для проверки. Обработка запускается отдельным запросом в ChatGPT.</span></div></div>
    {error&&<p className="error" role="alert">{error}</p>}{notice&&<p className="notice" role="status">{notice}</p>}
    {!config&&error&&<button className="secondary" onClick={()=>{setError("");setConfigRevision(v=>v+1);}}>Повторить загрузку примеров</button>}
    {!jobId?<section className="input-card"><h1>От сайта до проверенного ответа</h1><p className="muted">Выберите один из подготовленных примеров. Личные рассказы на этом этапе недоступны.</p><label htmlFor="bridge-case">Тестовый сон</label><select id="bridge-case" value={selected} onChange={e=>setSelected(e.target.value)} disabled={busy||!config?.cases.length}>{config?.cases.map(c=><option key={c.id} value={c.id}>{c.dream_text} ({c.locale})</option>)}</select><p className="bridge-dream">{config?.cases.find(c=>c.id===selected)?.dream_text}</p><button className="primary" disabled={busy||!config?.cases.length} onClick={()=>void create()}>{busy?"Создаём задание…":"Создать тестовое задание"}</button></section>:<>
      <section className="input-card"><p className="eyebrow">ВЫМЫШЛЕННЫЙ ПРИМЕР</p><h1 aria-live="polite">{unavailable?"Задание недоступно":snapshot?states[snapshot.state]||snapshot.state:error?"Не удалось загрузить статус":"Загружаем статус…"}</h1>{snapshot?.dream_text&&<p className="bridge-dream">{snapshot.dream_text}</p>}{snapshot&&<p className="muted">Попытка: {snapshot.attempt} из 3</p>}<p className="field-help">Сохраните ссылку на эту страницу, чтобы вернуться к заданию в том же браузере.</p>
      {snapshot?.error&&<p className="error" role="status">{errors[snapshot.error]||"Обработка не завершилась. Обновите статус или повторите задание, если повтор доступен."}</p>}
      {snapshot?.state==="queued"&&<><p>Скопируйте задание в проект ChatGPT после подключения моста. Оно не запускается автоматически.</p><div className="bridge-actions"><button className="secondary" onClick={()=>{void navigator.clipboard.writeText(task).then(()=>setNotice("Задание скопировано")).catch(()=>setError("Не удалось скопировать. Выделите текст ниже вручную."));}}>Скопировать задание для ChatGPT</button><a className="secondary" href={project} target="_blank" rel="noreferrer">Открыть проект ChatGPT</a></div><details><summary>Текст задания</summary><textarea aria-label="Текст задания для ChatGPT" readOnly value={task} rows={6}/></details></>}
      {snapshot?.state==="leased"&&<p>Обработчик получил задание. Сайт ждёт ответ; его ещё нужно будет проверить. Резервирование действует до 10 минут.</p>}
      <div className="bridge-actions">{!unavailable&&<button className="secondary" disabled={busy} onClick={refresh}>Обновить статус</button>}{snapshot&&["queued","leased","awaiting_review"].includes(snapshot.state)&&<button className="secondary" disabled={busy} onClick={()=>void action("cancel")}>Отменить задание</button>}{snapshot?.can_retry&&<button className="secondary" disabled={busy} onClick={()=>void action("retry")}>Повторить обработку</button>}{(unavailable||snapshot&&["completed","cancelled","failed","expired"].includes(snapshot.state))&&<button className="secondary" disabled={busy} onClick={reset}>{unavailable?"Начать новый тест":"Другой тест"}</button>}</div></section>
      {snapshot?.state==="awaiting_review"&&snapshot.candidate&&<section aria-label="Проверка кандидата"><h2>Кандидат на проверку</h2><p>Сверьте детали с исходным сном. Ответ ещё не принят для показа; происхождение из ChatGPT транспортом не доказано.</p><Card value={snapshot.candidate}/><label className="check-label"><input type="checkbox" disabled={busy} checked={!!snapshot.output_sha256&&checkedHash===snapshot.output_sha256} onChange={e=>setCheckedHash(e.target.checked?snapshot.output_sha256:null)}/>Я проверил соответствие сну и отсутствие выдуманных событий</label><div className="bridge-actions"><button className="primary" disabled={busy||!snapshot.output_sha256||checkedHash!==snapshot.output_sha256} onClick={()=>void action("review")}>Принять и показать результат</button><button className="secondary" disabled={busy} onClick={()=>void action("reject")}>Отклонить ответ</button></div></section>}
      {snapshot?.result&&<section aria-label="Проверенный результат"><h2>Результат проверен на сайте</h2><Card value={snapshot.result}/><p className="field-help">Синтетический тест. Проверка пользователем не подтверждает происхождение ответа из ChatGPT.</p></section>}
    </>}
    <details className="bridge-setup"><summary>Настройка подключения</summary><p>Адрес MCP: <code>{config?.mcp_url}</code></p><p>{config?.oauth_configured?"В ChatGPT используйте плагин Sonnic с указанным адресом. Если срок тестового доступа истёк, подключитесь повторно в том же браузере.":"Клиент OAuth пока не настроен. Для ChatGPT требуется доступный HTTPS-адрес моста."}</p><button className="secondary" onClick={()=>void api("/bridge/connections/revoke",{method:"POST"}).then(()=>setNotice("Разрешения обработчика для этой сессии отозваны")).catch(fail)}>Отозвать доступ обработчика</button></details>
  </main></div>;
}
