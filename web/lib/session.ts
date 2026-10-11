import { createCipheriv, createDecipheriv, hkdfSync, randomBytes } from "node:crypto";

// The person's API key is sealed with AES-256-GCM so it can sit in an httpOnly cookie without
// being readable by the browser, a proxy log or page script. The cookie is a convenience, not a
// new credential: the API still checks the key on every call.
const INFO = "divesafe-web-session-v1";

function derive(secret: string): Buffer {
  return Buffer.from(hkdfSync("sha256", secret, "divesafe-web", INFO, 32));
}

export function seal(apiKey: string, secret: string, nowMs: number, maxAgeSeconds: number): string {
  const iv = randomBytes(12);
  const cipher = createCipheriv("aes-256-gcm", derive(secret), iv);
  const payload = JSON.stringify({ k: apiKey, exp: nowMs + maxAgeSeconds * 1000 });
  const body = Buffer.concat([cipher.update(payload, "utf8"), cipher.final()]);
  return Buffer.concat([iv, cipher.getAuthTag(), body]).toString("base64url");
}

export function unseal(token: string, secret: string, nowMs: number): string | null {
  try {
    const raw = Buffer.from(token, "base64url");
    if (raw.length < 12 + 16 + 2) return null;
    const decipher = createDecipheriv("aes-256-gcm", derive(secret), raw.subarray(0, 12));
    decipher.setAuthTag(raw.subarray(12, 28));
    const text = Buffer.concat([decipher.update(raw.subarray(28)), decipher.final()]).toString("utf8");
    const data: unknown = JSON.parse(text);
    if (typeof data !== "object" || data === null) return null;
    const { k, exp } = data as { k?: unknown; exp?: unknown };
    if (typeof k !== "string" || typeof exp !== "number" || exp <= nowMs) return null;
    return k;
  } catch {
    return null;
  }
}
