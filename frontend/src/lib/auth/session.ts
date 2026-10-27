import { hostedAllowsInsecureCookie, isHostedProfile } from "@/lib/deployment-profile"

export const NEWSROOM_SESSION_COOKIE = "newsroom_session"
export const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    maxAge: SESSION_TTL_SECONDS,
    path: "/",
    sameSite: "lax" as const,
    // A hosted service always runs behind TLS; only explicit local
    // integration over plain http may drop the Secure flag.
    secure: isHostedProfile() ? !hostedAllowsInsecureCookie() : process.env.NODE_ENV === "production",
  }
}
