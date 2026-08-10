"use client";

import { useEffect, useRef, useState } from "react";
import ReactMarkdown from "react-markdown";
import { API_BASE, WS_BASE } from "@/lib/config";

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

const REALTIME_SAMPLE_RATE = 24000; // required by the OpenAI Realtime API's pcm16 input format

/** The Realtime API expects pcm16 at 24kHz; browsers hand back whatever the
 * mic's native rate is (commonly 44.1/48kHz) even when an AudioContext asks
 * for 24kHz, depending on the browser. Linear interpolation is good enough
 * for speech-to-text -- no need for a proper resampling filter here. */
function resampleTo24k(input: Float32Array, inputRate: number): Float32Array {
  if (inputRate === REALTIME_SAMPLE_RATE) return input;
  const ratio = inputRate / REALTIME_SAMPLE_RATE;
  const outLength = Math.round(input.length / ratio);
  const output = new Float32Array(outLength);
  for (let i = 0; i < outLength; i++) {
    const srcIndex = i * ratio;
    const i0 = Math.floor(srcIndex);
    const i1 = Math.min(i0 + 1, input.length - 1);
    const frac = srcIndex - i0;
    output[i] = input[i0] + (input[i1] - input[i0]) * frac;
  }
  return output;
}

function floatTo16BitPCM(input: Float32Array): Int16Array {
  const output = new Int16Array(input.length);
  for (let i = 0; i < input.length; i++) {
    const s = Math.max(-1, Math.min(1, input[i]));
    output[i] = s < 0 ? s * 0x8000 : s * 0x7fff;
  }
  return output;
}

function int16ToBase64(pcm: Int16Array): string {
  const bytes = new Uint8Array(pcm.buffer);
  let binary = "";
  for (let i = 0; i < bytes.length; i++) binary += String.fromCharCode(bytes[i]);
  return btoa(binary);
}

function fileToBase64(file: File): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve((reader.result as string).split(",")[1]);
    reader.onerror = reject;
    reader.readAsDataURL(file);
  });
}

