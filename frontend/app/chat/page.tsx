"use client";

import { useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { API_BASE } from "@/lib/config";

type Message = { role: "customer" | "agent"; text: string; at: Date };
type RecordedClip = { blob: Blob; extension: string; url: string };

const SUGGESTIONS = [
  "I'd like to return order ORD-1001, it's unopened",
  "I want a refund for my smartwatch, order ORD-1002",
  "I want to return my oak side table",
];

const CHECKING_PHRASES = [
  "One moment, checking that for you…",
  "Sure thing, let me look into that…",
  "Got it — give me just a second…",
  "Okay, checking on that now…",
  "One sec, pulling that up…",
];

function pickCheckingPhrase() {
  return CHECKING_PHRASES[Math.floor(Math.random() * CHECKING_PHRASES.length)];
}

function timeLabel(d: Date) {
  return d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function AgentIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="16" height="16">
      <path d="M12 2 4 5v6c0 5 3.4 9.4 8 11 4.6-1.6 8-6 8-11V5l-8-3z" />
      <path d="m9 12 2 2 4-4" />
    </svg>
  );
}

function MicIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="18" height="18">
      <rect x="9" y="2" width="6" height="12" rx="3" />
      <path d="M5 10a7 7 0 0 0 14 0" />
      <path d="M12 17v4M8 21h8" />
    </svg>
  );
}

function StopIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="currentColor" width="14" height="14">
      <rect x="5" y="5" width="14" height="14" rx="2" />
    </svg>
  );
}

function PaperclipIcon() {
  return (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" width="18" height="18">
      <path d="M21 11.5 12.5 20a4.5 4.5 0 0 1-6.36-6.36L14.5 5.28a3 3 0 0 1 4.24 4.24L10.4 17.86a1.5 1.5 0 0 1-2.12-2.12l7.78-7.78" />
    </svg>
  );
}

/** Claude's API caps images at 10MB -- phone photos and AI-generated images
 * routinely exceed that. Downscale + re-encode as JPEG in the browser before
 * upload so this just never comes up, rather than asking the user to resize
 * their own files. */
async function resizeImageFile(file: File, maxDimension = 1600, quality = 0.82): Promise<File> {
  const bitmap = await createImageBitmap(file);
  const scale = Math.min(1, maxDimension / Math.max(bitmap.width, bitmap.height));
  const width = Math.round(bitmap.width * scale);
  const height = Math.round(bitmap.height * scale);

  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  canvas.getContext("2d")?.drawImage(bitmap, 0, 0, width, height);

  const blob = await new Promise<Blob>((resolve, reject) => {
    canvas.toBlob((b) => (b ? resolve(b) : reject(new Error("canvas.toBlob failed"))), "image/jpeg", quality);
  });

  return new File([blob], file.name.replace(/\.[^.]+$/, "") + ".jpg", { type: "image/jpeg" });
}

/** Instant, free, local acknowledgment while the real request (Whisper -> agent
 * -> TTS) is still in flight -- uses the browser's built-in voice, not the
 * OpenAI pipeline, specifically so it can speak immediately with no network
 * round trip. Feature-detected; silently does nothing if unsupported. */
function speakInterim(text: string) {
  if (typeof window === "undefined" || !("speechSynthesis" in window)) return;
  window.speechSynthesis.cancel();
  window.speechSynthesis.speak(new SpeechSynthesisUtterance(text));
}

