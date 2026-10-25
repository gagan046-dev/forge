import { describe, expect, it } from "vitest";
import { linkCitations } from "./Markdown";

const valid = new Set([1, 2, 3]);

describe("linkCitations", () => {
  it("links single and grouped citations", () => {
    expect(linkCitations("See [1] and [2, 3].", valid)).toBe("See [\\[1\\]](#cite-1) and [\\[2\\]](#cite-2)[\\[3\\]](#cite-3).");
  });

  it("leaves code, unknown numbers and existing links alone", () => {
    const md = "Use `xs[1]` here.\n```py\nprint(a[2])\n```\nRef [9] and [1](http://x).";
    expect(linkCitations(md, valid)).toBe(md);
  });
});
