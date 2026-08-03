"""Operational scripts, importable so the test suite can exercise them.

Not shipped in the wheel (``pyproject.toml`` packages ``src/chaudron`` only).
The package marker exists for one reason: ``tests/tenancy`` provisions its
application role by calling ``provision_app_role`` rather than by re-typing its
``GRANT`` statements. A test that reimplements the procedure it is meant to
validate proves that the reimplementation works.
"""
