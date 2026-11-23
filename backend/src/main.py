from contextlib import asynccontextmanager
import logging
import os

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy import inspect, text
import uvicorn

from scripts.seed_demo import seed
from src.config.settings import engine
from src.middlewares.request import limit_body, request_headers
from src.models.tables import schema
from src.routes.workflow import router


def upgrade(engine):
    """Add columns introduced after the demo schema was first created.

    The initial snapshot only creates tables; existing demo volumes therefore
    need the new evidence column added in place.
    """

    inspector = inspect(engine)
    columns = {column['name'] for column in inspector.get_columns('business_evidence')}
    additions = {
        'correction_batch': 'INTEGER',
    }
    with engine.begin() as conn:
        for name, kind in additions.items():
            if name not in columns:
                conn.execute(text(f'ALTER TABLE business_evidence ADD COLUMN {name} {kind}'))


@asynccontextmanager
async def lifespan(app):
    schema.create_all(engine)
    upgrade(engine)
    if os.getenv('SEED_DEMO', 'true').lower() == 'true':
        try:
            seed()
        except Exception:
            logging.exception('Demo seeding failed')
    yield


app = FastAPI(title='Database business workflow', lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def validation_error(_: Request, exc: RequestValidationError):
    # Surface a readable reason instead of the framework's nested list, so a
    # rejected correction order explains why it was returned in full.
    problems = []
    for error in exc.errors():
        location = '.'.join(str(part) for part in error.get('loc', ()) if part != 'body')
        problems.append(f"{location or '输入'}：{error.get('msg', '格式不正确')}")
    return JSONResponse({'detail': '；'.join(problems) or '请求格式不正确'}, status_code=422)


app.middleware('http')(limit_body)
app.middleware('http')(request_headers)
app.include_router(router)

if __name__ == '__main__':
    uvicorn.run(app, host='0.0.0.0', port=8000)
