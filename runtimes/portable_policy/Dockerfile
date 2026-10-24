FROM python:3.12.14-slim-bookworm
ARG TRADEGRAD_ENVIRONMENT_DIGEST=unversioned
LABEL io.tradegrad.environment_digest=$TRADEGRAD_ENVIRONMENT_DIGEST
LABEL io.tradegrad.portable_role=policy
WORKDIR /opt/tradegrad
# Locked SDK dependencies only. No engine, pandas, API, DB, provider or model SDK.
COPY runtimes/portable_policy/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock
COPY packages/strategy_sdk ./packages/strategy_sdk
COPY packages/tradegrad_adapter/runtime_observation.py ./packages/tradegrad_adapter/runtime_observation.py
COPY runtimes/portable_policy ./runtimes/portable_policy
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tradegrad
USER 65532:65532
ENTRYPOINT ["python", "-m", "runtimes.portable_policy.entrypoint"]
