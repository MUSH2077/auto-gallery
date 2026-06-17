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
    if (typeof window === "undefined") return;
    const token = localStorage.getItem("ag_token");
    if (!token) return;

    const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
    const wsUrl = `${protocol}//${window.location.host}/api/v1/ws?token=${token}`;

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;

      ws.onopen = () => setConnected(true);

      ws.onclose = () => {
        setConnected(false);
        reconnectTimer.current = setTimeout(connect, 3000);
      };

      ws.onerror = () => ws.close();

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
