# pcbassist on top of the official KiCad 8 image, so `kicad-cli pcb drc` is always available
# and DRC results are reproducible.
#   docker build -t pcbassist .
#   docker run --rm -v "$PWD/out:/work/out" pcbassist generate -f /opt/pcbassist/examples/iot_env_sensor/prompt.txt --out out/iot --mock-llm
#   docker run --rm -p 8000:8000 -v "$PWD/sessions:/work/sessions" pcbassist serve --host 0.0.0.0
FROM ghcr.io/astral-sh/uv:0.5.11 AS uv

FROM kicad/kicad:8.0.9
USER root
COPY --from=uv /uv /usr/local/bin/uv
ENV UV_PYTHON=/usr/bin/python3 \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/pcbassist/.venv
WORKDIR /opt/pcbassist
COPY pyproject.toml uv.lock README.md ./
COPY src ./src
COPY data ./data
COPY fixtures ./fixtures
COPY examples ./examples
RUN uv sync --frozen --no-dev && /opt/pcbassist/.venv/bin/pcbassist version
ENV PATH=/opt/pcbassist/.venv/bin:$PATH \
    PCBASSIST_DATA_DIR=/opt/pcbassist/data \
    PCBASSIST_FIXTURES_DIR=/opt/pcbassist/fixtures \
    PCBASSIST_KICAD_CLI=/usr/bin/kicad-cli
RUN mkdir -p /work && chown kicad:kicad /work
USER kicad
WORKDIR /work
EXPOSE 8000
ENTRYPOINT ["pcbassist"]
CMD ["--help"]
