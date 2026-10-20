"""Moving data between servers (a robot's own server and the cloud) as a self-describing bundle.

Design: sati-client/docs/DATA_MIGRATION.md. Nothing in here moves by itself: an export or an import is
always started by a user, and an import plans first (plan.py), writes nothing until applied.
"""
