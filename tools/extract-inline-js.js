#!/usr/bin/env node
//
// Extract inline <script> blocks from Jinja/HTML templates so ESLint
// can check them. Writes each block to .eslint-tmp/<template>_<n>.js.
// Jinja expressions ({{ ... }}) are replaced with safe placeholders.
//
import { readFileSync, writeFileSync, mkdirSync, rmSync, readdirSync } from 'fs';
import { basename, join } from 'path';

const TEMPLATE_DIR = 'dashboard/templates';
const OUT_DIR = '.eslint-tmp';

rmSync(OUT_DIR, { recursive: true, force: true });
mkdirSync(OUT_DIR, { recursive: true });

const files = readdirSync(TEMPLATE_DIR)
  .filter(f => f.endsWith('.html'))
  .map(f => join(TEMPLATE_DIR, f));
let count = 0;

for (const file of files) {
  const html = readFileSync(file, 'utf8');
  const re = /<script(?![^>]*\bsrc\s*=)(?![^>]*\btype\s*=\s*["']importmap["'])[^>]*>([\s\S]*?)<\/script>/gi;
  let match;
  let idx = 0;

  while ((match = re.exec(html)) !== null) {
    let body = match[1].trim();
    if (!body) continue;

    // Replace Jinja expressions with valid JS placeholders
    body = body.replace(/\{\{.*?\}\}/g, '"__JINJA__"');
    body = body.replace(/\{%.*?%\}/g, '');
    body = body.replace(/\{#.*?#\}/g, '');

    const name = basename(file, '.html');
    const outPath = `${OUT_DIR}/${name}_${idx}.js`;
    writeFileSync(outPath, body + '\n');
    idx++;
    count++;
  }
}

console.log(`Extracted ${count} inline script block(s) to ${OUT_DIR}/`);
