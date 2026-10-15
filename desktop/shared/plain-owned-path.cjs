const fs = require("node:fs/promises")
const path = require("node:path")

function inside(root, candidate) {
  const relative = path.relative(root, candidate)
  return relative === "" || (relative !== ".." && !relative.startsWith(`..${path.sep}`) && !path.isAbsolute(relative))
}

function chainTo(candidate) {
  const resolved = path.resolve(candidate)
  const root = path.parse(resolved).root
  const relative = path.relative(root, resolved)
  const chain = [root]
  let current = root
  for (const part of relative.split(path.sep).filter(Boolean)) {
    current = path.join(current, part)
    chain.push(current)
  }
  return chain
}

async function assertPlainLexicalPath(candidate, options = {}) {
  const label = options.label || "path"
  for (const lexical of chainTo(candidate)) {
    let info
    try { info = await fs.lstat(lexical) }
    catch { throw Object.assign(new Error(`${label} is missing`), { code: "path_missing" }) }
    if (info.isSymbolicLink()) throw Object.assign(new Error(`${label} traverses a symbolic link or junction`), { code: "path_untrusted" })
  }
  return path.resolve(candidate)
}

async function assertPlainOwnedPath(root, candidate, options = {}) {
  const label = options.label || "path"
  const resolvedRoot = path.resolve(root)
  const resolvedCandidate = path.resolve(candidate)
  if (!inside(resolvedRoot, resolvedCandidate)) throw Object.assign(new Error(`${label} escapes the owned root`), { code: "path_outside_root" })
  await assertPlainLexicalPath(resolvedRoot, { label: `${label} root` })
  await assertPlainLexicalPath(resolvedCandidate, { label })
  const canonicalRoot = await fs.realpath(resolvedRoot)
  const canonicalCandidate = await fs.realpath(resolvedCandidate)
  if (!inside(canonicalRoot, canonicalCandidate)) throw Object.assign(new Error(`${label} resolves outside the owned root`), { code: "path_untrusted" })
  return { resolvedRoot, resolvedCandidate, canonicalRoot, canonicalCandidate }
}

module.exports = { assertPlainLexicalPath, assertPlainOwnedPath, inside }
