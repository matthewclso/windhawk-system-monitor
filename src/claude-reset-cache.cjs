// SPDX-License-Identifier: GPL-3.0-only
// Chromium blockfile cache reader: EntryStore is 256 bytes after an 8192-byte
// file header. CacheAddr selects external files or fixed-size allocation blocks.
// Read committed entries only, restrict URLs to the active organization, cap
// decompressed responses at 2 MiB, and keep the newest response by HTTP Date.
// This private format is best effort; no browser cookies or authenticated HTTP
// requests are used here. Requires Node with zlib.zstdDecompressSync (Node 24).
// Read only the active organization's cached Usage response. Emit no credentials,
// identifiers, billing data, conversation contents, or unrelated cached responses.
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');
const input = JSON.parse(fs.readFileSync(0, 'utf8'));
const root = path.join(input.profile, 'Cache', 'Cache_Data');
function stream(address, size) {
  if (!address || size <= 0 || size > 2 * 1024 * 1024) return Buffer.alloc(0);
  const type = (address >>> 28) & 7;
  let filename, offset;
  if (type === 0) {
    filename = 'f_' + (address & 0x0fffffff).toString(16).padStart(6, '0');
    offset = 0;
  } else {
    const block = {1: 36, 2: 256, 3: 1024, 4: 4096}[type];
    if (!block) return Buffer.alloc(0);
    filename = 'data_' + ((address >>> 16) & 255);
    offset = 8192 + (address & 65535) * block;
  }
  const fd = fs.openSync(path.join(root, filename), 'r');
  try {
    const buffer = Buffer.alloc(size);
    const count = fs.readSync(fd, buffer, 0, size, offset);
    return buffer.subarray(0, count);
  } finally { fs.closeSync(fd); }
}
let latest = null;
try {
  const entries = fs.readFileSync(path.join(root, 'data_1'));
  if (entries.length > 32 * 1024 * 1024) throw Error('Oversized metadata');
  for (let offset = 8192; offset + 256 <= entries.length; offset += 256) {
    try {
      if (entries.readUInt32LE(offset + 20) !== 0) continue;
      const length = entries.readUInt32LE(offset + 32);
      if (!length || length > 2000) continue;
      const longKey = entries.readUInt32LE(offset + 36);
      const key = (longKey ? stream(longKey, length) : entries.subarray(offset + 96, offset + 96 + length)).toString('utf8');
      const prefix = 'https://claude.ai/api/organizations/' + input.organization + '/usage';
      const index = key.lastIndexOf(prefix);
      if (index < 0) continue;
      const url = new URL(key.slice(index).split(/[\0\s]/)[0]);
      if (url.pathname !== '/api/organizations/' + input.organization + '/usage' || url.searchParams.get('cedar_ember') !== '1') continue;
      const header = stream(entries.readUInt32LE(offset + 56), entries.readUInt32LE(offset + 40)).toString('latin1');
      const date = header.match(/\bdate:\s*([^\0\r\n]+)/i);
      const updatedAt = date ? Date.parse(date[1]) / 1000 : Number(entries.readBigUInt64LE(offset + 24)) / 1e6 - 11644473600;
      let body = stream(entries.readUInt32LE(offset + 60), entries.readUInt32LE(offset + 44));
      if (body.subarray(0, 4).equals(Buffer.from('28b52ffd', 'hex'))) body = zlib.zstdDecompressSync(body, {maxOutputLength: 2 * 1024 * 1024});
      else if (body[0] === 0x1f && body[1] === 0x8b) body = zlib.gunzipSync(body, {maxOutputLength: 2 * 1024 * 1024});
      else if (/content-encoding:\s*br\b/i.test(header)) body = zlib.brotliDecompressSync(body, {maxOutputLength: 2 * 1024 * 1024});
      const inventory = JSON.parse(body.toString('utf8')).cedar_ember;
      if (!inventory || !Array.isArray(inventory.grants) || inventory.ineligible_reason === 'surface') continue;
      if (latest && latest.updatedAt > updatedAt) continue;
      latest = {updatedAt, grants: inventory.grants.map(g => ({
        resets_left: g.resets_left, claimable: g.claimable === true, paused: g.paused === true,
        starts_at: g.starts_at, ends_at: g.ends_at
      }))};
    } catch { /* A racing cache write or unsupported entry is unavailable. */ }
  }
} catch { /* A missing or unsupported desktop cache is unavailable. */ }
process.stdout.write(JSON.stringify(latest));
