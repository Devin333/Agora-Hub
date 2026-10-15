const fs = require("node:fs")
const path = require("node:path")
const zlib = require("node:zlib")
const size = 256
const bytes = Buffer.alloc((size * 4 + 1) * size)
for (let y = 0; y < size; y++) {
  const row = y * (size * 4 + 1)
  bytes[row] = 0
  for (let x = 0; x < size; x++) {
    const i = row + 1 + x * 4
    const rounded = (x < 52 && y < 52 && (x - 52) ** 2 + (y - 52) ** 2 > 52 ** 2) || (x > 203 && y < 52 && (x - 203) ** 2 + (y - 52) ** 2 > 52 ** 2) || (x < 52 && y > 203 && (x - 52) ** 2 + (y - 203) ** 2 > 52 ** 2) || (x > 203 && y > 203 && (x - 203) ** 2 + (y - 203) ** 2 > 52 ** 2)
    let c = rounded ? 0 : 17
    let a = rounded ? 0 : 255
    const left = 128 - (y - 46) * 0.45
    const right = 128 + (y - 46) * 0.45
    const inOuter = y >= 46 && y <= 210 && x >= left && x <= right
    const innerLeft = 128 - Math.max(0, y - 98) * 0.20
    const innerRight = 128 + Math.max(0, y - 98) * 0.20
    const inInner = y >= 93 && y <= 178 && x >= innerLeft && x <= innerRight
    const cross = y >= 147 && y <= 173 && x >= 91 && x <= 165
    if (inOuter && (!inInner || cross)) c = 247
    bytes[i] = c; bytes[i + 1] = c; bytes[i + 2] = c; bytes[i + 3] = a
  }
}
const table = new Uint32Array(256)
for (let n = 0; n < 256; n++) { let c = n; for (let k = 0; k < 8; k++) c = (c & 1) ? 0xedb88320 ^ (c >>> 1) : c >>> 1; table[n] = c >>> 0 }
function crc32(buffer) { let c = 0xffffffff; for (const b of buffer) c = table[(c ^ b) & 0xff] ^ (c >>> 8); return (c ^ 0xffffffff) >>> 0 }
function chunk(type, data) { const t = Buffer.from(type); const out = Buffer.alloc(12 + data.length); out.writeUInt32BE(data.length, 0); t.copy(out, 4); data.copy(out, 8); out.writeUInt32BE(crc32(Buffer.concat([t, data])), 8 + data.length); return out }
const ihdr = Buffer.alloc(13); ihdr.writeUInt32BE(size, 0); ihdr.writeUInt32BE(size, 4); ihdr[8] = 8; ihdr[9] = 6
const png = Buffer.concat([Buffer.from([137,80,78,71,13,10,26,10]), chunk("IHDR", ihdr), chunk("IDAT", zlib.deflateSync(bytes, { level: 9 })), chunk("IEND", Buffer.alloc(0))])
const dir = Buffer.alloc(22); dir.writeUInt16LE(0, 0); dir.writeUInt16LE(1, 2); dir.writeUInt16LE(1, 4); dir[6] = 0; dir[7] = 0; dir[8] = 0; dir[9] = 0; dir.writeUInt16LE(1, 10); dir.writeUInt16LE(32, 12); dir.writeUInt32LE(png.length, 14); dir.writeUInt32LE(22, 18)
const assets = path.join(__dirname, "..", "assets"); fs.writeFileSync(path.join(assets, "icon.png"), png); fs.writeFileSync(path.join(assets, "icon.ico"), Buffer.concat([dir, png]))
