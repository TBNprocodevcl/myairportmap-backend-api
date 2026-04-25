import csv
from app.db.session import SessionLocal
from app.models.runway import Runway
from app.models.airport import Airport

db = SessionLocal()

# lấy list airport có trong DB
valid_airports = set(
    r[0] for r in db.query(Airport.airport_id).all()
)

with open("runways.csv", newline="", encoding="utf-8") as f:
    reader = csv.DictReader(f)

    for row in reader:
        ident = row["airport_ident"]

        # 🔥 chỉ import nếu airport tồn tại
        if ident not in valid_airports:
            continue

        runway = Runway(
            airport_ident=ident,
            le_ident=row["le_ident"],
            he_ident=row["he_ident"],
        )

        db.add(runway)

db.commit()
db.close()

print("✅ Runways imported (filtered)")