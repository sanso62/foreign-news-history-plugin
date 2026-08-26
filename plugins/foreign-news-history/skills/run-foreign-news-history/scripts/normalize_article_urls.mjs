#!/usr/bin/env node

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);

function fail() {
  process.stdout.write(JSON.stringify({ ok: false, error: "normalizer_execution_failed" }));
  process.exitCode = 1;
}

try {
  const request = JSON.parse(Buffer.concat(chunks).toString("utf8"));
  if (typeof request.module_source !== "string" || !Array.isArray(request.urls)) {
    fail();
  } else {
    const encoded = Buffer.from(request.module_source, "utf8").toString("base64");
    const module = await import(`data:text/javascript;base64,${encoded}`);
    if (typeof module.normalizeArticleUrl !== "function") {
      fail();
    } else {
      const normalized = [];
      for (const rawUrl of request.urls) {
        try {
          const value = await module.normalizeArticleUrl(String(rawUrl ?? ""));
          normalized.push(value == null ? "" : String(value));
        } catch {
          normalized.push("");
        }
      }
      process.stdout.write(JSON.stringify({ ok: true, normalized }));
    }
  }
} catch {
  fail();
}
