#!/usr/bin/env python3
"""
Shared Service Utilities

Common utilities for microservices including health checks, stats, and logging configuration.
"""

import logging
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Dict, Any, Optional, List
from pydantic import BaseModel, Field


# ==================== Response Models ====================

class HealthResponse(BaseModel):
    """Standard health check response model for all services."""
    status: str = Field(..., description="Health status: 'healthy', 'degraded', or 'unhealthy'")
    service: str = Field(..., description="Service name")
    dependencies: Optional[Dict[str, bool]] = Field(None, description="Status of dependencies")
    details: Optional[Dict[str, Any]] = Field(None, description="Additional health details")


class StatsResponse(BaseModel):
    """Standard stats response model for all services."""
    service: str = Field(..., description="Service name")
    stats: Dict[str, Any] = Field(..., description="Service statistics")


# ==================== Health Check Utilities ====================

class DependencyHealthChecker:
    """
    Enhanced utility class for checking health of service dependencies.

    Features:
    - Timeout support for health checks
    - Retry logic with configurable attempts
    - Caching of health check results
    - Detailed error tracking

    Example usage:
        checker = DependencyHealthChecker(timeout=5.0, max_retries=2)
        checker.add_dependency("database", lambda: db_client.health_check())
        checker.add_dependency("cache", lambda: cache.ping(), critical=True)
        health_status = checker.check_all()
    """

    def __init__(
        self,
        timeout: float = 5.0,
        max_retries: int = 1,
        cache_ttl: float = 10.0
    ):
        """
        Initialize the health checker.

        Args:
            timeout: Timeout for each health check in seconds
            max_retries: Number of retry attempts for failed checks
            cache_ttl: Time-to-live for cached results in seconds
        """
        self.dependencies: Dict[str, Dict[str, Any]] = {}
        self.logger = logging.getLogger("DependencyHealthChecker")
        self.timeout = timeout
        self.max_retries = max_retries
        self.cache_ttl = cache_ttl
        self._cache: Dict[str, Dict[str, Any]] = {}

    def add_dependency(
        self,
        name: str,
        check_func: callable,
        critical: bool = False,
        timeout: Optional[float] = None
    ):
        """
        Add a dependency to check.

        Args:
            name: Name of the dependency
            check_func: Function that returns True if healthy, False otherwise
            critical: Whether this dependency is critical for service operation
            timeout: Optional custom timeout for this dependency
        """
        self.dependencies[name] = {
            "check_func": check_func,
            "critical": critical,
            "timeout": timeout or self.timeout
        }

    def _check_single(self, name: str, config: Dict[str, Any]) -> bool:
        """
        Check a single dependency with retry logic.

        Args:
            name: Name of the dependency
            config: Dependency configuration

        Returns:
            True if healthy, False otherwise
        """
        check_func = config["check_func"]
        timeout = config["timeout"]

        with ThreadPoolExecutor(max_workers=1) as executor:
            for attempt in range(self.max_retries + 1):
                try:
                    future = executor.submit(check_func)
                    result = future.result(timeout=timeout)
                    return bool(result)
                except FuturesTimeoutError:
                    self.logger.warning(f"Health check timeout for {name} (attempt {attempt + 1}/{self.max_retries + 1})")
                    if attempt == self.max_retries:
                        return False
                except Exception as e:
                    self.logger.warning(f"Health check failed for {name} (attempt {attempt + 1}/{self.max_retries + 1}): {e}")
                    if attempt == self.max_retries:
                        return False

        return False

    def check_all(self, use_cache: bool = False) -> Dict[str, bool]:
        """
        Check all registered dependencies.

        Args:
            use_cache: Whether to use cached results if available

        Returns:
            Dictionary mapping dependency names to their health status
        """

        results = {}
        current_time = time.time()

        for name, config in self.dependencies.items():
            # Check cache if enabled
            if use_cache and name in self._cache:
                cached = self._cache[name]
                if current_time - cached["timestamp"] < self.cache_ttl:
                    results[name] = cached["healthy"]
                    continue

            # Perform health check
            healthy = self._check_single(name, config)
            results[name] = healthy

            # Update cache
            self._cache[name] = {
                "healthy": healthy,
                "timestamp": current_time
            }

        return results

    def is_all_healthy(self, use_cache: bool = False) -> bool:
        """
        Check if all dependencies are healthy.

        Args:
            use_cache: Whether to use cached results

        Returns:
            True if all dependencies are healthy, False otherwise
        """
        results = self.check_all(use_cache=use_cache)
        return all(results.values())

    def is_critical_healthy(self, use_cache: bool = False) -> bool:
        """
        Check if all critical dependencies are healthy.

        Args:
            use_cache: Whether to use cached results

        Returns:
            True if all critical dependencies are healthy, False otherwise
        """
        results = self.check_all(use_cache=use_cache)
        critical_deps = [name for name, config in self.dependencies.items() if config["critical"]]
        return all(results.get(name, False) for name in critical_deps)

    def get_status(self, use_cache: bool = False) -> str:
        """
        Get overall health status.

        Args:
            use_cache: Whether to use cached results

        Returns:
            'healthy' if all dependencies are healthy,
            'degraded' if some are unhealthy but all critical ones are healthy,
            'unhealthy' if critical dependencies are unhealthy
        """
        results = self.check_all(use_cache=use_cache)

        # Critical dependency check takes precedence over aggregate counts.
        critical_deps = [name for name, config in self.dependencies.items() if config["critical"]]
        if critical_deps and not all(results.get(name, False) for name in critical_deps):
            return "unhealthy"

        return _derive_status(results)

    def clear_cache(self):
        """Clear the health check cache."""
        self._cache.clear()


