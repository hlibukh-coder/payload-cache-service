"""Alembic migration environment and revision scripts, shipped inside the package.

Living here rather than next to the repository's alembic.ini means the installed
application, the Docker image included, can migrate its own database without a checkout.
"""
