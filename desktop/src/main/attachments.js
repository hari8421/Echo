'use strict';

/** Read chat attachments in the main process. The renderer never sees file paths. */
const fs = require('node:fs');
const path = require('node:path');

const MAX_BYTES = 2 * 1024 * 1024;
const MAX_FILES = 12;

function readAttachments(filePaths) {
  const files = [];
  for (const filePath of (filePaths || []).slice(0, MAX_FILES)) {
    const name = path.basename(String(filePath || 'file'));
    try {
      const stats = fs.statSync(filePath);
      if (!stats.isFile()) {
        files.push({ name, error: 'Not a file.' });
        continue;
      }
      if (stats.size > MAX_BYTES) {
        files.push({ name, error: 'Larger than 2 MB.' });
        continue;
      }
      const content = fs.readFileSync(filePath, 'utf8');
      if (content.includes('\uFFFD')) {
        files.push({ name, error: 'Only text files can be attached.' });
        continue;
      }
      files.push({ name, content, size: stats.size });
    } catch (error) {
      files.push({ name, error: error.message || String(error) });
    }
  }
  return files;
}

module.exports = { readAttachments, MAX_BYTES, MAX_FILES };
