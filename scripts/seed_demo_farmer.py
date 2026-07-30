"""Seed the demo caller's farmer envelope into the voice backend's Redis cache.

Run inside the voice container:  docker exec amul_voice_app python3 /app/seed_demo_farmer.py
Key: sva-cache-farmer:<sha256(PHONE)>   TTL 7d, self-expiring.
Synthetic data on purpose — no real farmer's records are read aloud at a demo table.
"""
import asyncio, hashlib
from app.core.cache import cache, build_cache_key

PHONE = "8035454078"          # RAYA strips the country code
NS, TTL = "farmer", 60 * 60 * 24 * 7

envelope = {
    "farmers": [{
        "farmerName": "Bharatbhai Chaudhary",
        "societyName": "Thara Dudh Mandali",
        "farmerCode": "DEMO001",
        "unionCode": "BANAS",
        "societyCode": "DEMOSOC01",
        "totalAnimals": 3,
        "tagNumbers": "IN0099887766, IN0099887767, IN0099887768",
        "animals": [
            {"tagNumber": "IN0099887766", "animalType": "Cow", "breed": "HF Cross",
             "milkingStage": "Mid Lactation", "pregnancyStage": "Not Pregnant", "lactationNo": 3,
             "lastCalvingDate": "2026-02-14", "lastPD": "2026-06-20",
             "lastBreedingActivity": {"aiDate": "2026-06-02", "bullId": "HF-4471",
                                      "technician": "Rameshbhai Patel"},
             "lastHealthActivity": {"date": "2026-07-11", "complaint": "Udder swelling",
                                    "diagnosis": "Early mastitis",
                                    "medicineGiven": "Intramammary tube, 3 days"}},
            {"tagNumber": "IN0099887767", "animalType": "Buffalo", "breed": "Mehsani",
             "milkingStage": "Dry", "pregnancyStage": "Pregnant", "lactationNo": 2,
             "lastCalvingDate": "2025-11-30", "lastPD": "2026-07-05",
             "lastBreedingActivity": {"aiDate": "2026-03-18", "bullId": "MEH-2210",
                                      "technician": "Rameshbhai Patel"}},
            {"tagNumber": "IN0099887768", "animalType": "Cow", "breed": "Gir",
             "milkingStage": "Early Lactation", "pregnancyStage": "Not Pregnant",
             "lactationNo": 1, "lastCalvingDate": "2026-06-28"},
        ],
    }],
    # Shape matters: _build_ai_technician_summary (app/services/voice.py:909)
    # expects GROUPS keyed by farmer/society, each with a nested technicians[]
    # of {id, fullName, mobileNumber}. A flat list yields "technician details
    # are not available", and the agent then never calls create_ai_call.
    "aiTechnicians": [{
        "farmerName": "Bharatbhai Chaudhary",
        "societyName": "Thara Dudh Mandali",
        "societyCode": "DEMOSOC01",
        "unionCode": "BANAS",
        "technicians": [
            {"id": "T001", "fullName": "Rameshbhai Patel", "mobileNumber": "9876500011"},
            {"id": "T002", "fullName": "Dineshbhai Thakor", "mobileNumber": "9876500022"},
        ],
    }],
    "fetchedAt": "2026-07-30T14:00:00+00:00",
    "source": "api", "stale": False, "lookupStatus": "found",
}


async def main() -> None:
    key = hashlib.sha256(PHONE.encode()).hexdigest()
    await cache.set(key, envelope, ttl=TTL, namespace=NS)
    got = await cache.get(key, namespace=NS)
    print("redis key  :", build_cache_key(key, namespace=NS))
    print("write ok   :", bool(got))
    if got:
        f = got["farmers"][0]
        print("farmer     :", f["farmerName"], "|", f["societyName"], "|", f["farmerCode"])
        print("animals    :", len(f["animals"]), " technicians:", len(got["aiTechnicians"]))


asyncio.run(main())
