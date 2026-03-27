import pandas as pd
from sqlalchemy.orm import Session
from app.models.airport import Airport
from app.db.session import SessionLocal

df = pd.read_csv("./airports.csv")

# 👉 replace NaN → None
df = df.where(pd.notnull(df), None)

db: Session = SessionLocal()

airports = []

for _, row in df.iterrows():
    airports.append({
        "airport_id": row.get("airport_id"),
        "name": row.get("name"),
        "city": row.get("CITY"),
        "state": row.get("state"),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "elevation": row.get("ELEV"),
        "towered_status": row.get("towered_status"),
    })

# 🚀 FAST insert
db.bulk_insert_mappings(Airport, airports)

db.commit()
db.close()