def _derive_status(results: Dict[str, bool]) -> str:
    """Derive healthy/degraded/unhealthy from a flat {name: bool} results dict."""
    if not results:
        return "healthy"
    healthy_count = sum(1 for v in results.values() if v)
    total_count = len(results)
    if healthy_count == total_count:
        return "healthy"
    elif healthy_count > 0:
        return "degraded"
    else:
        return "unhealthy"


def create_health_response(
    service_name: str,
    dependencies: Optional[Dict[str, bool]] = None,
    details: Optional[Dict[str, Any]] = None
) -> HealthResponse:
    """
    Create a standardized health response.

    Args:
        service_name: Name of the service
        dependencies: Dictionary of dependency health statuses
        details: Additional health details

    Returns:
        HealthResponse object
    """
    return HealthResponse(
        status=_derive_status(dependencies or {}),
        service=service_name,
        dependencies=dependencies,
        details=details,
    )


# ==================== Stats Utilities ====================

def create_stats_response(service_name: str, stats: Dict[str, Any]) -> StatsResponse:
    """
    Create a standardized stats response.

    Args:
        service_name: Name of the service
        stats: Dictionary of service statistics

    Returns:
        StatsResponse object
    """
    return StatsResponse(
        service=service_name,
        stats=stats
    )


# ==================== Root Endpoint Utilities ====================

def create_root_response(
    service_name: str,
    version: str,
    description: str,
    endpoints: Dict[str, Any]
) -> Dict[str, Any]:
    """
    Create a standardized root endpoint response.

    Args:
        service_name: Name of the service
        version: Service version (e.g., "1.0.0")
        description: Service description
        endpoints: Dictionary of endpoint names to paths/methods

    Returns:
        Dictionary with service information

    Example:
        return create_root_response(
            service_name="Graph Database Service",
            version="1.0.0",
            description="Unified graph database with ArangoDB + R-tree spatial index",
            endpoints={
                "health": "GET /health",
                "stats": "GET /stats",
                "nodes": "POST /nodes",
                "edges": "POST /edges"
            }
        )
    """
    return {
        "service": service_name,
        "version": version,
        "description": description,
        "endpoints": endpoints
    }


# ==================== Logging Utilities ====================

def configure_service_logging(
    service_name: str,
    log_level: str = "INFO",
    log_format: Optional[str] = None
) -> logging.Logger:
    """
    Configure logging for a service with standardized format.
    
    Args:
        service_name: Name of the service
        log_level: Logging level (DEBUG, INFO, WARNING, ERROR)
        log_format: Custom log format (uses default if not provided)
    
    Returns:
        Configured logger instance
    """
    # Import config here to avoid circular imports
    try:
        from packages.config import LOG_FORMAT
        default_format = LOG_FORMAT
    except ImportError:
        default_format = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
    
    format_str = log_format or default_format
    
    logging.basicConfig(
        level=getattr(logging, log_level.upper()),
        format=format_str
    )
    
    logger = logging.getLogger(service_name)
    logger.setLevel(getattr(logging, log_level.upper()))
    
    return logger


# ==================== Service Configuration Utilities ====================



