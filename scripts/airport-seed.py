import pandas as pd
from sqlalchemy.orm import Session
from sqlalchemy.dialects.postgresql import insert
from app.models.airport import Airport
from app.db.session import SessionLocal

df = pd.read_csv("./airports.csv")

# 👉 replace NaN → None
df = df.where(pd.notnull(df), None)

db: Session = SessionLocal()

airports = []

for _, row in df.iterrows():
    airport_id = row.get("airport_id")

    # 🚨 skip dữ liệu lỗi
    if not airport_id or airport_id in ["0", 0]:
        continue

    airports.append({
        "airport_id": str(airport_id),  # 👈 đảm bảo string
        "name": row.get("name"),
        "city": row.get("CITY"),
        "state": row.get("state"),
        "latitude": row.get("latitude"),
        "longitude": row.get("longitude"),
        "elevation": row.get("ELEV"),
        "towered_status": row.get("towered_status"),
    })

# 🚀 INSERT + bỏ qua duplicate
stmt = insert(Airport).values(airports)

stmt = stmt.on_conflict_do_nothing(
    index_elements=["airport_id"]  # 👈 PK hoặc unique key
)

db.execute(stmt)
db.commit()
db.close()