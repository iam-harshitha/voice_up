// Static checks for the dashboard's deployment files. They cannot replace `nginx -t` or a
// real image build (see backend/docs/deployment.md), but they pin the properties that a
// review found broken: nginx header inheritance, cache headers and the build context.
import { describe, expect, it } from "vitest";
import dockerfile from "../../Dockerfile?raw";
import dockerignore from "../../.dockerignore?raw";
import nginxConf from "../../nginx.conf?raw";
import securityHeaders from "../../nginx-security-headers.conf?raw";

const SNIPPET_PATH = "/etc/nginx/snippets/security-headers.conf";
const REQUIRED_HEADERS = [
  "X-Content-Type-Options",
  "X-Frame-Options",
  "Referrer-Policy",
  "Content-Security-Policy",
  "Strict-Transport-Security",
];

function stripComments(text: string): string {
  return text
    .split("\n")
    .map((line) => line.replace(/#.*$/, ""))
    .join("\n");
}

/** Body text of each `location <path> { ... }` block, plus everything outside them. */
function parse(conf: string): { locations: Map<string, string>; outside: string } {
  const text = stripComments(conf);
  const locations = new Map<string, string>();
  let outside = "";
  const pattern = /location\s+([^\s{]+)\s*\{/g;
  let cursor = 0;
  let match: RegExpExecArray | null;
  while ((match = pattern.exec(text)) !== null) {
    outside += text.slice(cursor, match.index);
    let depth = 1;
    let i = pattern.lastIndex;
    while (depth > 0 && i < text.length) {
      if (text[i] === "{") depth += 1;
      if (text[i] === "}") depth -= 1;
      i += 1;
    }
    locations.set(match[1]!, text.slice(pattern.lastIndex, i - 1));
    cursor = i;
    pattern.lastIndex = i;
  }
  outside += text.slice(cursor);
  return { locations, outside };
}

describe("nginx security headers", () => {
  const { locations, outside } = parse(nginxConf);

  it("covers the dashboard, its assets and the API", () => {
    expect([...locations.keys()].sort()).toEqual(["/", "/api/", "/assets/"]);
  });

  it("includes the header snippet in every location", () => {
    for (const [path, body] of locations) {
      expect(body, path).toContain(`include ${SNIPPET_PATH};`);
    }
  });

  it("declares no add_header outside locations (it would be silently dropped)", () => {
    expect(outside).not.toMatch(/add_header/);
  });

  it("does not declare security headers outside the snippet (no duplicates)", () => {
    for (const [path, body] of locations) {
      for (const header of REQUIRED_HEADERS) {
        expect(body, `${path} ${header}`).not.toContain(`add_header ${header}`);
      }
    }
  });

  it("sends every required header, on error responses too", () => {
    const snippet = stripComments(securityHeaders);
    for (const header of REQUIRED_HEADERS) {
      expect(snippet).toMatch(new RegExp(`add_header ${header} ".+" always;`));
    }
    expect(snippet).toContain("frame-ancestors 'none'");
  });

  it("keeps one Cache-Control per location and the intended caching", () => {
    for (const [path, body] of locations) {
      expect((body.match(/add_header Cache-Control/g) ?? []).length, path).toBeLessThanOrEqual(1);
      expect(body, path).not.toMatch(/\bexpires\b/);
    }
    expect(locations.get("/")).toContain('add_header Cache-Control "no-store"');
    expect(locations.get("/assets/")).toMatch(/Cache-Control "public, max-age=31536000, immutable"/);
  });

  it("is installed where nginx.conf includes it", () => {
    expect(dockerfile).toContain(`COPY nginx-security-headers.conf ${SNIPPET_PATH}`);
  });
});

describe("frontend Docker build context", () => {
  const entries = stripComments(dockerignore)
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);

  it("excludes host dependencies, build output and environment files", () => {
    for (const entry of ["node_modules", "dist", "coverage", "*.tsbuildinfo", ".env", ".env.*"]) {
      expect(entries).toContain(entry);
    }
  });

  it("installs dependencies inside the image before copying sources", () => {
    const ci = dockerfile.indexOf("RUN npm ci");
    const copySources = dockerfile.indexOf("COPY . .");
    expect(ci).toBeGreaterThan(-1);
    expect(copySources).toBeGreaterThan(ci);
  });
});
