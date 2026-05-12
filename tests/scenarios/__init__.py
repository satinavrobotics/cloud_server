"""
Scenario-based testing framework for cloud_server.

This module provides a comprehensive framework for testing real-world use cases
and workflows across the entire system. Scenarios test complete user journeys
and business logic flows.

Scenarios differ from other test types:
- Unit tests: Test individual functions in isolation
- Integration tests: Test services with real dependencies
- E2E tests: Test full system with all services
- Scenario tests: Test specific business workflows and use cases

Scenario tests focus on:
1. Complete user workflows (e.g., "load map -> navigate -> explore")
2. Error handling and recovery
3. State transitions and consistency
4. Multi-service interactions
5. Real-world edge cases
"""

