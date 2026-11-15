/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

// The zone must be set BEFORE the module builds its Intl formatters, so the import is dynamic.
// New York is behind UTC all year: a formatter that shifted a date-only value would show the 25th.
process.env.TZ = 'America/New_York'
const { formatTemporal, temporalText } = await import('./formatTemporal.ts')

test('a date-only value never shifts a day, in a zone behind UTC', () => {
  const f = formatTemporal('2024-09-26')
  assert.ok(f)
  assert.match(f.text, /\b26\b/)
  assert.doesNotMatch(f.text, /\b25\b/)
  assert.match(f.text, /2024/)
  assert.equal(f.raw, '2024-09-26')
  // The first and last day of a year are where an off-by-one shows as the wrong YEAR.
  assert.match(formatTemporal('2025-01-01')!.text, /2025/)
  assert.match(formatTemporal('2024-12-31')!.text, /2024/)
})

test('a timestamp with a zone is shown in the viewer’s local time (a popup value)', () => {
  // 00:52:27 UTC on the 26th is 8:52 PM on the 25th in New York (EDT).
  const f = formatTemporal('2024-09-26T00:52:27Z')
  assert.ok(f)
  assert.match(f.text, /\b25\b/)
  assert.match(f.text, /8:52|20:52/)
  assert.doesNotMatch(f.text, /:27/) // seconds are dropped
  assert.equal(f.raw, '2024-09-26T00:52:27Z')
})

test('the system-column shape: microseconds, upper- or lower-case z', () => {
  // 19:43 UTC on Nov 5 is 2:43 PM EST.
  for (const v of ['2026-11-05T19:43:39.836279Z', '2026-11-05T19:43:39.836279z']) {
    const f = formatTemporal(v)
    assert.ok(f, v)
    assert.match(f.text, /2:43|14:43/)
    assert.doesNotMatch(f.text, /836279/)
    assert.equal(f.raw, v)
  }
})

test('every spelling of an offset is the same instant', () => {
  const want = formatTemporal('2024-09-26T00:52:27Z')!.text
  for (const v of [
    '2024-09-26T00:52:27+00:00',
    '2024-09-26T00:52:27+0000',
    '2024-09-26T00:52:27+00',
    '2024-09-26 00:52:27+00', // how Postgres prints a timestamptz
    '2024-09-25T20:52:27-04:00',
    '2024-09-26T00:52Z',
  ]) {
    assert.equal(formatTemporal(v)?.text, want, v)
  }
})

test('a timestamp with no zone is shown as written, not converted', () => {
  const f = formatTemporal('2024-09-26T00:52:27')
  assert.ok(f)
  assert.match(f.text, /\b26\b/)
  assert.match(f.text, /12:52|00:52|0:52/)
})

test('anything that is not, whole, a date or timestamp comes back null', () => {
  for (const v of [
    'Inspected 2024-09-26',
    '2024-09-26 to 2024-09-30',
    '2024-9-26',
    '2024-02-31', // not a day
    '0612-03-14', // an identifier, not the year 612
    '0612-03-14T10:00:00Z',
    '2024-13-01',
    '2024-09-26T25:00:00Z',
    '2024-09-26T',
    '20240926',
    '1234-5678-9012',
    'LDR',
    '',
    null,
    undefined,
    74,
    1727311947000, // an ArcGIS epoch-milliseconds number is NOT guessed at
    true,
    { a: 1 },
  ]) {
    assert.equal(formatTemporal(v), null, String(v))
    assert.equal(temporalText(v), null, String(v))
  }
})
