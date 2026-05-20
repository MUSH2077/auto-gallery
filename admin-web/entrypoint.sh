#!/bin/sh
# Replace placeholder admin key in built JS with runtime env var
if [ -n "$ADMIN_PASSWORD" ]; then
  find /app/.next -name "*.js" -exec sed -i "s/__ADMIN_KEY_PLACEHOLDER__/$ADMIN_PASSWORD/g" {} +
fi
exec node server.js