export default function ChatPage() {
  const threadId = useRef(crypto.randomUUID());
  const sending = useRef(false); // synchronous guard -- React state updates are async and can't
  // reliably block a fast double-submit (double-click, or Enter + button both firing) on their own.
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [pending, setPending] = useState(false);
  const [pendingText, setPendingText] = useState("");
  const [recording, setRecording] = useState(false);
  const [recordedClip, setRecordedClip] = useState<RecordedClip | null>(null);
  const [voiceError, setVoiceError] = useState<string | null>(null);
  const [attachedImage, setAttachedImage] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const silenceTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const silenceRafRef = useRef<number | null>(null);

  async function send(text: string) {
    const trimmed = text.trim();
    if (!trimmed || sending.current) return;
    sending.current = true;

    const imageToSend = attachedImage;
    setMessages((prev) => [
      ...prev,
      { role: "customer", text: imageToSend ? `${trimmed} (photo attached: ${imageToSend.name})` : trimmed, at: new Date() },
    ]);
    setInput("");
    setAttachedImage(null);
    setPending(true);
    setPendingText(pickCheckingPhrase());

    try {
      const formData = new FormData();
      formData.append("thread_id", threadId.current);
      formData.append("message", trimmed);
      if (imageToSend) formData.append("image", imageToSend);

      const res = await fetch(`${API_BASE}/chat`, { method: "POST", body: formData });
      if (!res.ok) throw new Error(`Server returned ${res.status}`);
      const data = await res.json();
      setMessages((prev) => [...prev, { role: "agent", text: data.reply, at: new Date() }]);
    } catch {
      setMessages((prev) => [
        ...prev,
        { role: "agent", text: "Sorry, something went wrong reaching the agent. Please try again.", at: new Date() },
      ]);
    } finally {
      setPending(false);
      sending.current = false;
    }
  }

  async function sendVoice(blob: Blob, extension: string, image: File | null) {
    if (sending.current) return;
    sending.current = true;
    setPending(true);
    const phrase = pickCheckingPhrase();
    setPendingText(phrase);
    speakInterim(phrase);

    try {
      const formData = new FormData();
      formData.append("audio", blob, `recording.${extension}`);
      formData.append("thread_id", threadId.current);
      if (image) formData.append("image", image);

      const res = await fetch(`${API_BASE}/voice`, { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Server returned ${res.status}`);

      setMessages((prev) => [
        ...prev,
        {
          role: "customer",
          text: image ? `${data.transcript} (photo attached: ${image.name})` : data.transcript,
          at: new Date(),
        },
        { role: "agent", text: data.reply, at: new Date() },
      ]);

      if ("speechSynthesis" in window) window.speechSynthesis.cancel();
      const audio = new Audio(`data:audio/mpeg;base64,${data.audio_base64}`);
      audio.play().catch(() => {
        // Autoplay can be blocked by the browser; the text reply is already shown either way.
      });
    } catch (err) {
      const detail = err instanceof Error && err.message ? err.message : "Sorry, something went wrong with voice.";
      setMessages((prev) => [
        ...prev,
        { role: "agent", text: `${detail} Please try again.`, at: new Date() },
      ]);
    } finally {
      setPending(false);
      sending.current = false;
    }
  }

  const SILENCE_THRESHOLD = 8; // 0-255 scale; empirical, room-noise dependent
  const SILENCE_DURATION_MS = 5000;

  function cleanupSilenceDetection() {
    if (silenceRafRef.current) cancelAnimationFrame(silenceRafRef.current);
    silenceRafRef.current = null;
    if (silenceTimeoutRef.current) clearTimeout(silenceTimeoutRef.current);
    silenceTimeoutRef.current = null;
    audioContextRef.current?.close().catch(() => {});
    audioContextRef.current = null;
    analyserRef.current = null;
  }

  function watchForSilence(stream: MediaStream) {
    const audioContext = new AudioContext();
    const source = audioContext.createMediaStreamSource(stream);
    const analyser = audioContext.createAnalyser();
    analyser.fftSize = 512;
    source.connect(analyser);
    audioContextRef.current = audioContext;
    analyserRef.current = analyser;

    const data = new Uint8Array(analyser.frequencyBinCount);

    const check = () => {
      if (!analyserRef.current) return; // recording already stopped, cleaned up
      analyser.getByteFrequencyData(data);
      const avgVolume = data.reduce((sum, v) => sum + v, 0) / data.length;

      if (avgVolume > SILENCE_THRESHOLD) {
        if (silenceTimeoutRef.current) {
          clearTimeout(silenceTimeoutRef.current);
          silenceTimeoutRef.current = null;
        }
      } else if (!silenceTimeoutRef.current) {
        // 5 seconds of quiet -- assume the customer is done talking.
        silenceTimeoutRef.current = setTimeout(stopRecording, SILENCE_DURATION_MS);
      }

      silenceRafRef.current = requestAnimationFrame(check);
    };
    check();
  }

  async function startRecording() {
    if (recording || pending || recordedClip) return;
    setVoiceError(null);
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      const mimeType = MediaRecorder.isTypeSupported("audio/webm") ? "audio/webm" : "audio/mp4";
      const recorder = new MediaRecorder(stream, { mimeType });
      chunksRef.current = [];

      recorder.ondataavailable = (e) => {
        if (e.data.size > 0) chunksRef.current.push(e.data);
      };
      recorder.onstop = () => {
        const blob = new Blob(chunksRef.current, { type: mimeType });
        stream.getTracks().forEach((t) => t.stop());
        const extension = mimeType.includes("webm") ? "webm" : "mp4";
        setRecordedClip({ blob, extension, url: URL.createObjectURL(blob) });
      };

      recorder.start();
      mediaRecorderRef.current = recorder;
      setRecording(true);
      watchForSilence(stream);
    } catch {
      setVoiceError("Couldn't access your microphone -- check browser permissions and try again.");
    }
  }

  function stopRecording() {
    if (mediaRecorderRef.current?.state !== "recording") return;
    mediaRecorderRef.current.stop();
    setRecording(false);
    cleanupSilenceDetection();
  }

  function discardRecording() {
    if (recordedClip) URL.revokeObjectURL(recordedClip.url);
    setRecordedClip(null);
  }

  function confirmSend() {
    if (!recordedClip) return;
    const { blob, extension, url } = recordedClip;
    URL.revokeObjectURL(url);
    setRecordedClip(null);
    const image = attachedImage;
    setAttachedImage(null);
    sendVoice(blob, extension, image);
  }

  /** Real customers each get their own device and their own thread_id
   * automatically -- this button exists for testing/demoing multiple
   * customers back-to-back in one browser tab, which is not otherwise a
   * real scenario. Starts a genuinely new thread_id rather than reusing the
   * old one, so no state (identity, order, decision, tool-call budget)
   * carries over from whoever the previous conversation was. */
  function startNewChat() {
    if (mediaRecorderRef.current?.state === "recording") {
      mediaRecorderRef.current.onstop = null;
      mediaRecorderRef.current.stop();
      mediaRecorderRef.current.stream?.getTracks().forEach((t) => t.stop());
    }
    cleanupSilenceDetection();
    if (recordedClip) URL.revokeObjectURL(recordedClip.url);

    threadId.current = crypto.randomUUID();
    sending.current = false;
    setMessages([]);
    setInput("");
    setPending(false);
    setPendingText("");
    setRecording(false);
    setRecordedClip(null);
    setVoiceError(null);
    setAttachedImage(null);
  }

  return (
    <div className="chat-shell">
      <div className="page-narrow">
        <h1>Customer Chat</h1>
        <p className="page-sub">Ask about a return or refund -- the agent will look up your account and order.</p>

        <div className="chat-panel">
          <div className="chat-widget-header">
            <div className="chat-widget-avatar">
              <AgentIcon />
            </div>
            <div>
              <div className="chat-widget-title">ClearCart Support</div>
              <div className="chat-widget-status">
                <span className="dot" />
                Online now
              </div>
            </div>
            <button
              type="button"
              className="new-chat-button"
              onClick={startNewChat}
              disabled={messages.length === 0 && !pending}
              title="Start a new conversation as a different customer"
            >
              New chat
            </button>
          </div>

          <div className="chat-thread">
            {messages.length === 0 && (
              <div className="chat-empty">
                <div className="chat-empty-icon">
                  <AgentIcon />
                </div>
                <h3>Start a conversation</h3>
                <p>Try one of these, type your own request, or use the mic to talk.</p>
                <div className="suggestion-chips">
                  {SUGGESTIONS.map((s) => (
                    <button key={s} className="chip" onClick={() => setInput(s)}>
                      {s}
                    </button>
                  ))}
                </div>
              </div>
            )}

            {messages.map((m, i) => (
              <div key={i} className={`message-row ${m.role}`}>
                <div className={`avatar ${m.role}`}>
                  {m.role === "customer" ? "C" : <AgentIcon />}
                </div>
                <div className="bubble-col">
                  <div className={`bubble ${m.role}`}>
                    {m.role === "agent" ? <ReactMarkdown>{m.text}</ReactMarkdown> : m.text}
                  </div>
                  <span className="timestamp">{timeLabel(m.at)}</span>
                </div>
              </div>
            ))}

            {pending && (
              <div className="message-row agent">
                <div className="avatar agent">
                  <AgentIcon />
                </div>
                <div className="bubble-col">
                  <div className="bubble pending">{pendingText}</div>
                </div>
              </div>
            )}
          </div>

          {voiceError && <div className="voice-error">{voiceError}</div>}

          <input
            ref={fileInputRef}
            type="file"
            accept="image/*"
            style={{ display: "none" }}
            onChange={async (e) => {
              const file = e.target.files?.[0];
              if (!file) {
                setAttachedImage(null);
                return;
              }
              try {
                setAttachedImage(await resizeImageFile(file));
              } catch {
                setAttachedImage(file); // fall back to the original if resizing fails
              }
            }}
          />

          {attachedImage && (
            <div className="image-attachment">
              <span>Attached: {attachedImage.name}</span>
              <button type="button" onClick={() => setAttachedImage(null)}>
                Remove
              </button>
            </div>
          )}

          {recordedClip ? (
            <div className="voice-review">
              <audio src={recordedClip.url} controls />
              <button
                type="button"
                className="attach-button"
                onClick={() => fileInputRef.current?.click()}
                aria-label="Attach a photo"
                title="Attach a photo (e.g. damage evidence)"
              >
                <PaperclipIcon />
              </button>
              <div className="voice-review-actions">
                <button type="button" className="voice-discard" onClick={discardRecording}>
                  Discard
                </button>
                <button type="button" className="voice-confirm" onClick={confirmSend}>
                  Send recording →
                </button>
              </div>
            </div>
          ) : (
            <form
              className="chat-input"
              onSubmit={(e) => {
                e.preventDefault();
                send(input);
              }}
            >
              <button
                type="button"
                className="attach-button"
                onClick={() => fileInputRef.current?.click()}
                disabled={pending || recording}
                aria-label="Attach a photo"
                title="Attach a photo (e.g. damage evidence)"
              >
                <PaperclipIcon />
              </button>
              <button
                type="button"
                className={`mic-button ${recording ? "recording" : ""}`}
                onClick={recording ? stopRecording : startRecording}
                disabled={pending}
                aria-label={recording ? "Stop recording" : "Record a voice message"}
                title={recording ? "Stop recording" : "Talk instead of typing"}
              >
                {recording ? <StopIcon /> : <MicIcon />}
              </button>
              <input
                value={input}
                onChange={(e) => setInput(e.target.value)}
                placeholder={recording ? "Recording…" : "Type a message…"}
                disabled={pending || recording}
              />
              <button type="submit" disabled={pending || recording}>
                Send
              </button>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
