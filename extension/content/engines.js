/* Search engine definitions: how to read the query and where to insert the PKDB box. */
/* exported PKDB_ENGINES, pkdbDetectEngine */
"use strict";

const _param = (...names) => () => {
  const p = new URLSearchParams(location.search);
  for (const n of names) {
    const v = p.get(n);
    if (v && v.trim()) return v.trim();
  }
  return "";
};

const _input = (selector) => {
  const el = document.querySelector(selector);
  return el && el.value ? el.value.trim() : "";
};

// Insertion targets are tried in order. mode: "prepend" (first child) or "before" (sibling before).
var PKDB_ENGINES = [
  {
    id: "google",
    name: "Google",
    match: () => /(^|\.)google\.[a-z.]+$/.test(location.hostname) && location.pathname === "/search",
    query: _param("q"),
    // Only on the "All" tab (tbm = images/news/..., udm = new vertical tabs; udm=14 is "Web")
    active: () => {
      const p = new URLSearchParams(location.search);
      return !p.get("tbm") && (!p.get("udm") || p.get("udm") === "14");
    },
    targets: [["#center_col", "prepend"], ["#rcnt", "prepend"], ["#search", "prepend"], ["#main", "prepend"]],
  },
  {
    id: "duckduckgo",
    name: "DuckDuckGo",
    match: () => /(^|\.)duckduckgo\.com$/.test(location.hostname),
    query: () => _param("q")() || _input("input[name=q]"),
    active: () => {
      const p = new URLSearchParams(location.search);
      return !["images", "videos", "news", "maps", "shopping"].includes(p.get("ia") || p.get("iax") || "");
    },
    targets: [
      ['[data-testid="mainline"]', "prepend"], ['section[data-area="mainline"]', "prepend"],
      [".react-results--main", "before"], ["#links", "before"], [".results", "before"], ["#react-layout", "prepend"],
    ],
  },
  {
    id: "ecosia",
    name: "Ecosia",
    match: () => /(^|\.)ecosia\.org$/.test(location.hostname) && location.pathname.startsWith("/search"),
    query: _param("q"),
    active: () => true,
    targets: [['[data-test-id="mainline"]', "prepend"], [".mainline", "prepend"], ["main section", "prepend"], ["main", "prepend"]],
  },
  {
    id: "bing",
    name: "Bing",
    match: () => /(^|\.)bing\.com$/.test(location.hostname) && location.pathname === "/search",
    query: _param("q"),
    active: () => true,
    targets: [["#b_results", "before"], ["#b_content", "prepend"]],
  },
  {
    id: "startpage",
    name: "Startpage",
    match: () => /(^|\.)startpage\.com$/.test(location.hostname) && /\/(sp|do|rvd)\/search|\/search/.test(location.pathname),
    query: () => _param("query", "q")() || _input("input[name=query]") || _input("#q"),
    active: () => !/images|video|news/.test(new URLSearchParams(location.search).get("cat") || ""),
    targets: [[".w-gl", "prepend"], [".mainline-results", "prepend"], ["#main", "prepend"], ["main", "prepend"]],
  },
  {
    id: "brave",
    name: "Brave Search",
    match: () => location.hostname === "search.brave.com" && location.pathname.startsWith("/search"),
    query: _param("q"),
    active: () => true,
    targets: [["#results", "prepend"], ["main", "prepend"]],
  },
  {
    id: "qwant",
    name: "Qwant",
    match: () => /(^|\.)qwant\.com$/.test(location.hostname),
    query: _param("q"),
    active: () => !["images", "videos", "news", "shopping"].includes(new URLSearchParams(location.search).get("t") || ""),
    targets: [['[data-testid="sectionWeb"]', "prepend"], ['[data-testid="webResults"]', "prepend"], ["main", "prepend"]],
  },
];

function pkdbDetectEngine() {
  return PKDB_ENGINES.find((e) => {
    try { return e.match(); } catch (err) { return false; }
  }) || null;
}
