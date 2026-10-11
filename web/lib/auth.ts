import { cookies } from "next/headers";
import { redirect } from "next/navigation";
import { SESSION_MAX_AGE_SECONDS, sessionSecret } from "./config.ts";
import { seal, unseal } from "./session.ts";

const COOKIE = "divesafe_session";

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
  (await cookies()).set(COOKIE, seal(apiKey, sessionSecret(), Date.now(), SESSION_MAX_AGE_SECONDS), {
    httpOnly: true,
    sameSite: "strict",
    secure: process.env.NODE_ENV === "production",
    path: "/",
    maxAge: SESSION_MAX_AGE_SECONDS,
  });
}

export async function endSession(): Promise<void> {
  (await cookies()).delete(COOKIE);
}
