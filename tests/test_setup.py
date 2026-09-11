import setup
from langchain_core.globals import get_verbose


def test_langchain_verbose_compatibility():
    """Pinned langchain and langchain-core must agree on the verbose setting."""
    assert isinstance(get_verbose(), bool)


def test_chatmodel_is_cached(monkeypatch):
    created = []

    class FakeModel:
        def __init__(self, **kwargs):
            created.append(kwargs)

    monkeypatch.setattr(setup, "ChatGoogleGenerativeAI", FakeModel)
    setup._chatmodel_resource.cache_clear()

    first = setup.chatmodel()
    second = setup.chatmodel()

    assert first is second
    assert len(created) == 1


def test_supabase_client_is_cached(monkeypatch):
    created = []

    def fake_create_client(url, key):
        created.append((url, key))
        return object()

    monkeypatch.setattr(setup, "create_client", fake_create_client)
    setup.supabase_server_client.cache_clear()

    first = setup.supabase_server_client()
    second = setup.supabase_server_client()

    assert first is second
    assert len(created) == 1
