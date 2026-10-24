from coach.courses import clean_title


def test_known_codes_get_full_names_and_types():
    assert clean_title("IF:3:S1::ALGO:CM::3IF #001") == "Algorithmes · CM"
    assert clean_title("IF:3:S1::MD:TD::3IF3 #004") == "Modélisation des données · TD"
    assert clean_title("IF:3:S1::PRC:TP::3IF3 #002") == "Programmation en C · TP"


def test_unknown_codes_kept_not_guessed():
    assert clean_title("IF:3:S1::POO1:TD::3IF3 #001") == "POO1 · TD"
    assert clean_title("IF:3:S1::POO1:TA::3IF3+3IF4 #001") == "POO1 · TA"
    assert clean_title("IF:3:S1::SHC1-HU:TD::3IF3 #011") == "SHC1-HU · TD"


def test_generic_slots_and_evaluations():
    assert clean_title("IF:3:S1::EPS:EDT::3IF #004") == "EPS"  # EDT = créneau générique : pas de type
    assert clean_title("IF:3:S1::*:EDT::3IF #001") == "Général"
    assert clean_title("IF:3:S1::BDR:EV::3IF #001") == "BDR · Évaluation"


def test_other_departments_and_free_titles_untouched():
    assert clean_title("HU:L:S1::ANG-3:TD::ANG-3IF-R10--02 #003") == "ANG-3 · TD"
    assert clean_title("CDS:SPE:S1::SPE:TD::SPE-S1--RUG1 #010") == "SPE · TD"
    for t in ("Tit : rdv Bard", "Réunion d'info - STAGES 3IF", "Atelier : Montez votre couteau en acier damas"):
        assert clean_title(t) == t
