// Fails if the production bundle could expose server-side secrets or credentials.
// Run after `npm run build`: node scripts/check-bundle.mjs
import { readdirSync, readFileSync, statSync } from "node:fs";
import { join } from "node:path";

const DIST = new URL("../dist/", import.meta.url).pathname.replace(/^\/([A-Za-z]:)/, "$1");
const FORBIDDEN = [
  [/al_[A-Za-z0-9_-]{40,}/, "an Approval Line API key"],
  [/X-API-Key/i, "an X-API-Key header (the dashboard must use its session cookie)"],
  [/BOLNA_API_KEY|VOICE_WEBHOOK_TOKEN|DATABASE_URL|TEST_DATABASE_URL/, "a server-side setting name"],
  [/postgres(ql)?(\+psycopg)?:\/\//i, "a database URL"],
  [/VITE_[A-Z_]*(KEY|TOKEN|SECRET|PASSWORD)/, "a public VITE_ variable holding a secret"],
  [/localStorage|sessionStorage/, "browser storage (no credentials may be stored client-side)"],
];

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

const all = files(DIST);
const problems = [];
if (all.some((f) => f.endsWith(".map"))) problems.push("source maps are present in dist/");
for (const file of all.filter((f) => /\.(js|css|html)$/.test(f))) {
  const text = readFileSync(file, "utf8");
  for (const [pattern, what] of FORBIDDEN) {
    if (pattern.test(text)) problems.push(`${file}: contains ${what}`);
  }
}
if (problems.length) {
  console.error("Bundle check FAILED:\n  " + problems.join("\n  "));
  process.exit(1);
}
console.log(`Bundle check passed: ${all.length} files scanned, no secrets or credential storage found.`);
