import pandas as pd
import random
from datetime import datetime

from sqlalchemy.orm import Session
from app.db.session import SessionLocal
from app.models.visit import Visit
from app.models.user import User


def parse_date(date_str):
    try:
        return datetime.strptime(date_str, "%m/%d/%Y")
    except:
        return None


def main():
    db: Session = SessionLocal()

    # 👉 load users từ DB
    users = db.query(User.id).all()
    user_ids = [u[0] for u in users]

    if not user_ids:
        print("❌ No users in DB")
        return

    # 👉 load CSV
    df = pd.read_csv("./my_visits.csv")
    df = df.where(pd.notnull(df), None)

    visits = []

    for _, row in df.iterrows():
        visits.append({
            "user_id": random.choice(user_ids),  # 🔥 random user
            "airport_id": row.get("airport_id"),
            "date_visited": parse_date(row.get("date_visited")),
            "callsign": row.get("callsign"),
            "notes": row.get("notes"),
        })

    # 👉 insert nhanh
    db.bulk_insert_mappings(Visit, visits)

    db.commit()
    db.close()

    print(f"✅ Imported {len(visits)} visits")


if __name__ == "__main__":
    main()