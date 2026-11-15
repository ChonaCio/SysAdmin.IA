"use strict";
// Tiny, safe Markdown renderer: escapes all HTML first, then applies a small subset
// (code blocks, inline code, headings, bold/italic, lists, links, tables, blockquotes).
(function () {
  const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

  function inline(s) {
    const codes = [];
    s = s.replace(/`([^`\n]+)`/g, (_, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
    s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
    s = s.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
    s = s.replace(/\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>');
    s = s.replace(/\u0000(\d+)\u0000/g, (_, i) => `<code>${codes[+i]}</code>`);
    return s;
  }

  function render(src) {
    const lines = esc(src || "").split("\n");
    const out = [];
    let i = 0;
    while (i < lines.length) {
      const line = lines[i];
      const fence = line.match(/^\s*```(\S*)/);
      if (fence) {
        const buf = [];
        i++;
        while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
        i++;
        out.push(`<pre class="code"><code>${buf.join("\n")}</code></pre>`);
        continue;
      }
      const h = line.match(/^(#{1,4})\s+(.*)/);
      if (h) { out.push(`<h${h[1].length + 2}>${inline(h[2])}</h${h[1].length + 2}>`); i++; continue; }
      if (/^\s*([-*+])\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*([-*+])\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*[-*+]\s+/, ""));
        out.push("<ul>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ul>");
        continue;
      }
      if (/^\s*\d+[.)]\s+/.test(line)) {
        const items = [];
        while (i < lines.length && /^\s*\d+[.)]\s+/.test(lines[i])) items.push(lines[i++].replace(/^\s*\d+[.)]\s+/, ""));
        out.push("<ol>" + items.map((t) => `<li>${inline(t)}</li>`).join("") + "</ol>");
        continue;
      }
      if (/^\s*\|.*\|\s*$/.test(line) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
        const row = (l) => l.trim().replace(/^\||\|$/g, "").split("|").map((c) => inline(c.trim()));
        const head = row(line);
        i += 2;
        const body = [];
        while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
        out.push(`<table class="table md"><thead><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr></thead><tbody>` +
          body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</tbody></table>");
        continue;
      }
      if (/^&gt;\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^&gt;\s?/.test(lines[i])) buf.push(lines[i++].replace(/^&gt;\s?/, ""));
        out.push(`<blockquote>${inline(buf.join("<br>"))}</blockquote>`);
        continue;
      }
      if (!line.trim()) { i++; continue; }
      const para = [];
      while (i < lines.length && lines[i].trim() && !/^\s*(```|#{1,4}\s|[-*+]\s|\d+[.)]\s|\|)/.test(lines[i]) && !/^&gt;/.test(lines[i])) para.push(lines[i++]);
      if (para.length) out.push(`<p>${inline(para.join("<br>"))}</p>`);
      else { out.push(`<p>${inline(line)}</p>`); i++; }
    }
    return out.join("\n");
  }

  window.renderMarkdown = render;
  window.escapeHtml = esc;
})();
