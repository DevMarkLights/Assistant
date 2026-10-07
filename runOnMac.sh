cd frontend
npm run build

cp -r dist ../backend

cd ../backend
fastapi dev main.py