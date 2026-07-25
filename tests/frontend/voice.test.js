import assert from 'node:assert/strict';
import test from 'node:test';

const values = new Map();
globalThis.localStorage = {
  getItem(key) { return values.has(key) ? values.get(key) : null; },
  setItem(key, value) { values.set(key, String(value)); },
  removeItem(key) { values.delete(key); },
  clear() { values.clear(); },
};

const appended = [];
globalThis.document = {
  addEventListener() {},
  createElement() {
    return {
      className: '',
      hidden: true,
      textContent: '',
      setAttribute() {},
    };
  },
  body: {
    appendChild(element) { appended.push(element); },
  },
};

globalThis.window = {
  setTimeout() { return 0; },
  speechSynthesis: {
    spoken: [],
    speak(utterance) { this.spoken.push(utterance); },
    cancel() {},
  },
};
globalThis.SpeechSynthesisUtterance = class {
  constructor(text) { this.text = text; }
};

class FakeAudio {
  constructor(url) {
    this.url = url;
    this.listeners = {};
  }
  addEventListener(name, callback) { this.listeners[name] = callback; }
  async play() { return undefined; }
  pause() {}
}
globalThis.Audio = FakeAudio;

const voice = await import('../../web/js/voice.js');

test.beforeEach(() => {
  values.clear();
  window.speechSynthesis.spoken.length = 0;
  appended.length = 0;
});

test('ElevenLabs stock narration is the default', () => {
  assert.deepEqual(voice.getVoiceChoice(), {
    kind: 'stock',
    voiceId: '',
    name: 'Threshold narrator',
  });
});

test('an explicit browser or supporter choice persists on this device', () => {
  const chosen = { kind: 'supporter', voiceId: 'shared-voice', name: 'Caregiver' };
  voice.setVoiceChoice(chosen);
  assert.deepEqual(voice.getVoiceChoice(), chosen);

  localStorage.setItem('threshold.voice.choice', '{bad json');
  assert.equal(voice.getVoiceChoice().kind, 'stock');
});

test('urgent speech requests the low-latency server mode', async () => {
  let request;
  globalThis.fetch = async (url, options) => {
    request = { url, options };
    return {
      ok: true,
      async blob() { return new Blob(['mp3']); },
      headers: { get() { return 'false'; } },
    };
  };

  assert.equal(
    await voice.speakWithChosenVoice('Call emergency services.', { loud: true }),
    'cloud',
  );
  assert.equal(request.url, '/api/voice/speak');
  assert.equal(JSON.parse(request.options.body).mode, 'urgent');
});

test('cloud failure speaks locally and discloses the fallback', async () => {
  globalThis.fetch = async () => { throw new Error('offline'); };

  assert.equal(await voice.speakWithChosenVoice('Stay with me.'), 'browser');
  assert.equal(window.speechSynthesis.spoken.at(-1).text, 'Stay with me.');
  assert.match(appended.at(-1).textContent, /ElevenLabs was unavailable/);
  assert.equal(appended.at(-1).hidden, false);
});
