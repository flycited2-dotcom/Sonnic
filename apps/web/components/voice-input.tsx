"use client";

import { useEffect, useRef, useState } from "react";
import type { Locale } from "../lib/api";
import ru from "../messages/voice-ru.json";
import en from "../messages/voice-en.json";

type SpeechResult = { isFinal: boolean; 0: { transcript: string } };
type Recognition = {
  lang: string; continuous: boolean; interimResults: boolean;
  onstart: (() => void) | null;
  onaudiostart: (() => void) | null;
  onend: (() => void) | null;
  onresult: ((event: { results: ArrayLike<SpeechResult> }) => void) | null;
  onerror: ((event: { error: string }) => void) | null;
  start(): void; stop(): void; abort(): void;
};
type SpeechWindow = Window & { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
type State = "idle" | "starting" | "listening" | "stopping";

export function VoiceInput({ locale, text, disabled, onText, onBusy }: {
  locale: Locale; text: string; disabled: boolean;
  onText: (text: string, recognitionLocale: string) => void; onBusy: (busy: boolean) => void;
}) {
  const t = locale === "ru-RU" ? ru : en;
  const [supported, setSupported] = useState<boolean | null>(null);
  const [language, setLanguage] = useState("ru-RU");
  const [state, setState] = useState<State>("idle");
  const [preview, setPreview] = useState("");
  const [status, setStatus] = useState<"done" | "cancelled" | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [recovery, setRecovery] = useState("");
  const [recoveryLanguage, setRecoveryLanguage] = useState("ru-RU");
  const [diagnostic, setDiagnostic] = useState("");
  const recognition = useRef<Recognition | null>(null);
  const requestStop = useRef<(() => void) | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const active = state !== "idle";

  function clearTimer() { if (timer.current) clearTimeout(timer.current); timer.current = null; }
  function detach(rec: Recognition) { rec.onstart = null; rec.onaudiostart = null; rec.onend = null; rec.onresult = null; rec.onerror = null; }
  function abort(rec: Recognition) { try { rec.abort(); } catch {} }

  useEffect(() => {
    const win = window as SpeechWindow;
    setSupported(Boolean(window.isSecureContext && (win.SpeechRecognition || win.webkitSpeechRecognition)));
    return () => {
      clearTimer();
      if (recognition.current) { detach(recognition.current); abort(recognition.current); }
      recognition.current = null; requestStop.current = null;
    };
  }, []);

  function cancel() {
    clearTimer();
    const rec = recognition.current;
    if (rec) { detach(rec); abort(rec); }
    requestStop.current = null;
    recognition.current = null; setState("idle"); onBusy(false);
    setPreview(""); setRecovery(""); setError(null); setStatus("cancelled"); setDiagnostic("");
  }

  function start() {
    const win = window as SpeechWindow;
    const Constructor = win.SpeechRecognition || win.webkitSpeechRecognition;
    if (!Constructor || recognition.current || disabled || recovery) return;
    setError(null); setStatus(null); setPreview(""); setDiagnostic("");
    function exceptionCode(err: unknown) {
      return err instanceof DOMException && (err.name === "NotAllowedError" || err.name === "SecurityError") ? "not-allowed" : "unknown";
    }
    let rec: Recognition;
    try { rec = new Constructor(); } catch (err) { setError(exceptionCode(err)); return; }
    recognition.current = rec;
    const base = text;
    let finalText = "";
    let heardText = "";
    let started = false;
    let audioStarted = false;
    let stopRequested = false;
    const beganAt = Date.now();
    function finish(code?: string) {
      if (recognition.current !== rec) return;
      clearTimer(); detach(rec); recognition.current = null; requestStop.current = null;
      setState("idle"); onBusy(false); setPreview("");
      const merged = [base.trim(), finalText.trim()].filter(Boolean).join("\n");
      if (!code && !finalText.trim()) code = stopRequested ? "no-speech" : "ended-early";
      if (!code && merged.length > 10000) code = "too-long";
      if (code) {
        const knownCode = Object.hasOwn(t.errors, code) ? code : "unknown";
        setError(knownCode); setRecovery(heardText); setRecoveryLanguage(language);
        // Local diagnostics contain no transcript, device identifier or audio.
        setDiagnostic(`${knownCode}; start=${started}; audio=${audioStarted}; stop=${stopRequested}; ${Date.now() - beganAt}ms`);
        return;
      }
      onText(merged, language); setStatus("done");
    }
    function fail(code: string) {
      if (recognition.current !== rec) return;
      finish(code); abort(rec);
    }
    rec.onstart = () => {
      if (recognition.current !== rec) return;
      started = true;
    };
    rec.onaudiostart = () => {
      if (recognition.current !== rec || stopRequested) return;
      audioStarted = true;
      clearTimer(); setState("listening");
      // Bound the capture session; no file or audio blob is created by Sonnic.
      timer.current = setTimeout(() => requestStop.current?.(), 120000);
    };
    rec.onresult = (event) => {
      if (recognition.current !== rec) return;
      const results = Array.from(event.results);
      finalText = results.filter(r => r.isFinal).map(r => r[0].transcript).join(" ");
      heardText = results.map(r => r[0].transcript).join(" ").trim();
      setPreview(heardText);
    };
    rec.onerror = (event) => fail(event.error);
    rec.onend = () => finish();
    requestStop.current = () => {
      if (recognition.current !== rec || stopRequested) return;
      stopRequested = true;
      clearTimer(); setState("stopping");
      timer.current = setTimeout(() => fail("timeout"), 10000);
      try { rec.stop(); } catch (err) { fail(exceptionCode(err)); }
    };
    setState("starting"); onBusy(true);
    timer.current = setTimeout(() => fail("timeout"), 20000);
    try { rec.lang = language; rec.continuous = true; rec.interimResults = true; rec.start(); }
    catch (err) { fail(exceptionCode(err)); }
  }

  function stop() {
    requestStop.current?.();
  }

  const recoveredText = [text.trim(), recovery.trim()].filter(Boolean).join("\n");
  function recover() {
    if (disabled || !recovery.trim() || recoveredText.length > 10000) return;
    onText(recoveredText, recoveryLanguage);
    setRecovery(""); setError(null); setDiagnostic(""); setStatus("done");
  }

  return <div className="voice-input">
    <div className="voice-controls">
      {!active ? <button type="button" className="mic-button" disabled={disabled || !supported || Boolean(recovery)} onClick={start} aria-describedby="voice-notice"><svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true"><rect x="9" y="2" width="6" height="12" rx="3"/><path d="M5 10v2a7 7 0 0 0 14 0v-2M12 19v3m-4 0h8"/></svg>{t.start}</button> : <><button type="button" className="mic-button recording" onClick={stop} disabled={state !== "listening"}>{t.stop}</button><button type="button" className="text-button" onClick={cancel}>{t.cancel}</button></>}
      <label className="voice-language">{t.language}<select aria-label={t.language} value={language} disabled={active || disabled} onChange={e => setLanguage(e.target.value)}><option value="ru-RU">Русский</option><option value="en-US">English</option><option value="ja-JP">日本語</option><option value="zh-CN">中文</option><option value="es-ES">Español</option><option value="fr-FR">Français</option><option value="de-DE">Deutsch</option><option value="ar-SA">العربية</option></select></label>
    </div>
    <p id="voice-notice" className="field-help">{supported === false ? t.unsupported : t.notice}</p>
    <div role="status" aria-live="polite" className="voice-status">{active ? t[state as Exclude<State,"idle">] : status ? t[status] : ""}</div>
    {preview && <p className="voice-preview" aria-label={t.preview}>{preview}</p>}
    {error && <p className="error" role="alert">{t.errors[error as keyof typeof t.errors] || t.errors.unknown}</p>}
    {recovery && <div className="voice-recovery">
      <label htmlFor="voice-recovery">{t.recovery}</label>
      <textarea id="voice-recovery" value={recovery} disabled={disabled} onChange={e => setRecovery(e.target.value)} />
      <p className="field-help">{t.recoveryHint}</p>
      <button type="button" className="mic-button" disabled={disabled || !recovery.trim() || recoveredText.length > 10000} onClick={recover}>{t.insertRecovery}</button>{" "}
      <button type="button" className="text-button" disabled={disabled} onClick={() => setRecovery("")}>{t.discardRecovery}</button>
    </div>}
    {(error || supported === false) && <div className="field-help"><p>{t.fallback}</p><p>{t.windowsFallback} <a href="https://support.microsoft.com/en-us/accessibility/windows/use-voice-typing-to-talk-instead-of-type-on-your-pc" target="_blank" rel="noreferrer">{t.windowsHelp}</a></p></div>}
    {diagnostic && <details className="field-help"><summary>{t.diagnostic}</summary><code>{diagnostic}</code></details>}
  </div>;
}
