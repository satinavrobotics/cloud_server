"""
Unit tests for FastAPI error handlers.

Tests the error handlers implemented in packages/utils/fastapi_helpers.py
to ensure they return consistent error responses for different exception types.
"""

import re
import pytest
import pydantic
import requests
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field, ValidationError
from packages.utils.fastapi_helpers import add_error_handlers


class SampleRequest(BaseModel):
    """Sample request model for validation testing."""
    name: str = Field(..., min_length=1, max_length=50)
    age: int = Field(..., ge=0, le=150)
    email: str

    @pydantic.validator('email')
    @classmethod
    def validate_email_pattern(cls, v):
        if not re.match(r'^[\w\.-]+@[\w\.-]+\.\w+$', v):
            raise ValueError('invalid email format')
        return v


@pytest.fixture
def test_app():
    """Create a test FastAPI app with error handlers."""
    app = FastAPI()
    add_error_handlers(app)
    
    @app.get("/test/success")
    async def success_endpoint():
        return {"message": "success"}
    
    @app.get("/test/http_exception")
    async def http_exception_endpoint():
        raise HTTPException(status_code=404, detail="Resource not found")
    
    @app.get("/test/generic_exception")
    async def generic_exception_endpoint():
        raise Exception("Something went wrong")
    
    @app.get("/test/service_error")
    async def service_error_endpoint():
        # Simulate a service communication error
        raise requests.RequestException("Service unavailable")
    
    @app.post("/test/validation")
    async def validation_endpoint(data: SampleRequest):
        return {"message": "valid", "data": data.dict()}
    
    return app


@pytest.mark.unit
class TestErrorHandlers:
    """Test error handler functionality."""
    
    def test_success_response(self, test_app):
        """Test that successful requests work normally."""
        client = TestClient(test_app)
        response = client.get("/test/success")
        
        assert response.status_code == 200
        assert response.json() == {"message": "success"}
    
    def test_http_exception_handler(self, test_app):
        """Test HTTPException handler returns proper error format."""
        client = TestClient(test_app)
        response = client.get("/test/http_exception")

        assert response.status_code == 404
        data = response.json()
        assert "error" in data
        assert "detail" in data
        assert "path" in data
        assert "status_code" in data
        assert data["error"] == "Resource not found"  # HTTPException uses detail as error
        assert data["detail"] == "Resource not found"
        assert data["path"] == "/test/http_exception"
        assert data["status_code"] == 404

    def test_generic_exception_handler(self, test_app):
        """Test generic Exception handler returns 500 error."""
        client = TestClient(test_app, raise_server_exceptions=False)
        response = client.get("/test/generic_exception")

        assert response.status_code == 500
        data = response.json()
        assert "error" in data
        assert "detail" in data
        assert "path" in data
        assert data["error"] == "Internal Server Error"
        assert data["detail"] == "An unexpected error occurred"
        assert data["path"] == "/test/generic_exception"

    def test_service_communication_error_handler(self, test_app):
        """Test requests.RequestException handler returns 503 error."""
        client = TestClient(test_app)
        response = client.get("/test/service_error")

        assert response.status_code == 503
        data = response.json()
        assert "error" in data
        assert "detail" in data
        assert "path" in data
        assert data["error"] == "Service Communication Error"
        assert data["detail"] == "Upstream service unavailable"
        assert data["path"] == "/test/service_error"

    def test_validation_error_handler_missing_field(self, test_app):
        """Test RequestValidationError handler for missing required field."""
        client = TestClient(test_app)
        response = client.post("/test/validation", json={"age": 25})

        assert response.status_code == 422
        data = response.json()
        assert "error" in data
        assert "detail" in data
        assert "path" in data
        assert "errors" in data  # Field is "errors" not "validation_errors"
        assert data["error"] == "Validation Error"
        assert data["detail"] == "Request validation failed"
        assert data["path"] == "/test/validation"
        assert len(data["errors"]) > 0

    def test_validation_error_handler_invalid_type(self, test_app):
        """Test RequestValidationError handler for invalid field type."""
        client = TestClient(test_app)
        response = client.post("/test/validation", json={
            "name": "John",
            "age": "not_a_number",
            "email": "john@example.com"
        })

        assert response.status_code == 422
        data = response.json()
        assert data["error"] == "Validation Error"
        assert "errors" in data  # Field is "errors" not "validation_errors"
        # Should have error about age not being an integer
        errors = data["errors"]
        assert any("age" in str(err.get("loc", [])) for err in errors)

    def test_validation_error_handler_invalid_pattern(self, test_app):
        """Test RequestValidationError handler for pattern mismatch."""
        client = TestClient(test_app)
        response = client.post("/test/validation", json={
            "name": "John",
            "age": 25,
            "email": "invalid_email"
        })

        assert response.status_code == 422
        data = response.json()
        assert data["error"] == "Validation Error"
        assert "errors" in data  # Field is "errors" not "validation_errors"
        # Should have error about email pattern
        errors = data["errors"]
        assert any("email" in str(err.get("loc", [])) for err in errors)

