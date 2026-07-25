/* ============================================================================
   THRESHOLD — voice.js
   The consented supporter-voice surface, and the app's speaking voice.

   WHAT THIS FILE DOES
     Two separate surfaces plus one shared primitive:

       1. CAREGIVER (/caregiver) — the sample script, in-browser recording with
          MediaRecorder + getUserMedia, playback and re-record per passage, a
          live recording indicator, the exact consent checkbox, and — only after
          the clone succeeds — the separate share toggle.
       2. MEMBER (/onboarding) — picking the app's speaking voice: a stock cloud
          narrator, the browser's own speech, or a supporter voice if one has
          been shared. Stock ElevenLabs narration is the configured default.
       3. speakWithChosenVoice() — the primitive app.js routes every utterance
          through.

   WHAT THIS FILE DELIBERATELY DOES NOT DO
     * It never uploads audio before the consent box is ticked. The Record
       button works without it; the Upload button does not exist until it is
       checked. The gate is also enforced server-side (app/routes/voice.py) —
       this is the humane half, not the load-bearing half.
     * It never sends a caregiver_user_id. The account being cloned is whoever
       is signed in. There is no field for it here because there is no parameter
       for it there.
     * It never touches Memory Vault clips. Those are REAL recordings of a real
       person and play as recorded, never synthesized (PRD §7.2).
     * It never lets a cloned voice speak unlabelled. See labelling, below.
     * It never goes silent. Any failure of cloud speech falls through to
       window.speechSynthesis. A robotic voice always beats no guidance
       mid-emergency, which is the one unacceptable outcome.

   LABELLING IS NON-NEGOTIABLE
     Every utterance in a cloned voice paints a visible banner for as long as
     the audio plays, and announces itself to assistive technology. It is
     attached in ONE place — playCloned() — so there is no call path that can
     play a clone without it. The flag comes from the server's X-Voice-Cloned
     response header rather than from what the client thinks it requested, so
     it cannot be switched off by tampering with local state.

   DEFAULT OFF AT EVERY STEP
     No recording without pressing record. No upload without the tick. No share
     without the toggle. No cloud voice until the member picks one — the stored
     preference starts absent and absent means browser speech.

   NO BUILD STEP
     Plain ES module, no bundler, no framework, no CDN.
   ========================================================================= */

'use strict';

/* -------------------------------------------------------------------------- */
/* The member's stored choice                                                  */
/* -------------------------------------------------------------------------- */

/* localStorage rather than the server, deliberately. The choice of who the app
   sounds like is a preference on this device, not a clinical fact about the
   person, and it does not belong in a profile a caregiver's surface reads from.
   It also means the default survives a server that is unreachable: with no
   stored value the app speaks in the browser's own voice, which needs no
   network at all. */
const CHOICE_KEY = 'threshold.voice.choice';

/**
 * Read the member's chosen voice.
 *
 * @returns {{kind: 'browser'|'stock'|'supporter', voiceId: string, name: string}}
 *   Stock ElevenLabs narration when nothing is stored. Browser speech remains
 *   the explicit no-network fallback.
 */
export function getVoiceChoice() {
  try {
    const raw = localStorage.getItem(CHOICE_KEY);
    if (!raw) return { kind: 'stock', voiceId: '', name: 'Threshold narrator' };
    const parsed = JSON.parse(raw);
    if (!parsed || !['browser', 'stock', 'supporter'].includes(parsed.kind)) {
      return { kind: 'stock', voiceId: '', name: 'Threshold narrator' };
    }
    return parsed;
  } catch {
    return { kind: 'stock', voiceId: '', name: 'Threshold narrator' };
  }
}

/** Persist the member's chosen voice. Storage failures are non-fatal — the
    app simply reverts to the default on next load rather than breaking. */
export function setVoiceChoice(choice) {
  try { localStorage.setItem(CHOICE_KEY, JSON.stringify(choice)); } catch { /* private mode */ }
}

/* -------------------------------------------------------------------------- */
/* The AI-recreation label                                                     */
/* -------------------------------------------------------------------------- */

/* One shared banner element, created lazily. A single instance means two
   overlapping utterances cannot leave a stale label on screen, and there is
   exactly one thing to style and one thing to test. */
let labelEl = null;

