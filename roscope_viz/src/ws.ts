/**
 * WebSocket client wrapper with auto-reconnect.
 *
 * Uses browser-native WebSocket API — no dependencies.
 */

import type { ClientMessage, ServerMessage } from "./types.generated";

export type MessageHandler = (msg: ServerMessage) => void;

export class VizWebSocket {
  private ws: WebSocket | null = null;
  private url: string;
  private onMessage: MessageHandler;
  private reconnectDelay = 1000;
  private maxReconnectDelay = 10000;
  private shouldReconnect = true;

  constructor(url: string, onMessage: MessageHandler) {
    this.url = url;
    this.onMessage = onMessage;
    this.connect();
  }

  private connect() {
    this.ws = new WebSocket(this.url);

    this.ws.onopen = () => {
      this.reconnectDelay = 1000;
    };

    this.ws.onmessage = (event) => {
      try {
        const msg = JSON.parse(event.data) as ServerMessage;
        this.onMessage(msg);
      } catch {
        console.error("Failed to parse WS message:", event.data);
      }
    };

    this.ws.onclose = () => {
      if (this.shouldReconnect) {
        setTimeout(() => this.connect(), this.reconnectDelay);
        this.reconnectDelay = Math.min(
          this.reconnectDelay * 2,
          this.maxReconnectDelay,
        );
      }
    };

    this.ws.onerror = () => {
      this.ws?.close();
    };
  }

  send(msg: ClientMessage) {
    if (this.ws?.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(msg));
    }
  }

  close() {
    this.shouldReconnect = false;
    this.ws?.close();
  }
}

/** Build the WebSocket URL relative to the current page */
export function getWsUrl(): string {
  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  return `${proto}//${window.location.host}/ws`;
}
