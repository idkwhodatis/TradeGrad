import procrastinate

from backend.app.core.config import get_settings

TASK_NAME = "tradegrad.import_data"


def create_data_queue(settings=None, database_url=None):
    if database_url is None:
        settings = settings or get_settings()
        database_url = settings.psycopg_url
    app = procrastinate.App(
        connector=procrastinate.PsycopgConnector(conninfo=database_url)
    )

    @app.task(
        name=TASK_NAME,
        queue="data",
        retry=procrastinate.RetryStrategy(max_attempts=3, wait=5),
    )
    def import_data(import_id: str, generation: int):
        from workers.data.executor import execute

        execute(import_id, generation, settings=settings)

    return app
