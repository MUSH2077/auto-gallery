"use client";

import { useCallback, useEffect, useRef, useState } from "react";

type WsMessage = {
  type: "status_change" | "progress" | "heartbeat" | "connected" | "pong";
  task_id?: string;
  task_type?: "download" | "import";
  old_status?: string;
  new_status?: string;
  progress?: Record<string, unknown>;
  timestamp?: string;
};

type UseWsOptions = {
  enabled?: boolean;
  onStatusChange?: (msg: WsMessage) => void;
  onProgress?: (msg: WsMessage) => void;
};

type ConnectionStatus = "idle" | "connecting" | "connected" | "polling";

function resolveWebSocketUrl(ticket?: string): string | null {
  if (typeof window === "undefined") return null;

  const configured = process.env.NEXT_PUBLIC_WS_URL?.trim();
  const protocol = window.location.protocol === "https:" ? "wss:" : "ws:";
  const base = configured || `${protocol}//${window.location.host}/api/v1/ws`;
  const url = base.startsWith("/")
    ? new URL(base, `${protocol}//${window.location.host}`)
    : new URL(base);

  if (url.protocol === "http:") url.protocol = "ws:";
  if (url.protocol === "https:") url.protocol = "wss:";
  if (ticket) url.searchParams.set("ticket", ticket);

  return url.toString();
}

async function createWebSocketTicket(): Promise<string | null> {
  if (typeof window === "undefined") return null;
  const token = localStorage.getItem("ag_token");
  if (!token) return null;

  const res = await fetch("/api/v1/auth/ws-ticket", {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "application/json",
    },
  });
  if (!res.ok) return null;
  const body = await res.json().catch(() => null);
  return typeof body?.ticket === "string" ? body.ticket : null;
}

export function useJobWebSocket(options?: UseWsOptions) {
  const { enabled = true, onStatusChange, onProgress } = options ?? {};
  const [connected, setConnected] = useState(false);
  const [status, setStatus] = useState<ConnectionStatus>("idle");
  const [lastEvent, setLastEvent] = useState<WsMessage | null>(null);
  const wsRef = useRef<WebSocket | null>(null);
  const handlersRef = useRef({ onStatusChange, onProgress });
  const ticketFallbackTriedRef = useRef(false);
  const intentionalCloseRef = useRef(false);
  const connectGenerationRef = useRef(0);

  useEffect(() => {
    handlersRef.current = { onStatusChange, onProgress };
  }, [onStatusChange, onProgress]);

  const connect = useCallback((ticket?: string) => {
    if (typeof window === "undefined") return;
    if (!localStorage.getItem("ag_token")) {
      setConnected(false);
      setStatus("idle");
      return;
    }

    const existing = wsRef.current;
    if (existing?.readyState === WebSocket.CONNECTING || existing?.readyState === WebSocket.OPEN) {
      return;
    }

    const wsUrl = resolveWebSocketUrl(ticket);
    if (!wsUrl) return;

    const generation = ++connectGenerationRef.current;
    intentionalCloseRef.current = false;
    setStatus("connecting");

    try {
      const ws = new WebSocket(wsUrl);
      wsRef.current = ws;
      let opened = false;

      ws.onopen = () => {
        if (generation !== connectGenerationRef.current) return;
        opened = true;
        setConnected(true);
        setStatus("connected");
      };

      ws.onclose = async () => {
        if (generation !== connectGenerationRef.current) return;
        wsRef.current = null;
        setConnected(false);
        if (intentionalCloseRef.current) return;

        if (!opened && !ticket && !ticketFallbackTriedRef.current) {
          ticketFallbackTriedRef.current = true;
          const nextTicket = await createWebSocketTicket().catch(() => null);
          if (generation !== connectGenerationRef.current || intentionalCloseRef.current) {
            return;
          }
          if (nextTicket) {
            connect(nextTicket);
            return;
          }
        }
        setStatus("polling");
      };

      ws.onerror = () => {
        if (generation !== connectGenerationRef.current) return;
        setConnected(false);
      };

      ws.onmessage = (event) => {
        if (generation !== connectGenerationRef.current) return;
        try {
          const data: WsMessage = JSON.parse(event.data);
          setLastEvent(data);
          if (data.type === "status_change") handlersRef.current.onStatusChange?.(data);
          if (data.type === "progress") handlersRef.current.onProgress?.(data);
        } catch {
          // Ignore malformed push messages; polling remains the source of truth.
        }
      };
    } catch {
      setConnected(false);
      setStatus("polling");
    }
  }, []);

  useEffect(() => {
    if (!enabled) {
      setStatus("idle");
      setConnected(false);
      return;
    }

    ticketFallbackTriedRef.current = false;
    intentionalCloseRef.current = false;
    connect();

    const pingInterval = setInterval(() => {
      if (wsRef.current?.readyState === WebSocket.OPEN) {
        wsRef.current.send(JSON.stringify({ action: "ping" }));
      }
    }, 30000);

    return () => {
      intentionalCloseRef.current = true;
      connectGenerationRef.current += 1;
      clearInterval(pingInterval);
      wsRef.current?.close();
      wsRef.current = null;
    };
  }, [enabled, connect]);

  return { connected, status, lastEvent, ws: wsRef.current };
}
