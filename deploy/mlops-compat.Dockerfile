ARG GEPPETTO_IMAGE=geppetto:dev
FROM ${GEPPETTO_IMAGE}

COPY tools/sync_real_ball.py ./tools/sync_real_ball.py
