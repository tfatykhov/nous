"""F040: Verify graph densifier and reverse linker wiring exists in main.py."""


def test_main_has_f040_wiring():
    """F040 wiring code exists in main.py."""
    import nous.main as main_module
    source = open(main_module.__file__).read()
    assert "GraphDensifier" in source
    assert "DecisionGraphLinker" in source
    assert "ProcedureGraphLinker" in source
    assert "_graph_densifier" in source
    assert "_embedder" in source
