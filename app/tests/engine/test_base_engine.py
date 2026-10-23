import socket

from backend.engine.dirsearch import DirsearchEngine


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_is_target_reachable_is_true_for_a_real_listening_socket() -> None:
    port = _free_port()
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", port))
    listener.listen(1)
    try:
        engine = DirsearchEngine()
        assert engine.is_target_reachable(f"http://127.0.0.1:{port}/") is True
    finally:
        listener.close()


def test_is_target_reachable_is_false_when_nothing_is_listening() -> None:
    # A freed port with nothing bound to it -- real, reported bug this
    # check exists for: dirsearch itself treats every per-request connection
    # failure as an ordinary "no match" and exits 0, so this is the only
    # place that can actually tell "target dead" apart from "target alive,
    # nothing interesting found" (see FuzzingEngine.is_target_reachable's
    # own docstring).
    port = _free_port()
    engine = DirsearchEngine()

    assert engine.is_target_reachable(f"http://127.0.0.1:{port}/") is False


def test_is_target_reachable_defaults_to_true_for_an_unparseable_url() -> None:
    engine = DirsearchEngine()

    assert engine.is_target_reachable("not-a-url") is True
