# Employee Attendance & Analytics API

A FastAPI and MongoDB service implementing employee attendance tracking, punch regularization with an audit trail, and department analytics aggregation pipelines conforming to OpenAPI 3.0 specifications.

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Local Setup & Run

1. **Install Dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure Environment Variables**:
   Provide `MONGO_URI` and `MONGO_DB` via environment variables or a local `.env` file:
   ```bash
   MONGO_URI="mongodb://localhost:27017"
   MONGO_DB="attendance_db"
   ```

3. **Start the API Server**:
   ```bash
   uvicorn app.main:app --port 8000
   ```

4. **Interactive Documentation**:
   Once running, access Swagger UI at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs).

## Project Structure
- `app/main.py`: Single-file FastAPI application containing all routes, business logic (R1–R10), and aggregation pipelines.
- `requirements.txt`: Python package dependencies.
- `REVIEW.md`: Detailed defect log and resolution notes from the starter kit review.
- `DECISIONS.md`: Architectural decisions and design trade-offs.
