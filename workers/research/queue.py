"""Queue registration without importing an engine or executing strategy code."""

import procrastinate

from backend.app.core.config import Settings, get_settings

TASK_NAME = "tradegrad.execute_run"


def create_queue_app(settings: Settings | None = None, database_url: str | None = None):
    if database_url is None:
        settings = settings or get_settings()
        database_url = settings.psycopg_url
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=database_url)
    )

    @app.task(
        name=TASK_NAME,
        queue="research",
        retry=procrastinate.RetryStrategy(max_attempts=3, wait=5),
    )
    def execute_run(run_id: str, generation: int):
        from workers.research.executor import execute

        execute(run_id, generation, settings=settings)

    return app
