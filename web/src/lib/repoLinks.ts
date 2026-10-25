/**
 * Builds a link to a file range on the forge that hosts the repository.
 * The indexer records the commit sha, so links point at the exact revision the
 * answer was generated from rather than a branch that may have moved since.
 */
export function sourceUrl(
  repoUrl: string,
  ref: string | null,
  path: string,
  startLine: number,
  endLine: number,
): string | null {
  let url: URL;
  try {
    url = new URL(repoUrl);
  } catch {
    return null;
  }
  const base = `${url.origin}${url.pathname.replace(/\/+$/, "").replace(/\.git$/, "")}`;
  const rev = encodeURIComponent(ref ?? "HEAD");
  const file = path.split("/").map(encodeURIComponent).join("/");

  switch (url.hostname) {
    case "gitlab.com":
      return `${base}/-/blob/${rev}/${file}#L${startLine}-${endLine}`;
    case "codeberg.org":
      return `${base}/src/commit/${rev}/${file}#L${startLine}-L${endLine}`;
    default:
      // GitHub, and the URL form most other forges also understand.
      return `${base}/blob/${rev}/${file}#L${startLine}-L${endLine}`;
  }
}

/** "owner/name" for display. */
export function repoName(repoUrl: string): string {
  try {
    const u = new URL(repoUrl);
    return u.pathname.replace(/^\/+/, "").replace(/\/+$/, "").replace(/\.git$/, "");
  } catch {
    return repoUrl;
  }
}
