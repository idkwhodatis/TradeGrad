// Session-scoped persistence survives reloads without retaining credentials indefinitely.
const SESSION_KEY = "tradegrad.session.v1";
const SESSION_EVENT = "tradegrad:session";
export type Session = { accessToken: string; expiresAt: number };

export function readSession(): Session | null {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const value = JSON.parse(raw) as Session;
    if (
      typeof value.accessToken !== "string" ||
      !Number.isFinite(value.expiresAt) ||
      value.expiresAt <= Date.now()
    ) {
      sessionStorage.removeItem(SESSION_KEY);
      return null;
    }
    return value;
  } catch {
    return null;
  }
}
export function saveSession(accessToken: string, expiresIn: number) {
  sessionStorage.setItem(
    SESSION_KEY,
    JSON.stringify({ accessToken, expiresAt: Date.now() + expiresIn * 1000 }),
  );
  window.dispatchEvent(new Event(SESSION_EVENT));
}
export function clearSession(expired = false) {
  sessionStorage.removeItem(SESSION_KEY);
  window.dispatchEvent(new CustomEvent(SESSION_EVENT, { detail: { expired } }));
}
export function watchSession(callback: (event: Event) => void) {
  window.addEventListener(SESSION_EVENT, callback);
  return () => window.removeEventListener(SESSION_EVENT, callback);
}
export function safeReturnTo(value: unknown): string {
  if (
    typeof value !== "string" ||
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    /[\u0000-\u001f\u007f]/.test(value) ||
    value.startsWith("/login")
  )
    return "/";
  return value;
}
