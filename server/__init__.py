"""Nodal API server (§11): a thin translation of the engine API.

Read models and commands only — no business logic lives here, and the engine
never imports this package (CI proves the engine suite passes without it).
"""
