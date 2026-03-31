from sqlalchemy.orm import Session
from app.db.session import SessionLocal

from app.models.airport import Airport
from app.models.runway import Runway


def seed_airports_from_db():
    db: Session = SessionLocal()

    # 🔥 1. lấy airport từ runway
    runway_airports = set(
        r[0] for r in db.query(Runway.airport_ident).distinct().all()
    )

    print("Runway airports:", len(runway_airports))

    # 🔥 2. airport đã có
    existing_airports = set(
        r[0] for r in db.query(Airport.airport_id).all()
    )

    print("Existing airports:", len(existing_airports))

    # 🔥 3. tìm cái thiếu
    missing = runway_airports - existing_airports

    print("Missing airports:", len(missing))

    added = 0

    for airport_id in missing:
        airport = Airport(
            airport_id=airport_id,
            name=f"Airport {airport_id}",
            city="Unknown",
            state="Unknown",
            latitude=None,
            longitude=None,
            elevation=None,
            towered_status="unknown"
        )

        db.add(airport)
        added += 1

        if added % 500 == 0:
            print(f"Inserted {added} airports...")

    db.commit()
    db.close()

    print("🎯 DONE")
    print("Added airports:", added)


if __name__ == "__main__":
    seed_airports_from_db()