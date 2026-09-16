// Minimal MP4/MOV container probe (no ffmpeg needed).
//
// Reads box headers only and skips mdat payloads, so it stays fast on huge
// files. Reports: brand, top-level box order, whether moov sits before mdat
// (faststart), duration, and per-track codec / resolution / fps / audio.
const fs = require('fs')

const file = process.argv[2]
if (!file) {
  console.error('usage: node probe-mp4.js <file.mp4>')
  process.exit(1)
}

const fd = fs.openSync(file, 'r')
const size = fs.fstatSync(fd).size

// Boxes we descend into. `moov` and `trak` are handled explicitly so each is
// walked exactly once -- listing them here as well double-walks them and
// duplicates every track in the report.
const CONTAINERS = new Set(['mdia', 'minf', 'stbl', 'edts', 'udta'])

function readAt(offset, len) {
  const buf = Buffer.alloc(len)
  fs.readSync(fd, buf, 0, len, offset)
  return buf
}
const u32 = (buf, o) => buf.readUInt32BE(o)
const u64 = (buf, o) => Number(buf.readBigUInt64BE(o))

const found = { tracks: [], brand: null, topLevel: [] }
let trak = null

function walk(start, end, depth, path) {
  let offset = start
  while (offset + 8 <= end) {
    const header = readAt(offset, 8)
    let boxSize = u32(header, 0)
    const type = header.toString('latin1', 4, 8)
    let headerLen = 8
    if (boxSize === 1) {
      boxSize = u64(readAt(offset, 16), 8)
      headerLen = 16
    } else if (boxSize === 0) {
      boxSize = end - offset
    }
    if (boxSize < headerLen || offset + boxSize > end) break

    const bodyStart = offset + headerLen
    const bodyEnd = offset + boxSize
    const here = path ? path + '/' + type : type
    const head = (n) => readAt(bodyStart, Math.min(n, bodyEnd - bodyStart))
    if (depth === 0) found.topLevel.push(type)

    if (type === 'ftyp') {
      found.brand = head(12).toString('latin1', 0, 4)
    } else if (type === 'moov') {
      walk(bodyStart, bodyEnd, depth + 1, here)
    } else if (type === 'trak') {
      trak = { handler: null, codec: null, coded: null, display: null, mdhd: null, samples: null }
      walk(bodyStart, bodyEnd, depth + 1, here)
      found.tracks.push(trak)
      trak = null
    } else if (type === 'mdat') {
      // payload skipped on purpose
    } else if (type === 'mvhd') {
      const b = head(32)
      found.mvhd = b[0] === 1
        ? { timescale: u32(b, 20), duration: u64(b, 24) }
        : { timescale: u32(b, 12), duration: u32(b, 16) }
    } else if (trak) {
      if (type === 'tkhd') {
        const b = head(96)
        const off = b[0] === 1 ? 88 : 76
        if (b.length >= off + 8) {
          trak.display = { width: u32(b, off) / 65536, height: u32(b, off + 4) / 65536 }
        }
      } else if (type === 'mdhd') {
        const b = head(32)
        trak.mdhd = b[0] === 1
          ? { timescale: u32(b, 20), duration: u64(b, 24) }
          : { timescale: u32(b, 12), duration: u32(b, 16) }
      } else if (type === 'hdlr') {
        trak.handler = head(24).toString('latin1', 8, 12)
      } else if (type === 'stsd') {
        const b = head(64)
        if (u32(b, 4) > 0) {
          const entrySize = u32(b, 8)
          trak.codec = b.toString('latin1', 12, 16)
          if (trak.handler === 'vide' && entrySize >= 96 && b.length >= 44) {
            trak.coded = { w: b.readUInt16BE(40), h: b.readUInt16BE(42) }
          }
        }
      } else if (type === 'stsz') {
        const b = head(16)
        trak.samples = { sampleSize: u32(b, 4), sampleCount: u32(b, 8) }
      }
    }

    if (CONTAINERS.has(type)) walk(bodyStart, bodyEnd, depth + 1, here)
    offset = bodyEnd
  }
}

walk(0, size, 0, '')

const order = found.topLevel
const moovIdx = order.indexOf('moov')
const mdatIdx = order.indexOf('mdat')
const faststart = moovIdx !== -1 && mdatIdx !== -1 ? moovIdx < mdatIdx : null

const out = {
  file,
  bytes: size,
  brand: found.brand,
  topLevel: order,
  faststart, // true = moov before mdat, i.e. plays before the whole file arrives
  durationSec: found.mvhd
    ? Number((found.mvhd.duration / found.mvhd.timescale).toFixed(2))
    : null,
  tracks: [],
}

for (const t of found.tracks) {
  const dur = t.mdhd ? t.mdhd.duration / t.mdhd.timescale : out.durationSec
  const entry = {
    kind: t.handler,
    codec: t.codec,
    coded: t.coded,
    display: t.display,
    durationSec: dur ? Number(dur.toFixed(2)) : null,
  }
  if (t.handler === 'vide' && t.samples && dur) {
    const frames = t.samples.sampleSize === 0 ? t.samples.sampleCount : null
    entry.frames = frames
    entry.fps = frames ? Number((frames / dur).toFixed(2)) : null
    entry.megapixels = t.coded ? Number(((t.coded.w * t.coded.h) / 1e6).toFixed(2)) : null
  }
  out.tracks.push(entry)
}

console.log(JSON.stringify(out, null, 2))
fs.closeSync(fd)
