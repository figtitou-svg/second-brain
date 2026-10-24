import drive_sync
import memory
from llm_router import router


def test_keywords_unambiguous_only():
    assert drive_sync.classify_by_keywords("Facture_EDF.pdf") == "admin"
    assert drive_sync.classify_by_keywords("recette gâteau.txt") == "recettes"
    assert drive_sync.classify_by_keywords("facture recette.pdf") is None  # ambigu
    assert drive_sync.classify_by_keywords("IMG_001.jpg") is None


def test_sensitivity_from_folder():
    assert drive_sync.sensitivity_of("admin") == drive_sync.sensitivity_of("perso") == "private"
    assert drive_sync.sensitivity_of("cours") == drive_sync.sensitivity_of("recettes") == "public"
    assert drive_sync.sensitivity_of(None) == "private"  # non classé = privé


class StubDrive:
    def __init__(self, text=""):
        self.text, self.moves = text, []

    def read_text(self, f): return self.text
    def folder(self, path): return f"id:{path}"
    def move(self, fid, to, frm): self.moves.append((fid, to, frm))


def _fresh_memory(monkeypatch, tmp_path):
    monkeypatch.setattr(memory, "DB_PATH", tmp_path / "m.sqlite")
    monkeypatch.setattr(memory, "_conn", None)


def test_keyword_file_moved_and_indexed_with_folder_sensitivity(monkeypatch, tmp_path):
    _fresh_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "complete", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pas de LLM")))
    d = StubDrive("montant à payer")
    r = drive_sync.process_file(d, {"id": "F1", "name": "facture_edf.pdf", "mimeType": ""}, "id:inbox")
    assert (r.folder_key, r.sensitivity) == ("admin", "private")
    assert d.moves == [("F1", "id:Admin", "id:inbox")]
    assert drive_sync.process_file(d, {"id": "F1", "name": "facture_edf.pdf", "mimeType": ""}, "id:inbox") is None


def test_unknown_stays_in_inbox_and_asks(monkeypatch, tmp_path):
    _fresh_memory(monkeypatch, tmp_path)

    def down(*a, **k): raise RuntimeError("ollama absent")
    monkeypatch.setattr(router, "complete", down)
    d = StubDrive()
    r = drive_sync.process_file(d, {"id": "F2", "name": "IMG_001.jpg", "mimeType": ""}, "id:inbox")
    assert r.folder_key is None and r.sensitivity == "private" and d.moves == []
    memory._last[r.doc_id] = r
    text, buttons = memory.describe(r.doc_id)
    assert "Inbox" in text and {cb.split(":")[2] for _, cb in buttons} == set(drive_sync.FOLDERS)


def test_llm_course_creates_subfolder(monkeypatch, tmp_path):
    _fresh_memory(monkeypatch, tmp_path)
    monkeypatch.setattr(router, "complete", lambda *a, **k: '{"folder":"cours","subfolder":"Thermodynamique","confidence":0.9}')
    d = StubDrive("premier principe")
    r = drive_sync.process_file(d, {"id": "F3", "name": "chap1.pdf", "mimeType": ""}, "id:inbox")
    assert (r.folder_key, r.subfolder, r.sensitivity) == ("cours", "Thermodynamique", "public")
    assert d.moves[0][1] == "id:Cours/Thermodynamique"
