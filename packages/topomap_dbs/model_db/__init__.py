#!/usr/bin/env python3
"""
Model Database Service Package

Standalone ML model storage (ONNX and other formats) backed by MinIO.
"""

from .server import ModelDatabaseService

__all__ = [
    'ModelDatabaseService',
]
