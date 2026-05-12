#!/usr/bin/env python3
"""
FastAPI Helper Functions

Utilities for FastAPI applications including standardized error handling.
"""

import logging
from typing import Union
from fastapi import FastAPI, Request, status
from fastapi.exceptions import RequestValidationError, HTTPException
from fastapi.responses import JSONResponse
from pydantic import ValidationError
import requests


logger = logging.getLogger(__name__)


def add_error_handlers(app: FastAPI):
    """
    Add standardized error handlers to a FastAPI application.

    Handles:
    - RequestValidationError (422): Pydantic validation errors
    - HTTPException: Explicit HTTP exceptions
    - requests.RequestException: Service communication errors
    - Generic Exception (500): Unexpected errors

    Args:
        app: FastAPI application instance
    """

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(request: Request, exc: RequestValidationError):
        """
        Handle Pydantic validation errors with detailed error messages.

        Returns 422 Unprocessable Entity with validation error details.
        """
        errors = exc.errors()
        error_details = []

        for error in errors:
            error_details.append({
                "loc": error.get("loc", []),
                "msg": error.get("msg", ""),
                "type": error.get("type", "")
            })

        logger.warning(
            f"Validation error on {request.method} {request.url.path}: {error_details}"
        )

        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            content={
                "error": "Validation Error",
                "detail": "Request validation failed",
                "errors": error_details,
                "path": str(request.url.path)
            }
        )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException):
        """
        Handle explicit HTTP exceptions with consistent format.

        Preserves the status code and detail from the HTTPException.
        """
        logger.warning(
            f"HTTP {exc.status_code} on {request.method} {request.url.path}: {exc.detail}"
        )

        return JSONResponse(
            status_code=exc.status_code,
            content={
                "error": exc.detail if isinstance(exc.detail, str) else "HTTP Error",
                "detail": exc.detail,
                "status_code": exc.status_code,
                "path": str(request.url.path)
            }
        )

    @app.exception_handler(requests.RequestException)
    async def service_communication_exception_handler(
        request: Request,
        exc: requests.RequestException
    ):
        """
        Handle service-to-service communication errors.

        Returns 503 Service Unavailable for communication failures.
        """
        logger.error(
            f"Service communication error on {request.method} {request.url.path}: {exc}"
        )

        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "Service Communication Error",
                "detail": "Upstream service unavailable",
                "path": str(request.url.path)
            }
        )

    @app.exception_handler(Exception)
    async def generic_exception_handler(request: Request, exc: Exception):
        """
        Handle unexpected errors with generic 500 response.

        Logs the full exception and returns a safe error message.
        """
        logger.error(
            f"Unexpected error on {request.method} {request.url.path}: {exc}",
            exc_info=True
        )

        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "Internal Server Error",
                "detail": "An unexpected error occurred",
                "path": str(request.url.path)
            }
        )


