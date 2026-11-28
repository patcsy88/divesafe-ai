import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { SESSION_MAX_AGE_SECONDS, sessionSecret } from "./config.ts";
import { COOKIE, COOKIE_OPTIONS } from "./cookie.ts";
import { seal, unseal } from "./session.ts";

export async function readKey(): Promise<string | null> {
  const token = (await cookies()).get(COOKIE)?.value;
  return token ? unseal(token, sessionSecret(), Date.now()) : null;
}

export async function requireKey(): Promise<string> {
  const key = await readKey();
  if (!key) redirect("/login");
  return key;
}

export async function startSession(apiKey: string): Promise<void> {
  (await cookies()).set(
    COOKIE,
    seal(apiKey, sessionSecret(), Date.now(), SESSION_MAX_AGE_SECONDS),
    COOKIE_OPTIONS,
  );
}

// A browser refuses any `__Host-` cookie, the clearing one included, without Secure and Path=/.
export async function endSession(): Promise<void> {
  (await cookies()).delete({ name: COOKIE, ...COOKIE_OPTIONS });
}
