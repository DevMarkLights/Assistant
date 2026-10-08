cd frontend
npm ci # to use what is in the package.json
npm run build

cp -r dist ../backend

cd ../backend
source .venv/bin/activate
pip install -r requirements

uvicorn main:app --port 8091