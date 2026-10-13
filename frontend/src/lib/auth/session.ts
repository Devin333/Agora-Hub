import { localDesktopAllowsInsecureCookie } from "@/lib/deployment-profile"

export const NEWSROOM_SESSION_COOKIE = "newsroom_session"
export const SESSION_TTL_SECONDS = 7 * 24 * 60 * 60

export function sessionCookieOptions() {
  return {
    httpOnly: true,
    maxAge: SESSION_TTL_SECONDS,
    path: "/",
    sameSite: "lax" as const,
    secure: localDesktopAllowsInsecureCookie() ? false : process.env.NODE_ENV === "production",
  }
}
