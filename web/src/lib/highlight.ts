/**
 * highlight.js core with a hand-picked language set. The full build adds
 * ~900 KB for 190 languages nobody here writes; these cover what a coding
 * assistant answers in.
 */
import hljs from "highlight.js/lib/core";
import bash from "highlight.js/lib/languages/bash";
import c from "highlight.js/lib/languages/c";
import cpp from "highlight.js/lib/languages/cpp";
import csharp from "highlight.js/lib/languages/csharp";
import css from "highlight.js/lib/languages/css";
import diff from "highlight.js/lib/languages/diff";
import dockerfile from "highlight.js/lib/languages/dockerfile";
import go from "highlight.js/lib/languages/go";
import ini from "highlight.js/lib/languages/ini";
import java from "highlight.js/lib/languages/java";
import javascript from "highlight.js/lib/languages/javascript";
import json from "highlight.js/lib/languages/json";
import kotlin from "highlight.js/lib/languages/kotlin";
import makefile from "highlight.js/lib/languages/makefile";
import markdown from "highlight.js/lib/languages/markdown";
import php from "highlight.js/lib/languages/php";
import plaintext from "highlight.js/lib/languages/plaintext";
import python from "highlight.js/lib/languages/python";
import ruby from "highlight.js/lib/languages/ruby";
import rust from "highlight.js/lib/languages/rust";
import shell from "highlight.js/lib/languages/shell";
import sql from "highlight.js/lib/languages/sql";
import swift from "highlight.js/lib/languages/swift";
import typescript from "highlight.js/lib/languages/typescript";
import xml from "highlight.js/lib/languages/xml";
import yaml from "highlight.js/lib/languages/yaml";

const languages = {
  bash, c, cpp, csharp, css, diff, dockerfile, go, ini, java, javascript, json, kotlin, makefile,
  markdown, php, plaintext, python, ruby, rust, shell, sql, swift, typescript, xml, yaml,
};
for (const [name, lang] of Object.entries(languages)) hljs.registerLanguage(name, lang);
hljs.registerAliases(["powershell", "ps1", "console"], { languageName: "shell" });
hljs.registerAliases(["toml"], { languageName: "ini" });
hljs.registerAliases(["html", "svg", "vue"], { languageName: "xml" });

// Auto-detection is quadratic-ish on big inputs; past this size we just escape.
const AUTO_DETECT_LIMIT = 12_000;

function escapeHtml(s: string): string {
  return s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

export function highlight(code: string, language: string | null): string {
  if (language && hljs.getLanguage(language)) {
    return hljs.highlight(code, { language, ignoreIllegals: true }).value;
  }
  if (!language && code.length <= AUTO_DETECT_LIMIT) {
    return hljs.highlightAuto(code).value;
  }
  return escapeHtml(code);
}
