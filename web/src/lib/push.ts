/**
 * Browser notifications through Web Push (plan 6.2): register public/sw.js, ask for
 * permission, subscribe with the API's VAPID key and send the subscription to the API.
 */
import { api, unwrap } from "@/lib/api";

export type PushState = "unsupported" | "unconfigured" | "denied" | "off" | "on";

export function pushSupported(): boolean {
  return typeof window !== "undefined" && "serviceWorker" in navigator && "PushManager" in window && "Notification" in window;
}

function keyBytes(base64url: string): Uint8Array<ArrayBuffer> {
  const padded = (base64url + "=".repeat((4 - (base64url.length % 4)) % 4)).replace(/-/g, "+").replace(/_/g, "/");
  const raw = atob(padded);
  const bytes = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) bytes[i] = raw.charCodeAt(i);
  return bytes;
}

async function registration(): Promise<ServiceWorkerRegistration> {
  return (await navigator.serviceWorker.getRegistration("/")) ?? navigator.serviceWorker.register("/sw.js", { scope: "/" });
}

export async function pushState(publicKey: string | null | undefined): Promise<PushState> {
  if (!pushSupported()) return "unsupported";
  if (!publicKey) return "unconfigured";
  if (Notification.permission === "denied") return "denied";
  const existing = await (await navigator.serviceWorker.getRegistration("/"))?.pushManager.getSubscription();
  return existing && Notification.permission === "granted" ? "on" : "off";
}

/** Asks for permission (must follow a click) and registers this browser. */
export async function enablePush(publicKey: string): Promise<PushState> {
  if ((await Notification.requestPermission()) !== "granted") return "denied";
  const reg = await registration();
  await navigator.serviceWorker.ready;
  const subscription =
    (await reg.pushManager.getSubscription()) ??
    (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: keyBytes(publicKey) }));
  const json = subscription.toJSON();
  await unwrap(
    api.PUT("/v1/me/push-subscriptions", {
      body: { endpoint: subscription.endpoint, keys: { p256dh: json.keys?.p256dh ?? "", auth: json.keys?.auth ?? "" } },
    }),
  );
  return "on";
}

export async function disablePush(): Promise<PushState> {
  const subscription = await (await navigator.serviceWorker.getRegistration("/"))?.pushManager.getSubscription();
  if (subscription) {
    await unwrap(api.DELETE("/v1/me/push-subscriptions", { params: { query: { endpoint: subscription.endpoint } } }));
    await subscription.unsubscribe();
  }
  return "off";
}