/**
 * Show or hide the "AI recreation" banner.
 *
 * NON-NEGOTIABLE, per the brief and app/voice.py: every utterance in a cloned
 * voice is visibly labelled while it plays. The label names the speaker and
 * states plainly that this is a synthetic copy, because "AI voice" alone does
 * not tell a frightened person that the human they can hear is not on the line
 * (PRD P5).
 *
 * role="status" + aria-live="polite" so a screen-reader user is told too: a
 * visual-only label would leave exactly the users least able to detect a
 * synthetic voice by ear without the disclosure.
 *
 * @param {boolean} on
 * @param {string} name Whose voice is being recreated.
 */
function showClonedLabel(on, name = '') {
  if (!labelEl) {
    labelEl = document.createElement('div');
    labelEl.className = 'ai-voice-label';
    labelEl.setAttribute('role', 'status');
    labelEl.setAttribute('aria-live', 'polite');
    document.body.appendChild(labelEl);
  }
  labelEl.textContent = on
    ? `AI recreation of ${name || 'a supporter'}’s voice — not a live person`
    : '';
  labelEl.hidden = !on;
}

/* -------------------------------------------------------------------------- */
/* Speaking                                                                    */
/* -------------------------------------------------------------------------- */

/* The currently playing cloud audio, so a new utterance can cut off an old one
   rather than talking over it — and so the label is cleared when it does. */
let currentAudio = null;
let currentSource = null;
let reusableAudio = null;
let audioPrimed = false;
let audioPriming = false;
let audioContext = null;
let fallbackEl = null;

function showFallbackNotice(message = 'ElevenLabs was unavailable — using this device’s voice.') {
  if (!fallbackEl) {
    fallbackEl = document.createElement('div');
    fallbackEl.className = 'voice-fallback-label';
    fallbackEl.setAttribute('role', 'status');
    fallbackEl.setAttribute('aria-live', 'polite');
    document.body.appendChild(fallbackEl);
  }
  fallbackEl.textContent = message;
  fallbackEl.hidden = false;
  window.setTimeout(() => { if (fallbackEl) fallbackEl.hidden = true; }, 6000);
}

function silentWavUrl() {
  const samples = 80;
  const buffer = new ArrayBuffer(44 + samples);
  const view = new DataView(buffer);
  const write = (offset, value) => {
    for (let i = 0; i < value.length; i += 1) {
      view.setUint8(offset + i, value.charCodeAt(i));
    }
  };
  write(0, 'RIFF');
  view.setUint32(4, 36 + samples, true);
  write(8, 'WAVE');
  write(12, 'fmt ');
  view.setUint32(16, 16, true);
  view.setUint16(20, 1, true);
  view.setUint16(22, 1, true);
  view.setUint32(24, 8000, true);
  view.setUint32(28, 8000, true);
  view.setUint16(32, 1, true);
  view.setUint16(34, 8, true);
  write(36, 'data');
  view.setUint32(40, samples, true);
  for (let i = 44; i < 44 + samples; i += 1) view.setUint8(i, 128);
  return URL.createObjectURL(new Blob([buffer], { type: 'audio/wav' }));
}

/**
 * Unlock one reusable media element while the browser still has a user gesture.
 *
 * Cloud audio arrives after a network round trip, by which time Safari/Chrome
 * may have discarded the click's playback permission. Playing 10ms of muted
 * silence synchronously blesses this element; the returned ElevenLabs MP3 is
 * then loaded into the same element instead of a new autoplay-blocked one.
 */
export function primeAudioPlayback() {
  const AudioContextClass = window.AudioContext || window.webkitAudioContext;
  if (AudioContextClass && !audioContext) {
    audioContext = new AudioContextClass();
  }
  if (audioContext?.state === 'suspended') {
    // Called directly from the user's tap. Once resumed here, decoded
    // ElevenLabs bytes can be started after the network request finishes.
    audioContext.resume().catch(() => {});
  }
  if (audioPrimed || audioPriming || typeof Audio === 'undefined') return;
  audioPriming = true;
  reusableAudio ||= new Audio();
  reusableAudio.setAttribute('playsinline', '');
  const url = silentWavUrl();
  reusableAudio.muted = true;
  reusableAudio.src = url;
  const attempt = reusableAudio.play();
  Promise.resolve(attempt)
    .then(() => {
      reusableAudio.pause();
      reusableAudio.currentTime = 0;
      reusableAudio.muted = false;
      audioPrimed = true;
    })
    .catch(() => {})
    .finally(() => {
      URL.revokeObjectURL(url);
      audioPriming = false;
    });
}