/** Instant, free, local acknowledgment while the real request (agent -> TTS)
 * is still in flight -- uses the browser's built-in voice, not the OpenAI
 * pipeline, specifically so it can speak immediately with no network round
 * trip. Feature-detected; silently does nothing if unsupported. */
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
  const [liveTranscript, setLiveTranscript] = useState("");
  const [attachedImage, setAttachedImage] = useState<File | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const mediaRecorderRef = useRef<MediaRecorder | null>(null);
  const chunksRef = useRef<Blob[]>([]);
  const audioContextRef = useRef<AudioContext | null>(null);
  const analyserRef = useRef<AnalyserNode | null>(null);
  const silenceTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const silenceRafRef = useRef<number | null>(null);
  const recordingLimitTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // OpenAI Realtime voice pipeline -- one WebSocket + one Web Audio capture
  // graph per push-to-talk turn, opened in startRecording() and torn down
  // after finalize/cancel. See backend/api/main.py's ws_voice() for the
  // wire protocol.
  const voiceSocketRef = useRef<WebSocket | null>(null);
  const pcmContextRef = useRef<AudioContext | null>(null);
  const pcmSourceRef = useRef<MediaStreamAudioSourceNode | null>(null);
  const pcmProcessorRef = useRef<ScriptProcessorNode | null>(null);
  const transcriptPartsRef = useRef<string[]>([]);

  async function send(text: string) {
    const trimmed = text.trim();
    const imageToSend = attachedImage;
    // An attachment alone is a valid message -- a customer submitting damage
    // evidence with nothing typed shouldn't be silently blocked from sending.
    if ((!trimmed && !imageToSend) || sending.current) return;
    sending.current = true;

    const messageText = trimmed || "Here's a photo.";
    setMessages((prev) => [
      ...prev,
      { role: "customer", text: imageToSend ? `${messageText} (photo attached: ${imageToSend.name})` : messageText, at: new Date() },
    ]);
    setInput("");
    setAttachedImage(null);
    setPending(true);
    setPendingText(pickCheckingPhrase());

    try {
      const formData = new FormData();
      formData.append("thread_id", threadId.current);
      formData.append("message", messageText);
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

  const finalizeImageNameRef = useRef<string | null>(null);

  /** Routes every message from the voice WebSocket. One handler, attached
   * once per push-to-talk turn in startRecording() -- it has to distinguish
   * a guardrail/error hit *while still recording* (auto-stop into review,
   * nothing lost) from one hit *after* finalize was sent (terminal, shown as
   * a chat bubble like the old REST error path did). mediaRecorderRef's
   * live .state is used rather than the `recording` React state so this
   * doesn't need to be redefined (and risk a stale closure) on every render. */
  function attachVoiceSocketHandlers(ws: WebSocket) {
    ws.onmessage = (event) => {
      let data: any;
      try {
        data = JSON.parse(event.data);
      } catch {
        return;
      }
      switch (data.type) {
        case "transcript_delta":
          transcriptPartsRef.current.push(data.text);
          setLiveTranscript(transcriptPartsRef.current.join(" "));
          break;
        case "error":
          if (data.stage === "guardrail" && mediaRecorderRef.current?.state === "recording") {
            setVoiceError(data.message);
            stopRecording();
          } else if (sending.current) {
            // Backend error messages are already complete, customer-facing
            // sentences (e.g. "...Please try again.") -- don't append another.
            setMessages((prev) => [
              ...prev,
              { role: "agent", text: data.message, at: new Date() },
            ]);
            setPending(false);
            sending.current = false;
            cleanupVoiceSocket();
          } else {
            setVoiceError(data.message);
            cleanupVoiceSocket();
          }
          break;
        case "final": {
          const image = finalizeImageNameRef.current;
          setMessages((prev) => [
            ...prev,
            {
              role: "customer",
              text: image ? `${data.transcript} (photo attached: ${image})` : data.transcript,
              at: new Date(),
            },
            { role: "agent", text: data.reply, at: new Date() },
          ]);
          if ("speechSynthesis" in window) window.speechSynthesis.cancel();
          if (data.audio_base64) {
            new Audio(`data:audio/mpeg;base64,${data.audio_base64}`).play().catch(() => {
              // Autoplay can be blocked by the browser; the text reply is already shown either way.
            });
          }
          setPending(false);
          sending.current = false;
          cleanupVoiceSocket();
          break;
        }
      }
    };
    ws.onerror = () => {
      if (mediaRecorderRef.current?.state === "recording") {
        setVoiceError("Voice connection failed. Please try again.");
        stopRecording();
      }
    };
  }

  const SILENCE_THRESHOLD = 8; // 0-255 scale; empirical, room-noise dependent
  const SILENCE_DURATION_MS = 5000;
  const MAX_RECORDING_MS = 90_000; // client-side backstop; the server enforces a harder 120s cap independently

  function cleanupSilenceDetection() {
    if (silenceRafRef.current) cancelAnimationFrame(silenceRafRef.current);
    silenceRafRef.current = null;
    if (silenceTimeoutRef.current) clearTimeout(silenceTimeoutRef.current);
    silenceTimeoutRef.current = null;
    if (recordingLimitTimeoutRef.current) clearTimeout(recordingLimitTimeoutRef.current);
    recordingLimitTimeoutRef.current = null;
    audioContextRef.current?.close().catch(() => {});
    audioContextRef.current = null;
    analyserRef.current = null;
  }

  /** Stops the raw PCM capture graph feeding the voice WebSocket, without
   * closing the socket itself -- the socket stays open through the review
   * step so a still-loading transcript can finish arriving and finalize()
   * can be sent over the same connection. */
  function teardownPcmCapture() {
    pcmProcessorRef.current?.disconnect();
    pcmProcessorRef.current = null;
    pcmSourceRef.current?.disconnect();
    pcmSourceRef.current = null;
    pcmContextRef.current?.close().catch(() => {});
    pcmContextRef.current = null;
  }

  function cleanupVoiceSocket() {
    teardownPcmCapture();
    const ws = voiceSocketRef.current;
    if (ws && ws.readyState <= WebSocket.OPEN) ws.close();
    voiceSocketRef.current = null;
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
    setLiveTranscript("");
    transcriptPartsRef.current = [];
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });

      // Parallel MediaRecorder purely so the review screen can play back what
      // was said -- the transcript itself comes from the streamed PCM below,
      // not from this blob.
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

      const ws = new WebSocket(`${WS_BASE}/ws/voice/${threadId.current}`);
      voiceSocketRef.current = ws;
      attachVoiceSocketHandlers(ws);

      const pcmContext = new AudioContext({ sampleRate: REALTIME_SAMPLE_RATE });
      pcmContextRef.current = pcmContext;
      const source = pcmContext.createMediaStreamSource(stream);
      pcmSourceRef.current = source;
      const processor = pcmContext.createScriptProcessor(4096, 1, 1);
      pcmProcessorRef.current = processor;
      // ScriptProcessorNode only fires onaudioprocess while connected to a
      // destination; route through a silent gain so nothing is played back
      // (would otherwise echo the customer's own mic to their speakers).
      const silentGain = pcmContext.createGain();
      silentGain.gain.value = 0;

      processor.onaudioprocess = (e) => {
        if (ws.readyState !== WebSocket.OPEN) return;
        const resampled = resampleTo24k(e.inputBuffer.getChannelData(0), pcmContext.sampleRate);
        const pcm = floatTo16BitPCM(resampled);
        ws.send(JSON.stringify({ type: "audio", audio: int16ToBase64(pcm) }));
      };

      source.connect(processor);
      processor.connect(silentGain);
      silentGain.connect(pcmContext.destination);

      setRecording(true);
      watchForSilence(stream);
      recordingLimitTimeoutRef.current = setTimeout(() => {
        setVoiceError(`Recording stopped automatically after ${MAX_RECORDING_MS / 1000} seconds.`);
        stopRecording();
      }, MAX_RECORDING_MS);
    } catch {
      setVoiceError("Couldn't access your microphone -- check browser permissions and try again.");
      cleanupVoiceSocket();
    }
  }

  function stopRecording() {
    if (mediaRecorderRef.current?.state !== "recording") return;
    mediaRecorderRef.current.stop();
    setRecording(false);
    cleanupSilenceDetection();
    teardownPcmCapture();
    const ws = voiceSocketRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stop" }));
  }

  function discardRecording() {
    if (recordedClip) URL.revokeObjectURL(recordedClip.url);
    setRecordedClip(null);
    setLiveTranscript("");
    transcriptPartsRef.current = [];
    const ws = voiceSocketRef.current;
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "cancel" }));
    cleanupVoiceSocket();
  }

  function confirmSend() {
    if (!recordedClip || sending.current) return;
    const ws = voiceSocketRef.current;
    if (!ws || ws.readyState !== WebSocket.OPEN) {
      setVoiceError("Voice connection was lost. Please try again.");
      discardRecording();
      return;
    }
    sending.current = true;
    setPending(true);
    const phrase = pickCheckingPhrase();
    setPendingText(phrase);
    speakInterim(phrase);

    URL.revokeObjectURL(recordedClip.url);
    setRecordedClip(null);
    const image = attachedImage;
    setAttachedImage(null);
    finalizeImageNameRef.current = image ? image.name : null;

    (async () => {
      let imageB64: string | null = null;
      let imageType: string | null = null;
      if (image) {
        imageB64 = await fileToBase64(image);
        imageType = image.type || "image/jpeg";
      }
      ws.send(JSON.stringify({ type: "finalize", image: imageB64, image_type: imageType }));
    })();
  }

  const AUTO_SEND_DELAY_MS = 2000;
  const autoSendTimeoutRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  function cancelAutoSend() {
    if (autoSendTimeoutRef.current) {
      clearTimeout(autoSendTimeoutRef.current);
      autoSendTimeoutRef.current = null;
    }
  }

  /** Auto-sends the recording shortly after it lands on the review screen,
   * so a customer doesn't have to click through a confirm step for every
   * turn -- effect-based (not scheduled inside recorder.onstop) specifically
   * so it always calls the current confirmSend()/attachedImage closure
   * rather than a stale one captured back when startRecording() ran.
   * Discarding (or confirmSend() firing first) clears recordedClip, which
   * unmounts this effect and cancels the pending timer for free.
   *
   * Attaching a photo (see the attach-button onClick below) cancels this
   * timer rather than just letting it race the native file picker -- a
   * damage-evidence photo is exactly the kind of thing that must not get
   * silently left off because the 2s window closed while the OS file dialog
   * was still open. That path falls back to the manual "Send now" button. */
  useEffect(() => {
    if (!recordedClip) return;
    autoSendTimeoutRef.current = setTimeout(() => {
      autoSendTimeoutRef.current = null;
      confirmSend();
    }, AUTO_SEND_DELAY_MS);
    return cancelAutoSend;
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recordedClip]);

  /** Releases the mic and closes the voice WebSocket -- used both when
   * starting a fresh conversation and when the page unmounts (nav bar's
   * "home" link is a Next.js <Link>, i.e. client-side navigation with no
   * full page reload, so nothing stops an in-progress recording or an open
   * Realtime connection unless something here explicitly does it; left
   * alone, the mic would stay hot and the backend would keep the OpenAI
   * connection open -- billed connection time -- until the server's own
   * 120s guardrail eventually forces it closed). */
  function stopAllVoiceActivity() {
    if (mediaRecorderRef.current?.state === "recording") {
      mediaRecorderRef.current.onstop = null;
      mediaRecorderRef.current.stop();
      mediaRecorderRef.current.stream?.getTracks().forEach((t) => t.stop());
    }
    cleanupSilenceDetection();
    cleanupVoiceSocket();
  }

  useEffect(() => stopAllVoiceActivity, []);

  /** Real customers each get their own device and their own thread_id
   * automatically -- this button exists for testing/demoing multiple
   * customers back-to-back in one browser tab, which is not otherwise a
   * real scenario. Starts a genuinely new thread_id rather than reusing the
   * old one, so no state (identity, order, decision, tool-call budget)
   * carries over from whoever the previous conversation was. */
  function startNewChat() {
    stopAllVoiceActivity();
    if (recordedClip) URL.revokeObjectURL(recordedClip.url);

    threadId.current = crypto.randomUUID();
    sending.current = false;
    transcriptPartsRef.current = [];
    finalizeImageNameRef.current = null;
    setMessages([]);
    setInput("");
    setPending(false);
    setPendingText("");
    setRecording(false);
    setRecordedClip(null);
    setVoiceError(null);
    setLiveTranscript("");
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

          {recording && (
            <div className="voice-live-caption">
              {liveTranscript || "Listening…"}
            </div>
          )}

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
              {liveTranscript && <div className="voice-transcript-preview">&ldquo;{liveTranscript}&rdquo;</div>}
              <div className="voice-auto-send-hint">
                {attachedImage ? "Attached -- click “Send now” when ready" : "Sending automatically…"}
              </div>
              <audio src={recordedClip.url} controls />
              <button
                type="button"
                className="attach-button"
                onClick={() => {
                  cancelAutoSend();
                  fileInputRef.current?.click();
                }}
                aria-label="Attach a photo"
                title="Attach a photo (e.g. damage evidence)"
              >
                <PaperclipIcon />
              </button>
              <div className="voice-review-actions">
                <button type="button" className="voice-discard" onClick={discardRecording}>
                  Cancel
                </button>
                <button type="button" className="voice-confirm" onClick={confirmSend}>
                  Send now →
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
