import pytest
from fastapi.testclient import TestClient

from app.config import get_settings
from app.main import app

API_KEY = get_settings().api_key
HEADERS = {"X-API-Key": API_KEY}


@pytest.fixture(scope="module")
def client():
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_missing_api_key_is_401(client, ellis_p21_bytes):
    r = client.post("/api/v1/door-schedule/extract",
                    files={"file": ("p21.pdf", ellis_p21_bytes, "application/pdf")})
    assert r.status_code == 401
    assert r.json()["detail"] == "Invalid API key."


def test_extract_returns_the_documented_contract(client, ellis_p21_bytes):
    r = client.post("/api/v1/door-schedule/extract", headers=HEADERS,
                    files={"file": ("p21.pdf", ellis_p21_bytes, "application/pdf")},
                    params={"allow_ai": False})
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["method"] == "deterministic_ruled"
    assert body["row_count"] == 23
    assert body["source_pages"] == [1]
    assert body["rows"][0]["from_space"] == "RECEPTION"
    assert set(body["rows"][0]) >= {"door_tag", "hw_set", "comments", "extra"}


def test_non_pdf_is_400_not_500(client):
    r = client.post("/api/v1/door-schedule/extract", headers=HEADERS,
                    files={"file": ("notes.txt", b"hello", "text/plain")})
    assert r.status_code == 400
    assert r.json()["detail"] == "File is not a readable PDF."


def test_pdf_without_schedule_is_422_with_a_real_message(client, no_schedule_pdf):
    r = client.post("/api/v1/door-schedule/extract", headers=HEADERS,
                    files={"file": ("blank.pdf", no_schedule_pdf, "application/pdf")},
                    params={"allow_ai": False})
    assert r.status_code == 422
    assert "No door schedule found" in r.json()["detail"]


def test_oversize_upload_is_413(client, monkeypatch):
    settings = get_settings()
    monkeypatch.setattr(settings, "max_upload_mb", 0.001, raising=False)
    r = client.post("/api/v1/door-schedule/extract", headers=HEADERS,
                    files={"file": ("big.pdf", b"x" * 200_000, "application/pdf")})
    assert r.status_code == 413
    assert "too large" in r.json()["detail"]


def test_debug_flag_returns_per_page_scores(client, ellis_p21_bytes):
    r = client.post("/api/v1/door-schedule/extract", headers=HEADERS,
                    files={"file": ("p21.pdf", ellis_p21_bytes, "application/pdf")},
                    params={"debug": True, "allow_ai": False})
    assert r.status_code == 200
    scores = r.json()["page_scores"]
    assert len(scores) == 1
    assert scores[0]["passed"] is True


def test_inspect_endpoint_reports_candidates(client, ellis_p21_bytes):
    r = client.post("/api/v1/door-schedule/inspect", headers=HEADERS,
                    files={"file": ("p21.pdf", ellis_p21_bytes, "application/pdf")})
    assert r.status_code == 200
    assert r.json()["passing_pages"] == [1]


def test_openapi_schema_renders(client):
    r = client.get("/openapi.json")
    assert r.status_code == 200
    paths = r.json()["paths"]
    assert "/api/v1/door-schedule/extract" in paths
    assert "/health" in paths
