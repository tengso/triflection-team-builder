ARG HERMES_IMAGE
FROM ${HERMES_IMAGE}
LABEL io.team-builder.internal-relay="1"
USER root
COPY . /opt/team-builder
RUN for wheel in /opt/team-builder/wheelhouse/pip-*.whl; do \
      PYTHONPATH="$wheel" /opt/hermes/.venv/bin/python -m pip install --no-index \
        --find-links=/opt/team-builder/wheelhouse /opt/team-builder; \
    done \
    && /opt/hermes/.venv/bin/python /opt/team-builder/src/team_builder/resources/patch_hermes.py
ENV PATH=/opt/hermes/.venv/bin:/usr/local/bin:/usr/bin:/bin
ENTRYPOINT ["/opt/hermes/.venv/bin/team-builder-manager"]
