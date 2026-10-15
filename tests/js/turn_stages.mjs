/* `Robot.turnStages()` splits a turn's cumulative metrics into per-stage durations.
 *
 * The property that makes it trustworthy: the durations must sum to `finished_ms`. Every
 * figure in `turn_done` is measured from the end of speech, so a breakdown that does not
 * add back up has either double-counted a stage or lost one -- and either way it would
 * point at the wrong culprit, which is the only thing this table is for.
 */
import { readFileSync } from 'node:fs';
import { install } from './dom.mjs';

install({ page: 'headless' });
(0, eval)(readFileSync('src/robot/ui/static/app.js', 'utf8'));

const problems = [];
const check = (label, cond, detail = '') => {
  if (!cond) problems.push(`${label}${detail ? `: ${detail}` : ''}`);
};

const sumOf = (rows) => rows.filter((r) => !r.absolute)
                            .reduce((t, r) => t + r.ms, 0);

/* A real turn, copied from core.log. The acknowledgement played BEFORE the answer's
 * first clause existed, which is the normal case with CORE_FILLER on. */
const withAck = { asr_decode_ms: 1355, transcript_ms: 1557, first_token_ms: 1732,
                  first_chunk_ms: 2641, first_audio_ms: 1683, finished_ms: 5766,
                  retrieval_ms: 1558, audio_s: 3.29 };
let rows = Robot.turnStages(withAck);
check('acknowledgement is reported as its own absolute marker',
      rows.some((r) => r.absolute && r.ms === 1683));
check('durations account for the whole turn',
      sumOf(rows) === withAck.finished_ms,
      `${sumOf(rows)} != ${withAck.finished_ms}`);
check('no negative stage', rows.every((r) => r.ms >= 0));
check('endpointing is transcript minus decode',
      rows.find((r) => r.name === 'endpointing').ms === 202);

/* No filler: first sound comes after the first clause, so synthesis is a real stage. */
const noAck = { asr_decode_ms: 500, transcript_ms: 700, retrieval_ms: 710,
                first_token_ms: 900, first_chunk_ms: 1800, first_audio_ms: 2400,
                finished_ms: 6000, audio_s: 3.6 };
rows = Robot.turnStages(noAck);
check('synthesis appears when nothing spoke before the answer',
      rows.some((r) => r.name === 'synthesis → first sound' && r.ms === 600));
check('durations still account for the whole turn',
      sumOf(rows) === noAck.finished_ms, `${sumOf(rows)} != ${noAck.finished_ms}`);
check('no absolute marker without a filler',
      !rows.some((r) => r.absolute));

/* A turn that died early -- the LLM never produced anything. Must not throw and must
 * not invent stages it has no evidence for. */
rows = Robot.turnStages({ asr_decode_ms: 400, transcript_ms: 600, retrieval_ms: 601,
                          first_token_ms: null, first_chunk_ms: null,
                          first_audio_ms: null, finished_ms: null });
check('a turn with no reply still reports what it does know',
      rows.some((r) => r.name === 'ASR decode'));
check('and invents nothing further',
      !rows.some((r) => r.name === 'speaking'));

/* An empty object: a page must not crash on a malformed event. */
check('an empty turn yields no rows', Robot.turnStages({}).length === 0);

/* A text turn: no speech, no decode. */
rows = Robot.turnStages({ asr_decode_ms: 0, transcript_ms: 0, retrieval_ms: 1,
                          first_token_ms: 300, first_chunk_ms: 700,
                          first_audio_ms: 900, finished_ms: 2000, audio_s: 1.0 });
check('a typed turn accounts for its whole duration too',
      sumOf(rows) === 2000, `${sumOf(rows)} != 2000`);

if (problems.length) {
  for (const p of problems) console.error(`  ${p}`);
  process.exit(1);
}
console.log('ok turnStages');
