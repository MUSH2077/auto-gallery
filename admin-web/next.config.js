/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // Playwright and LAN development use the loopback IP rather than localhost.
  // Next 16 blocks cross-origin development assets unless this is explicit.
  allowedDevOrigins: ['127.0.0.1'],

  // API proxying is handled by Next.js rewrites (server-side proxy).
  // Do NOT add a catch-all route handler under src/app/api/v1/ — rewrites
  // intercept those paths first, so any manual proxy there would be dead code.
  async rewrites() {
    return [
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
