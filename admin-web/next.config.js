/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  // Playwright and LAN development use the loopback IP rather than localhost.
  // Next 16 blocks cross-origin development assets unless this is explicit.
  allowedDevOrigins: ['127.0.0.1'],

  async redirects() {
    return [
      {
        source: '/admin/settings/data-mgmt',
        destination: '/admin/data-mgmt',
        permanent: false,
      },
    ]
  },

  // API proxying is handled by Next.js rewrites (server-side proxy).
  // Do NOT add a catch-all route handler under src/app/api/v1/ — rewrites
  // intercept those paths first, so any manual proxy there would be dead code.
  async rewrites() {
    return [
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