/**
 * Speak with the browser's own speech synthesis.
 *
 * The floor of this whole feature. It is always available, costs nothing, needs
 * no network, and impersonates nobody. Every other path falls back to it.
 *
 * Slower and lower-pitched than the default, matching speak() in app.js: the
 * listener may be intoxicated, panicking, or both.
 */
export function speakInBrowser(text, { loud = false } = {}) {
  if (!('speechSynthesis' in window) || !text) return false;
  const u = new SpeechSynthesisUtterance(text);
  u.rate = 0.9;
  u.pitch = 0.95;
  u.volume = loud ? 1 : 0.9;
  window.speechSynthesis.speak(u);
  return true;
}

/** Stop whatever is currently speaking, from either channel, and drop the label. */
export function cancelSpeech() {
  window.speechSynthesis?.cancel();
  if (currentSource) {
    try { currentSource.stop(); } catch { /* already ended */ }
    currentSource = null;
  }
  if (currentAudio) {
    currentAudio.pause();
    currentAudio = null;
  }
  showClonedLabel(false);
}

/**
 * Play cloud audio, applying the AI-recreation label for the whole duration.
 *
 * THE SINGLE PLACE cloned audio is played, which is what makes the label
 * impossible to bypass: there is no other code path to the speaker for a clone.
 * `cloned` comes from the server's X-Voice-Cloned header, so the client cannot
 * decide to omit the label by lying to itself about what it asked for.
 *
 * @returns {Promise<boolean>} Whether playback actually started.
 */
async function playCloned(blob, { cloned, name }) {
  // Web Audio is the reliable path for audio returned after an async request.
  // Its context is unlocked synchronously in primeAudioPlayback() when the user
  // taps the voice control; source.start() is therefore not treated as
  // unsolicited autoplay when ElevenLabs bytes arrive a moment later.
  if (audioContext) {
    try {
      if (audioContext.state === 'suspended') await audioContext.resume();
      const bytes = await blob.arrayBuffer();
      const buffer = await audioContext.decodeAudioData(bytes.slice(0));
      const source = audioContext.createBufferSource();
      source.buffer = buffer;
      source.connect(audioContext.destination);
      currentSource = source;
      if (cloned) showClonedLabel(true, name);
      source.onended = () => {
        if (currentSource === source) currentSource = null;
        showClonedLabel(false);
      };
      source.start(0);
      return true;
    } catch {
      currentSource = null;
      showClonedLabel(false);
      // Fall through to the reusable <audio> element for browsers without
      // MP3 decoding in Web Audio or with a vendor-specific implementation.
    }
  }

  const url = URL.createObjectURL(blob);
  const audio = reusableAudio || new Audio();
  reusableAudio = audio;
  audio.pause();
  audio.src = url;
  audio.muted = false;
  audio.currentTime = 0;
  currentAudio = audio;
  if (cloned) showClonedLabel(true, name);

  // Revoke the object URL and clear the label on every exit path — ended,
  // paused, or errored. A label left on screen after the audio stops would
  // start describing silence, and a leaked blob URL is a slow memory leak in a
  // page that may stay open for hours.
  const done = () => {
    URL.revokeObjectURL(url);
    if (currentAudio === audio) {
      currentAudio = null;
      showClonedLabel(false);
    }
  };
  audio.onended = done;
  audio.onerror = done;

  try {
    await audio.play();
    return true;
  } catch {
    // Autoplay policy, no output device, decode failure. Caller falls back.
    done();
    return false;
  }
}

/**
 * Speak text through whichever voice the member chose.
 *
 * THE ROUTING RULE, and the reason this function exists at all:
 *   - choice is browser  -> browser speech.
 *   - choice is a cloud voice -> ask the server; on ANY failure — offline, rate
 *     limited, revoked mid-session, autoplay blocked, network gone — fall
 *     through to browser speech.
 *
 * NEVER SILENT. There is no branch of this function that ends without something
 * having been said, unless the browser itself has no speech synthesis. Falling
 * back to a robotic voice is always better than no guidance mid-emergency.
 *
 * Note the server can refuse a cloned utterance that would claim presence
 * (PRD P5). That arrives here as an ordinary failure and degrades to browser
 * speech — the guidance is still delivered, just not in the borrowed voice.
 *
 * @param {string} text
 * @param {{loud?: boolean}} opts
 * @returns {Promise<'cloud'|'browser'|'none'>} Which channel actually spoke.
 */
