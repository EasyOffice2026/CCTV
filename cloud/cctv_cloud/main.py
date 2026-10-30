from fastapi import FastAPI

from cctv_cloud import __version__
from cctv_cloud.api import health


def create_app() -> FastAPI:
    app = FastAPI(title="AI CCTV Cloud API", version=__version__)
    app.include_router(health.router)
    return app


app = create_app()
