import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_python_parses():
    for path in ROOT.rglob("*.py"):
        ast.parse(path.read_text(encoding="utf-8"))


def test_prompt_routing():
    import sys
    sys.path.insert(0, str(ROOT))
    from intent import heuristic_intent, prompt_bundle

    selfie = heuristic_intent("给我拍一张你的自拍，穿白色连衣裙")
    assert selfie.mode == "persona_selfie"
    assert selfie.use_persona
    scene = heuristic_intent("给我画一片大海")
    assert scene.mode == "scene"
    assert not scene.use_persona
    positive, negative = prompt_bundle({"style_prompt": "anime", "positive_prompt": "silver hair", "negative_prompt": "blurry", "state": {}}, selfie)
    assert "anime" in positive and "blurry" in negative


def test_storage_roundtrip(tmp_path):
    import sys
    sys.path.insert(0, str(ROOT))
    from storage import Storage

    store = Storage(tmp_path)
    item = store.upsert_persona({"id": "test", "name": "测试", "state": {"outfit": "coat"}})
    assert store.persona("test")["name"] == "测试"
    assert store.persona("test")["state"]["outfit"] == "coat"
    assert (tmp_path / "personas.json").exists()


def test_moderation():
    import sys
    sys.path.insert(0, str(ROOT))
    from moderation import Moderation

    moderator = Moderation({"moderation": {"daily_limit": 1, "min_interval_sec": 0, "max_concurrency": 1}})
    assert moderator.allow("u", "普通图片")[0]
    moderator.finish("u")
    assert not moderator.allow("u", "普通图片")[0]
    assert not moderator.allow("admin", "未成年色情", is_admin=True)[0]
