# One-shot init container that seeds the `corpus` named volume with the
# Hansard demo documents produced by scripts/prep_hansard.py.
#
# Runs before the `simon` service via docker-compose depends_on with
# condition=service_completed_successfully, copies the per-tier
# directories into the shared volume, sets read permissions, and exits.

FROM busybox:1.36

COPY seed.sh /seed.sh
RUN chmod +x /seed.sh

ENTRYPOINT ["/seed.sh"]
