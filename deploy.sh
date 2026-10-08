cd frontend
npm run build

cp -r dist ../backend

cd ../backend
source .venv/bin/activate
pip install -r requirements

uvicorn main:app --port 8091
fastapi dev main.py