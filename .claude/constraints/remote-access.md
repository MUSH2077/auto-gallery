# Remote Access Constraint

## Rule

The system is designed for NAS LAN deployment in v1. Remote client access must be provided through network-layer solutions, not by exposing the application directly to the public internet. Application code must not implement its own internet-facing security.

## Access tiers

### v1 (current): LAN only

- All services on internal Docker network
- Backend API port (`BACKEND_PORT`, e.g. 8818) exposed on NAS LAN
- Admin web port (`ADMIN_WEB_PORT`, e.g. 3000) exposed on NAS LAN
- Client (Flutter) connects via LAN IP while on same network
- No public internet exposure
- No HTTPS required (LAN HTTP is acceptable for v1)

### v1.5 (optional): VPN access

For remote client access without exposing to internet:

- **Recommended**: Tailscale (free for personal use, simple setup)
- **Alternative**: WireGuard VPN server on NAS or router
- Client connects to NAS via VPN IP (e.g., `100.x.x.x` for Tailscale)
- No application changes needed — API and media endpoints work identically
- HTTPS still not required (VPN provides encryption layer)

### v2 (future): Reverse proxy with HTTPS

For controlled internet exposure (if VPN is not acceptable):

- **Recommended**: Caddy or Nginx reverse proxy on NAS
- **Alternative**: Cloudflare Tunnel (no open ports on router)
- HTTPS via Let's Encrypt (Caddy automates this) or Cloudflare origin cert
- Reverse proxy handles TLS termination; backend services remain HTTP internally
- Firewall: only allow 443 (HTTPS) from internet

## Application code must NOT implement

- TLS/HTTPS handling (leave to reverse proxy)
- IP allowlisting/blocklisting (leave to firewall or VPN)
- DDoS protection (leave to reverse proxy / Cloudflare)
- Port forwarding logic
- Dynamic DNS

## Application code MUST implement

- Bind to `0.0.0.0` inside container (Docker networking requirement)
- CORS: allow only known origins (admin-web LAN IP, not wildcard)
- Trust `X-Forwarded-For` and `X-Forwarded-Proto` headers when behind reverse proxy (configured via env var `BEHIND_PROXY=true`)
- Rate limiting on auth endpoints (`/api/v1/auth/*`): max 10 requests/minute per IP
- Rate limiting on media endpoints (`/media/*`): max 100 requests/minute per user (prevent NAS I/O saturation from a single client)

## Network architecture

```
┌─────────────────────────────────────────────────┐
│  Internet (v2 only, via reverse proxy)           │
│  HTTPS :443 → Caddy/Nginx                        │
└──────────────────┬──────────────────────────────┘
                   │ TLS terminated here
┌──────────────────▼──────────────────────────────┐
│  NAS LAN                                         │
│  ┌─────────┐  ┌──────────┐  ┌───────────────┐   │
│  │ backend │  │admin-web │  │ postgres/redis │   │
│  │ :8818   │  │ :3000    │  │ /meilisearch  │   │
│  └────┬────┘  └────┬─────┘  │ (internal)    │   │
│       │            │        └───────────────┘   │
│       └─── LAN ────┘                             │
│            │                                     │
│  ┌─────────▼────────┐                            │
│  │ Client (Flutter) │                            │
│  │ LAN or VPN       │                            │
│  └──────────────────┘                            │
└─────────────────────────────────────────────────┘
```

## Authentication for remote access

When client connects remotely:
- JWT tokens transmitted over HTTPS (v2) or VPN-encrypted connection (v1.5)
- Never transmit credentials over plain HTTP outside LAN
- Token storage on client: secure device storage (Flutter: `flutter_secure_storage`)
- Refresh token rotation: each refresh invalidates the previous refresh token

## What v1 explicitly does NOT support

- Direct public internet exposure
- HTTPS (unless behind user-managed reverse proxy)
- Multi-factor authentication
- OAuth / social login
- IP geolocation blocking
- Intrusion detection
