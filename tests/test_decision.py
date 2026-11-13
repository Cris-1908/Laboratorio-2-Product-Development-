"""El criterio de reemplazo del modelo, solo (sin MLflow)."""
from novamercado.reentrenar import decidir


def test_el_primer_modelo_entra_siempre():
    ok, motivo = decidir(500, None, 900, 0.01)
    assert ok and "No había modelo vigente" in motivo


def test_promueve_si_mejora_al_vigente_en_el_margen():
    ok, motivo = decidir(18_210, 19_940, 30_000, 0.01)
    assert ok
    assert "18 210" in motivo and "19 940" in motivo


def test_no_promueve_si_la_mejora_no_llega_al_margen():
    ok, _ = decidir(19_900, 19_940, 30_000, 0.01)
    assert not ok


def test_no_promueve_si_es_peor():
    ok, _ = decidir(21_000, 19_940, 30_000, 0.01)
    assert not ok


def test_no_promueve_si_no_le_gana_al_baseline_aunque_mejore_al_vigente():
    ok, motivo = decidir(25_000, 26_000, 24_000, 0.01)
    assert not ok and "baseline" in motivo


def test_la_mejora_exacta_del_margen_promueve():
    ok, _ = decidir(990, 1000, 5000, 0.01)
    assert ok
