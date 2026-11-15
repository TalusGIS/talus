/// <reference types="node" />
import assert from 'node:assert/strict'
import { test } from 'node:test'

import { isEmbedPath, isFramed } from './embedPath.ts'

// Every spelling the edge serves as frameable (measured on the dev edge 2026-11-14) must be the
// embed view in the app too — otherwise the frameable response boots the signed-in portal.
test('every spelling the edge treats as /embed/* is an embed path here', () => {
  for (const p of [
    '/embed/map/abc',
    '/EMBED/map/abc',
    '/Embed/Map/abc',
    '/%65mbed/map/abc',
    '/%45MBED/x',
    '//embed/x',
    '/embed//x',
    '/embed%2Fx',
    '/%2Fembed/x',
    '/embed/',
  ]) {
    assert.equal(isEmbedPath(p), true, p)
  }
})

test('paths the edge does NOT frame are not embed paths', () => {
  for (const p of [
    '/',
    '/map',
    '/embed', // no trailing slash: the edge answers it with framing forbidden
    '/embedded/x',
    '/maps/embed/x', // contains /embed/ but does not start with it
    '/catalog?next=/embed/x',
    '/x/%2Fembed/y',
    '/embed%252Fx', // double-encoded: one decode leaves a literal %2F, as at the edge
  ]) {
    assert.equal(isEmbedPath(p), false, p)
  }
})

test('a malformed escape does not throw, and is judged as written', () => {
  assert.equal(isEmbedPath('/embed/%E0%A4%A'), true)
  assert.equal(isEmbedPath('/%E0%A4%A/embed/x'), false)
})

test('framed: top is another window', () => {
  const top = {}
  const self = {}
  assert.equal(isFramed({ self, top }), true)
  assert.equal(isFramed({ self, top: self }), false)
})

test('framed: a window that cannot read its top is framed by another origin', () => {
  const win = {
    self: {},
    get top(): unknown {
      throw new Error('cross-origin')
    },
  }
  assert.equal(isFramed(win), true)
})