export async function speakWithChosenVoice(text, { loud = false } = {}) {
  if (!text) return 'none';
  const choice = getVoiceChoice();

  if (choice.kind === 'browser') {
    return speakInBrowser(text, { loud }) ? 'browser' : 'none';
  }

  try {
    const res = await fetch('/api/voice/speak', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        text,
        voice_id: choice.voiceId,
        mode: loud ? 'urgent' : 'expressive',
      }),
      // A hard ceiling: a hanging synthesis request during an emergency is
      // worse than a robotic voice arriving now.
      signal: AbortSignal.timeout(loud ? 8000 : 20000),
    });
    if (!res.ok) throw new Error(`voice ${res.status}`);

    const blob = await res.blob();
    // Trust the RESPONSE, not the request, for whether this needs labelling.
    const cloned = res.headers.get('X-Voice-Cloned') === 'true';
    if (await playCloned(blob, { cloned, name: choice.name })) return 'cloud';
    showFallbackNotice(
      'Your browser blocked cloud audio — using this device’s voice. Tap the voice control once to enable it.'
    );
    return speakInBrowser(text, { loud }) ? 'browser' : 'none';
  } catch {
    showFallbackNotice();
    return speakInBrowser(text, { loud }) ? 'browser' : 'none';
  }
}

/* -------------------------------------------------------------------------- */
/* Caregiver: record, consent, clone, share                                    */
/* -------------------------------------------------------------------------- */

/* Recorded audio per passage index. Held in memory only and never written to
   disk or storage: an unconsented recording of someone's voice is exactly the
   thing that should not persist anywhere, and closing the tab must discard it. */
const takes = new Map();
let mediaRecorder = null;
let mediaStream = null;
let recordingIndex = -1;

/** Escape text before it touches innerHTML. Same rule as every other file here. */
function esc(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]
  ));
}

/**
 * Stop recording and release the microphone.
 *
 * The track stop is not optional housekeeping. Leaving the stream open keeps
 * the browser's recording indicator lit and the microphone hot after the user
 * believes they have stopped — in THIS product, on a page about consenting to be
 * recorded, that would be a genuine betrayal rather than a resource leak.
 */
function releaseMic() {
  mediaStream?.getTracks().forEach((t) => t.stop());
  mediaStream = null;
  mediaRecorder = null;
  recordingIndex = -1;
}

/**
 * Wire the caregiver's supporter-voice section.
 *
 * No-ops when the section is absent, so this module is safe to import from any
 * page.
 */
