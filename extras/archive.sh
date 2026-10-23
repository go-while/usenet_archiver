#!/usr/bin/env bash
# Monthly snapshot of tradspool-style article directories (GNU find date filters).
# Requires: bash, GNU find, GNU date, rsync (3.1 or later), zip.
#
# SPOOL, WORK and DEST may be set in the environment (defaults below).

set -euo pipefail

SPOOL="${SPOOL:-/var/spool/news/articles}"
WORK="${WORK:-/tmp/usenet}"
DEST="${DEST:-/opt/usenet}"

### Gathering Input

read -r -p "Month Number (1-12): " FDATE
read -r -p "Year: " YEAR

if [[ ! "${FDATE}" =~ ^[0-9]{1,2}$ ]] || [[ ! "${YEAR}" =~ ^[0-9]{4}$ ]]; then
	echo "Invalid month or year: ${FDATE} ${YEAR}" >&2
	exit 1
fi
MONTH=$((10#${FDATE}))  # 10#: "08" is eight, not a bad octal number

### Month logic (including leap years)

case "${MONTH}" in
	1|3|5|7|8|10|12)
		LDAY_NUM=31
		;;
	4|6|9|11)
		LDAY_NUM=30
		;;
	2)
		Y=$((10#${YEAR}))
		if (( (Y % 4 == 0 && Y % 100 != 0) || Y % 400 == 0 )); then
			LDAY_NUM=29
		else
			LDAY_NUM=28
		fi
		;;
	*)
		echo "Invalid month: ${FDATE}" >&2
		exit 1
		;;
esac

FMONTH=$(printf '%02d' "${MONTH}")
LDAY_PAD=$(printf '%02d' "${LDAY_NUM}")
NAME="${MONTH}-${YEAR}"

### Creating Archive

# -newermt is "newer than", strictly: the month is (last instant of the day
# before, last instant of its last day], i.e. 00:00:00 on the 1st up to and
# including 23:59:59.999999999 on the last day.
PREV_DAY=$(date -d "${YEAR}-${FMONTH}-01 -1 day" '+%F')
START_MT="${PREV_DAY} 23:59:59.999999999"
END_MT="${YEAR}-${FMONTH}-${LDAY_PAD} 23:59:59.999999999"

echo
echo "starting archive (${YEAR}-${FMONTH}-01 00:00:00 .. ${END_MT})"
mkdir -pv "${WORK}/${NAME}/articles"
# Articles (files) dated in the month, not whole directories: a directory's
# mtime changes whenever an article is added, so selecting directories copied
# every article in them, of any month. --ignore-missing-args and rsync exit
# 24: an article expired between find and rsync is simply not there.
rc=0
(cd "${SPOOL}" && find . -type f -newermt "${START_MT}" -not -newermt "${END_MT}" -print0) |
	rsync -a --from0 --ignore-missing-args --files-from=- "${SPOOL}/" "${WORK}/${NAME}/articles/" || rc=$?
if (( rc != 0 && rc != 24 )); then
	echo "copying the articles failed (exit ${rc})" >&2
	exit "${rc}"
fi

### Zip Archive
echo
echo "Zipping archive"
mkdir -pv "${DEST}"
zip -r "${DEST}/${NAME}.zip" "${WORK}/${NAME}"

### Report Size
echo "Updating archivelog.txt"
date >>"${DEST}/archivelog.txt"
du -sh "${WORK}/${NAME}" >>"${DEST}/archivelog.txt"
du -sh "${DEST}/${NAME}.zip" >>"${DEST}/archivelog.txt"
echo "Total number of files =" >>"${DEST}/archivelog.txt"
find "${WORK}/${NAME}" -type f | wc -l >>"${DEST}/archivelog.txt"

### Cleanup
echo "Cleaning Up"
rm -rf "${WORK:?}/${NAME}"
