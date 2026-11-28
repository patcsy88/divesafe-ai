// `__Host-`: the browser keeps it only if it is Secure, has Path=/ and no Domain. No Max-Age or
// Expires, so it ends when the browser closes (owner #139a); the sealed expiry still ends it after
// SESSION_MAX_AGE_SECONDS. Signing out removes the browser's copy only: to end a session that may
// have been copied, revoke the API key or rotate the session secret.
export const COOKIE = "__Host-divesafe_session";

export const COOKIE_OPTIONS = {
  httpOnly: true,
  sameSite: "strict",
  secure: true,
  path: "/",
} as const;