export async function initCaregiverVoice() {
  const host = document.getElementById('voice-consent');
  if (!host) return;

  const status = document.getElementById('voice-consent-status');
  const passagesEl = document.getElementById('voice-passages');
  const consentBox = document.getElementById('voice-consent-check');
  const consentLabel = document.getElementById('voice-consent-text');
  const submitBtn = document.getElementById('voice-submit');
  const shareRow = document.getElementById('voice-share-row');
  const shareToggle = document.getElementById('voice-share-toggle');
  const existingEl = document.getElementById('voice-existing');

  /** Report offline honestly rather than showing a recorder that cannot finish. */
  const setStatus = (text, tone = '') => {
    if (!status) return;
    status.textContent = text;
    status.dataset.tone = tone;
  };

  let script = { passages: [], consent_text: '' };
  try {
    const [scriptRes, statusRes] = await Promise.all([
      fetch('/api/voice/script').then((r) => r.json()),
      fetch('/api/voice/status').then((r) => r.json()),
    ]);
    script = scriptRes;

    if (!statusRes.online) {
      // CONTRACT.md: an offline cloud voice is stated plainly, never disguised.
      setStatus('Cloud voice is offline — no API key. Recording is disabled.', 'warn');
      host.querySelectorAll('button, input').forEach((el) => { el.disabled = true; });
    }
    renderExisting(statusRes.voices || []);
  } catch {
    setStatus('Could not load the recording script. Try reloading.', 'warn');
    return;
  }

  // The consent wording comes from the server, so the text the supporter reads
  // is byte-for-byte the text stored on their row. If the page hardcoded its
  // own copy, the stored consent could document something nobody ever saw.
  if (consentLabel) consentLabel.textContent = script.consent_text;

  /* -- Passages ---------------------------------------------------------- */
  passagesEl.innerHTML = script.passages.map((text, i) => `
    <li class="voice-passage" data-index="${i}">
      <p class="voice-passage__text">${esc(text)}</p>
      <div class="voice-passage__controls">
        <button type="button" class="btn" data-act="record" data-i="${i}">Record</button>
        <button type="button" class="btn btn--quiet" data-act="play" data-i="${i}" disabled>Play back</button>
        <span class="voice-passage__state" data-state="${i}" role="status" aria-live="polite">Not recorded</span>
      </div>
    </li>`).join('');

  const stateEl = (i) => passagesEl.querySelector(`[data-state="${i}"]`);
  const btn = (act, i) => passagesEl.querySelector(`[data-act="${act}"][data-i="${i}"]`);

  /** Enable upload only when every passage is recorded AND consent is ticked. */
  function refreshSubmit() {
    const allRecorded = script.passages.every((_, i) => takes.has(i));
    // BOTH conditions. Consent is checked again on the server, but a disabled
    // button is the honest UI: nothing suggests uploading is possible until the
    // person has actually agreed.
    submitBtn.disabled = !(allRecorded && consentBox.checked);
  }

  consentBox.addEventListener('change', refreshSubmit);

  /** Start recording one passage. */
  async function startRecording(i) {
    if (mediaRecorder) stopRecording();
    try {
      mediaStream = await navigator.mediaDevices.getUserMedia({ audio: true });
    } catch {
      // Permission denied or no microphone. Said plainly — a silent failure
      // here looks like a broken button.
      setStatus('Microphone unavailable. Check your browser’s permission for this site.', 'warn');
      return;
    }
    const chunks = [];
    mediaRecorder = new MediaRecorder(mediaStream);
    mediaRecorder.addEventListener('dataavailable', (e) => {
      if (e.data.size) chunks.push(e.data);
    });
    mediaRecorder.addEventListener('stop', () => {
      const blob = new Blob(chunks, { type: mediaRecorder?.mimeType || 'audio/webm' });
      if (blob.size) {
        takes.set(i, blob);
        stateEl(i).textContent = 'Recorded';
        btn('play', i).disabled = false;
        btn('record', i).textContent = 'Re-record';
      }
      // Always release, even on an empty take.
      releaseMic();
      document.body.dataset.recording = 'false';
      btn('record', i).textContent = takes.has(i) ? 'Re-record' : 'Record';
      refreshSubmit();
    });

    recordingIndex = i;
    mediaRecorder.start();
    // The live indicator. A data attribute on <body> so the CSS can pulse a
    // page-level indicator: someone must never be unsure whether the microphone
    // is currently on, least of all on this page.
    document.body.dataset.recording = 'true';
    stateEl(i).textContent = 'Recording…';
    btn('record', i).textContent = 'Stop';
  }

  function stopRecording() {
    if (mediaRecorder?.state === 'recording') mediaRecorder.stop();
    else releaseMic();
  }

  passagesEl.addEventListener('click', (e) => {
    const target = e.target.closest('button');
    if (!target) return;
    const i = Number(target.dataset.i);

    if (target.dataset.act === 'record') {
      if (recordingIndex === i && mediaRecorder?.state === 'recording') stopRecording();
      else startRecording(i);
    } else if (target.dataset.act === 'play') {
      const blob = takes.get(i);
      if (!blob) return;
      // Playback of the supporter's OWN raw recording. Not synthesized, not
      // labelled as AI, because it is not AI — it is what they just said.
      const url = URL.createObjectURL(blob);
      const audio = new Audio(url);
      audio.addEventListener('ended', () => URL.revokeObjectURL(url));
      audio.play().catch(() => URL.revokeObjectURL(url));
    }
  });

  // Releasing the microphone if the page goes away mid-take. Without this, a
  // navigation during recording can leave the mic indicator lit in some browsers.
  window.addEventListener('pagehide', releaseMic);

  /* -- Upload ------------------------------------------------------------ */
  submitBtn.addEventListener('click', async () => {
    if (!consentBox.checked) return;   // belt and braces; the button is disabled
    submitBtn.disabled = true;
    setStatus('Building the voice… this takes a few seconds.');

    const form = new FormData();
    // The literal string "true", matching the server's explicit comparison.
    // Note what is NOT here: no account id of any kind. The account being
    // cloned is whoever is signed in, decided server-side.
    form.append('consent', 'true');
    form.append('display_name', document.getElementById('voice-name')?.value || '');
    [...takes.entries()]
      .sort((a, b) => a[0] - b[0])
      .forEach(([i, blob]) => form.append('samples', blob, `passage-${i + 1}.webm`));

    try {
      const res = await fetch('/api/voice/clone', { method: 'POST', body: form });
      const body = await res.json();
      if (!res.ok) throw new Error(body.detail || 'Could not build the voice.');

      setStatus('Voice created. It is private to you until you share it.', 'ok');
      // Sharing is offered ONLY NOW, and separately. Recording it and sharing
      // it are two distinct decisions, and the UI makes the second one an act
      // rather than a consequence of the first.
      shareRow.hidden = false;
      shareToggle.checked = false;
      shareToggle.dataset.voiceId = body.voice.id;
      renderExisting([body.voice]);
      // Discard the raw audio now that the model exists. There is no reason to
      // keep an unencrypted recording of someone's voice sitting in a tab.
      takes.clear();
    } catch (err) {
      setStatus(err.message || 'Could not build the voice.', 'warn');
      submitBtn.disabled = false;
    }
  });

  /* -- Share ------------------------------------------------------------- */
  shareToggle?.addEventListener('change', async () => {
    const id = shareToggle.dataset.voiceId;
    if (!id) return;
    const wanted = shareToggle.checked;
    try {
      const res = await fetch('/api/voice/share', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ id, shared: wanted }),
      });
      if (!res.ok) throw new Error('share failed');
      setStatus(
        wanted
          ? 'Shared. They still have to choose to turn it on for themselves.'
          : 'No longer shared. The voice model still exists — delete it to remove it entirely.',
        'ok',
      );
    } catch {
      // Revert the switch: a toggle that shows "shared" when the server says
      // otherwise is worse than an error, because the person stops checking.
      shareToggle.checked = !wanted;
      setStatus('Could not change sharing. Nothing was changed.', 'warn');
    }
  });

  /* -- Existing voices, and revocation ----------------------------------- */
  function renderExisting(voices) {
    if (!existingEl) return;
    if (!voices.length) { existingEl.innerHTML = ''; return; }
    existingEl.innerHTML = voices.map((v) => `
      <li class="panel stack-tight">
        <p><strong>${esc(v.name)}</strong> — ${v.shared ? 'shared with your member' : 'private to you'}</p>
        <!-- The consent statement shown back to the person who agreed to it.
             Recording consent and honouring it are different things; this is
             the second one. -->
        <details>
          <summary class="hint">What you agreed to, on ${esc(new Date(v.consented_at).toLocaleDateString())}</summary>
          <p class="hint">${esc(v.consent_text)}</p>
        </details>
        <button type="button" class="btn btn--quiet" data-act="revoke" data-id="${esc(v.id)}">
          Delete this voice
        </button>
      </li>`).join('');

    if (voices[0] && shareRow) {
      shareRow.hidden = false;
      shareToggle.checked = !!voices[0].shared;
      shareToggle.dataset.voiceId = voices[0].id;
    }
  }

  existingEl?.addEventListener('click', async (e) => {
    const target = e.target.closest('[data-act="revoke"]');
    if (!target) return;
    // Confirmed, because it is irreversible: the model is destroyed at the
    // provider, not merely unlinked here.
    if (!confirm('Delete this voice permanently? The voice model is destroyed, not just hidden.')) return;
    try {
      const res = await fetch(`/api/voice/${encodeURIComponent(target.dataset.id)}`, {
        method: 'DELETE',
      });
      if (!res.ok) throw new Error('delete failed');
      target.closest('li').remove();
      if (shareRow) shareRow.hidden = true;
      setStatus('Voice deleted. The model has been removed.', 'ok');
    } catch {
      setStatus('Could not delete the voice. Nothing was changed — try again.', 'warn');
    }
  });

  refreshSubmit();
}

