FROM python:3.12.14-slim-bookworm
ARG TRADEGRAD_ENVIRONMENT_DIGEST=unversioned
LABEL io.tradegrad.environment_digest=$TRADEGRAD_ENVIRONMENT_DIGEST
WORKDIR /opt/tradegrad
COPY runtimes/evaluator/requirements.lock /tmp/requirements.lock
RUN pip install --no-cache-dir --require-hashes -r /tmp/requirements.lock && rm /tmp/requirements.lock
COPY src/evaluate.py src/evaluate_time.py src/market.py src/data_cache.py src/resources.py ./src/
COPY src/__init__.py ./src/__init__.py
COPY packages/tradegrad_adapter/results.py ./packages/tradegrad_adapter/results.py
COPY packages/tradegrad_adapter/native_contract.py ./packages/tradegrad_adapter/native_contract.py
COPY runtimes/evaluator/entrypoint.py ./entrypoint.py
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/opt/tradegrad
USER 65532:65532
ENTRYPOINT ["python", "/opt/tradegrad/entrypoint.py"]
