#!/usr/bin/env python3
"""
Image Database Service Package

Image database with MinIO object storage for topological map images.
"""

from .server import ImageDatabaseService

__all__ = [
    'ImageDatabaseService',
]
