FROM python:3.12.14-slim-bookworm
WORKDIR /opt/tradegrad
COPY runtimes/data/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock \
    && HOME=/tmp/openbb-build-home openbb-build \
    && rm /tmp/requirements.lock \
    && rm -rf /tmp/openbb-build-home
COPY backend ./backend
COPY workers ./workers
COPY packages ./packages
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tradegrad
USER 65532:65532
ENTRYPOINT ["python", "-m", "workers.data"]
CMD ["worker"]
