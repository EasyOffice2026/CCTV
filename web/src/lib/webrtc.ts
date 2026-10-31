"use client";

import { api, unwrap } from "@/lib/api";

let iceServers: Promise<RTCIceServer[]> | null = null;

function getIceServers(): Promise<RTCIceServer[]> {
  iceServers ??= unwrap(api.GET("/v1/live/ice-servers"))
    .then((r) => r.ice_servers as unknown as RTCIceServer[]) // server config, passed as-is
    .catch((error) => {
      iceServers = null;
      throw error;
    });
  return iceServers;
}

function iceGatheringDone(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === "complete") return Promise.resolve();
  return new Promise((resolve) => {
    const timer = setTimeout(resolve, timeoutMs); // send what we have; enough on most networks
    pc.addEventListener("icegatheringstatechange", () => {
      if (pc.iceGatheringState === "complete") {
        clearTimeout(timer);
        resolve();
      }
    });
  });
}

/**
 * Receive-only WebRTC session with the branch's edge box (go2rtc). `exchange` sends our
 * offer through the API and returns the box's answer. Candidates are gathered before
 * sending (no trickle ICE through the relay).
 */
export async function startWebRtc(
  exchange: (offerSdp: string) => Promise<string>,
  onStream: (stream: MediaStream) => void,
): Promise<RTCPeerConnection> {
  const pc = new RTCPeerConnection({ iceServers: await getIceServers() });
  try {
    pc.addTransceiver("video", { direction: "recvonly" });
    pc.addTransceiver("audio", { direction: "recvonly" });
    pc.addEventListener("track", (event) => {
      const [stream] = event.streams;
      onStream(stream ?? new MediaStream([event.track]));
    });
    await pc.setLocalDescription(await pc.createOffer());
    await iceGatheringDone(pc, 2500);
    const answer = await exchange(pc.localDescription?.sdp ?? "");
    await pc.setRemoteDescription({ type: "answer", sdp: answer });
    return pc;
  } catch (error) {
    pc.close();
    throw error;
  }
}
