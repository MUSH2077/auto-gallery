/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // The bounded Docker build runs `tsc --noEmit` in a separate, mandatory
  // stage after Next has generated its route types. Keeping the switch
  // environment-scoped means ordinary `next build` calls still use Next's
  // built-in checker and cannot accidentally weaken local type safety.
  typescript: {
    ignoreBuildErrors: process.env.NEXT_SKIP_INTERNAL_TYPECHECK === '1',
  },
  // Playwright and LAN development use the loopback IP rather than localhost.
  // Next 16 blocks cross-origin development assets unless this is explicit.
  allowedDevOrigins: ['127.0.0.1'],

  // X redirects to this frontend route with a short-lived code/state query.
  // The head bootstrap scrubs it before hydration; suppress Next's request log
  // for the route so the original request target cannot persist there either.
  logging: {
    incomingRequests: {
      ignore: [/^\/admin\/discovery(?:\?.*)?$/],
    },
  },

  // Most API calls use rewrites. /api/upload has a streaming API handler
  // to avoid cloning and truncating large multipart request bodies.
  // Do NOT add a catch-all API handler under src/app/api/v1/ — rewrites
  // intercept those paths first, so any manual proxy there would be dead code.
  async rewrites() {
    return [
      // Preserve callers of the existing frontend API URL while routing only
      // uploads through the streaming handler, before the external catch-all.
      {
        source: '/api/v1/upload',
        destination: '/api/upload',
      },
      {
        source: '/docs',
        destination: 'http://backend:8000/docs',
      },
      {
        source: '/redoc',
        destination: 'http://backend:8000/redoc',
      },
      {
        source: '/openapi.json',
        destination: 'http://backend:8000/openapi.json',
      },
      {
        source: '/api/docs',
        destination: 'http://backend:8000/api/docs',
      },
      {
        source: '/api/redoc',
        destination: 'http://backend:8000/api/redoc',
      },
      {
        source: '/api/openapi.json',
        destination: 'http://backend:8000/api/openapi.json',
      },
      {
        source: '/api/asyncapi.yaml',
        destination: 'http://backend:8000/api/asyncapi.yaml',
      },
      {
        source: '/api/v1/:path*',
        destination: 'http://backend:8000/api/v1/:path*',
      },
      {
        source: '/media/:path*',
        destination: 'http://backend:8000/media/:path*',
      },
    ]
  },
}
module.exports = nextConfig
