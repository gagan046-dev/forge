import { describe, expect, it } from "vitest";
import { repoName, sourceUrl } from "./repoLinks";

describe("sourceUrl", () => {
  it("links GitHub files at the indexed commit with a line range", () => {
    expect(sourceUrl("https://github.com/psf/requests", "abc123", "src/requests/api.py", 10, 69)).toBe(
      "https://github.com/psf/requests/blob/abc123/src/requests/api.py#L10-L69",
    );
  });

  it("strips .git and trailing slashes and falls back to HEAD", () => {
    expect(sourceUrl("https://github.com/a/b.git/", null, "README.md", 1, 5)).toBe("https://github.com/a/b/blob/HEAD/README.md#L1-L5");
  });

  it("uses each forge's own URL scheme", () => {
    expect(sourceUrl("https://gitlab.com/g/p", "main", "x/y.go", 3, 9)).toBe("https://gitlab.com/g/p/-/blob/main/x/y.go#L3-9");
    expect(sourceUrl("https://codeberg.org/o/r", "f00", "a b.rs", 1, 2)).toBe("https://codeberg.org/o/r/src/commit/f00/a%20b.rs#L1-L2");
  });

  it("returns null for an unparseable URL", () => {
    expect(sourceUrl("not a url", "x", "a", 1, 1)).toBeNull();
  });
});

describe("repoName", () => {
  it("shows owner/name", () => {
    expect(repoName("https://github.com/psf/requests.git")).toBe("psf/requests");
  });
});
