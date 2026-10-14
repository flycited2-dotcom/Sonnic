#!/bin/sh
case "${RENEWED_LINEAGE:-}" in
    /etc/letsencrypt/live/sonnic-test|/etc/letsencrypt/live/sonnic-public)
        nginx -t && systemctl reload nginx
        ;;
esac
