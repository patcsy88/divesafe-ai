import { isIP } from "node:net";

// A failed-sign-in limit for the web server, in this process only (one instance; counters are not
// shared across replicas or restarts). It must trip before the API's own failed-authentication
// limit (10 a minute by default), because behind this server every person reaches the API from one
// address and the API would otherwise refuse them all.
export const MAX_FAILURES = 5;
export const WINDOW_MS = 60_000;
export const MAX_TRACKED = 5_000;

export class LoginThrottle {
  private hits = new Map<string, number[]>();
  private readonly max: number;
  private readonly windowMs: number;
  private readonly maxTracked: number;

  constructor(max = MAX_FAILURES, windowMs = WINDOW_MS, maxTracked = MAX_TRACKED) {
    this.max = max;
    this.windowMs = windowMs;
    this.maxTracked = maxTracked;
  }

  private live(source: string, now: number): number[] {
    const kept = (this.hits.get(source) ?? []).filter((t) => now - t < this.windowMs);
    if (kept.length > 0) this.hits.set(source, kept);
    else this.hits.delete(source);
    return kept;
  }

  // Counts this attempt as a failure up front, in one synchronous step, so parallel attempts cannot
  // all pass a check before any is counted. Returns whole seconds to wait if the source is already
  // at its limit (and counts nothing, so a blocked source cannot extend its own block), else null.
  reserve(source: string, now: number): number | null {
    const kept = this.live(source, now);
    if (kept.length >= this.max) {
      return Math.max(1, Math.ceil((this.windowMs - (now - kept[0]!)) / 1000));
    }
    kept.push(now);
    this.hits.set(source, kept);
    if (this.hits.size > this.maxTracked) this.shed(now);
    return null;
  }

  // Takes back one reserved attempt: the key was right, or the failure was not the person's.
  release(source: string): void {
    const kept = this.hits.get(source);
    if (!kept) return;
    kept.pop();
    if (kept.length === 0) this.hits.delete(source);
  }

  // Past the cap: drop expired entries, then the oldest unblocked ones, then the oldest blocked.
  private shed(now: number): void {
    for (const key of [...this.hits.keys()]) this.live(key, now);
    const target = Math.floor((this.maxTracked * 9) / 10);
    for (const blockedToo of [false, true]) {
      for (const [key, times] of [...this.hits]) {
        if (this.hits.size <= target) return;
        if (blockedToo || times.length < this.max) this.hits.delete(key);
      }
    }
  }
}

// IPv6 as its /64 prefix (one subscriber controls 2**64 addresses), IPv4-mapped IPv6 as IPv4, zone
// ids dropped. Null if it is not an address.
export function bucketOf(text: string): string | null {
  const cleaned = text.trim().split("%")[0]!;
  if (cleaned.length === 0 || cleaned.length > 64) return null;
  const kind = isIP(cleaned);
  if (kind === 4) return cleaned;
  if (kind !== 6) return null;
  const lower = cleaned.toLowerCase();
  const mapped = /^(?:0{0,4}:){2,5}ffff:(\d{1,3}(?:\.\d{1,3}){3})$/.exec(lower);
  if (mapped && isIP(mapped[1]!) === 4) return mapped[1]!;
  const hex = /^(?:0{0,4}:){2,5}ffff:([0-9a-f]{1,4}):([0-9a-f]{1,4})$/.exec(lower);
  if (hex) {
    const hi = parseInt(hex[1]!, 16);
    const lo = parseInt(hex[2]!, 16);
    return `${hi >> 8}.${hi & 255}.${lo >> 8}.${lo & 255}`;
  }
  let head = lower;
  let tail = "";
  if (lower.includes("::")) {
    const [a, b] = lower.split("::") as [string, string];
    head = a;
    tail = b;
  }
  const front = head === "" ? [] : head.split(":");
  const back = tail === "" ? [] : tail.split(":");
  const groups = lower.includes("::")
    ? [...front, ...Array<string>(8 - front.length - back.length).fill("0"), ...back]
    : front;
  if (groups.length !== 8 || groups.some((g) => g.includes("."))) return null;
  return groups.slice(0, 4).map((g) => g.replace(/^0+(?=.)/, "")).join(":") + "::/64";
}

// Who is signing in. The forwarded address is believed only when this server is configured to sit
// behind a proxy that sets it; otherwise everyone shares one bucket, which is safer than letting a
// client pick its own.
export function sourceOf(forwardedFor: string | null, trustProxy: boolean): string {
  if (!trustProxy || !forwardedFor) return "shared";
  return bucketOf(forwardedFor.split(",").pop() ?? "") ?? "shared";
}

// The caller's address as the proxy saw it, for passing on to the API (which does its own
// normalising). Null unless the proxy is trusted and the last entry is a real address.
export function forwardedAddress(forwardedFor: string | null, trustProxy: boolean): string | null {
  if (!trustProxy || !forwardedFor) return null;
  const last = (forwardedFor.split(",").pop() ?? "").trim();
  return bucketOf(last) === null ? null : last.split("%")[0]!;
}
