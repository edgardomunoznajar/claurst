#!/bin/sh
#
# Seed /data (the mounted `corpus` named volume) from /corpus-src (bind
# mount of the repo's demo/corpus/ tree). Runs once at stack start.
#
# Idempotent: we simply re-copy. cp -a preserves permissions and mtimes
# so the second run is a cheap no-op.

set -eu

SRC="/corpus-src"
DST="/data"

echo "corpus-init: seeding ${DST} from ${SRC}..."

if [ ! -d "${SRC}" ]; then
    echo "corpus-init: ERROR: ${SRC} does not exist. Has scripts/prep_hansard.py run?"
    exit 1
fi

# Copy every tier directory (and anything else sitting at the top of
# demo/corpus/) into the volume. The trailing /. is load-bearing — it
# copies the *contents* of SRC, not SRC itself, so /data/unofficial
# exists rather than /data/corpus/unofficial.
cp -a "${SRC}/." "${DST}/"

# Make sure simon (uid 1000) can read everything even if the source
# files on the host have restrictive modes.
chmod -R a+rX "${DST}"

echo "corpus-init: done. Document count by tier:"
for tier in unofficial official official-sensitive protected; do
    if [ -d "${DST}/${tier}" ]; then
        count=$(find "${DST}/${tier}" -name '*.md' 2>/dev/null | wc -l | tr -d ' ')
        echo "  ${tier}: ${count}"
    else
        echo "  ${tier}: (missing)"
    fi
done

echo "corpus-init: exiting cleanly."
