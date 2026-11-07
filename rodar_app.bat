@echo off
REM Abre a interface da demo (Streamlit) no navegador. Clique duplo neste arquivo.
cd /d "%~dp0"
".\venv\Scripts\python.exe" -m streamlit run app.py
pause
