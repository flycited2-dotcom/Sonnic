#!/bin/sh
if [ "$RENEWED_LINEAGE" = /etc/letsencrypt/live/sonnic-test ]; then
    nginx -t && systemctl reload nginx
fi