/* -------------------------------------------------------------------------- */
/* Member: choosing the app's speaking voice                                   */
/* -------------------------------------------------------------------------- */

/**
 * Wire the member's voice picker.
 *
 * Stock ElevenLabs narration is selected when nothing else has been chosen.
 * Browser speech remains visible as the offline option.
 *
 * No-ops when the section is absent.
 */
export async function initVoicePicker() {
  const host = document.getElementById('voice-picker');
  if (!host) return;

  const listEl = document.getElementById('voice-options');
  const noteEl = document.getElementById('voice-picker-note');
  const previewBtn = document.getElementById('voice-picker-preview');

  let data = { online: false, stock: [], supporter: [] };
  try {
    data = await fetch('/api/voice/available').then((r) => r.json());
  } catch {
    // Non-fatal: the browser option alone is still a complete, working choice.
    if (noteEl) noteEl.textContent = 'Could not reach the voice service — browser voice only.';
  }

  const chosen = getVoiceChoice();

  const options = [];

  data.stock?.forEach((v) => options.push({
    kind: 'stock',
    voiceId: v.voice_id,
    name: v.name,
    note: 'Natural ElevenLabs narration. Not a real person, and not anyone you know.',
  }));

  // The server-side default narrator works even if listing voices failed.
  if (data.online && !options.length) options.push({
    kind: 'stock',
    voiceId: '',
    name: 'Threshold narrator',
    note: 'Natural ElevenLabs narration.',
  });

  options.push({
    kind: 'browser',
    voiceId: '',
    name: 'Your device’s voice',
    note: 'Always works without a connection. Used if ElevenLabs is unavailable.',
  });

  data.supporter?.forEach((v) => options.push({
    kind: 'supporter',
    voiceId: v.voice_id,
    name: v.name,
    // Stated at the point of choosing, not only while it speaks. Someone
    // deciding whether to hear a copy of their mother's voice is entitled to
    // know what it is before they turn it on, not just afterwards.
    note: 'An AI recreation of a supporter who recorded and consented to this. '
        + 'It is not them, and it will never say they are here with you.',
    cloned: true,
  }));

  listEl.innerHTML = options.map((o, i) => `
    <li>
      <label class="switch">
        <input type="radio" name="voice-choice" value="${i}"
               ${o.kind === chosen.kind && o.voiceId === chosen.voiceId ? 'checked' : ''}>
        <span class="switch__text">
          ${esc(o.name)}${o.cloned ? ' <span class="badge">AI recreation</span>' : ''}
          <span class="hint">${esc(o.note)}</span>
        </span>
      </label>
    </li>`).join('');

  // If nothing matched the stored choice — a shared voice was revoked since,
  // for instance — fall back to browser speech rather than leaving nothing
  // selected. Silent reversion to the safe default, which is the right
  // direction to fail in.
  if (!listEl.querySelector('input:checked')) {
    listEl.querySelector('input').checked = true;
    setVoiceChoice(options[0]);
  }

  listEl.addEventListener('change', (e) => {
    const opt = options[Number(e.target.value)];
    if (!opt) return;
    setVoiceChoice({ kind: opt.kind, voiceId: opt.voiceId, name: opt.name });
    if (noteEl) {
      noteEl.textContent = opt.cloned
        ? 'Every time this voice speaks, the screen will say it is an AI recreation.'
        : '';
    }
  });

  previewBtn?.addEventListener('click', async () => {
    cancelSpeech();
    // Deliberately bland preview copy. It claims nothing, comforts nobody, and
    // could not be mistaken for a crisis line if it were overheard — the same
    // constraints the recording script is written under.
    const channel = await speakWithChosenVoice(
      'This is how Threshold will sound when it speaks to you.',
    );
    if (noteEl && channel === 'browser' && getVoiceChoice().kind !== 'browser') {
      // Honest about the fallback rather than letting the wrong voice pass for
      // the chosen one.
      noteEl.textContent = 'Cloud voice was unavailable, so that was your device’s voice.';
    }
  });
}

/* Auto-wire both surfaces. Each initialiser no-ops when its section is not on
   the page, so a single import is safe everywhere. */
document.addEventListener('DOMContentLoaded', () => {
  initCaregiverVoice();
  initVoicePicker();
});
