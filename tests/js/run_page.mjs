/* Load app.js and one page's script against the stub DOM, then drive them with the
 * messages robot-core really sends. Any throw exits non-zero.
 *
 * Usage: node tests/js/run_page.mjs <page> [js-file ...]
 */
import { readFileSync } from 'node:fs';
import { install } from './dom.mjs';

const [page, ...files] = process.argv.slice(2);
const { sockets } = install({ page });

const problems = [];
process.on('uncaughtException', (err) => problems.push(`uncaught: ${err.stack}`));

/* `emit()` in app.js wraps every listener in try/catch and console.errors the failure,
 * so a page whose paint function throws on every message does not crash -- it just
 * never draws. That is precisely what "the dashboard is empty" looks like from the
 * outside, so here a swallowed listener failure IS the failure. */
const realError = console.error;
console.error = (...args) => {
  problems.push(`console.error: ${args.map(String).join(' ')}`);
  realError(...args);
};

const STATIC = 'src/robot/ui/static';
for (const file of ['app.js', ...files]) {
  const code = readFileSync(`${STATIC}/${file}`, 'utf8');
  try {
    // Indirect eval: run it as a script in global scope, the way a <script> tag does.
    // eslint-disable-next-line no-eval
    (0, eval)(code);
  } catch (err) {
    problems.push(`${file} threw while loading: ${err.message}`);
  }
}

if (typeof globalThis.Robot === 'undefined') {
  problems.push('window.Robot was never assigned -- app.js did not finish');
}

/* The payloads core actually broadcasts. Shapes copied from a live `hello`. */
const HELLO = {
  type: 'hello', state: 'idle', connected: true,
  settings: [
    { key: 'mic_source', label: 'Microphone', kind: 'restart', type: 'select',
      group: 'hardware', options: ['alsa_input.usb.x'], enabled: true,
      disabled_reason: '', help: 'h', where: 'asr', page: 'config',
      minimum: null, maximum: null, step: null, component: 'asr', preview_langs: [] },
    { key: 'temperature', label: 'LLM temperature', kind: 'hot', type: 'number',
      group: 'thinking', options: [], enabled: true, disabled_reason: '', help: 'h',
      where: 'core', page: 'config', minimum: 0, maximum: 2, step: 0.1,
      component: null, preview_langs: [] },
  ],
  groups: [{ key: 'hardware', label: 'Hardware', blurb: 'b' },
           { key: 'thinking', label: 'Thinking', blurb: 'b' }],
  values: { mic_source: 'alsa_input.usb.x', temperature: 0.7, listen_mode: 'no_interrupt',
            rag_k: 4, vad_min_silence: 0.2, asr_denoise: 'off', tts_volume: 5 },
  services: [{ key: 'llm', label: 'LLM', state: 'up', detail: 'ok', rss_mib: 2800,
               swap_mib: 0, running: true, instances: [] }],
  models: [], languages: ['en', 'id', 'ar'],
  // Every key `catalog.budget()` really returns. A fake that is missing one is a test
  // that passes while the page it is testing reads `undefined.toLocaleString()`.
  budget: {
    ceiling_mib: 7620, target_mib: 7000, items: [], steady_mib: 6000, peak_mib: 6100,
    headroom_mib: 1520, resident_mib: 5800, asleep_mib: 200, fits: true,
    within_target: true, llm_ctx: 2048, measured_mib: 5900, preview: false,
    measured_at: 1791277402.0,
    system: { total_mib: 7620, used_mib: 6100, free_mib: 300, cached_mib: 400,
              available_mib: 700, swap_used_mib: 0 },
  },
  selection: { llm_model: 'models/llm/x.gguf', asr_model: 'models/asr/whisper/y.bin',
               voices: [], tts_engine: 'supertonic', supertonic_voice: 'M1',
               llm_ctx: 2048 },
  overview: {
    usage: { turns: 12, prompt_tokens: 3014, completion_tokens: 936, cached_tokens: 11574 },
    knowledge: { chunks: 15, sources: 1 }, turns: 12, uptime_s: 438.4,
    language_default: 'id', reply_format: 'json', listen_mode: 'no_interrupt',
    filler: true,
  },
};

const FOLLOW_UPS = [
  { type: 'health', services: HELLO.services, overview: HELLO.overview },
  { type: 'budget', ...HELLO.budget },
  { type: 'link', connected: true },
  { type: 'transcript', turn_id: 't1', text: 'halo', source: 'voice' },
  { type: 'language', turn_id: 't1', language: 'id', language_source: 'previous-turn' },
  { type: 'chunk', turn_id: 't1', seq: 0, text: 'Oke, sebentar ya.', filler: true },
  { type: 'reply', turn_id: 't1', text: 'Halo!', language: 'id' },
  { type: 'reply_fields', turn_id: 't1', answer: 'Halo!', language: 'id',
    foot_direction: 'none', foot_step: 0, hand_movement: 'none', face: 'neutral',
    style: 'neutral' },
  { type: 'turn_done', turn_id: 't1', audio_s: 1.2, tokens: 12, prompt_tokens: 900,
    cached_tokens: 800, chunks: 1, truncated: false, timed_out: false, source: 'voice',
    filler: 'Oke, sebentar ya.', language: 'id' },
  { type: 'set_result', ok: true, key: 'temperature', value: 0.7 },
  { type: 'level', rms: 0.01, floor: -60 },
  { type: 'log', source: 'core', line: 'hello' },
];

if (!problems.length) {
  const socket = sockets[0];
  if (!socket) {
    // Monitor and Services are driven by fetch against /api/..., not by the relay.
    // Loading cleanly is all there is to check for those.
    console.log(`ok ${page} (no socket; fetch-driven)`);
    process.exit(0);
  } else {
    const deliver = (msg) => {
      try { socket.onmessage({ data: JSON.stringify(msg) }); }
      catch (err) { problems.push(`${msg.type}: ${err.message}`); }
    };
    try { socket.onopen(); } catch (err) { problems.push(`onopen: ${err.message}`); }
    deliver(HELLO);
    for (const msg of FOLLOW_UPS) deliver(msg);
  }
}

console.error = realError;        // or reporting a problem becomes a problem
if (problems.length) {
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}
console.log(`ok ${page}`);
