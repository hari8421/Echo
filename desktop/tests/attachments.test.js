'use strict';

const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const test = require('node:test');

const { readAttachments, MAX_BYTES, MAX_FILES } = require('../src/main/attachments');

test('reads text files with their names and contents', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-attach-'));
  try {
    const file = path.join(dir, 'notes.md');
    fs.writeFileSync(file, '# Notes\nA useful line.', 'utf8');
    const result = readAttachments([file]);
    assert.equal(result.length, 1);
    assert.equal(result[0].name, 'notes.md');
    assert.equal(result[0].content, '# Notes\nA useful line.');
    assert.equal(result[0].error, undefined);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('rejects binary files and oversized files', () => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'echo-attach-'));
  try {
    const binary = path.join(dir, 'photo.png');
    fs.writeFileSync(binary, Buffer.from([0x89, 0x50, 0x4e, 0x47, 0xff, 0xfe]));
    const large = path.join(dir, 'huge.txt');
    fs.writeFileSync(large, Buffer.alloc(MAX_BYTES + 1, 0x61));
    const missing = path.join(dir, 'gone.txt');
    const result = readAttachments([binary, large, missing]);
    assert.match(result[0].error, /text files/i);
    assert.match(result[1].error, /2 MB/);
    assert.ok(result[2].error);
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
  }
});

test('caps the number of attachments', () => {
  const files = Array.from({ length: MAX_FILES + 4 }, (_, i) => `/tmp/file-${i}.txt`);
  const result = readAttachments(files);
  assert.equal(result.length, MAX_FILES);
  assert.ok(result.every((item) => item.error));
});
