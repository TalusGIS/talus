/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { convertEsriDateProps, esriDateFieldNames, esriDateToIsoDay } from './esriDates.ts'

test('esriDateToIsoDay: epoch milliseconds become a UTC day', () => {
  assert.equal(esriDateToIsoDay(1776959694000), '2026-04-23') // 15:54:54 UTC
  assert.equal(esriDateToIsoDay(0), '1970-01-01')
  assert.equal(esriDateToIsoDay(-1), '1969-12-31')
})

test('esriDateToIsoDay: the day is cut in UTC, not the local zone', () => {
  // A date entered with no time of day is stored at midnight UTC and must keep its day.
  assert.equal(esriDateToIsoDay(Date.UTC(2026, 3, 23, 0, 0, 0)), '2026-04-23')
  // The last millisecond of the same UTC day.
  assert.equal(esriDateToIsoDay(Date.UTC(2026, 3, 23, 23, 59, 59, 999)), '2026-04-23')
  assert.equal(esriDateToIsoDay(Date.UTC(2026, 3, 24, 0, 0, 0)), '2026-04-24')
})

test('esriDateToIsoDay: anything that is not a usable number is left alone', () => {
  assert.equal(esriDateToIsoDay(null), null)
  assert.equal(esriDateToIsoDay(undefined), undefined)
  assert.equal(esriDateToIsoDay('2026-04-23'), '2026-04-23')
  assert.equal(esriDateToIsoDay(Number.NaN), Number.NaN)
  assert.equal(esriDateToIsoDay(8.7e15), 8.7e15) // past what a Date can hold
  assert.equal(esriDateToIsoDay(Date.UTC(10000, 0, 1)), Date.UTC(10000, 0, 1)) // a five-digit year
})

test('esriDateFieldNames: only named date fields, whatever the service sends', () => {
  const fields = [
    { name: 'objectid', type: 'esriFieldTypeOID' },
    { name: 'lastupdate', type: 'esriFieldTypeDate' },
    { name: 'stopid', type: 'esriFieldTypeInteger' },
    { name: '', type: 'esriFieldTypeDate' },
    { type: 'esriFieldTypeDate' },
    null,
    'esriFieldTypeDate',
  ]
  assert.deepEqual(esriDateFieldNames(fields), ['lastupdate'])
  assert.deepEqual(esriDateFieldNames(undefined), [])
  assert.deepEqual(esriDateFieldNames({ fields: [] }), [])
})

test('convertEsriDateProps: converts the date fields and nothing else', () => {
  const features = [
    { properties: { stopid: 1776959694000, lastupdate: 1776959694000, name: 'a' } },
    { properties: { stopid: 2, lastupdate: null } },
    { properties: null },
    { properties: { stopid: 3 } },
  ]
  convertEsriDateProps(features, ['lastupdate'])
  assert.deepEqual(features, [
    { properties: { stopid: 1776959694000, lastupdate: '2026-04-23', name: 'a' } }, // a big number in a non-date field is not a date
    { properties: { stopid: 2, lastupdate: null } },
    { properties: null },
    { properties: { stopid: 3 } }, // an absent property is not added
  ])
})

test('convertEsriDateProps: a field named like an Object member is only read as an own property', () => {
  const features = [{ properties: { toString: 1776959694000 } as Record<string, unknown> }, { properties: {} }]
  convertEsriDateProps(features, ['toString', '__proto__'])
  assert.equal(features[0].properties?.toString, '2026-04-23')
  assert.deepEqual(Object.keys(features[1].properties ?? {}), [])
})
