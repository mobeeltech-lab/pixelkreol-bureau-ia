"""Kap Numérik suspendu depuis décembre 2025 : plus de montant ni de pourcentage, réactivable par un réglage."""
import importlib
import importlib.util
import sys
from pathlib import Path

ATELIER = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ATELIER))


def _outil():
    spec = importlib.util.spec_from_file_location("pk_calculs", ATELIER / "outils-openwebui" / "pk_calculs.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.Tools()


def test_la_simulation_est_suspendue_par_defaut():
    t = _outil()
    assert t.valves.KAP_OUVERT is False
    for effectif in (3, 15, 40):
        rep = t.simulation_kap_numerik(1500, effectif)
        assert "suspendu depuis décembre 2025" in rep and "à vérifier auprès de la Région" in rep
        assert "80" not in rep and "3 200" not in rep and "€" not in rep.replace("Aucun", "")


def test_la_simulation_se_reactive_par_un_reglage():
    t = _outil()
    t.valves.KAP_OUVERT = True
    assert "aide estimée 1 200,00 €" in t.simulation_kap_numerik(1500, 5)
    assert "Non éligible" in t.simulation_kap_numerik(1500, 40)


def test_la_mention_du_devis_est_sans_montant_tant_que_suspendu(monkeypatch):
    from app import config, kap
    monkeypatch.setattr(config, "KAP_OUVERT", False)
    bloc = kap.bloc("aide estimée 3 200 €, reste à charge 2 300 € HT")
    assert "suspendu depuis décembre 2025" in bloc and "3 200" not in bloc and "estimation" not in bloc
    assert kap.bloc("") == "" and kap.bloc(None) == ""


def test_la_mention_du_devis_se_reactive_par_un_reglage(monkeypatch):
    from app import config, kap
    monkeypatch.setattr(config, "KAP_OUVERT", True)
    assert "aide estimée 3 200 €" in kap.bloc("aide estimée 3 200 €") and "suspendu" not in kap.bloc("aide estimée 3 200 €")


def test_le_reglage_vient_de_l_environnement(monkeypatch):
    from app import config
    monkeypatch.setenv("KAP_NUMERIK_OUVERT", "true")
    assert importlib.reload(config).KAP_OUVERT is True
    monkeypatch.delenv("KAP_NUMERIK_OUVERT")
    assert importlib.reload(config).KAP_OUVERT is False
