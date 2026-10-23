#!/usr/bin/env bash
# Report of active Big-8 hierarchy directories by immediate subtree size (tradspool layout).
# Writes ~/TIMESTAMP.mostarticles_big8.txt — portable paths; no web root required.
# SPOOL may be set in the environment (default /var/spool/news/articles).

SPOOL="${SPOOL:-/var/spool/news/articles}"
SPOOL="${SPOOL%/}"
NOW=$(date '+%F_%H:%M:%S')

# a whole hierarchy name: comp and comp/lang, not compx or socal
BIG8='(comp|humanities|misc|news|rec|sci|soc|talk)'
PATTERN="^[0-9]+ ${SPOOL}/${BIG8}(/|$)"

echo "running big8 report"
echo
echo "${NOW}" >"${HOME}/${NOW}.mostarticles_big8.txt"
find "${SPOOL}" -xdev -type d -exec sh -c '
    echo "$(find "$0" -mindepth 1 -maxdepth 1 -printf x | wc -c) $0"' {} \; | sort -rn | grep -E "${PATTERN}" >>"${HOME}/${NOW}.mostarticles_big8.txt"
