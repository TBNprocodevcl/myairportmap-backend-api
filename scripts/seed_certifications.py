# seed_certifications.py

from app.db.session import SessionLocal
from app.models.certification import Certification

db = SessionLocal()

CERTIFICATIONS = [
    # ================= ATP =================
    ("ATP_ASEL", "ASEL", "ATP"),
    ("ATP_AMEL", "AMEL", "ATP"),
    ("ATP_ASES", "ASES", "ATP"),
    ("ATP_AMES", "AMES", "ATP"),
    ("ATP_HELI", "Helicopter", "ATP"),

    # ================= CPL =================
    ("CPL_ASEL", "ASEL", "CPL"),
    ("CPL_AMEL", "AMEL", "CPL"),
    ("CPL_ASES", "ASES", "CPL"),
    ("CPL_AMES", "AMES", "CPL"),
    ("CPL_HELI", "Helicopter", "CPL"),

    # ================= PPL =================
    ("PPL_ASEL", "ASEL", "PPL"),
    ("PPL_AMEL", "AMEL", "PPL"),
    ("PPL_ASES", "ASES", "PPL"),
    ("PPL_AMES", "AMES", "PPL"),
    ("PPL_HELI", "Helicopter", "PPL"),

    # ================= CFI =================
    ("CFI", "CFI", "CFI"),
    ("CFI_I", "CFI-I", "CFI"),
    ("MEI", "MEI", "CFI"),
    ("CFI_HELI", "Helicopter", "CFI"),

    # ================= OTHER =================
    ("INSTRUMENT", "Instrument Rated", "Other"),
    ("FLIGHT_ATTENDANT", "Flight Attendant", "Other"),
    ("DISPATCHER", "Aircraft Dispatcher", "Other"),
    ("STUDENT", "Student Pilot", "Other"),
    ("UAS", "UAS", "Other"),
    ("DPE", "Designated Pilot Examiner (DPE)", "Other"),
    ("AP_MECHANIC", "A&P Mechanic", "Other"),
    ("ATC", "Air Traffic Controller", "Other"),
    ("FLIGHT_ENGINEER", "Flight Engineer", "Other"),
]


def seed():
    inserted = 0

    for code, name, group in CERTIFICATIONS:
        existing = db.query(Certification).filter_by(code=code).first()

        if existing:
            continue  # tránh duplicate

        cert = Certification(
            code=code,
            name=name,
            group=group
        )

        db.add(cert)
        inserted += 1

    db.commit()

    print(f"✅ Inserted {inserted} certifications")


if __name__ == "__main__":
    seed()