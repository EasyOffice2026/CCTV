/**
 * Sign-in from the client's operational system (plan 8.1). Its links and embedded pages
 * carry a short-lived sign-in token in the URL fragment (`#token=…&lang=ar&next=/events/`):
 * browsers never send fragments to servers, so the token stays out of logs and Referer
 * headers. We read it once and remove it from the address bar.
 */
let taken: URLSearchParams | null = null;

export function takeHandoff(): URLSearchParams {
  if (taken) return taken; // effects run twice in development
  taken = new URLSearchParams(window.location.hash.slice(1));
  if (window.location.hash) window.history.replaceState(null, "", window.location.pathname + window.location.search);
  return taken;
}

/** Tell the page around an embedded one what happened (nothing secret is sent). */
export function tellParent(message: { type: string; [key: string]: unknown }): void {
  if (window.parent !== window) window.parent.postMessage(message, "*");
}
