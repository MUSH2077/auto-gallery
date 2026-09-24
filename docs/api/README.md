# LAN API contracts

The files in this directory are generated from the running FastAPI
application and are the versioned machine-readable contract for LAN clients:

- `openapi.json` describes every public HTTP operation under `/api/v1`.
- `asyncapi.yaml` describes the public `/api/v1/ws` WebSocket protocol.
- `capability-matrix.md` maps product behavior to the supported public API.

Do not edit the generated OpenAPI file by hand. After changing a route, request
model, response model, permission dependency, or WebSocket message, run:

```bash
cd backend
python scripts/export_api_contracts.py
cd ../admin-web
npm run generate:api-types
```

CI runs the exporter in check mode and compares the generated TypeScript types,
so runtime schema drift blocks a merge.

## Interactive documentation

An administrator can open `/api/docs` for Swagger UI or `/api/redoc` for the
read-only reference. The HTML, JavaScript, and CSS are served locally, so these
pages work without internet access. The contract endpoints are also protected:

- `/api/openapi.json`
- `/api/asyncapi.yaml`

An active administrator browser session can open the documentation and call
business APIs. Browser writes require a session-bound CSRF header and exact
allowed Origin. Swagger's **Authorize** action supports script clients: obtain
an explicit JWT from `POST /api/v1/auth/login` and enter it as Bearer. The
browser login endpoint does not return an access token to JavaScript.

The legacy `/docs`, `/redoc`, and `/openapi.json` paths permanently redirect to
their protected `/api/*` equivalents.

## LAN origins

CORS remains an explicit allowlist. Add each trusted client origin to
`CORS_ORIGINS`, including its scheme and port, for example:

```dotenv
CORS_ORIGINS=http://gallery-client.example.test:3000
```

Do not use `*` for a credentialed LAN installation. Browser session origins
use the dedicated `BROWSER_SESSION_ORIGINS` allowlist and must match the
exact scheme, host, and port of each admin web entry. HTTP LAN access
remains possible but sends its session cookie without HTTPS protection.
