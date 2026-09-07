"use client";

import { useEffect, useRef, useState, type FormEvent } from "react";
import { api, ApiError, type Config, type Job, type Locale } from "../lib/api";
import { messages } from "../lib/i18n";
import { VoiceInput } from "../components/voice-input";

const ENTRY_KEY = "sonnic.currentDream";
function Moon({ small = false }: { small?: boolean }) {
  return <svg width={small ? 22 : 34} height={small ? 22 : 34} viewBox="0 0 36 36" fill="none" aria-hidden="true"><path d="M28 23A13 13 0 0 1 13 5a13 13 0 1 0 15 18Z" fill="currentColor"/><path d="m26 4 1.4 4.6L32 10l-4.6 1.4L26 16l-1.4-4.6L20 10l4.6-1.4L26 4Z" fill="currentColor"/></svg>;
}

export default function Home() {
  const [locale, setLocale] = useState<Locale>("ru-RU");
  const t = messages[locale];
  const [config, setConfig] = useState<Config | null>(null);
  const [text, setText] = useState("");
  const [emotion, setEmotion] = useState("");
  const [recurring, setRecurring] = useState(false);
  const [date, setDate] = useState("");
  const [language, setLanguage] = useState("und");
  const [culture, setCulture] = useState("");
  const [voiceBusy, setVoiceBusy] = useState(false);
  const [recognitionLocale, setRecognitionLocale] = useState<string | null>(null);
  const [did, setDid] = useState<string | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState(false);
  const [pollVersion, setPollVersion] = useState(0);
  const attempt = useRef<{ payload: string; key: string } | null>(null);
  const resultRef = useRef<HTMLElement>(null);
  const formRef = useRef<HTMLTextAreaElement>(null);

  function errorCode(err: unknown) { return err instanceof ApiError ? err.code : "unknown"; }
  async function connect() {
    setError(null);
    try { setConfig(await api<Config>("/config")); }
    catch (err) { setError(errorCode(err)); }
  }

  useEffect(() => {
    // Only IDs and interface preference persist in browser storage, never raw dream text.
    try {
      const saved = localStorage.getItem("sonnic.locale");
      if (saved === "ru-RU" || saved === "en-US") setLocale(saved);
      const current = sessionStorage.getItem(ENTRY_KEY);
      if (current) setDid(current);
    } catch { /* Private browsing may disable storage; the current page still works. */ }
    void connect();
  }, []);

  useEffect(() => { document.documentElement.lang = locale.split("-")[0]; }, [locale]);

  useEffect(() => {
    if (!did) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const start = Date.now();
    async function poll() {
      try {
        const next = await api<Job>(`/dreams/${did}/free`);
        if (cancelled) return;
        setJob(next);
        setError(null);
        if (next.status === "completed" || next.status === "failed") return;
        if (Date.now() - start > 110000) { setError("poll_timeout"); return; }
        timer = setTimeout(poll, 700);
      } catch (err) { if (!cancelled) setError(errorCode(err)); }
    }
    void poll();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [did, pollVersion]);

  useEffect(() => {
    if (job?.status !== "completed" || !did) return;
    resultRef.current?.focus();
    // Event is recorded only after the result is rendered, and deduplicated by the API.
    void api(`/dreams/${did}/viewed`, { method: "POST" }).catch(() => {});
  }, [job?.status, did]);

  function changeLocale(value: Locale) {
    setLocale(value);
    try { localStorage.setItem("sonnic.locale", value); } catch {}
  }

  async function submit(event: FormEvent) {
    event.preventDefault();
    if (busy || voiceBusy) return;
    const payload = JSON.stringify({ dream_text: text.trim(), wake_emotion: emotion.trim(), recurring,
      dream_date: date || null, locale, content_language: language, recognition_locale: recognitionLocale, cultural_perspective: culture.trim() || null });
    if (text.trim().length < 20 || text.trim().length > 10000) { setError("invalid_input"); return; }
    setBusy(true); setError(null); setNotice(false);
    if (attempt.current?.payload !== payload) attempt.current = { payload, key: crypto.randomUUID() };
    try {
      const params = new URLSearchParams(window.location.search);
      await api("/sessions/guest", { method: "POST", body: JSON.stringify({ locale, attribution: {
        source: params.get("utm_source")?.slice(0, 100) || null,
        content_key: params.get("content_key")?.slice(0, 100) || null,
        campaign: params.get("utm_campaign")?.slice(0, 100) || null,
      } }) });
      const entry = await api<{dream_id: string}>("/dreams", { method: "POST", body: payload,
        headers: { "Idempotency-Key": attempt.current!.key } });
      setDid(entry.dream_id); setJob(null);
      try { sessionStorage.setItem(ENTRY_KEY, entry.dream_id); } catch {}
    } catch (err) { setError(errorCode(err)); }
    finally { setBusy(false); }
  }

  function reset() {
    setDid(null); setJob(null); setError(null); setText(""); setEmotion("");
    setDate(""); setRecurring(false); setCulture(""); setLanguage("und"); setRecognitionLocale(null); attempt.current = null;
    try { sessionStorage.removeItem(ENTRY_KEY); } catch {}
    setTimeout(() => formRef.current?.focus(), 0);
  }

  async function retry() {
    setBusy(true); setError(null);
    try {
      await api(`/dreams/${did}/free`, { method: "POST" });
      setJob(null); setPollVersion(v => v + 1);
    } catch (err) { setError(errorCode(err)); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true); setError(null);
    try { await api(`/dreams/${did}`, { method: "DELETE" }); reset(); setNotice(true); }
    catch (err) { setError(errorCode(err)); }
    finally { setBusy(false); }
  }

  const result = job?.result;
  const real = config?.ai_mode === "real";
  const displayReal = result ? result.mode === "real" : real;
  const stages = ["queued", "extracting", "composing", "completed"] as const;
  const stageIndex = stages.indexOf(job?.stage as typeof stages[number]);
  const errText = error ? (t.errors[error as keyof typeof t.errors] || t.errors.unknown) : null;

  return <div className="site-shell">
    <header className="header">
      <a href="/" className="brand" aria-label="Sonnic"><Moon /><span>sonnic<span className="brand-dot">.</span></span></a>
      <div className="nav"><a href="/bridge">{locale === "ru-RU" ? "Разбор через ChatGPT" : "ChatGPT reflection"}</a><a href="#approach">{t.how}</a><div className="locale-switch" role="group" aria-label={t.localeLabel}>
        <button type="button" aria-pressed={locale === "ru-RU"} onClick={() => changeLocale("ru-RU")}>RU</button>
        <button type="button" aria-pressed={locale === "en-US"} onClick={() => changeLocale("en-US")}>EN</button>
      </div></div>
    </header>

    <main>
      <div className="mode-banner"><span className="status-dot"/><div><strong>{displayReal ? t.real : config?.ai_mode === "disabled" ? t.off : t.demo}</strong><span>{displayReal ? t.realBody : config?.ai_mode === "disabled" ? t.disabled : t.demoBody}</span></div></div>
      {!displayReal&&<section className="bridge-entry" aria-label={locale==="ru-RU"?"Разбор через ChatGPT":"Analysis through ChatGPT"}>
        <div><strong>{locale==="ru-RU"?"Персональный разбор через ваш проект ChatGPT":"Personal reflection through your ChatGPT project"}</strong><p>{locale==="ru-RU"?"Введите или надиктуйте сон, передайте задание в проект ChatGPT и получите результат на сайте. API-ключ OpenAI не используется.":"Type or dictate a dream, hand the job to your ChatGPT project, and receive the result on the site. No OpenAI API key is used."}</p></div>
        <a className="secondary" href="/bridge">{locale==="ru-RU"?"Открыть рабочий разбор":"Open the reflection flow"}</a>
      </section>}
      {notice && <p className="notice" role="status">{t.deleted}</p>}
      {!did ? <div className="entry-grid">
        <section className="hero">
          <p className="eyebrow">{t.eyebrow}</p>
          <h1>{t.hero1}<br/><em>{t.hero2}</em></h1>
          <p className="intro">{t.intro}</p>
          <div className="dream-orbit" aria-hidden="true"><div className="orbit-ring ring-one"/><div className="orbit-ring ring-two"/><div className="orb"/><span className="star star-one">✦</span><span className="star star-two">✧</span><div className="orbit-caption">S O N N I C</div></div>
          <span className="edition">{t.edition}</span>
        </section>

        <section className="input-card" aria-labelledby="form-heading">
          <div className="card-top"><span className="small-moon"><Moon small/></span><span className="chip">{t.chip}</span></div>
          <h2 id="form-heading">{t.formTitle}</h2><p className="muted">{t.formSub}</p>
          <form onSubmit={submit}>
            <div className="field-top"><label htmlFor="dream">{t.dream}</label><button type="button" className="text-button" disabled={voiceBusy || busy} onClick={() => { setText(t.exampleText); setRecognitionLocale(null); setLanguage(locale.split("-")[0]); formRef.current?.focus(); }}>{t.example}</button></div>
            <textarea ref={formRef} id="dream" required minLength={20} maxLength={10000} rows={6} readOnly={voiceBusy || busy} placeholder={t.placeholder} value={text} onChange={e => setText(e.target.value)} aria-describedby="dream-limit"/>
            <div className="field-bottom" id="dream-limit"><span>{t.limit}</span><span>{text.length.toLocaleString(locale)} / 10 000</span></div>
            <VoiceInput locale={locale} text={text} disabled={busy} onBusy={setVoiceBusy} onText={(value, speechLocale) => { setText(value); setRecognitionLocale(speechLocale); formRef.current?.focus(); }}/>
            <label htmlFor="emotion">{t.emotion} <span className="optional">· {t.optional}</span></label>
            <input id="emotion" maxLength={120} placeholder={t.emotionPlaceholder} value={emotion} onChange={e => setEmotion(e.target.value)}/>
            <label className="checkbox"><input type="checkbox" checked={recurring} onChange={e => setRecurring(e.target.checked)}/><span>{t.recurring}</span></label>
            <details className="more-details"><summary>{t.details}</summary>
              <div className="detail-fields"><label htmlFor="date">{t.date}</label><input type="date" id="date" value={date} onChange={e => setDate(e.target.value)}/>
                <label htmlFor="language">{t.language}</label><select id="language" value={language} onChange={e => setLanguage(e.target.value)}><option value="und">{t.languageAuto}</option><option value="ru">Русский</option><option value="en">English</option><option value="ja">日本語</option><option value="zh">中文</option><option value="es">Español</option><option value="fr">Français</option><option value="de">Deutsch</option><option value="ar">العربية</option><option value="mul">{t.languageOther}</option></select><p className="field-help">{t.languageHelp}</p>
                <label htmlFor="culture">{t.culture}</label><input id="culture" value={culture} maxLength={120} onChange={e => setCulture(e.target.value)} placeholder={t.culturePlaceholder}/><p className="field-help">{t.cultureHelp}</p>
              </div>
            </details>
            {errText && <p className="error" role="alert">{errText}</p>}
            {!config && <p className="field-help">{t.connecting} <button type="button" className="text-button" onClick={connect}>{t.connectionRetry}</button></p>}
            {config?.ai_mode === "disabled" && <p className="error">{t.disabled}</p>}
            <button className="primary" disabled={busy || voiceBusy || (!config || config.ai_mode === "disabled")} type="submit">{busy ? t.submitting : real ? t.submitReal : t.submit}<span aria-hidden="true">↗</span></button>
            <p className="privacy"><span aria-hidden="true">◇</span>{real ? t.privacyReal : t.privacy}</p>
          </form>
        </section>
      </div> : <section className="result-area" ref={resultRef} tabIndex={-1} aria-labelledby="result-heading">
        {result && job?.status === "completed" ? <>
          <div className="result-heading"><p className="eyebrow">{result.mode === "real" ? t.freeReal : t.free}</p><h1 id="result-heading" lang={result.locale}>{result.title}</h1><span className="result-locale">{t.resultLanguage}: {result.locale}</span></div>
          <div className="result-grid"><article className="result-card" lang={result.locale}>
            <div className="symbol-section"><h2>{t.symbols}</h2>{result.symbols.length ? <div className="symbols">{result.symbols.map(s => <span className="symbol" key={s.label}>{s.label}<small>«{s.evidence}»</small></span>)}</div> : <p className="muted">{result.mode === "real" ? t.noSymbolsReal : t.noSymbols}</p>}</div>
            <p className="emotion-line"><span>{t.tone}</span>{result.emotional_tone}</p>
            <div className="prose">{result.summary.split("\n\n").map((p, i) => <p key={i}>{p}</p>)}</div>
            <div className="connection"><h2>{t.connection}</h2><p>{result.key_connection}</p></div>
          </article><aside className="reflection-card"><span className="small-moon"><Moon/></span><p className="eyebrow">{t.question}</p><p className="reflection-question" lang={result.locale}>{result.reflection_question}</p><div className="rule"/><h2>{t.future}</h2><ul>{result.locked_sections.map(s => <li key={s} lang={result.locale}><span aria-hidden="true">◇</span>{s}</li>)}</ul><p className="field-help">{t.futureBody}</p></aside></div>
        </> : <div className="processing-card"><span className={`small-moon ${job?.status !== "failed" && !error ? "pulse" : ""}`}><Moon/></span><h1 id="result-heading">{job?.status === "failed" ? t.failed : real ? t.processingReal : t.processing}</h1><p className="muted">{t.processingSub}</p><ol className="stages" aria-live="polite">{stages.map((stage, i) => <li key={stage} className={i <= stageIndex ? "active" : ""}><span>{i < stageIndex ? "✓" : i+1}</span>{real && stage === "extracting" ? t.extractingReal : real && stage === "composing" ? t.composingReal : t[stage]}</li>)}</ol>{job?.error && <p className="error" role="alert">{t.errors[job.error as keyof typeof t.errors] || t.errors.unknown}</p>}{job?.can_retry && <button className="primary" onClick={retry} disabled={busy}>{t.retry}</button>}</div>}
        {errText && <p className="error" role="alert">{errText}</p>}
        <div className="result-actions">{error && <button className="secondary" onClick={() => { setError(null); setPollVersion(v => v+1); }}>{t.refresh}</button>}<button className="secondary" disabled={busy} onClick={reset}>{t.newDream}</button><button className="text-button delete-button" disabled={busy} onClick={remove}>{busy ? t.deleting : t.delete}</button></div>
      </section>}

      {!did && <section className="steps" aria-label={t.how}>{[1,2,3].map((n) => <div key={n}><span className="step-number">0{n}</span><div><h3>{t[`step${n}` as "step1"]}</h3><p>{t[`step${n}body` as "step1body"]}</p></div></div>)}</section>}
      <p className="disclaimer">{t.disclaimer}</p>
      <details id="approach" className="approach"><summary>{t.approachTitle}</summary><p>{t.approachBody}</p></details>
    </main>
    <footer><span>{t.footer}</span><span>© 2026 Sonnic</span></footer>
  </div>;
}
