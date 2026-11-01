import { useCallback, useEffect, useRef, useState } from "react";
import { listen, speak, speakable, stopSpeaking } from "./speech";

const PHASE_LABEL = {
  idle: "Tap to talk",
  listening: "Listening…",
  thinking: "Thinking…",
  speaking: "Speaking",
  error: "Tap to try again",
};

// Full-screen voice conversation over the chat: listen -> send -> speak the reply -> listen again.
// Messages go through the normal chat pipeline (sendMessage), so they also appear in the chat.
export default function VoiceMode({ sendMessage, onClose }) {
  const [phase, setPhase] = useState("idle");
  const [heard, setHeard] = useState("");
  const [reply, setReply] = useState("");
  const [status, setStatus] = useState(null);
  const [error, setError] = useState(null);
  const closed = useRef(false);
  const recognition = useRef(null);

  // Conversation loop: listen, send, speak, repeat, until closed or nothing is heard
  const converse = useCallback(async () => {
    while (!closed.current) {
      setError(null);
      setStatus(null);
      setHeard("");
      setPhase("listening");
      let text;
      try {
        recognition.current = listen(setHeard);
        text = await recognition.current.done;
      } catch (e) {
        if (!closed.current) {
          setError(e.message);
          setPhase("error");
        }
        return;
      } finally {
        recognition.current = null;
      }
      if (closed.current) return;
      if (!text) return setPhase("idle");

      setHeard(text);
      setReply("");
      setPhase("thinking");
      let answer = "";
      const { error: failed } = await sendMessage(text, {
        voice: true,
        onEvent: (ev) => {
          if (ev.type === "text") answer += ev.text;
          else if (ev.type === "retract") answer = answer.slice(0, Math.max(0, answer.length - ev.chars));
          else if (ev.type === "tool_start") setStatus(`${ev.label}…`);
          else if (ev.type === "tool_end") setStatus(ev.summary);
          if (!closed.current) setReply(speakable(answer));
        },
      });
      if (closed.current) return;
      if (failed || !answer.trim()) {
        setError(failed || "No reply came back.");
        return setPhase("error");
      }

      setPhase("speaking");
      await speak(speakable(answer)); // resolves early if interrupted, then we listen again
    }
  }, [sendMessage]);

  // Start listening as soon as voice mode opens (it opens from a tap, which also unlocked speech)
  useEffect(() => {
    closed.current = false;
    converse();
    return () => {
      closed.current = true;
      recognition.current?.abort();
      stopSpeaking();
    };
  }, [converse]);

  const close = useCallback(() => {
    closed.current = true;
    recognition.current?.abort();
    stopSpeaking();
    onClose();
  }, [onClose]);

  useEffect(() => {
    const onKey = (e) => e.key === "Escape" && close();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [close]);

  function tapOrb() {
    if (phase === "listening") recognition.current?.stop(); // done talking: send now
    else if (phase === "speaking") {
      stopSpeaking(); // interrupt; the turn loop then listens again
    } else if (phase === "idle" || phase === "error") converse();
  }

  return (
    <div className={`voice-overlay ${phase}`} role="dialog" aria-modal="true" aria-label="Voice mode">
      <button className="icon-button voice-close" onClick={close} aria-label="Close voice mode" title="Close">
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <path d="M6 6l12 12M18 6L6 18" />
        </svg>
      </button>

      <div className="voice-stage">
        <button className="orb" onClick={tapOrb} disabled={phase === "thinking"}
          aria-label={phase === "listening" ? "Stop listening" : phase === "speaking" ? "Interrupt" : "Start talking"}>
          <span className="orb-ring" />
          <span className="orb-core" />
        </button>
        <p className="voice-phase" aria-live="polite">{PHASE_LABEL[phase]}</p>
        {status && <p className="voice-status">{status}</p>}
        {heard && <p className="voice-heard">“{heard}”</p>}
        {reply && phase !== "listening" && <p className="voice-reply">{reply}</p>}
        {error && <p className="voice-error">{error}</p>}
      </div>
    </div>
  );
}
