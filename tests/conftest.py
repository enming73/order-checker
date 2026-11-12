import pytest
from asgi_client import ASGIClient
from app.config import Settings
from app.main import create_app

@pytest.fixture
def application(tmp_path):
    return create_app(Settings(data_dir=tmp_path, erp_timeout=0.05))

@pytest.fixture
def client(application):
    return ASGIClient(application)
