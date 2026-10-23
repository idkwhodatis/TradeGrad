FROM python:3.12.14-slim-bookworm
ARG TRADEGRAD_ENVIRONMENT_DIGEST=unversioned
LABEL io.tradegrad.environment_digest=$TRADEGRAD_ENVIRONMENT_DIGEST
LABEL io.tradegrad.portable_role=engine
WORKDIR /opt/tradegrad
COPY runtimes/evaluator/requirements.lock /tmp/evaluator.lock
COPY runtimes/portable_engine/calendar-requirements.lock /tmp/calendar.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/evaluator.lock -r /tmp/calendar.lock && rm /tmp/evaluator.lock /tmp/calendar.lock
COPY packages/strategy_sdk ./packages/strategy_sdk
COPY packages/tradegrad_adapter/portable_results.py ./packages/tradegrad_adapter/portable_results.py
COPY runtimes/portable_policy/protocol.py ./runtimes/portable_policy/protocol.py
COPY runtimes/portable_engine ./runtimes/portable_engine
ENV TRADEGRAD_PORTABLE_ENGINE_CONTAINER=1 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tradegrad
USER 65532:65532
ENTRYPOINT ["python", "-m", "runtimes.portable_engine.entrypoint"]
