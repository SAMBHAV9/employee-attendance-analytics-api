# Employee Attendance & Analytics API

FastAPI + PyMongo service for employee attendance, manual corrections, and MongoDB-powered analytics. Python 3.11+ and MongoDB 6.0+ are required. Indexes are created automatically on startup.

## Run on Windows PowerShell

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
docker run -d --name attendance-mongodb -p 27017:27017 mongo:7
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --reload --port 8000
```

If the container already exists, use `docker start attendance-mongodb` instead of `docker run`. Open `http://127.0.0.1:8000/docs`; `GET /health` checks MongoDB connectivity. Stop the API with Ctrl+C. The local demonstration database may contain employee `EMP9911`.
