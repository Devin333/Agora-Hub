declare module "*.cjs" {
  export function inside(root: string, candidate: string): boolean
  export function assertPlainLexicalPath(candidate: string, options?: { label?: string }): Promise<string>
  export function assertPlainOwnedPath(root: string, candidate: string, options?: { label?: string }): Promise<{ resolvedRoot: string; resolvedCandidate: string; canonicalRoot: string; canonicalCandidate: string }>
  const defaultExport: { inside: typeof inside; assertPlainLexicalPath: typeof assertPlainLexicalPath; assertPlainOwnedPath: typeof assertPlainOwnedPath }
  export default defaultExport
}
