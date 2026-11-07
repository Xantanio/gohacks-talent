"""A interface (app.py) abre, roda a fila sem IA e permite ao supervisor agir e avançar a semana. Sem custo de IA.

Rodar:  .\\venv\\Scripts\\python.exe -m pytest tests\\test_app.py -q
"""
from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

APP = str(Path(__file__).resolve().parent.parent / "app.py")


def test_interface_ponta_a_ponta_sem_ia(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)  # a saída da execução vai para uma pasta temporária
    at = AppTest.from_file(APP, default_timeout=180).run()
    assert not at.exception
    at.sidebar.toggle[0].set_value(False).run()  # sem IA: nada é classificado, tudo vai para o supervisor
    at.sidebar.button[0].click().run()
    assert not at.exception and at.success
    rotulos = [t.label for t in at.tabs]
    assert rotulos[0] == "Fila" and rotulos[1].startswith("Para decidir")

    liberar = [b for b in at.button if b.label == "Liberar e publicar"]
    liberar[0].click().run()
    assert at.error and "rascunho" in at.error[0].value          # sem texto, pede o texto
    at.text_area[0].set_value("Olá! Recebemos sua mensagem e já estamos cuidando do seu caso.").run()
    [b for b in at.button if b.label == "Liberar e publicar"][0].click().run()
    assert "Publicado" in at.success[0].value

    [b for b in at.button if b.label == "+1 dia"][0].click().run()
    assert not at.exception and "03/11 08:00" in at.success[0].value
