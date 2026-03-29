alembic revision --autogenerate -m "update something"
alembic upgrade head
python -m scripts.airport-seed