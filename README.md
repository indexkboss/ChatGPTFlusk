# FlaskGPT
1. `poetry install`
2. Copier `.env.example` en `.env` et y mettre `GROQ_API_KEY`.
3. CSS : `tailwindcss -i ./static/css/input.css -o ./static/dist/css/style.css --watch`
   (un `style.css` déjà compilé est fourni si la compilation a réussi).
4. `poetry run python app.py` puis ouvrir http://127.0.0.1:5000/
