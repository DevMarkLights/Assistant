import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { MicError, audioSettled, closeMicSession, lastUploadTiming, listenOnce, savedLook, speak, speakable, splitWords, stopSpeaking } from "./speech";
import VoiceVisual from "./VoiceVisual";

const PHASE_LABEL = {
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking",
  error: "Tap to try again",
};

// Full-screen voice conversation over the chat: listen -> send -> speak the reply -> listen again.
// Messages go through the normal chat pipeline (sendMessage), so they also appear in the chat.
// It keeps going hands-free until closed. Each turn records what the user says (listenOnce: the mic
// is opened once per session and muted between turns) and has the server transcribe it. A failed transcription or reply
// is shown while it listens again; only a microphone error stops it.
export default function VoiceMode({ sendMessage, onClose }) {
  const [phase, setPhase] = useState("listening");
  const [look] = useState(savedLook); // wave or orb, chosen in Settings
  const [heard, setHeard] = useState("");
  const [reply, setReply] = useState("");
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const [micStage, setMicStage] = useState(null); // what the mic is doing, shown while listening
  const [spokenWord, setSpokenWord] = useState(-1); // index of the word being spoken, for highlighting
  const [activity, setActivity] = useState(0); // bumped on every heard or spoken word: animates the visual
  const bump = useCallback(() => setActivity((n) => n + 1), []);
  const replyRef = useRef(null);
  const closed = useRef(false);
  const take = useRef(null); // the current listenOnce: stop() sends it now, abort() drops it
  const running = useRef(false); // one conversation loop at a time
  const lastBump = useRef(0);
  const lastTiming = useRef(null); // how long the previous turn's steps took, for the server log

  // Conversation loop: listen, send, speak, repeat, until closed
  const conversation = useCallback(async () => {
    while (!closed.current) {
      setStatus(null);
      setHeard("");
      setMicStage(null);
      setPhase("listening");
      // Don't open the mic while audio is still playing (the unlock clip, or the reply just spoken)
      await audioSettled();
      if (closed.current) return;
      let text;
      const marks = {}; // when each step of this turn happened (performance.now), for lastTiming
      try {
        take.current = listenOnce({
          timing: lastTiming.current,
          onStage: (stage) => {
            if (stage === "transcribing…") marks.stopped = performance.now();
            setMicStage(stage);
          },
          onLevel: (level) => {
            // Animate with the voice: a bump at most every 120 ms while it's loud
            const now = performance.now();
            if (level > 0.3 && now - lastBump.current > 120) {
              lastBump.current = now;
              bump();
            }
          },
        });
        text = await take.current.done;
      } catch (e) {
        if (closed.current) return;
        setError(e.message);
        if (e instanceof MicError) return setPhase("error"); // needs the user (permission)
        await new Promise((resolve) => setTimeout(resolve, 2000)); // transcription failed: listen again
        continue;
      } finally {
        take.current = null;
      }
      if (closed.current) return;
      if (!text) continue;
      marks.transcribed = performance.now();
      marks.upload = lastUploadTiming();
      setError(null);

      setHeard(text);
      setReply("");
      setPhase("thinking");
      let answer = "";
      const { error: failed } = await sendMessage(text, {
        voice: true,
        onEvent: (ev) => {
          if (ev.type === "text") {
            answer += ev.text;
            marks.firstText ??= performance.now();
          }
          else if (ev.type === "retract") answer = answer.slice(0, Math.max(0, answer.length - ev.chars));
          else if (ev.type === "tool_start") setStatus(`${ev.label}…`);
          else if (ev.type === "tool_end") setStatus(ev.summary);
          if (!closed.current) setReply(speakable(answer));
        },
      });
      marks.replyDone = performance.now();
      if (closed.current) return;
      if (failed || !answer.trim()) {
        // Shown while it listens again, until the next words are heard
        setError(failed || "No reply came back.");
        continue;
      }

      setSpokenWord(-1);
      setPhase("speaking");
      marks.speak = performance.now();
      // resolves early if interrupted, then we listen again
      await speak(speakable(answer), {
        onWord: (i) => {
          marks.sound ??= performance.now(); // the first word is playing
          if (closed.current) return;
          setSpokenWord((prev) => {
            if (prev !== i) bump();
            return i;
          });
        },
      });
      // Sent with the next recording, so the server log shows where this turn's time went
      const since = (from, to) => (marks[from] != null && marks[to] != null ? `${((marks[to] - marks[from]) / 1000).toFixed(1)}s` : "-");
      lastTiming.current = `transcription ${since("stopped", "transcribed")} (${marks.upload || "no breakdown"}), first words ${since("transcribed", "firstText")}, `
        + `reply done ${since("transcribed", "replyDone")}, until speaking ${since("replyDone", "speak")}, `
        + `until sound ${since("speak", "sound")} (${speakable(answer).length} chars)`;
    }
  }, [sendMessage, bump]);

  // Starts the loop unless it's already running (a second loop would open a second mic)
  const converse = useCallback(async () => {
    if (running.current) return;
    running.current = true;
    try {
      await conversation();
    } finally {
      running.current = false;
    }
  }, [conversation]);

  // Start the conversation as soon as voice mode opens (it opens from a tap, which also unlocked
  // speech); the loop opens the mic once the unlock clip has finished
  useEffect(() => {
    closed.current = false;
    converse();
    return () => {
      closed.current = true;
      take.current?.abort();
      stopSpeaking();
      closeMicSession();
    };
  }, [converse]);

  const close = useCallback(() => {
    closed.current = true;
    take.current?.abort();
    stopSpeaking();
    closeMicSession();
    onClose();
  }, [onClose]);

  // Keep the word being spoken in view in long replies
  useEffect(() => {
    if (spokenWord < 0) return;
    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Scroll the reply box itself; scrollIntoView would also scroll the chat underneath the overlay
    const box = replyRef.current;
    const word = box?.querySelector(".current");
    if (!word) return;
    const outer = box.getBoundingClientRect();
    const inner = word.getBoundingClientRect();
    const by = inner.top < outer.top ? inner.top - outer.top : inner.bottom > outer.bottom ? inner.bottom - outer.bottom : 0;
    if (by) box.scrollBy({ top: by, behavior: reduced ? "auto" : "smooth" });
  }, [spokenWord]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  function tapOrb() {
    if (phase === "listening") take.current?.stop(); // done talking: send now
    else if (phase === "speaking") {
      stopSpeaking(); // interrupt; the turn loop then listens again
    } else if (phase === "error") {
      setError(null);
      converse();
    }
  }

  return (
    <div className={`voice-overlay ${phase}`} role="dialog" aria-modal="true" aria-label="Voice mode">
      <button className="icon-button voice-close" onClick={close} aria-label="Close voice mode" title="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>

      <div className="voice-stage">
        <button className={`voice-visual-button ${look}`} onClick={tapOrb} disabled={phase === "thinking"}
          aria-label={phase === "listening" ? "Done talking" : phase === "speaking" ? "Interrupt" : "Start listening again"}>
          <VoiceVisual phase={phase} activity={activity} look={look} />
        </button>
        <p className="voice-phase" aria-live="polite">{PHASE_LABEL[phase]}</p>
        {phase === "listening" && micStage && !heard && <p className="voice-status">{micStage}</p>}
        {status && <p className="voice-status">{status}</p>}
        {heard && <p className="voice-heard">“{heard}”</p>}
        {reply && phase !== "listening" && (
          <p className="voice-reply" ref={replyRef}>
            {phase === "speaking"
              ? splitWords(reply).map((word, i) => (
                  <Fragment key={i}>
                    <span className={i < spokenWord ? "spoken" : i === spokenWord ? "current" : "upcoming"}>{word}</span>{" "}
                  </Fragment>
                ))
              : reply}
          </p>
        )}
        {error && <p className="voice-error">{error}</p>}
      </div>
    </div>
  );
}
