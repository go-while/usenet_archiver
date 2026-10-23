#!/usr/bin/env bash
# Lists article paths with a crosspost count from the Newsgroups: header (comma-separated groups).
# Requires: bash, find, awk, sort. SPOOL may be set in the environment
# (default: tradspool under /var/spool/news/articles/).

SPOOL="${SPOOL:-/var/spool/news/articles}"
NOW=$(date '+%F_%H:%M:%S')
LIST=$(mktemp)
STAGE=$(mktemp)
trap 'rm -f "${LIST}" "${STAGE}"' EXIT

find "${SPOOL}/" -type f >"${LIST}"

OUT="${HOME}/${NOW}-sorted.csv"
: >"${STAGE}"

while IFS= read -r path; do
	[ -f "${path}" ] || continue
	# The Newsgroups field of the header (it ends at the first empty line),
	# with its continuation lines: the number of groups in it.
	ngroups=$(awk '
		/^\r?$/ { exit }
		infield && /^[ \t]/ { value = value $0; next }
		infield { exit }
		tolower(substr($0, 1, 11)) == "newsgroups:" { infield = 1; value = substr($0, 12) }
		END {
			n = 0
			count = split(value, groups, ",")
			for (i = 1; i <= count; i++) {
				gsub(/[ \t\r]/, "", groups[i])
				if (groups[i] != "") n++
			}
			print n
		}' "${path}" 2>/dev/null || echo 0)
	printf '%s,%s\n' "${path}" "${ngroups}" >>"${STAGE}"
done <"${LIST}"

sort -t, -k2 -nr "${STAGE}" >"${OUT}"
