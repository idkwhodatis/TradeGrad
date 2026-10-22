# API contracts

`openapi.json` is generated from `backend.app.main.create_app` plus the typed
SSE `EventPublic` envelope. Generate from the repository root:

```sh
.venv-api/bin/python -m backend.app.export_openapi
```

No running database, server or runtime credentials are required for generation.
Frontend client/types should be generated with the locked @hey-api toolchain.
Do not hand-edit this schema. Public request/response source lives in
`backend/app/schemas.py`; SSE remains an authenticated fetch stream.
