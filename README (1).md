# Employee Attendance & Analytics API

All code is in `app/main.py` (FastAPI + PyMongo).

```bash
pip install -r requirements.txt
export MONGO_URI="mongodb://localhost:27017" MONGO_DB="attendance_db"   # or use a local .env
uvicorn app.main:app --port 8000
```

- Needs MongoDB 6.0+ (uses `$setWindowFields`, `$dateAdd`, `$lookup` with `localField` + `pipeline`). Indexes are created at startup.
- Every endpoint in `openapi.yaml` is implemented, including `/admin/explain/{endpoint}`.
- Analytics are single aggregation pipelines. The department trend fills gaps in the database (`$range`/`$map`), not in Python.
- Known limits: the working-day count for the employee monthly summary is simple date arithmetic in Python; there is no holiday calendar (per R7).
