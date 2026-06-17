"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type WsMessage = {
  type: "status_change" | "progress" | "heartbeat" | "connected" | "pong";
  task_id: string;
  task_type: "download" | "import";
  old_status?: string;
  new_status?: string;
  progress?: Record<string, unknown>;
  timestamp: string;
};

type UseWsOptions = {
  enabled?: boolean;
  onStatusChange?: (msg: WsMessage) => void;
  onProgress?: (msg: WsMessage) => void;
};

export function useJobWebSocket(options?: UseWsOptions) {
  const { enabled = true, onStatusChange, onProgress } = options ?? {};
  const [connected, setConnected] = useState(false);
  const [lastEvent, setLastEvent] = useState<WsMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();

  const connect = useCallback(() => {
    // Check if token exists before attempting connection
    if (typeof window !== "undefined" && !localStorage.getItem("ag_token")) {
      return;
    }
    if (typeof window === "undefined") return;
    // Auth is via JWT cookie (ag_token) — browser sends it automatically
    // on the WebSocket upgrade request. No token in URL needed.
    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.hostname}:8818/api/v1/ws`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);

      ws.onopen = () => setConnected(true);

      ws.onclose = () => {
        setConnected(false);
        // Don't retry — polling is the fallback. WebSocket will reconnect
        // on next page navigation or manual refresh only.
      };

      ws.onerror = () => {
        // Connection failed — close silently, rely on polling
        setConnected(false);
      };

      ws.onmessage = (event) => {
        try {
          const data: WsMessage = JSON.parse(event.data);
          setLastEvent(data);
          if (data.type === "status_change") onStatusChange?.(data);
          if (data.type === "progress") onProgress?.(data);
        } catch { /* ignore parse errors */ }
      };
    } catch {
      reconnectTimer.current = setTimeout(connect, 5000);
    }
  }, [onStatusChange, onProgress]);

  useEffect(() => {
    if (!enabled) return;
    connect();
    const pingInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action: "ping" }));
      }
    }, 30000);
    return () => {
      clearInterval(pingInterval);
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close();
    };
  }, [enabled, connect]);

  return { connected, lastEvent, ws: wsRef.current };
}
